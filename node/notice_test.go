package node

import (
	"context"
	"crypto/ecdh"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// revoke ends a session as the controller does: a notice inside the channel,
// then the close frame that nodes from before notices go by.
func revoke(ch *channel) {
	ch.send(map[string]any{"t": "close", "code": closeRevoked, "reason": "revoked"})
	ch.conn.CloseWith(closeRevoked, "revoked")
}

func TestHandshakeDeclaresNotices(t *testing.T) {
	f := newFake()
	got := make(chan map[string]any, 1)
	s := f.serve(t, func(ch *channel, auth map[string]any) {
		got <- auth
		revoke(ch)
	})
	defer s.Close()
	runSession(t, f.agent(s.URL, Capabilities{}))
	if auth := <-got; auth["notices"] != true {
		t.Fatalf("the handshake does not declare notices: %v", auth)
	}
}

// A close frame is plaintext: a revocation by close code alone, as a
// controller from before notices sends it, is an ordinary disconnect.
func TestPlainRevocationIsAnOrdinaryDisconnect(t *testing.T) {
	f := newFake()
	s := f.serve(t, func(ch *channel, _ map[string]any) {
		next(t, ch, "hello")
		ch.conn.CloseWith(closeRevoked, "revoked")
	})
	defer s.Close()
	err := runSession(t, f.agent(s.URL, Capabilities{}))
	var closeErr *CloseError
	if errors.Is(err, errRevoked) || !errors.As(err, &closeErr) || closeErr.Code != closeRevoked || !plainRefusal(err) {
		t.Fatalf("got %v, want a plain 4403", err)
	}
}

// The notice can also be the first message after the handshake, for a key
// revoked while the node signed; probes see it too.
func TestNoticeInPlaceOfTheSessionConfig(t *testing.T) {
	f := newFake()
	f.sessionConfig = map[string]any{"t": "close", "code": closeRevoked, "reason": "revoked"}
	s := f.serve(t, func(ch *channel, _ map[string]any) { ch.conn.CloseWith(closeRevoked, "revoked") })
	defer s.Close()
	a := f.agent(s.URL, Capabilities{})
	if err := runSession(t, a); !errors.Is(err, errRevoked) {
		t.Fatalf("session: %v", err)
	}
	if a.answers(context.Background(), a.config()) {
		t.Fatal("a revoked probe counts as reachable")
	}
}

// refusingController closes every handshake with code before its welcome,
// with the reason signed by signer over the handshake that sign returns.
func refusingController(t *testing.T, code int, signer ed25519.PrivateKey, sign func(base []byte) []byte) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := accept(w, r)
		if err != nil {
			return
		}
		eph, _ := ecdh.X25519().GenerateKey(rand.Reader)
		mine := b64.EncodeToString(eph.PublicKey().Bytes())
		sendText(c, map[string]any{"t": "challenge", "nonce": "n0nce", "eph": mine})
		auth := readText(t, c)
		base := handshakeBase(auth["node"].(string), "n0nce", mine, auth["eph"].(string))
		c.CloseWith(code, "unauthorized "+b64.EncodeToString(ed25519.Sign(signer, sign(base))))
	}))
}

func TestSignedRefusalBeforeTheWelcome(t *testing.T) {
	f := newFake()
	_, other, _ := ed25519.GenerateKey(rand.Reader)
	var replayed atomic.Pointer[[]byte]
	for _, c := range []struct {
		name   string
		signer ed25519.PrivateKey
		sign   func(base []byte) []byte
		want   error
	}{
		{"signed", f.key, func(base []byte) []byte { return rejection(base, closeUnauthorized, "unauthorized") }, errUnauthorized},
		{"another key", other, func(base []byte) []byte { return rejection(base, closeUnauthorized, "unauthorized") }, nil},
		{"another code", f.key, func(base []byte) []byte { return rejection(base, closeRevoked, "unauthorized") }, nil},
		// A refusal signed for an earlier handshake: the node's key share differs.
		{"replayed", f.key, func(base []byte) []byte {
			first := rejection(base, closeUnauthorized, "unauthorized")
			replayed.CompareAndSwap(nil, &first)
			return *replayed.Load()
		}, nil},
	} {
		s := refusingController(t, closeUnauthorized, c.signer, c.sign)
		if c.name == "replayed" {
			runSession(t, f.agent(s.URL, Capabilities{}))
		}
		err := runSession(t, f.agent(s.URL, Capabilities{}))
		s.Close()
		if c.want != nil && !errors.Is(err, c.want) {
			t.Errorf("%s: got %v, want %v", c.name, err, c.want)
		}
		if c.want == nil && (errors.Is(err, errUnauthorized) || !plainRefusal(err)) {
			t.Errorf("%s: got %v, want a plain refusal", c.name, err)
		}
	}
}

