//go:build windows

package node

import (
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"syscall"

	"golang.org/x/sys/windows"
)

// Windows runs only files ending in .exe, and refuses to overwrite or delete
// a running one, though renaming it is allowed.
func stagingPath(exe string) string { return strings.TrimSuffix(exe, ".exe") + ".new.exe" }

func replacedPath(exe string) string { return strings.TrimSuffix(exe, ".exe") + ".old.exe" }

func replaceExecutable(next, exe string) error {
	old := replacedPath(exe)
	os.Remove(old)
	if err := os.Rename(exe, old); err != nil {
		return err
	}
	if err := os.Rename(next, exe); err != nil {
		os.Rename(old, exe)
		return err
	}
	return nil
}

// RemoveReplaced deletes the previous build once the new one is running.
func RemoveReplaced(exe string) { os.Remove(replacedPath(exe)) }

// protectDir limits dir and what it holds to SYSTEM and Administrators;
// ProgramData lets every user read by default, and the config holds the
// node's private key.
func protectDir(dir string) error {
	sd, err := windows.SecurityDescriptorFromString("D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)")
	if err != nil {
		return err
	}
	dacl, _, err := sd.DACL()
	if err != nil {
		return err
	}
	return windows.SetNamedSecurityInfo(dir, windows.SE_FILE_OBJECT,
		windows.DACL_SECURITY_INFORMATION|windows.PROTECTED_DACL_SECURITY_INFORMATION, nil, nil, dacl, nil)
}

// Go reports a file or directory behind a reparse point that is no symlink as irregular: a
// junction (mklink /J), OneDrive's cloud files and folders. reparseFile is a file to read and
// write like any; reparseDir a directory entered like a link to one.
func reparseFile(info fs.FileInfo) bool { return reparseKind(info, false) }
func reparseDir(info fs.FileInfo) bool  { return reparseKind(info, true) }

func reparseKind(info fs.FileInfo, dir bool) bool {
	data, ok := info.Sys().(*syscall.Win32FileAttributeData)
	return ok && info.Mode()&fs.ModeIrregular != 0 && (data.FileAttributes&windows.FILE_ATTRIBUTE_DIRECTORY != 0) == dir
}

func prepareSharedDownloads(path string) error {
	if info, err := os.Lstat(path); err != nil || !info.IsDir() {
		os.Remove(path)
		return os.MkdirAll(path, 0o777)
	}
	return nil
}

// sharedDownloads is a directory any account's browser can save into: SYSTEM's temporary
// directory is closed to them (Windows 11's SystemTemp).
func sharedDownloads() string {
	public := os.Getenv("PUBLIC")
	if public == "" {
		public = `C:\Users\Public`
	}
	return filepath.Join(public, "Downloads", "OpenBear")
}
