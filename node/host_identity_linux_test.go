//go:build linux

package node

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestHostListIdentityUsesSameStatSnapshotAndDoesNotMixReusedPIDRates(t *testing.T) {
	root := t.TempDir()
	proc(t, root, 90, "same-name", 10, 5, 100, 100, "0")
	h := linuxHost{root: root}
	before := h.snapshot()[90]
	top, err := h.top(context.Background(), "cpu", 10, false, 1, func(context.Context) (float64, error) {
		proc(t, root, 90, "same-name", 900, 5, 900, 900, "0")
		path := filepath.Join(root, "proc/90/stat")
		b, _ := os.ReadFile(path)
		os.WriteFile(path, []byte(strings.Replace(string(b), " 100 1000 ", " 200 1000 ", 1)), 0600)
		return 1, nil
	})
	if err != nil || len(top.Processes) != 1 {
		t.Fatalf("top: %v %v", top, err)
	}
	p := top.Processes[0]
	if p.Identity == "" || p.Identity == hostProcessIdentity(90, before.start) {
		t.Fatal("same-name PID reuse retained identity")
	}
	if p.CPU != 0 || p.ReadRate != 0 || p.WriteRate != 0 {
		t.Fatalf("mixed process lifetimes: %+v", p)
	}
}
