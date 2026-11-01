//go:build darwin || linux

package node

import (
	"errors"
	"os"
	"path/filepath"
	"syscall"
	"testing"
)

// needChown skips unless this process can give files to another account: root
// without CAP_CHOWN (a container that drops every capability) cannot.
func needChown(t *testing.T) {
	t.Helper()
	if os.Geteuid() != 0 {
		t.Skip("changing owners needs root")
	}
	probe := filepath.Join(t.TempDir(), "probe")
	if err := os.WriteFile(probe, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Lchown(probe, 65534, 65534); errors.Is(err, syscall.EPERM) {
		t.Skip("changing owners needs CAP_CHOWN")
	}
}
