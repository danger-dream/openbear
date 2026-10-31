//go:build unix

package node

import (
	"io/fs"
	"os"
	"path/filepath"
	"syscall"
)

const hasDrives = false

func localPath(p string) (string, bool) {
	if !filepath.IsAbs(p) {
		return "", false
	}
	return filepath.Clean(p), true
}

func shownPath(p string) string { return p }

func drives() (any, error) { return nil, nil }

func keepOwner(temp string, prior fs.FileInfo) {
	if st, ok := prior.Sys().(*syscall.Stat_t); ok {
		os.Lchown(temp, int(st.Uid), int(st.Gid))
	}
}

func platformFileError(error) string { return "" }
