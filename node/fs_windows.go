//go:build windows

package node

import (
	"errors"
	"io/fs"
	"path/filepath"
	"strings"
	"unicode/utf16"

	"golang.org/x/sys/windows"
)

// Paths travel with forward slashes ("C:/Users"); "/" lists the drives.
const hasDrives = true

func localPath(p string) (string, bool) {
	p = filepath.FromSlash(p)
	if len(p) == 2 && p[1] == ':' {
		p += `\`
	}
	if !filepath.IsAbs(p) {
		return "", false
	}
	return filepath.Clean(p), true
}

func shownPath(p string) string { return filepath.ToSlash(p) }

// drives lists fixed, removable and network drives; empty CD drives and
// unmounted card readers would only fail when opened.
func drives() (any, error) {
	roots, err := driveRoots()
	if err != nil {
		return nil, fileError(err)
	}
	entries := []fileEntry{}
	for _, root := range roots {
		name, _ := windows.UTF16PtrFromString(root)
		switch windows.GetDriveType(name) {
		case windows.DRIVE_FIXED, windows.DRIVE_REMOVABLE, windows.DRIVE_REMOTE, windows.DRIVE_RAMDISK:
		default:
			continue
		}
		var free, total, totalFree uint64
		if windows.GetDiskFreeSpaceEx(name, &free, &total, &totalFree) != nil {
			continue
		}
		entries = append(entries, fileEntry{Name: strings.TrimSuffix(root, `\`), Type: "dir", Dir: true, Size: int64(total), Mode: "0777"})
	}
	return map[string]any{"path": "/", "entries": entries, "truncated": false}, nil
}

// driveRoots lists the drive roots, which the API returns NUL-separated.
func driveRoots() ([]string, error) {
	buf := make([]uint16, 254)
	n, err := windows.GetLogicalDriveStrings(uint32(len(buf)), &buf[0])
	if err != nil {
		return nil, err
	}
	var roots []string
	for _, root := range strings.Split(string(utf16.Decode(buf[:n])), "\x00") {
		if root != "" {
			roots = append(roots, root)
		}
	}
	return roots, nil
}

func keepOwner(string, fs.FileInfo) {}

func platformFileError(err error) string {
	switch {
	case errors.Is(err, windows.ERROR_DISK_FULL), errors.Is(err, windows.ERROR_HANDLE_DISK_FULL):
		return "no_space"
	case errors.Is(err, windows.ERROR_DIRECTORY):
		return "not_a_directory"
	case errors.Is(err, windows.ERROR_WRITE_PROTECT):
		return "read_only_filesystem"
	case errors.Is(err, windows.ERROR_SHARING_VIOLATION), errors.Is(err, windows.ERROR_LOCK_VIOLATION):
		return "file_in_use"
	}
	return ""
}
