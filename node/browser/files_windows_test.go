//go:build windows

package browser

import (
	"bytes"
	"encoding/binary"
	"errors"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"testing"
	"time"
	"unicode/utf16"
	"unsafe"

	"golang.org/x/sys/windows"
)

// junction makes link a mount point to the NT path target, which, unlike
// mklink /J, may be an object directory such as \RPC Control.
func junction(t *testing.T, link, target string) error {
	t.Helper()
	if err := os.Mkdir(link, 0o700); err != nil {
		return err
	}
	name, _ := windows.UTF16PtrFromString(link)
	h, err := windows.CreateFile(name, windows.GENERIC_WRITE, 0, nil, windows.OPEN_EXISTING,
		windows.FILE_FLAG_BACKUP_SEMANTICS|windows.FILE_FLAG_OPEN_REPARSE_POINT, 0)
	if err != nil {
		return err
	}
	defer windows.CloseHandle(h)
	substitute := utf16.Encode([]rune(target))
	names := append(substitute, 0, 0) // the substitute name, then an empty print name
	var b bytes.Buffer
	for _, v := range []any{uint32(windows.IO_REPARSE_TAG_MOUNT_POINT), uint16(8 + 2*len(names)), uint16(0),
		uint16(0), uint16(2 * len(substitute)), uint16(2*len(substitute) + 2), uint16(0), names} {
		binary.Write(&b, binary.LittleEndian, v)
	}
	var n uint32
	return windows.DeviceIoControl(h, windows.FSCTL_SET_REPARSE_POINT, &b.Bytes()[0], uint32(b.Len()), nil, 0, &n, nil)
}

var ntCreateSymbolicLinkObject = windows.NewLazySystemDLL("ntdll.dll").NewProc("NtCreateSymbolicLinkObject")

