package node

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"testing"
	"time"
)

func TestHostIdentityChild(t *testing.T) {
	if os.Getenv("OPENBEAR_TEST_IDENTITY_CHILD") != "1" {
		return
	}
	for {
		time.Sleep(time.Second)
	}
}

func TestHostKillIdentityRejectsStaleInstanceAndWaitRecognizesReuse(t *testing.T) {
	child := exec.Command(os.Args[0], "-test.run=^TestHostIdentityChild$")
	child.Env = append(os.Environ(), "OPENBEAR_TEST_IDENTITY_CHILD=1")
	if err := child.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { child.Process.Kill(); child.Wait() }()
	pid := child.Process.Pid
	identity := currentProcessIdentity(pid)
	if identity == "" {
		t.Fatal("no child start identity")
	}
	if err := checkProcessIdentity(pid, identity); err != nil {
		t.Fatal(err)
	}
	stale := identity + "-previous-start"
	if _, err := signalProcess(pid, true, stale); err == nil || err.Error() != "process_changed" {
		t.Fatalf("stale instance signalled: %v", err)
	}
	if !processAlive(pid) {
		t.Fatal("stale request terminated current occupant")
	}
	if !waitExited(context.Background(), pid, stale, time.Second) {
		t.Fatal("new PID occupant kept old instance wait alive")
	}
	if waitExited(context.Background(), pid, identity, time.Millisecond) {
		t.Fatal("current instance incorrectly treated as exited")
	}
	if _, err := signalProcess(pid, true, identity); err != nil {
		t.Fatal(err)
	}
	if !waitExited(context.Background(), pid, identity, 5*time.Second) {
		t.Fatal("did not observe selected child exit")
	}
}

func TestHostKillIdentityRequiresIdentityBeforeDispatch(t *testing.T) {
	a := NewAgent(Config{Capabilities: Capabilities{Exec: true}}, nil)
	raw, _ := json.Marshal(map[string]any{"pid": 2147483647, "force": true})
	_, err := a.hostCall(context.Background(), envelope{Method: "host.killIdentity", Params: raw})
	if err == nil || err.Error() != "process_identity_required" {
		t.Fatalf("new RPC accepted PID-only request: %v", err)
	}
}
