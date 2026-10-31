package gateway

import (
	"crypto/sha256"
	"encoding/hex"
	"io"
	"os"
	"sync"
)

var (
	buildOnce sync.Once
	buildHash string
)

// executableBuild is the sha256 of the program this process runs, read once at
// start. The controller compares it with its bundled builds to offer updates.
// "" when the program cannot be read.
func executableBuild() string {
	buildOnce.Do(func() {
		// /proc/self/exe is the running file even after an update replaced the path.
		h, e := hashFile("/proc/self/exe")
		if e != nil {
			if path, err := os.Executable(); err == nil {
				h, e = hashFile(path)
			}
		}
		if e == nil {
			buildHash = h
		}
	})
	return buildHash
}

func hashFile(path string) (string, error) {
	f, e := os.Open(path)
	if e != nil {
		return "", e
	}
	defer f.Close()
	h := sha256.New()
	if _, e = io.Copy(h, f); e != nil {
		return "", e
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}