// A controller refuses an unknown node before the upgrade with a 401 signed
// for the nonce of that request; anything else answering 401 is not believed.
func TestSignedUnknownNode(t *testing.T) {
	f := newFake()
	var mode atomic.Value
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		nonce := r.Header.Get("X-OpenBear-Nonce")
		body := map[string]any{"ok": false, "error": "unknown_node"}
		switch mode.Load() {
		case "signed":
			body["sig"] = b64.EncodeToString(ed25519.Sign(f.key, rejection(handshakeBase(r.Header.Get("X-OpenBear-Node"), nonce, "", ""), closeUnauthorized, "unknown_node")))
		case "other nonce":
			body["sig"] = b64.EncodeToString(ed25519.Sign(f.key, rejection(handshakeBase(r.Header.Get("X-OpenBear-Node"), "n0nce-n0nce-n0nce", "", ""), closeUnauthorized, "unknown_node")))
		}
		w.WriteHeader(http.StatusUnauthorized)
		json.NewEncoder(w).Encode(body)
	}))
	defer s.Close()
	for _, m := range []string{"signed", "other nonce", "unsigned"} {
		mode.Store(m)
		err := runSession(t, f.agent(s.URL, Capabilities{}))
		if m == "signed" && !errors.Is(err, errUnauthorized) {
			t.Errorf("%s: got %v", m, err)
		}
		if m != "signed" && (errors.Is(err, errUnauthorized) || !plainRefusal(err)) {
			t.Errorf("%s: got %v, want a plain 401", m, err)
		}
	}
}

// Run moves to the backup at once after a plain revocation, as it would for
// any other disconnect it cannot trust; a notice keeps it waiting.
func TestRunLeavesAnAddressThatRefusesInPlaintext(t *testing.T) {
	for _, signed := range []bool{false, true} {
		t.Run(fmt.Sprint("notice=", signed), func(t *testing.T) {
			f := newFake()
			primary := f.serve(t, func(ch *channel, _ map[string]any) {
				if signed {
					revoke(ch)
				} else {
					ch.conn.CloseWith(closeRevoked, "revoked")
				}
			})
			defer primary.Close()
			hellos := make(chan map[string]any, 1)
			backup := f.serve(t, func(ch *channel, _ map[string]any) {
				hellos <- next(t, ch, "hello")
				ch.conn.Read()
			})
			defer backup.Close()
			a := f.agent(primary.URL, Capabilities{})
			a.Config.Backup = backup.URL
			ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
			defer cancel()
			go a.Run(ctx)
			select {
			case hello := <-hellos:
				if signed || hello["backup"] != true {
					t.Fatalf("on the backup after a notice=%v revocation: %v", signed, hello)
				}
			case <-ctx.Done():
				if !signed {
					t.Fatal("still not on the backup after a plain revocation")
				}
			}
		})
	}
}

func TestUpgradeResponseIsBounded(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func() {
				defer c.Close()
				buf := make([]byte, 4096)
				c.Read(buf)
				// Headers that never end.
				io.WriteString(c, "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n")
				line := "X-Filler: " + strings.Repeat("x", 1000) + "\r\n"
				for i := 0; i < 1000; i++ {
					if _, err := io.WriteString(c, line); err != nil {
						return
					}
				}
				// Without a limit the node would wait here for its deadline.
				io.Copy(io.Discard, c)
			}()
		}
	}()
	_, err = Dial(context.Background(), "ws://"+ln.Addr().String()+"/", nil, 5*time.Second)
	if err == nil || errors.Is(err, context.DeadlineExceeded) || strings.Contains(err.Error(), "timeout") {
		t.Fatalf("an endless upgrade response must fail at its limit, got %v", err)
	}
}

func enrollServer(t *testing.T, controllerKey string, hits *atomic.Int32) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		json.NewEncoder(w).Encode(map[string]any{"ok": true, "nodeId": "srv-1", "controllerKey": controllerKey})
	}))
}

func TestEnrollChecksTheControllerFingerprint(t *testing.T) {
	pub, _, _ := ed25519.GenerateKey(rand.Reader)
	sum := sha256.Sum256(pub)
	fingerprint := hex.EncodeToString(sum[:])
	var hits atomic.Int32
	s := enrollServer(t, b64.EncodeToString(pub), &hits)
	defer s.Close()
	ctx := context.Background()
	for _, fp := range []string{fingerprint, strings.ToUpper(fingerprint), ""} {
		c, err := Enroll(ctx, s.URL, "", "c0de", fp, Capabilities{})
		if err != nil || c.ControllerKey != b64.EncodeToString(pub) {
			t.Fatalf("fingerprint %q: %v", fp, err)
		}
	}
	_, err := Enroll(ctx, s.URL, "", "c0de", strings.Repeat("0", 64), Capabilities{})
	if err == nil || !strings.Contains(err.Error(), "enrollment stopped") || !strings.Contains(err.Error(), fingerprint) {
		t.Fatalf("another controller key must stop the enrollment, got %v", err)
	}
	hits.Store(0)
	if _, err = Enroll(ctx, s.URL, "", "c0de", "abc", Capabilities{}); err == nil || hits.Load() != 0 {
		t.Fatalf("a malformed fingerprint must fail before the code is sent: %v (%d requests)", err, hits.Load())
	}
}
