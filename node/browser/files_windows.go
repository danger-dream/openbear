//go:build windows

package browser

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"
)

// identify returns which file f is and how many names it has.
func identify(f *os.File) (fileIdentity, uint64, error) {
	var d syscall.ByHandleFileInformation
	if err := syscall.GetFileInformationByHandle(syscall.Handle(f.Fd()), &d); err != nil {
		return fileIdentity{}, 0, err
	}
	return fileIdentity{uint64(d.VolumeSerialNumber), uint64(d.FileIndexHigh)<<32 | uint64(d.FileIndexLow)}, uint64(d.NumberOfLinks), nil
}

var errReparsePoint = errors.New("passes through a link or junction")

func removeFile(path string) error { return RemovePlain(path) }

func readDir(path string) ([]os.DirEntry, error) {
	dir, err := openPlainDir(path, windows.FILE_LIST_DIRECTORY, false)
	if err != nil {
		return nil, &os.PathError{Op: "readdir", Path: path, Err: err}
	}
	defer dir.close()
	h := dir.handles[len(dir.handles)-1]
	dir.handles = dir.handles[:len(dir.handles)-1]
	file := os.NewFile(uintptr(h), path)
	defer file.Close()
	return file.ReadDir(-1)
}

func openRegular(path string) (*os.File, error) { return OpenPlain(path) }

// MkdirAllPlain creates directories without traversing reparse points.
func MkdirAllPlain(path string) error {
	dir, err := openPlainDir(path, 0, true)
	if err != nil {
		return &os.PathError{Op: "mkdir", Path: path, Err: err}
	}
	dir.close()
	return nil
}

// The download directory may be in a user's profile: SYSTEM must not follow a junction there.
func mkdirAll(path string) error { return MkdirAllPlain(path) }

// RemovePlain deletes only a non-directory, non-reparse file by handle.
func RemovePlain(path string) error {
	err := func() error {
		dir, err := openPlainDir(filepath.Dir(path), 0, false)
		if err != nil {
			return err
		}
		defer dir.close()
		h, err := openAt(dir.last(), filepath.Base(path), windows.DELETE, windows.FILE_OPEN, windows.FILE_NON_DIRECTORY_FILE)
		if err != nil {
			return err
		}
		defer windows.CloseHandle(h)
		if err := plain(h); err != nil {
			return err
		}
		deleteFile := byte(1) // FILE_DISPOSITION_INFO
		return windows.SetFileInformationByHandle(h, windows.FileDispositionInfo, &deleteFile, 1)
	}()
	if err != nil {
		return &os.PathError{Op: "remove", Path: path, Err: err}
	}
	return nil
}

// OpenPlain opens a regular file without traversing any reparse point.
func OpenPlain(path string) (*os.File, error) {
	dir, err := openPlainDir(filepath.Dir(path), 0, false)
	if err != nil {
		return nil, &os.PathError{Op: "open", Path: path, Err: err}
	}
	defer dir.close()
	h, err := openAt(dir.last(), filepath.Base(path), windows.FILE_READ_DATA, windows.FILE_OPEN, windows.FILE_NON_DIRECTORY_FILE)
	if err == nil {
		err = plain(h)
		if err != nil {
			windows.CloseHandle(h)
		}
	}
	if err != nil {
		return nil, &os.PathError{Op: "open", Path: path, Err: err}
	}
	file := os.NewFile(uintptr(h), path)
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() {
		file.Close()
		return nil, &os.PathError{Op: "open", Path: path, Err: errNotRegular}
	}
	return file, nil
}

type plainDir struct{ handles []windows.Handle }

func (d *plainDir) last() windows.Handle { return d.handles[len(d.handles)-1] }
func (d *plainDir) close() {
	for i := len(d.handles) - 1; i >= 0; i-- {
		windows.CloseHandle(d.handles[i])
	}
}