// objectLink links \RPC Control\name to the NT path target until the test ends.
func objectLink(t *testing.T, name, target string) error {
	t.Helper()
	objectName, _ := windows.NewNTUnicodeString(`\RPC Control\` + name)
	targetName, _ := windows.NewNTUnicodeString(target)
	attrs := windows.OBJECT_ATTRIBUTES{ObjectName: objectName, Attributes: windows.OBJ_CASE_INSENSITIVE}
	attrs.Length = uint32(unsafe.Sizeof(attrs))
	var h windows.Handle
	if err := ntCreateSymbolicLinkObject.Find(); err != nil {
		return err
	}
	status, _, _ := ntCreateSymbolicLinkObject.Call(uintptr(unsafe.Pointer(&h)), 0xF0001, // SYMBOLIC_LINK_ALL_ACCESS
		uintptr(unsafe.Pointer(&attrs)), uintptr(unsafe.Pointer(targetName)))
	if status != 0 {
		return windows.NTStatus(status)
	}
	t.Cleanup(func() { windows.CloseHandle(h) })
	return nil
}

func exists(path string) bool {
	_, err := os.Lstat(path)
	return err == nil
}

// throughEngine has the engine, with its download directory at dir, save,
// expire and prune a download named guid.
func throughEngine(t *testing.T, dir, guid string) {
	t.Helper()
	e, page, _ := testEngine(t, nil)
	e.config.Set("downloadHostPath", dir)
	loop(e, func() {
		e.files.event("Browser.downloadWillBegin", O("frameId", page.mainID, "guid", guid, "suggestedFilename", "a"))
		e.files.event("Browser.downloadProgress", O("guid", guid, "state", "completed", "receivedBytes", 6))
		if code := saveError(e, page, 1, filepath.Join(t.TempDir(), "saved")); code != "download_file_unavailable: configure shared download paths for a remote browser" {
			t.Fatalf("saved through %s: %q", dir, code)
		}
		record, _ := page.downloads.Get(1)
		record.updated = time.Now().Add(-2 * downloadTTL)
		e.files.prune()
	})
}

func TestPlainPaths(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "a", "b")
	for i := 0; i < 2; i++ {
		if err := MkdirAllPlain(dir); err != nil {
			t.Fatal(err)
		}
	}
	if info, err := os.Stat(dir); err != nil || !info.IsDir() {
		t.Fatal("directory not made")
	}
	file := filepath.Join(dir, guidA)
	os.WriteFile(file, []byte("data"), 0o600)
	f, err := OpenPlain(file)
	if err != nil {
		t.Fatal(err)
	}
	data, _ := io.ReadAll(f)
	_, links, err := identify(f)
	f.Close()
	if string(data) != "data" || err != nil || links != 1 {
		t.Fatalf("read %q, %d links, %v", data, links, err)
	}
	entries, err := readDir(dir)
	if err != nil || len(entries) != 1 || entries[0].Name() != guidA {
		t.Fatalf("listed %v %v", entries, err)
	}
	if info, err := entries[0].Info(); err != nil || !info.Mode().IsRegular() || info.Size() != 4 {
		t.Fatalf("entry info %v %v", info, err)
	}

	// Open without delete sharing, as a running browser keeps its lockfile.
	name, _ := windows.UTF16PtrFromString(file)
	held, err := windows.CreateFile(name, windows.GENERIC_WRITE, windows.FILE_SHARE_READ, nil, windows.OPEN_EXISTING, 0, 0)
	if err != nil {
		t.Fatal(err)
	}
	if err := RemovePlain(file); err == nil || errors.Is(err, fs.ErrNotExist) {
		t.Fatalf("removed a held file: %v", err)
	}
	windows.CloseHandle(held)
	if err := RemovePlain(file); err != nil || exists(file) {
		t.Fatalf("not removed: %v", err)
	}
	if err := RemovePlain(file); !errors.Is(err, fs.ErrNotExist) {
		t.Fatalf("missing file: %v", err)
	}
	if err := RemovePlain(dir); err == nil || !exists(dir) {
		t.Fatal("directory removed")
	}

	// The engine keeps working there.
	os.WriteFile(file, []byte("first!"), 0o600)
	old := time.Now().Add(-2 * downloadTTL)
	os.WriteFile(filepath.Join(dir, guidB), []byte("x"), 0o600)
	os.Chtimes(filepath.Join(dir, guidB), old, old)
	e, page, _ := testEngine(t, nil)
	e.config.Set("downloadHostPath", dir)
	out := filepath.Join(t.TempDir(), "saved")
	loop(e, func() {
		e.files.event("Browser.downloadWillBegin", O("frameId", page.mainID, "guid", guidA, "suggestedFilename", "a"))
		e.files.event("Browser.downloadProgress", O("guid", guidA, "state", "completed", "receivedBytes", 6))
		if code := saveError(e, page, 1, out); code != "" {
			t.Fatalf("save: %q", code)
		}
		record, _ := page.downloads.Get(1)
		record.updated = old
		e.files.prune()
	})
	if data, _ := os.ReadFile(out); string(data) != "first!" || exists(file) || exists(filepath.Join(dir, guidB)) {
		t.Fatalf("saved %q; expired files kept: %v %v", data, exists(file), exists(filepath.Join(dir, guidB)))
	}
}

func TestPlainPathsRefuseJunctions(t *testing.T) {
	base, outside := t.TempDir(), t.TempDir()
	victim := filepath.Join(outside, guidA)
	os.WriteFile(victim, []byte("victim"), 0o600)
	link := filepath.Join(base, "Browser")
	if err := junction(t, link, `\??\`+outside); err != nil {
		t.Fatal("junction setup:", err)
	}
	if data, _ := os.ReadFile(filepath.Join(link, guidA)); string(data) != "victim" {
		t.Fatal("junction fixture does not reach its target")
	}
	if err := RemovePlain(filepath.Join(link, guidA)); err == nil {
		t.Fatal("removed through a junction")
	}
	if f, err := OpenPlain(filepath.Join(link, guidA)); err == nil {
		f.Close()
		t.Fatal("opened through a junction")
	}
	if _, err := readDir(link); err == nil {
		t.Fatal("listed through a junction")
	}
	if err := MkdirAllPlain(filepath.Join(link, "made")); err == nil || exists(filepath.Join(outside, "made")) {
		t.Fatal("made a directory through a junction")
	}
	throughEngine(t, link, guidA)
	if !exists(victim) {
		t.Fatal("the engine removed a file through a junction")
	}

	// A reparse point is refused even when it is the last component.
	if err := os.Symlink(victim, filepath.Join(base, guidB)); err != nil {
		t.Log("symlinks unavailable:", err)
		return
	}
	if f, err := OpenPlain(filepath.Join(base, guidB)); err == nil {
		f.Close()
		t.Fatal("opened a link")
	}
	if err := RemovePlain(filepath.Join(base, guidB)); err == nil || !exists(filepath.Join(base, guidB)) || !exists(victim) {
		t.Fatalf("link: %v", err)
	}
}

// TestRPCControlRedirectIsRefused replays the known way from SYSTEM deleting
// in a user's folder to deleting any file: the folder becomes a junction to
// \RPC Control, where the name being deleted links to the victim.
func TestRPCControlRedirectIsRefused(t *testing.T) {
	base, victims := t.TempDir(), t.TempDir()
	link := filepath.Join(base, "Browser")
	if err := junction(t, link, `\RPC Control`); err != nil {
		t.Skip("cannot make a junction to \\RPC Control:", err)
	}
	victim := func(name string) string {
		path := filepath.Join(victims, name)
		os.WriteFile(path, []byte("victim"), 0o600)
		if err := objectLink(t, name, `\??\`+path); err != nil {
			t.Skip("cannot link in \\RPC Control:", err)
		}
		return path
	}
	// What os.Remove did there, to show the redirect works on this system.
	shown := victim(uuidHex() + "-shown")
	os.Remove(filepath.Join(link, filepath.Base(shown)))
	if exists(shown) {
		t.Skip("the redirect does not reach the victim here")
	}

	guid := "4e5f6a7b-8c9d-4eaf-b0c1-" + uuidHex()[:12]
	kept := victim(guid)
	if err := RemovePlain(filepath.Join(link, guid)); err == nil {
		t.Fatal("removed through the redirect")
	}
	if f, err := OpenPlain(filepath.Join(link, guid)); err == nil {
		f.Close()
		t.Fatal("opened through the redirect")
	}
	if _, err := readDir(link); err == nil {
		t.Fatal("listed through the redirect")
	}
	throughEngine(t, link, guid)
	if !exists(kept) {
		t.Fatal("victim removed")
	}
}
