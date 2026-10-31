//go:build !windows

package transport

import (
	"os"
	"path/filepath"

	"golang.org/x/sys/unix"
)

func securePath(path string, directory bool) error {
	if err := rejectSymlink(path); err != nil {
		return err
	}
	mode := os.FileMode(0600)
	if directory {
		mode = 0700
	}
	return os.Chmod(path, mode)
}
func lockFile(f *os.File) error { return unix.Flock(int(f.Fd()), unix.LOCK_EX) }
func unlockFile(f *os.File)     { _ = unix.Flock(int(f.Fd()), unix.LOCK_UN) }
func replaceFile(from, to string) error {
	if err := os.Rename(from, to); err != nil {
		return err
	}
	d, err := os.Open(filepath.Dir(to))
	if err != nil {
		return err
	}
	defer d.Close()
	return d.Sync()
}
