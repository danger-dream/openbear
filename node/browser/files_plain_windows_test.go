//go:build windows

package browser

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/sys/windows"
)

func TestPlainAncestorsStayPinned(t *testing.T) {
	base := t.TempDir()
	parent := filepath.Join(base, "parent")
	path := filepath.Join(parent, "downloads")
	if err := MkdirAllPlain(path); err != nil {
		t.Fatal(err)
	}
	open := func(path string, access uint32) (windows.Handle, error) {
		t.Helper()
		name, err := windows.UTF16PtrFromString(path)
		if err != nil {
			t.Fatal(err)
		}
		return windows.CreateFile(name, access, windows.FILE_SHARE_READ|windows.FILE_SHARE_WRITE|windows.FILE_SHARE_DELETE,
			nil, windows.OPEN_EXISTING, windows.FILE_FLAG_BACKUP_SEMANTICS|windows.FILE_FLAG_OPEN_REPARSE_POINT, 0)
	}
	func() {
		dir, err := openPlainDir(path, 0, false)
		if err != nil {
			t.Fatal(err)
		}
		defer dir.close()
		for _, target := range []string{parent, path} {
			for _, access := range []uint32{windows.GENERIC_WRITE, windows.DELETE} {
				h, err := open(target, access)
				if err == nil {
					windows.CloseHandle(h)
				}
				if !errors.Is(err, windows.ERROR_SHARING_VIOLATION) {
					t.Fatalf("pinned %s access %#x: %v, want sharing violation", target, access, err)
				}
			}
		}
		if err := os.Rename(parent, parent+"-moved"); err == nil {
			t.Fatal("ancestor could be moved while in use")
		}
		if err := os.Remove(path); err == nil {
			t.Fatal("empty directory could be removed while in use")
		}
	}()
	for _, target := range []string{parent, path} {
		for _, access := range []uint32{windows.GENERIC_WRITE, windows.DELETE} {
			h, err := open(target, access)
			if err != nil {
				t.Fatalf("released %s access %#x: %v", target, access, err)
			}
			windows.CloseHandle(h)
		}
	}
	if err := os.Rename(parent, parent+"-moved"); err != nil {
		t.Fatal("rename after release:", err)
	}
}

func TestTheVolumeRootAndItsFirstLevelStayShared(t *testing.T) {
	base := t.TempDir()
	volume := filepath.VolumeName(base) + `\`
	first := volume + strings.Split(base[len(volume):], `\`)[0]
	// Someone holding them open for writing does not stop an operation below them.
	for _, held := range []string{volume, first} {
		name, _ := windows.UTF16PtrFromString(held)
		h, err := windows.CreateFile(name, windows.FILE_LIST_DIRECTORY|windows.FILE_WRITE_DATA, windows.FILE_SHARE_READ|windows.FILE_SHARE_WRITE|windows.FILE_SHARE_DELETE,
			nil, windows.OPEN_EXISTING, windows.FILE_FLAG_BACKUP_SEMANTICS, 0)
		if err != nil {
			t.Skip("cannot hold", held, "open for writing here:", err)
		}
		defer windows.CloseHandle(h)
	}
	if err := MkdirAllPlain(filepath.Join(base, "downloads")); err != nil {
		t.Fatal(err)
	}
}

func TestPlainPathsRejectIntermediateJunction(t *testing.T) {
	base, outside := t.TempDir(), t.TempDir()
	if err := os.Mkdir(filepath.Join(outside, "downloads"), 0o700); err != nil {
		t.Fatal(err)
	}
	victim := filepath.Join(outside, "downloads", guidA)
	if err := os.WriteFile(victim, []byte("victim"), 0o600); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(base, "parent")
	if err := junction(t, link, `\??\`+outside); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(link, "downloads", guidA)
	if err := RemovePlain(path); err == nil {
		t.Fatal("intermediate junction allowed a delete")
	}
	if f, err := OpenPlain(path); err == nil {
		f.Close()
		t.Fatal("intermediate junction allowed a read")
	}
	if err := MkdirAllPlain(filepath.Join(link, "downloads", "new")); err == nil {
		t.Fatal("intermediate junction allowed mkdir")
	}
	throughEngine(t, filepath.Join(link, "downloads"), guidA)
	if data, err := os.ReadFile(victim); err != nil || string(data) != "victim" {
		t.Fatal("victim changed", err)
	}
}

func TestPlainPathsRejectDeviceNamespacesAndStreams(t *testing.T) {
	for _, path := range []string{`\\.\PIPE\test`, `\\server\share\profile`, `\\?\GLOBALROOT\Device\HarddiskVolume1\profile`} {
		if err := MkdirAllPlain(path); err == nil {
			t.Fatal("accepted non-local volume path", path)
		}
	}
	path := filepath.Join(t.TempDir(), "file:stream")
	if f, err := OpenPlain(path); err == nil {
		f.Close()
		t.Fatal("opened alternate stream")
	}
	if err := RemovePlain(path); err == nil {
		t.Fatal("deleted alternate stream")
	}
}
