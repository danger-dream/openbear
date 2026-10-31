//go:build unix || windows

package node

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"io/fs"
	"os"
	"regexp"
	"strconv"
)

var fileDigest = regexp.MustCompile(`^[0-9a-f]{64}$`)

type fileChecksum struct {
	Size   int64  `json:"size"`
	SHA256 string `json:"sha256"`
}

func hashFile(path string) (fileChecksum, error) {
	info, err := os.Stat(path)
	if err != nil {
		return fileChecksum{}, fileError(err)
	}
	if !info.Mode().IsRegular() {
		return fileChecksum{}, errors.New("not_regular_file")
	}
	f, err := os.Open(path)
	if err != nil {
		return fileChecksum{}, fileError(err)
	}
	defer f.Close()
	before, err := f.Stat()
	if err != nil {
		return fileChecksum{}, fileError(err)
	}
	if !before.Mode().IsRegular() {
		return fileChecksum{}, errors.New("not_regular_file")
	}
	h := sha256.New()
	n, err := io.Copy(h, f)
	if err != nil {
		return fileChecksum{}, fileError(err)
	}
	after, err := f.Stat()
	if err != nil {
		return fileChecksum{}, fileError(err)
	}
	if n != before.Size() || after.Size() != before.Size() || !after.ModTime().Equal(before.ModTime()) {
		return fileChecksum{}, errors.New("file_changed")
	}
	return fileChecksum{Size: n, SHA256: hex.EncodeToString(h.Sum(nil))}, nil
}

// A hard link publishes a complete file without replacing a racing destination.
func commitNew(temp, path, mode string) error {
	defer os.Remove(temp)
	perm := fs.FileMode(0o644)
	if mode != "" {
		n, err := strconv.ParseUint(mode, 8, 32)
		if err != nil || n > 0o7777 {
			return errors.New("invalid_mode")
		}
		perm = fs.FileMode(n)
	}
	if err := os.Chmod(temp, perm); err != nil {
		return fileError(err)
	}
	if err := os.Link(temp, path); err != nil {
		if errors.Is(err, fs.ErrExist) {
			return errors.New("file_exists")
		}
		return fileError(err)
	}
	return nil
}
