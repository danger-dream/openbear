package node

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"syscall"
	"testing"

	"openbear/node/mesh/bypass"
)

// The test binary stands in for the node in bypass-run (main_test.go); its resolv.conf goes here.
func init() {
	if p := os.Getenv("OB_TEST_BYPASS_RESOLV"); p != "" {
		bypassResolv = p
	}
}

func bypassActive(t *testing.T, on bool) {
	keep := bypass.StateFile
	t.Cleanup(func() { bypass.StateFile = keep; bypass.Forget() })
	bypass.StateFile = filepath.Join(t.TempDir(), "openbear-bypass")
	if on {
		os.WriteFile(bypass.StateFile, []byte("{}"), 0o644)
	}
	bypass.Forget()
}

func TestBypassOnlyWhileItIsInPlace(t *testing.T) {
	s := testStore(t)
	for _, on := range []bool{false, true} {
		bypassActive(t, on)
		id := jobCall(t, s, "job.start", map[string]any{"command": "true", "bypass": true})["id"].(string)
		raw, _ := os.ReadFile(filepath.Join(s.dir, id, "job.json"))
		var spec jobSpec
		json.Unmarshal(raw, &spec)
		if spec.Bypass != on {
			t.Fatalf("bypass in place %v, job bypassed %v", on, spec.Bypass)
		}
		waitJob(t, s, id)
	}
	cmd := exec.Command("/bin/sh", "-c", "true")
	bypassedCommand(cmd)
	self, _ := os.Executable()
	if cmd.Path != self || !slices.Equal(cmd.Args, []string{self, "bypass-run", "--", "/bin/sh", "-c", "true"}) {
		t.Fatalf("not through bypass-run: %s %q", cmd.Path, cmd.Args)
	}
}

// A bypassed job runs in the bypass group with the upstream DNS servers; the machine's own
// /etc/resolv.conf is left as it is.
func TestABypassedJobRunsInTheGroupWithTheUpstreamDNS(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("bypass-run takes a group and a mount namespace as root")
	}
	probe := exec.Command("/bin/true")
	probe.SysProcAttr = &syscall.SysProcAttr{Credential: &syscall.Credential{Uid: 0, Gid: bypass.GID, NoSetGroups: true}}
	if err := probe.Run(); errors.Is(err, syscall.EPERM) {
		t.Skip("no CAP_SETGID here (a container); the group cannot be taken")
	}
	bypassActive(t, true)
	t.Setenv("OB_TEST_BYPASS_RESOLV", filepath.Join(t.TempDir(), "resolv"))
	host, _ := os.ReadFile("/etc/resolv.conf")
	s := testStore(t)
	id := jobCall(t, s, "job.start", map[string]any{"command": "id -g; cat /etc/resolv.conf", "bypass": true})["id"].(string)
	done := waitJob(t, s, id)
	out := done["log"].(map[string]any)["data"].(string)
	first, rest, _ := strings.Cut(string(out), "\n")
	if first != "52999" {
		t.Fatalf("group %q, output %q", first, out)
	}
	if !strings.HasPrefix(rest, "# openbear-node: the upstream DNS servers") {
		probe := exec.Command("/bin/true")
		probe.SysProcAttr = &syscall.SysProcAttr{Unshareflags: syscall.CLONE_NEWNS}
		if err := probe.Run(); errors.Is(err, syscall.EPERM) {
			t.Skip("no mount namespaces here (a container); the group is checked")
		}
		t.Fatalf("resolv.conf not replaced: %q", rest)
	}
	if after, _ := os.ReadFile("/etc/resolv.conf"); !bytes.Equal(after, host) {
		t.Fatalf("the machine's resolv.conf changed")
	}
}
