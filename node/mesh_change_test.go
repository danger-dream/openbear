//go:build linux

package node

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func changeFixture(t *testing.T) (*Agent, *meshHost) {
	t.Helper()
	h, f, _ := testMesh(t)
	a := meshAgent(h, Capabilities{Exec: true})
	a.Config.URL = "http://100.96.0.1:18961"
	a.ConfigPath = filepath.Join(t.TempDir(), "node.json")
	if _, err := meshDispatch(t, a, "mesh.install", "{}"); err != nil {
		t.Fatal(err)
	}
	f.mu.Lock()
	f.active = true
	f.mu.Unlock()
	for _, name := range []string{"config.json", "applied.json"} {
		if err := os.WriteFile(h.path(name), []byte(`{"revision":1}`), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	writeStatus(t, h, map[string]any{"appliedRevision": 1, "ready": true})
	t.Cleanup(func() {
		h.mu.Lock()
		if h.changeTimer != nil {
			h.changeTimer.Stop()
		}
		h.mu.Unlock()
	})
	return a, h
}

const changePrepare = `{"id":"change-1","config":{"revision":2},"url":"http://100.97.0.1:18961","timeoutSeconds":180}`

func changeCall(t *testing.T, a *Agent, method, params string) map[string]any {
	t.Helper()
	out, err := meshDispatch(t, a, method, params)
	if err != nil {
		t.Fatalf("%s: %v", method, err)
	}
	return out
}

func expireChange(t *testing.T, h *meshHost) {
	t.Helper()
	h.mu.Lock()
	defer h.mu.Unlock()
	c, err := h.readChange()
	if err != nil || c == nil {
		t.Fatal(err)
	}
	c.Deadline = time.Now().Add(-time.Second).UnixMilli()
	if err = h.saveChange(c); err != nil {
		t.Fatal(err)
	}
}

func activateChange(t *testing.T, a *Agent, h *meshHost) {
	t.Helper()
	changeCall(t, a, "mesh.activate", `{"id":"change-1","delayMs":0}`)
	a.resumeMeshChange()
	data, _ := os.ReadFile(h.path("config.json"))
	if err := os.WriteFile(h.path("applied.json"), data, 0o600); err != nil {
		t.Fatal(err)
	}
	writeStatus(t, h, map[string]any{"appliedRevision": 2, "ready": true})
}

func restoredByDataPlane(t *testing.T, a *Agent, h *meshHost) {
	t.Helper()
	data, _ := os.ReadFile(h.path("config.json"))
	if string(data) != `{"revision":1}` {
		t.Fatalf("old config not restored: %s", data)
	}
	if err := os.WriteFile(h.path("applied.json"), data, 0o600); err != nil {
		t.Fatal(err)
	}
	writeStatus(t, h, map[string]any{"appliedRevision": 1, "ready": true})
	a.resumeMeshChange()
}

func TestMeshChangeExpiryRestoresConfigAndURLIncludingRelay(t *testing.T) {
	for _, address := range []string{"http://100.96.0.1:18961", "https://relay.example.test"} {
		t.Run(address, func(t *testing.T) {
			a, h := changeFixture(t)
			a.Config.URL = address
			changeCall(t, a, "mesh.prepare", changePrepare)
			if a.config().URL != address {
				t.Fatal("prepare switched URL")
			}
			before, _ := os.ReadFile(h.path("config.json"))
			if string(before) != `{"revision":1}` {
				t.Fatal("prepare applied config")
			}
			activateChange(t, a, h)
			if a.config().URL != "http://100.97.0.1:18961" {
				t.Fatal("new URL not staged with config")
			}
			expireChange(t, h)
			a.resumeMeshChange()
			if h.changeView()["state"] != "restoring" {
				t.Fatal("write alone counted as restored")
			}
			restoredByDataPlane(t, a, h)
			after, _ := os.ReadFile(h.path("config.json"))
			if string(after) != string(before) || a.config().URL != address || h.changeView()["state"] != "aborted" {
				t.Fatal("no autonomous rollback")
			}
			var persisted Config
			data, _ := os.ReadFile(a.ConfigPath)
			if json.Unmarshal(data, &persisted) != nil || persisted.URL != address {
				t.Fatal("URL rollback not durable")
			}
		})
	}
}

func TestMeshChangeConfirmationAndCompensationAreIdempotent(t *testing.T) {
	a, h := changeFixture(t)
	changeCall(t, a, "mesh.prepare", changePrepare)
	first := h.changeView()["deadline"]
	changeCall(t, a, "mesh.prepare", changePrepare)
	if h.changeView()["deadline"] != first {
		t.Fatal("retry extended lease")
	}
	if _, err := meshDispatch(t, a, "mesh.confirm", `{"id":"change-1"}`); err == nil {
		t.Fatal("unapplied confirmed")
	}
	activateChange(t, a, h)
	for range 2 {
		changeCall(t, a, "mesh.confirm", `{"id":"change-1"}`)
	}
	expireChange(t, h)
	a.resumeMeshChange()
	if h.changeView()["state"] != "confirmed" || a.config().URL != "http://100.97.0.1:18961" {
		t.Fatal("confirmed change expired")
	}
	for range 2 {
		changeCall(t, a, "mesh.abort", `{"id":"change-1"}`)
		restoredByDataPlane(t, a, h)
	}
	if a.config().URL != "http://100.96.0.1:18961" {
		t.Fatal("partial commit compensation failed")
	}
}

func TestMeshChangeRestartResumesExpiredRollbackBeforeConnect(t *testing.T) {
	a, h := changeFixture(t)
	changeCall(t, a, "mesh.prepare", changePrepare)
	activateChange(t, a, h)
	expireChange(t, h)
	h.mu.Lock()
	h.changeTimer.Stop()
	h.mu.Unlock()
	restarted := newMeshHost(h.dir, h.exe, h.build)
	restarted.service = h.service
	b := meshAgent(restarted, Capabilities{Exec: true})
	b.Config, b.ConfigPath = a.config(), a.ConfigPath
	b.resumeMeshChange()
	restoredByDataPlane(t, b, restarted)
	if b.config().URL != "http://100.96.0.1:18961" || restarted.changeView()["state"] != "aborted" {
		t.Fatal("restart lost rollback")
	}
}

func TestMeshChangePendingBlocksOrdinaryMutationsAndAbortBeatsDelayedPrepare(t *testing.T) {
	a, h := changeFixture(t)
	changeCall(t, a, "mesh.prepare", changePrepare)
	for _, item := range [][2]string{{"mesh.apply", `{"config":{"revision":3}}`}, {"mesh.upgrade", "{}"}, {"mesh.uninstall", "{}"},
		{"config.url", `{"url":"https://other.example.test"}`}, {"mesh.prepare", `{"id":"other","config":{"revision":3},"url":"https://other.example.test","timeoutSeconds":180}`}} {
		if _, err := meshDispatch(t, a, item[0], item[1]); err == nil || err.Error() != "mesh_change_pending" {
			t.Fatalf("%s: %v", item[0], err)
		}
	}
	changeCall(t, a, "mesh.abort", `{"id":"change-1"}`)
	if _, err := meshDispatch(t, a, "mesh.activate", `{"id":"change-1"}`); err == nil {
		t.Fatal("aborted activated")
	}
	h.mu.Lock()
	os.Remove(h.path("change.json"))
	h.mu.Unlock()
	changeCall(t, a, "mesh.abort", `{"id":"change-1"}`)
	if _, err := meshDispatch(t, a, "mesh.prepare", changePrepare); err == nil {
		t.Fatal("delayed prepare revived aborted transaction")
	}
}

func TestMeshChangeTimerRunsWithoutControllerSession(t *testing.T) {
	a, h := changeFixture(t)
	changeCall(t, a, "mesh.prepare", changePrepare)
	activateChange(t, a, h)
	h.mu.Lock()
	c, _ := h.readChange()
	c.Deadline = time.Now().Add(40 * time.Millisecond).UnixMilli()
	if err := h.saveChange(c); err != nil {
		t.Fatal(err)
	}
	a.scheduleMeshChangeLocked(h, c)
	h.mu.Unlock()
	deadline := time.Now().Add(2 * time.Second)
	for h.changeView()["state"] != "restoring" && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if h.changeView()["state"] != "restoring" {
		t.Fatal("timer did not restore config")
	}
	data, _ := os.ReadFile(h.path("config.json"))
	if err := os.WriteFile(h.path("applied.json"), data, 0o600); err != nil {
		t.Fatal(err)
	}
	writeStatus(t, h, map[string]any{"appliedRevision": 1, "ready": true})
	for h.changeView()["state"] != "aborted" && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if h.changeView()["state"] != "aborted" {
		t.Fatal("timer needs a session")
	}
}
