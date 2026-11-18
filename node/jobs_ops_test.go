package node

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestInterruptedJobStopCapabilityRequiresChildIdentity(t *testing.T) {
	dir := t.TempDir()
	spec, _ := json.Marshal(jobSpec{ID: "orphan01", Command: "fixture only", StartedAt: time.Now().Add(-time.Hour).UnixMilli()})
	if err := os.WriteFile(filepath.Join(dir, "job.json"), spec, 0600); err != nil {
		t.Fatal(err)
	}
	assertView := func(want bool) {
		t.Helper()
		v, err := readJob(dir)
		if err != nil || v.State != "interrupted" || v.CanStop != want {
			t.Fatalf("readJob: %+v, %v; want canStop=%v", v, err, want)
		}
		if _, err := os.Stat(filepath.Join(dir, "stop")); !os.IsNotExist(err) {
			t.Fatal("reading status must never request a stop")
		}
	}
	assertView(false)
	token, alive := processStart(os.Getpid())
	if !alive {
		t.Skip("platform cannot verify this test process identity")
	}
	// Only read our identity, never stop it. A stale token must not grant capability.
	if err := writeAtomic(dir, "child", []byte(fmt.Sprintf("%d wrong-%s\n", os.Getpid(), token))); err != nil {
		t.Fatal(err)
	}
	assertView(false)
	if err := writeAtomic(dir, "child", []byte(fmt.Sprintf("%d %s\n", os.Getpid(), token))); err != nil {
		t.Fatal(err)
	}
	assertView(true)
	exit, _ := json.Marshal(jobExit{ExitCode: 0, EndedAt: time.Now().UnixMilli()})
	if err := writeAtomic(dir, "exit", exit); err != nil {
		t.Fatal(err)
	}
	v, err := readJob(dir)
	if err != nil || v.CanStop || v.State != "exited" {
		t.Fatalf("completed jobs cannot claim orphan capability: %+v, %v", v, err)
	}
}

func TestCleanupKeepsConfirmedResidualJobAddressable(t *testing.T) {
	root := t.TempDir()
	dir := filepath.Join(root, "orphan01")
	if err := os.Mkdir(dir, 0700); err != nil {
		t.Fatal(err)
	}
	token, alive := processStart(os.Getpid())
	if !alive {
		t.Skip("platform cannot verify this test process identity")
	}
	spec, _ := json.Marshal(jobSpec{ID: "orphan01", StartedAt: time.Now().Add(-jobKeepFor - time.Hour).UnixMilli()})
	if err := writeAtomic(dir, "job.json", spec); err != nil {
		t.Fatal(err)
	}
	if err := writeAtomic(dir, "child", []byte(fmt.Sprintf("%d %s\n", os.Getpid(), token))); err != nil {
		t.Fatal(err)
	}
	s := &jobStore{dir: root}
	views := s.cleanup()
	if len(views) != 1 || !views[0].CanStop {
		t.Fatalf("orphan must remain available for explicit stop: %+v", views)
	}
	if err := os.Remove(filepath.Join(dir, "child")); err != nil {
		t.Fatal(err)
	}
	if views = s.cleanup(); len(views) != 0 {
		t.Fatalf("ordinary retention still applies once the child is gone: %+v", views)
	}
}
