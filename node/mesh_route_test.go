//go:build linux

package node

import (
	"encoding/json"
	"net/http/httptest"
	"os"
	"os/exec"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func writeApplied(t *testing.T, h *meshHost, prefix, address string) {
	t.Helper()
	os.MkdirAll(h.dir, 0o700)
	data := `{"schema":1,"revision":1,"network":{"prefix":"` + prefix + `"},"self":{"address":"` + address + `"}}`
	if err := replaceFile(h.path("applied.json"), strings.NewReader(data), 0o600); err != nil {
		t.Fatal(err)
	}
}

// A control address is a mesh address when its host is an IP in the network
// prefix of the config last applied, not of one only written.
func TestMeshCarriesAddressesInTheAppliedPrefix(t *testing.T) {
	h, _, _ := testMesh(t)
	if h.carries("http://100.96.0.5:18961") {
		t.Fatal("carried with nothing applied")
	}
	os.MkdirAll(h.dir, 0o700)
	os.WriteFile(h.path("config.json"), []byte(`{"network":{"prefix":"100.96.0.0/16"}}`), 0o600)
	if h.carries("http://100.96.0.5:18961") {
		t.Fatal("carried by a config not applied")
	}
	writeApplied(t, h, "100.96.0.0/16", "100.96.0.9")
	for address, want := range map[string]bool{
		"http://100.96.0.5:18961":    true,
		"https://100.96.255.1":       true,
		"http://100.97.0.5:18961":    false,
		"http://ks.example:18961":    false,
		"http://[fd00::1]:18961":     false,
		"http://[::ffff:100.96.0.5]": true,
		"not a url\x7f":              false,
	} {
		if got := h.carries(address); got != want {
			t.Errorf("carries(%q) = %v", address, got)
		}
	}
	// A new applied config is read again.
	time.Sleep(10 * time.Millisecond)
	writeApplied(t, h, "10.200.0.0/16", "10.200.0.9")
	if h.carries("http://100.96.0.5:18961") || !h.carries("http://10.200.1.1:18961") {
		t.Fatal("the new applied prefix was not read")
	}
}

func TestMeshUpFollowsTheDataPlane(t *testing.T) {
	h, _, _ := testMesh(t)
	os.MkdirAll(h.dir, 0o700)
	check := func(want bool, why string) {
		t.Helper()
		if up, reason := h.up(); up != want || !strings.Contains(reason, why) {
			t.Fatalf("up() = %v, %q; want %v, %q", up, reason, want, why)
		}
	}
	check(false, "no data plane status")
	self := os.Getpid()
	writeStatus(t, h, map[string]any{"pid": self, "ready": true, "iface": "lo"})
	check(true, "")
	writeStatus(t, h, map[string]any{"pid": self, "ready": false, "iface": "lo"})
	check(false, "not ready")
	writeStatus(t, h, map[string]any{"pid": 999990, "ready": true})
	check(false, "not running")
	writeStatus(t, h, map[string]any{"pid": self, "ready": true})
	old := time.Now().Add(-meshStale - time.Second)
	os.Chtimes(h.path("status.json"), old, old)
	check(false, "not updated for")
	// An interface never found is not taken for gone; one found and then
	// missing is.
	writeStatus(t, h, map[string]any{"pid": self, "ready": true, "iface": "obtest-none"})
	check(true, "")
	h.iface = "obtest-none"
	check(false, "interface obtest-none is gone")
}

func TestMeshTakesDown(t *testing.T) {
	h, sys, exe := testMesh(t)
	a := meshAgent(h, Capabilities{Exec: true})
	if _, err := meshDispatch(t, a, "mesh.install", ""); err != nil {
		t.Fatal(err)
	}
	writeApplied(t, h, "100.96.0.0/16", "100.96.0.9")
	apply := func(prefix, address string) json.RawMessage {
		return json.RawMessage(`{"config":{"revision":2,"network":{"prefix":"` + prefix + `","mtu":1380},"self":{"address":"` + address + `","name":"x"}}}`)
	}
	sys.active = true
	for _, c := range []struct {
		method string
		params json.RawMessage
		want   bool
	}{
		{"mesh.apply", apply("100.96.0.0/16", "100.96.0.9"), false},
		{"mesh.apply", apply("100.96.0.0/16", "100.96.0.10"), true},
		{"mesh.apply", apply("100.96.0.0/15", "100.96.0.9"), true},
		{"mesh.upgrade", nil, false}, // the same build
		{"mesh.uninstall", nil, true},
		{"mesh.status", nil, false},
		{"mesh.tls", json.RawMessage(`{}`), false},
	} {
		if got := h.takesDown(c.method, c.params) != ""; got != c.want {
			t.Errorf("%s %s: takes down %v", c.method, c.params, got)
		}
	}
	os.WriteFile(exe, []byte("build 2"), 0o755)
	if h.takesDown("mesh.upgrade", nil) == "" {
		t.Error("an upgrade to a new build of a running data plane does not take it down")
	}
	sys.active = false
	if h.takesDown("mesh.upgrade", nil) != "" || h.takesDown("mesh.apply", apply("100.96.0.0/16", "100.96.0.10")) != "" {
		t.Error("a stopped data plane taken down")
	}
}

// A mesh.upgrade that comes over a session on the mesh address: the node
// moves the session to the backup, then upgrades, replies on the new
// session, and goes back to the mesh address once the data plane is up.
func TestMeshUpgradeMovesTheControlConnectionFirst(t *testing.T) {
	fastMeshTimers(t)
	h, sys, exe := testMesh(t)
	type event struct {
		at   time.Time
		what string
		msg  map[string]any
	}
	events := make(chan event, 256)
	svc := h.service.(*systemdService)
	inner := svc.systemctl
	svc.systemctl = func(args ...string) (string, error) {
		if args[0] == "stop" {
			events <- event{time.Now(), "stop", nil}
		}
		return inner(args...)
	}
	f := newFake()
	var called atomic.Bool
	serve := func(name string) *httptest.Server {
		return f.serve(t, func(ch *channel, auth map[string]any) {
			if auth["probe"] == true {
				events <- event{time.Now(), name + " probe", nil}
				return
			}
			next(t, ch, "hello")
			events <- event{time.Now(), name + " hello", nil}
			if name == "primary" && called.CompareAndSwap(false, true) {
				ch.send(map[string]any{"t": "call", "id": "up-1", "method": "mesh.upgrade", "params": map[string]any{}})
			}
			ch.conn.SetIdleTimeout(time.Minute)
			for {
				k, data, err := ch.read()
				if err != nil {
					events <- event{time.Now(), name + " closed", nil}
					return
				}
				var m map[string]any
				if k == kindJSON && json.Unmarshal(data, &m) == nil && m["t"] == "reply" {
					events <- event{time.Now(), name + " reply", m}
				}
			}
		})
	}
	primary, backup := serve("primary"), serve("backup")
	defer primary.Close()
	defer backup.Close()
	// The primary is the mesh address: 127.0.0.1 inside the applied prefix;
	// the backup, by name, is not.
	a := f.agent(primary.URL, Capabilities{Exec: true})
	a.Config.Backup = strings.Replace(backup.URL, "127.0.0.1", "localhost", 1)
	a.meshHost = h
	if _, err := meshDispatch(t, a, "mesh.install", ""); err != nil {
		t.Fatal(err)
	}
	writeApplied(t, h, "127.0.0.0/8", "127.0.0.1")
	os.WriteFile(h.path("config.json"), []byte(`{"revision":1}`), 0o600)
	sys.active = true
	writeStatus(t, h, map[string]any{"pid": os.Getpid(), "ready": true})
	// The new copy comes up as another live process.
	sleeper := exec.Command("sleep", "60")
	if err := sleeper.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { sleeper.Process.Kill(); sleeper.Wait() }()
	sys.onStart = func() {
		time.Sleep(300 * time.Millisecond)
		writeStatus(t, h, map[string]any{"pid": sleeper.Process.Pid, "ready": true})
	}
	os.WriteFile(exe, []byte("build 2"), 0o755)
	runAgent(t, a)

	var seen []string
	at := map[string]time.Time{}
	var reply map[string]any
	deadline := time.After(15 * time.Second)
	for at["primary hello#2"].IsZero() {
		select {
		case e := <-events:
			what := e.what
			if what == "primary hello" && !at["primary hello"].IsZero() {
				what = "primary hello#2"
			}
			if _, ok := at[what]; !ok {
				at[what] = e.at
			}
			if e.what == "backup reply" {
				reply = e.msg
			}
			seen = append(seen, what)
		case <-deadline:
			t.Fatalf("events so far: %v", seen)
		}
	}
	t.Logf("events: %v", seen)
	order := []string{"primary hello", "backup probe", "primary closed", "backup hello", "stop", "backup reply", "primary hello#2"}
	for i := 1; i < len(order); i++ {
		if at[order[i]].IsZero() || at[order[i]].Before(at[order[i-1]]) {
			t.Fatalf("%s not after %s: %v", order[i], order[i-1], seen)
		}
	}
	if reply["id"] != "up-1" || reply["ok"] != true {
		t.Fatalf("reply on the backup: %v", reply)
	}
	if result, _ := reply["result"].(map[string]any); result["changed"] != true {
		t.Fatalf("upgrade result: %v", reply)
	}
	if back := at["primary hello#2"].Sub(at["backup reply"]); back > 5*time.Second {
		t.Fatalf("took %s to go back to the mesh address", back)
	}
	for _, s := range seen {
		if s == "primary reply" {
			t.Fatalf("a reply went out on the mesh session: %v", seen)
		}
	}
}
