package node

import (
	"context"
	"crypto/ecdh"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

var b64 = base64.StdEncoding

// fakeController speaks the controller side of the protocol for one node key.
type fakeController struct {
	key   ed25519.PrivateKey
	node  ed25519.PublicKey
	nodeK ed25519.PrivateKey
	// config is added to the session config message, welcome to the welcome.
	sessionConfig map[string]any
	welcome       func(base []byte, auth map[string]any) map[string]any
}

func newFake() *fakeController {
	_, key, _ := ed25519.GenerateKey(rand.Reader)
	pub, nodeKey, _ := ed25519.GenerateKey(rand.Reader)
	return &fakeController{key: key, node: pub, nodeK: nodeKey}
}

func (f *fakeController) config(url string, caps Capabilities) Config {
	return Config{URL: url, NodeID: "srv-1", PrivateKey: b64.EncodeToString(f.nodeK.Seed()),
		ControllerKey: b64.EncodeToString(f.key.Public().(ed25519.PublicKey)), Capabilities: caps}
}

func (f *fakeController) agent(url string, caps Capabilities) *Agent {
	return NewAgent(f.config(url, caps), log.New(io.Discard, "", 0))
}

// handshake returns the encrypted channel and the node's auth message, or nil
// when the node's signature does not verify.
func (f *fakeController) handshake(t *testing.T, w http.ResponseWriter, r *http.Request, signer ed25519.PrivateKey) (*channel, map[string]any) {
	t.Helper()
	c, err := accept(w, r)
	if err != nil {
		return nil, nil
	}
	eph, _ := ecdh.X25519().GenerateKey(rand.Reader)
	mine := b64.EncodeToString(eph.PublicKey().Bytes())
	sendText(c, map[string]any{"t": "challenge", "nonce": "n0nce", "eph": mine})
	auth := readText(t, c)
	base := handshakeBase(auth["node"].(string), "n0nce", mine, auth["eph"].(string))
	sig, _ := b64.DecodeString(auth["sig"].(string))
	if !ed25519.Verify(f.node, signed("client", base), sig) {
		c.CloseWith(closeUnauthorized, "bad signature")
		return nil, auth
	}
	if signer == nil {
		signer = f.key
	}
	welcome := map[string]any{"t": "welcome", "sig": b64.EncodeToString(ed25519.Sign(signer, signed("server", base)))}
	if f.welcome != nil {
		for k, v := range f.welcome(base, auth) {
			welcome[k] = v
		}
	}
	sendText(c, welcome)
	raw, _ := b64.DecodeString(auth["eph"].(string))
	peer, _ := ecdh.X25519().NewPublicKey(raw)
	shared, _ := eph.ECDH(peer)
	n2c, c2n, _ := sessionKeys(shared, base)
	ch := &channel{conn: c, tx: c2n, rx: n2c}
	config := map[string]any{"t": "config", "interval": 1}
	for k, v := range f.sessionConfig {
		config[k] = v
	}
	ch.send(config)
	return ch, auth
}

// serve runs fn on each accepted, authenticated node connection.
func (f *fakeController) serve(t *testing.T, fn func(ch *channel, auth map[string]any)) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/server-nodes/v1/connect" || r.Header.Get("X-OpenBear-Node") != "srv-1" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		if ch, auth := f.handshake(t, w, r, nil); ch != nil {
			fn(ch, auth)
		}
	}))
}

func sendText(c *Conn, v any) { data, _ := json.Marshal(v); c.WriteText(data) }

func readText(t *testing.T, c *Conn) map[string]any {
	t.Helper()
	_, data, err := c.Read()
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	var v map[string]any
	if err = json.Unmarshal(data, &v); err != nil {
		t.Fatalf("json: %v", err)
	}
	return v
}

// next returns the next JSON message, skipping reports and terminal output.
func next(t *testing.T, ch *channel, kind string) map[string]any {
	t.Helper()
	ch.conn.SetIdleTimeout(15 * time.Second)
	for {
		k, data, err := ch.read()
		if err != nil {
			t.Fatalf("waiting for %s: %v", kind, err)
		}
		if k != kindJSON {
			continue
		}
		var m map[string]any
		json.Unmarshal(data, &m)
		if m["t"] == kind {
			return m
		}
	}
}

// call sends one call and waits for its reply.
func call(t *testing.T, ch *channel, method string, params any) map[string]any {
	t.Helper()
	id := method + "-" + b64.EncodeToString([]byte(time.Now().String()))[:8]
	ch.send(map[string]any{"t": "call", "id": id, "method": method, "params": params})
	for {
		if m := next(t, ch, "reply"); m["id"] == id {
			return m
		}
	}
}

func termInput(ch *channel, sid uint32, text string) {
	ch.write(kindTerminal, binary.BigEndian.AppendUint32(nil, sid), []byte(text))
}

// output reads terminal frames for sid until want appears; JSON messages
// seen on the way are collected into control.
func output(t *testing.T, ch *channel, sid uint32, want string, control map[string]map[string]any) string {
	t.Helper()
	var out strings.Builder
	ch.conn.SetIdleTimeout(15 * time.Second)
	for !strings.Contains(out.String(), want) {
		k, data, err := ch.read()
		if err != nil {
			t.Fatalf("waiting for %q: %v (got %q)", want, err, out.String())
		}
		if k == kindTerminal && binary.BigEndian.Uint32(data[:4]) == sid {
			out.Write(data[4:])
			continue
		}
		var m map[string]any
		json.Unmarshal(data, &m)
		if kind, _ := m["t"].(string); kind != "" && control != nil {
			control[kind] = m
			if want == kind {
				return out.String()
			}
		}
	}
	return out.String()
}

// runSession runs one agent session against srv until the controller side ends it.
func runSession(t *testing.T, a *Agent) error {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 40*time.Second)
	defer cancel()
	return a.session(ctx)
}

// terminalSession runs one fake-controller session and hands its channel to fn.
func terminalSession(t *testing.T, caps Capabilities, fn func(ch *channel)) {
	f := newFake()
	done := make(chan struct{})
	s := f.serve(t, func(ch *channel, _ map[string]any) {
		defer close(done)
		next(t, ch, "hello")
		fn(ch)
		ch.conn.CloseWith(closeRevoked, "")
	})
	defer s.Close()
	runSession(t, f.agent(s.URL, caps))
	<-done
}

func open(t *testing.T, ch *channel, sid uint32) {
	t.Helper()
	ch.send(map[string]any{"t": "term.open", "sid": sid, "cols": 80, "rows": 24})
	output(t, ch, sid, "term.opened", map[string]map[string]any{})
}

func fileCall(t *testing.T, u *uploads, method string, p map[string]any) (map[string]any, error) {
	t.Helper()
	raw, _ := json.Marshal(p)
	v, err := u.handle(method, raw)
	if err != nil {
		return nil, err
	}
	out, _ := json.Marshal(v)
	var m map[string]any
	json.Unmarshal(out, &m)
	return m, nil
}
