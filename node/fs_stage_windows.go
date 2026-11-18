//go:build windows

package node

import (
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"unsafe"

	"golang.org/x/sys/windows"
)

type fileStage struct {
	file       *os.File
	parent     windows.Handle
	base, temp string
	published  bool
}

func newFileStage(path string) (*fileStage, error) {
	dir, err := windows.UTF16PtrFromString(filepath.Dir(path))
	if err != nil {
		return nil, fileError(err)
	}
	// Resolve existing junctions as before, then pin that directory. All further
	// creates and the final rename are relative to this handle, not the path.
	parent, err := windows.CreateFile(dir, windows.FILE_LIST_DIRECTORY|windows.SYNCHRONIZE,
		windows.FILE_SHARE_READ|windows.FILE_SHARE_WRITE|windows.FILE_SHARE_DELETE, nil, windows.OPEN_EXISTING, windows.FILE_FLAG_BACKUP_SEMANTICS, 0)
	if err != nil {
		return nil, fileError(err)
	}
	name := ".obstage-" + randomHex(16)
	objectName, err := windows.NewNTUnicodeString(name)
	if err != nil {
		windows.CloseHandle(parent)
		return nil, fileError(err)
	}
	attrs := windows.OBJECT_ATTRIBUTES{RootDirectory: parent, ObjectName: objectName, Attributes: windows.OBJ_CASE_INSENSITIVE}
	attrs.Length = uint32(unsafe.Sizeof(attrs))
	var handle windows.Handle
	var status windows.IO_STATUS_BLOCK
	// No write/delete sharing: nobody can replace, link, or modify the temporary
	// object between chunks. Rename and cancellation also operate by this handle.
	err = windows.NtCreateFile(&handle, windows.GENERIC_READ|windows.GENERIC_WRITE|windows.DELETE|windows.SYNCHRONIZE,
		&attrs, &status, nil, windows.FILE_ATTRIBUTE_NORMAL, windows.FILE_SHARE_READ, windows.FILE_CREATE,
		windows.FILE_NON_DIRECTORY_FILE|windows.FILE_OPEN_REPARSE_POINT|windows.FILE_SYNCHRONOUS_IO_NONALERT, 0, 0)
	if err != nil {
		windows.CloseHandle(parent)
		return nil, fileError(stageWindowsError(err))
	}
	temp := filepath.Join(filepath.Dir(path), name)
	return &fileStage{file: os.NewFile(uintptr(handle), temp), parent: parent, base: filepath.Base(path), temp: temp}, nil
}

func stageWindowsError(err error) error {
	if status, ok := err.(windows.NTStatus); ok {
		return status.Errno()
	}
	return err
}

func (s *fileStage) publish(noReplace bool) error {
	name, err := windows.UTF16FromString(s.base)
	if err != nil {
		return err
	}
	// FILE_RENAME_INFORMATION: a variable-size filename relative to the pinned
	// destination directory. ReplaceIfExists=false is an atomic kernel decision.
	var header struct {
		replace uint32
		root    windows.Handle
		length  uint32
		name    [1]uint16
	}
	offset := unsafe.Offsetof(header.name)
	buf := make([]byte, max(int(unsafe.Sizeof(header)), int(offset)+2*(len(name)-1)))
	info := (*struct {
		replace uint32
		root    windows.Handle
		length  uint32
	})(unsafe.Pointer(&buf[0]))
	if !noReplace {
		info.replace = 1
	}
	info.root, info.length = s.parent, uint32(2*(len(name)-1))
	copy(unsafe.Slice((*uint16)(unsafe.Pointer(&buf[offset])), len(name)-1), name[:len(name)-1])
	var status windows.IO_STATUS_BLOCK
	err = windows.NtSetInformationFile(windows.Handle(s.file.Fd()), &status, &buf[0], uint32(len(buf)), windows.FileRenameInformation)
	if err == nil {
		s.published = true
	}
	return stageWindowsError(err)
}

func (s *fileStage) close() {
	if s.file != nil {
		if !s.published {
			// A failed commit may already have set read-only mode. Clear it on
			// our held source only so cancellation can delete that object.
			s.file.Chmod(0600)
			remove := byte(1)
			windows.SetFileInformationByHandle(windows.Handle(s.file.Fd()), windows.FileDispositionInfo, &remove, 1)
		}
		s.file.Close()
		s.file = nil
	}
	windows.CloseHandle(s.parent)
}

func recoverFileStage(record uploadRecord) {
	if !filepath.IsAbs(record.Temp) || !strings.HasPrefix(filepath.Base(record.Temp), ".obstage-") {
		return
	}
	name, err := windows.UTF16PtrFromString(record.Temp)
	if err != nil {
		return
	}
	h, err := windows.CreateFile(name, windows.GENERIC_READ|windows.DELETE, windows.FILE_SHARE_READ, nil,
		windows.OPEN_EXISTING, windows.FILE_FLAG_OPEN_REPARSE_POINT, 0)
	if err != nil {
		return
	}
	f := os.NewFile(uintptr(h), record.Temp)
	defer f.Close()
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() || fileIdentity(f, info) != record.Identity {
		return
	}
	remove := byte(1)
	windows.SetFileInformationByHandle(h, windows.FileDispositionInfo, &remove, 1)
}

func keepFileOwner(*os.File, fs.FileInfo) error  { return nil }
func openReadFile(path string) (*os.File, error) { return os.Open(path) }
