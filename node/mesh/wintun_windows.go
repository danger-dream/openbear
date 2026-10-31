package mesh

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"

	"golang.org/x/sys/windows"
)

var (
	wintunMu   sync.Mutex
	wintunPath string
)

// ExtractWintun writes the embedded wintun.dll into the bin directory of
// the mesh directory dir, next to the data plane, unless an identical copy
// is there, and loads it. The tun package then finds it
// loaded under its base name. An already installed Wintun driver is
// reused by the DLL itself.
func ExtractWintun(dir string) (string, error) {
	wintunMu.Lock()
	defer wintunMu.Unlock()
	if wintunPath != "" {
		return wintunPath, nil
	}
	if len(wintunDLL) == 0 {
		return "", errors.New("this build has no wintun.dll for its architecture")
	}
	sum := sha256.Sum256(wintunDLL)
	if hex.EncodeToString(sum[:]) != wintunSHA256 {
		return "", errors.New("embedded wintun.dll does not match the official 0.14.1 build")
	}
	path := filepath.Join(dir, "bin", "wintun.dll")
	if cur, err := os.ReadFile(path); err != nil || !bytes.Equal(cur, wintunDLL) {
		if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
			return "", err
		}
		tmp := path + ".tmp"
		if err := os.WriteFile(tmp, wintunDLL, 0o644); err != nil {
			return "", err
		}
		if err := os.Rename(tmp, path); err != nil {
			os.Remove(tmp)
			return "", fmt.Errorf("replace wintun.dll: %w", err)
		}
	}
	written, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	if got := sha256.Sum256(written); got != sum {
		return "", errors.New("wintun.dll on disk does not match the embedded copy")
	}
	if _, err := windows.LoadLibraryEx(path, 0, windows.LOAD_LIBRARY_SEARCH_SYSTEM32|windows.LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR); err != nil {
		return "", fmt.Errorf("load %s: %w", path, err)
	}
	wintunPath = path
	return path, nil
}
