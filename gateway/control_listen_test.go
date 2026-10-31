package gateway

import (
	"net/http/httptest"
	"testing"
)

// A separate control listener takes the control API off the public port
// entirely; the single-port configuration keeps sharing it.
func TestControlListenerKeepsControlOffThePublicPort(t *testing.T) {
	cfg := Config{Listen: "0.0.0.0:18962", ControlListen: "127.0.0.1:18963", PublicURL: "http://203.0.113.10:18962", StateDir: t.TempDir(), ControlKey: randomToken()}
	g, e := New(cfg)
	if e != nil {
		t.Fatal(e)
	}
	public, control := httptest.NewServer(g.PublicHandler()), httptest.NewServer(g.ControlHandler())
	t.Cleanup(func() { g.Close(); public.Close(); control.Close() })
	auth := map[string]string{"Authorization": "Bearer " + cfg.ControlKey, "X-OpenBear-Probe-Instance": "a"}
	for _, path := range []string{"/control/v1/status", "/control/v1/events", "/api/gateway/probe"} {
		res, b := request(t, "GET", public.URL+path, nil, auth)
		requireStatus(t, res, b, 404)
	}
	res, b := request(t, "GET", control.URL+"/control/v1/status", nil, auth)
	requireStatus(t, res, b, 200)

	f := newFixture(t, nil)
	res, b = request(t, "GET", f.public.URL+"/control/v1/status", nil, map[string]string{"Authorization": "Bearer " + f.cfg.ControlKey})
	requireStatus(t, res, b, 200)
}

// From the 10-30 check: an archived (disabled) instance kept its username, so a new instance
// for the same person could never be synced. It no longer holds it; the enabled one logs in.
func TestAnArchivedInstanceGivesUpItsUsername(t *testing.T) {
	f := newFixture(t, nil)
	archived := copyInput(f.input)
	archived["revision"], archived["enabled"] = 2, false
	delete(archived, "username")
	delete(archived, "password")
	f.put(t, "a", archived, 200)
	fresh := copyInput(f.input)
	fresh["businessKey"] = randomToken()
	f.put(t, "b", fresh, 200)
	if got := f.login(t, "alice", f.password).instance; got != "b" {
		t.Fatalf("logged in to %q, want the enabled b", got)
	}
	// Enabling the archived one again while b holds the name is refused.
	back := copyInput(f.input)
	back["revision"] = 3
	delete(back, "username")
	delete(back, "password")
	if body := f.put(t, "a", back, 409); body["error"] != "username_unavailable" {
		t.Fatalf("re-enabling: %v", body)
	}
}
