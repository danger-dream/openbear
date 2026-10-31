package node

import (
	"bytes"
	"crypto/ecdh"
	"crypto/ed25519"
	"crypto/rand"
	"errors"
	"log"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
)

func TestHandshakeAndHelloDeclareTheProtocolRange(t *testing.T) {
	f := newFake()
	got := make(chan [2]map[string]any, 1)
	s := f.serve(t, func(ch *channel, auth map[string]any) {
		got <- [2]map[string]any{auth, next(t, ch, "hello")}
		ch.conn.CloseWith(closeRevoked, "")
	})
	defer s.Close()
	runSession(t, f.agent(s.URL, Capabilities{}))
	m := <-got
	for i, name := range []string{"handshake", "hello"} {
		if m[i]["protocol"] != float64(ProtocolVersion) || m[i]["protocolMin"] != float64(ProtocolMin) {
			t.Errorf("%s declares protocol %v..%v, want %d..%d", name, m[i]["protocolMin"], m[i]["protocol"], ProtocolMin, ProtocolVersion)
		}
	}
}

// A controller that has no protocol in common with the node closes with
// closeProtocol before its welcome, signed for this handshake; the node
// reports that, not a generic disconnect, and Run waits long before trying
// again. Unsigned, the same close is an ordinary disconnect.
func TestControllerRefusingTheProtocolRange(t *testing.T) {
	f := newFake()
	var sign atomic.Bool
	sign.Store(true)
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := accept(w, r)
		if err != nil {
			return
		}
		eph, _ := ecdh.X25519().GenerateKey(rand.Reader)
		mine := b64.EncodeToString(eph.PublicKey().Bytes())
		sendText(c, map[string]any{"t": "challenge", "nonce": "n0nce", "eph": mine})
		auth := readText(t, c)
		reason := "node_protocol_outdated"
		if sign.Load() {
			base := handshakeBase(auth["node"].(string), "n0nce", mine, auth["eph"].(string))
			reason += " " + b64.EncodeToString(ed25519.Sign(f.key, rejection(base, closeProtocol, reason)))
		}
		c.CloseWith(closeProtocol, reason)
	}))
	defer s.Close()
	err := runSession(t, f.agent(s.URL, Capabilities{}))
	var p *protocolError
	if !errors.As(err, &p) || !strings.Contains(err.Error(), "(node_protocol_outdated)") || !strings.Contains(err.Error(), "protocol 1..1") {
		t.Fatalf("got %v, want the controller's protocol refusal", err)
	}
	sign.Store(false)
	err = runSession(t, f.agent(s.URL, Capabilities{}))
	if errors.As(err, &p) || !plainRefusal(err) {
		t.Fatalf("an unsigned refusal must be an ordinary disconnect, got %v", err)
	}
}

// A controller that keeps the session though nothing overlaps: the node
// stays (it can still be updated) and says why in its log.
func TestNoCommonProtocolIsLogged(t *testing.T) {
	f := newFake()
	f.sessionConfig = map[string]any{"protocol": ProtocolVersion + 2, "protocolMin": ProtocolVersion + 1}
	s := f.serve(t, func(ch *channel, _ map[string]any) {
		next(t, ch, "hello")
		revoke(ch)
	})
	defer s.Close()
	var out syncBuffer
	a := f.agent(s.URL, Capabilities{})
	a.Logger = log.New(&out, "", 0)
	if err := runSession(t, a); !errors.Is(err, errRevoked) {
		t.Fatalf("session ended with %v", err)
	}
	if !strings.Contains(out.String(), "the controller speaks protocol 2..3 and this node 1..1: no protocol in common") {
		t.Fatalf("log: %s", out.String())
	}

	f.sessionConfig = map[string]any{"protocol": ProtocolVersion + 1, "protocolMin": ProtocolMin}
	out.Reset()
	runSession(t, a)
	if strings.Contains(out.String(), "no protocol in common") {
		t.Fatalf("overlapping ranges reported: %s", out.String())
	}
}

func TestCommonProtocol(t *testing.T) {
	for _, c := range []struct {
		low, high, want int
		ok              bool
	}{
		{1, 1, 1, true},
		{0, 1, 1, true}, // a controller from before protocolMin
		{1, 5, ProtocolVersion, true},
		{ProtocolVersion + 1, ProtocolVersion + 2, ProtocolVersion, false},
		{0, ProtocolMin - 1, ProtocolMin - 1, false},
	} {
		got, ok := commonProtocol(c.low, c.high)
		if got != c.want || ok != c.ok {
			t.Errorf("commonProtocol(%d, %d) = %d, %v; want %d, %v", c.low, c.high, got, ok, c.want, c.ok)
		}
	}
}

type syncBuffer struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (s *syncBuffer) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.Write(p)
}
func (s *syncBuffer) String() string { s.mu.Lock(); defer s.mu.Unlock(); return s.b.String() }
func (s *syncBuffer) Reset()         { s.mu.Lock(); defer s.mu.Unlock(); s.b.Reset() }
