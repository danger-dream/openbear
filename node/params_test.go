package node

import (
	"context"
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"testing"
	"time"
)

// signedParams is how a controller that takes signed parameters answers them.
func signedParams(f *fakeController) func(base []byte, auth map[string]any) map[string]any {
	return func(base []byte, auth map[string]any) map[string]any {
		bound := paramsBase(base, []byte(auth["params"].(string)))
		return map[string]any{"paramsSig": b64.EncodeToString(ed25519.Sign(f.key, signed("server-params", bound)))}
	}
}

func TestHandshakeSignsItsParameters(t *testing.T) {
	f := newFake()
	got := make(chan map[string]any, 1)
	s := f.serve(t, func(ch *channel, auth map[string]any) {
		got <- auth
		revoke(ch)
	})
	defer s.Close()
	runSession(t, f.agent(s.URL, Capabilities{}))
	auth := <-got
	var params map[string]any
	if err := json.Unmarshal([]byte(auth["params"].(string)), &params); err != nil {
		t.Fatalf("params: %v", err)
	}
	if params["version"] != Version || params["protocol"] != float64(ProtocolVersion) || params["protocolMin"] != float64(ProtocolMin) || params["notices"] != true {
		t.Fatalf("signed parameters: %v", params)
	}
	// The plain copies stay for controllers from before.
	if auth["version"] != Version || auth["protocol"] != float64(ProtocolVersion) {
		t.Fatalf("plain copies: %v", auth)
	}
}

// The signature covers the parameters exactly: the fake controller checks it
// the way the controller does, against a changed copy too.
func TestSignedParametersVerify(t *testing.T) {
	f := newFake()
	checked := make(chan [2]bool, 1)
	f.welcome = func(base []byte, auth map[string]any) map[string]any {
		sig, _ := b64.DecodeString(auth["paramsSig"].(string))
		params := auth["params"].(string)
		tampered := `{"probe":true,` + params[1:]
		checked <- [2]bool{ed25519.Verify(f.node, signed("client-params", paramsBase(base, []byte(params))), sig),
			ed25519.Verify(f.node, signed("client-params", paramsBase(base, []byte(tampered))), sig)}
		return signedParams(f)(base, auth)
	}
	f.sessionConfig = map[string]any{"signedParams": true}
	s := f.serve(t, func(ch *channel, _ map[string]any) {
		next(t, ch, "hello")
		revoke(ch)
	})
	defer s.Close()
	if err := runSession(t, f.agent(s.URL, Capabilities{})); !errors.Is(err, errRevoked) {
		t.Fatalf("session with a controller that takes signed parameters: %v", err)
	}
	if c := <-checked; !c[0] || c[1] {
		t.Fatalf("signature over the parameters: valid %v, over changed ones %v", c[0], c[1])
	}
}

func TestWelcomeOverOtherParametersIsRefused(t *testing.T) {
	f := newFake()
	f.welcome = func(base []byte, auth map[string]any) map[string]any {
		other := paramsBase(base, []byte(`{"probe":true}`))
		return map[string]any{"paramsSig": b64.EncodeToString(ed25519.Sign(f.key, signed("server-params", other)))}
	}
	s := f.serve(t, func(ch *channel, _ map[string]any) { ch.conn.Read() })
	defer s.Close()
	if err := runSession(t, f.agent(s.URL, Capabilities{})); !errors.Is(err, errControllerIdentity) {
		t.Fatalf("got %v, want a controller identity mismatch", err)
	}
}

// A welcome changed on the way (its signature no longer verifies) is refused
// but proves nothing about the address: the node moves to the backup at once,
// as after a plain refusal, instead of waiting as for a signed one.
func TestTamperedWelcomeMovesToTheBackup(t *testing.T) {
	f := newFake()
	p := *f
	p.welcome = func([]byte, map[string]any) map[string]any {
		return map[string]any{"sig": b64.EncodeToString(make([]byte, ed25519.SignatureSize))}
	}
	primary := p.serve(t, func(ch *channel, _ map[string]any) { ch.conn.Read() })
	defer primary.Close()
	if err := runSession(t, f.agent(primary.URL, Capabilities{})); !errors.Is(err, errControllerIdentity) {
		t.Fatalf("got %v, want a controller identity mismatch", err)
	}
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
		if hello["backup"] != true {
			t.Fatalf("hello: %v", hello)
		}
	case <-ctx.Done():
		t.Fatal("still not on the backup after a tampered welcome")
	}
}

// A controller that took the plain copies (signed ones stripped on the way)
// says so inside the channel; the node leaves, for the backup at once.
func TestStrippedParametersEndTheSession(t *testing.T) {
	f := newFake()
	f.sessionConfig = map[string]any{"signedParams": false}
	primary := f.serve(t, func(ch *channel, _ map[string]any) { ch.conn.Read() })
	defer primary.Close()
	if err := runSession(t, f.agent(primary.URL, Capabilities{})); !errors.Is(err, errParamsStripped) {
		t.Fatalf("got %v", err)
	}
	g := *f
	g.sessionConfig = nil
	hellos := make(chan map[string]any, 1)
	backup := g.serve(t, func(ch *channel, _ map[string]any) {
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
		if hello["backup"] != true {
			t.Fatalf("hello: %v", hello)
		}
	case <-ctx.Done():
		t.Fatal("still not on the backup")
	}
}