// FILE_LIST_DIRECTORY makes these handles participate in sharing checks;
// attribute-only handles do not prevent a writer from changing reparse data.
// Keep the ancestors open without write/delete sharing until the operation
// ends, from the second level down (C:\Users\name, C:\ProgramData\OpenBear):
// the volume root and its first level (C:\Users, C:\ProgramData), which
// users cannot change, are shared fully, or anyone holding one of them open
// for writing would stop every operation.
func openPlainDir(path string, access uint32, create bool) (*plainDir, error) {
	path = strings.ReplaceAll(path, "/", `\`)
	if rest, ok := strings.CutPrefix(path, `\\?\`); ok {
		if len(rest) < 3 || rest[1] != ':' || rest[2] != '\\' {
			return nil, windows.ERROR_INVALID_NAME
		}
		path = rest
	}
	for _, name := range strings.Split(path, `\`) {
		if name == ".." {
			return nil, windows.ERROR_INVALID_NAME
		}
	}
	path, err := filepath.Abs(path)
	if err != nil {
		return nil, err
	}
	volume := filepath.VolumeName(path)
	if len(volume) != 2 || volume[1] != ':' || !((volume[0] >= 'A' && volume[0] <= 'Z') || (volume[0] >= 'a' && volume[0] <= 'z')) {
		return nil, windows.ERROR_INVALID_NAME
	}
	names := strings.FieldsFunc(path[len(volume):], func(r rune) bool { return r == '\\' })
	root, err := windows.UTF16PtrFromString(volume + `\`)
	if err != nil {
		return nil, err
	}
	rootAccess := uint32(0)
	if len(names) == 0 {
		rootAccess = access
	}
	h, err := windows.CreateFile(root, windows.FILE_LIST_DIRECTORY|windows.FILE_READ_ATTRIBUTES|windows.SYNCHRONIZE|rootAccess,
		shareAll, nil, windows.OPEN_EXISTING,
		windows.FILE_FLAG_BACKUP_SEMANTICS|windows.FILE_FLAG_OPEN_REPARSE_POINT, 0)
	if err != nil {
		return nil, err
	}
	dir := &plainDir{handles: []windows.Handle{h}}
	fail := func(err error) (*plainDir, error) { dir.close(); return nil, err }
	if err := plain(h); err != nil {
		return fail(err)
	}
	disposition := uint32(windows.FILE_OPEN)
	if create {
		disposition = windows.FILE_OPEN_IF
	}
	for i, name := range names {
		stepAccess := uint32(0)
		if i == len(names)-1 {
			stepAccess = access
		}
		share := uint32(windows.FILE_SHARE_READ)
		if i == 0 {
			share = shareAll
		}
		next, err := openShared(dir.last(), name, stepAccess, disposition, windows.FILE_DIRECTORY_FILE, share)
		if err != nil {
			return fail(err)
		}
		dir.handles = append(dir.handles, next)
		if err := plain(next); err != nil {
			return fail(err)
		}
	}
	return dir, nil
}

const shareAll = windows.FILE_SHARE_READ | windows.FILE_SHARE_WRITE | windows.FILE_SHARE_DELETE

func openAt(dir windows.Handle, name string, access, disposition, options uint32) (windows.Handle, error) {
	share := uint32(shareAll)
	if options&windows.FILE_DIRECTORY_FILE != 0 {
		share = windows.FILE_SHARE_READ
	}
	return openShared(dir, name, access, disposition, options, share)
}

func openShared(dir windows.Handle, name string, access, disposition, options, share uint32) (windows.Handle, error) {
	if name == "" || name == "." || name == ".." || strings.ContainsAny(name, `\/:`) {
		return 0, windows.ERROR_INVALID_NAME
	}
	objectName, err := windows.NewNTUnicodeString(name)
	if err != nil {
		return 0, err
	}
	attrs := windows.OBJECT_ATTRIBUTES{RootDirectory: dir, ObjectName: objectName, Attributes: windows.OBJ_CASE_INSENSITIVE}
	attrs.Length = uint32(unsafe.Sizeof(attrs))
	if options&windows.FILE_DIRECTORY_FILE != 0 {
		access |= windows.FILE_LIST_DIRECTORY
	}
	var h windows.Handle
	var status windows.IO_STATUS_BLOCK
	err = windows.NtCreateFile(&h, access|windows.FILE_READ_ATTRIBUTES|windows.SYNCHRONIZE, &attrs, &status, nil, 0,
		share, disposition, options|windows.FILE_OPEN_REPARSE_POINT|windows.FILE_SYNCHRONOUS_IO_NONALERT, 0, 0)
	if s, ok := err.(windows.NTStatus); ok {
		return 0, s.Errno()
	}
	return h, err
}

func plain(h windows.Handle) error {
	var info struct{ attributes, tag uint32 } // FILE_ATTRIBUTE_TAG_INFO
	if err := windows.GetFileInformationByHandleEx(h, windows.FileAttributeTagInfo, (*byte)(unsafe.Pointer(&info)), uint32(unsafe.Sizeof(info))); err != nil {
		return err
	}
	if info.attributes&windows.FILE_ATTRIBUTE_REPARSE_POINT != 0 {
		return errReparsePoint
	}
	return nil
}
