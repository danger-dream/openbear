package gateway

import (
	"crypto/sha256"
	"encoding/hex"
	"os"
	"testing"
)

// The status reports the sha256 of the running program next to the unchanged
// fields, so the controller can tell an outdated install from a current one.
func TestStatusReportsTheRunningBuild(t *testing.T) {
	path, e := os.Executable()
	if e != nil {
		t.Fatal(e)
	}
	raw, e := os.ReadFile(path)
	if e != nil {
		t.Fatal(e)
	}
	sum := sha256.Sum256(raw)
	want := hex.EncodeToString(sum[:])

	f := newFixture(t, nil)
	res, b := request(t, "GET", f.public.URL+"/control/v1/status", nil, map[string]string{"Authorization": "Bearer " + f.cfg.ControlKey})
	requireStatus(t, res, b, 200)
	v := object(t, b)
	if v["build"] != want {
		t.Fatalf("build=%v want=%s", v["build"], want)
	}
	for _, key := range []string{"ok", "version", "publicUrl", "clientIp", "clientIpObserved", "instances", "eventsPending", "eventsDropped"} {
		if _, ok := v[key]; !ok {
			t.Fatalf("status lost %q: %s", key, b)
		}
	}
	if v["version"] != float64(1) {
		t.Fatalf("state format version changed: %v", v["version"])
	}
	if got, e := hashFile(path); e != nil || got != want {
		t.Fatalf("hashFile=%q,%v", got, e)
	}
	if _, e := hashFile(t.TempDir() + "/missing"); e == nil {
		t.Fatal("missing file hashed")
	}
}
