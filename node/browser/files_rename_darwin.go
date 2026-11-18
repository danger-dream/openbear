//go:build darwin

package browser

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"time"

	"golang.org/x/sys/unix"
)

func secureDownloadRemovalDir(dir *os.File) error {
	// Unlike Linux's POSIX ACL mask, macOS inherited ACLs may grant access even
	// with mode 0700. Remove ACLs only from OUR verified private directory, via
	// an inherited descriptor (not a replaceable path in the shared directory).
	// chmod is a system utility; no shell, user arguments or profile is involved.
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "/bin/chmod", "-N", "/dev/fd/3")
	cmd.ExtraFiles = []*os.File{dir}
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("secure private download cleanup directory: %w", err)
	}
	return nil
}

func renameDownloadNoReplace(from int, old string, to int, name string) error {
	return unix.RenameatxNp(from, old, to, name, unix.RENAME_EXCL)
}
