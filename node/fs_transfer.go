//go:build unix || windows

package node

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"os"
	"regexp"
)

var fileDigest = regexp.MustCompile(`^[0-9a-f]{64}$`)

type fileChecksum struct {
	Size   int64  `json:"size"`
	SHA256 string `json:"sha256"`
}

func hashFile(path string) (fileChecksum, error) {
	f, err := openReadFile(path)
	if err != nil {
		return fileChecksum{}, fileError(err)
	}
	defer f.Close()
	return hashOpenFile(f)
}

func hashOpenFile(f *os.File) (fileChecksum, error) {
	before, err := f.Stat()
	if err != nil {
		return fileChecksum{}, fileError(err)
	}
	if !before.Mode().IsRegular() {
		return fileChecksum{}, errors.New("not_regular_file")
	}
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		return fileChecksum{}, fileError(err)
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
