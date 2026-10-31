//go:build !windows

package node

import (
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"syscall"

	"openbear/node/browser"
)

func stagingPath(exe string) string { return exe + ".new" }

func replaceExecutable(next, exe string) error { return os.Rename(next, exe) }

// RemoveReplaced deletes what an update left behind; only Windows leaves anything.
func RemoveReplaced(string) {}

// protectDir is a no-op: the directory is created 0700.
func protectDir(string) error { return nil }

func secureDataDirs(string) error { return nil }

// SecureConfig: only Windows keeps state below a directory every user can write.
func SecureConfig(string, Config) error { return nil }

// reparseFile and reparseDir: only Windows has reparse points that are no symlink.
func reparseFile(fs.FileInfo) bool { return false }
func reparseDir(fs.FileInfo) bool  { return false }

// sharedDownloads is a directory any account's browser can save into.
func sharedDownloads() string { return filepath.Join(os.TempDir(), "openbear-browser-downloads") }

func prepareSharedDownloads(path string) error {
	dir, err := browser.OpenDownloadDir(path, true)
	if err != nil {
		return err
	}
	defer dir.Close()
	info, err := dir.Stat()
	if err != nil {
		return err
	}
	st, ok := info.Sys().(*syscall.Stat_t)
	if !ok || st.Uid != 0 && st.Uid != uint32(os.Geteuid()) {
		return errors.New("browser_download_directory_owner_invalid")
	}
	if info.Mode().Perm() == 0o777 && info.Mode()&os.ModeSticky != 0 {
		return nil
	}
	return dir.Chmod(0o777 | os.ModeSticky)
}
