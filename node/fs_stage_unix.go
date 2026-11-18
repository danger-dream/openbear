//go:build unix

package node

import (
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"syscall"

	"golang.org/x/sys/unix"
)

// Both directories are pinned. The private staging directory prevents another
// user who can rename entries in the destination directory from substituting
// our source between its identity check and rename/link. Writes, metadata and
// hashing use the same open file, never the externally visible pathname.
type fileStage struct {
	file             *os.File
	parent, dir      int
	name, base, temp string
}

func newFileStage(path string) (*fileStage, error) {
	parent, err := unix.Open(filepath.Dir(path), unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, fileError(err)
	}
	name := ".obstage-" + randomHex(16)
	if err = unix.Mkdirat(parent, name, 0700); err != nil {
		unix.Close(parent)
		return nil, fileError(err)
	}
	dir, err := unix.Openat(parent, name, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		unix.Close(parent)
		return nil, fileError(err)
	}
	var st unix.Stat_t
	if err = unix.Fstat(dir, &st); err != nil || st.Uid != uint32(os.Geteuid()) || st.Mode&0777 != 0700 {
		unix.Close(dir)
		unix.Close(parent)
		return nil, errors.New("upload_changed")
	}
	fd, err := unix.Openat(dir, "data", unix.O_RDWR|unix.O_CREAT|unix.O_EXCL|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0600)
	s := &fileStage{parent: parent, dir: dir, name: name, base: filepath.Base(path), temp: filepath.Join(filepath.Dir(path), name, "data")}
	if err != nil {
		s.close()
		return nil, fileError(err)
	}
	s.file = os.NewFile(uintptr(fd), s.temp)
	return s, nil
}

func (s *fileStage) publish(noReplace bool) error {
	var held, named unix.Stat_t
	if unix.Fstat(int(s.file.Fd()), &held) != nil || unix.Fstatat(s.dir, "data", &named, unix.AT_SYMLINK_NOFOLLOW) != nil || held.Dev != named.Dev || held.Ino != named.Ino || named.Mode&unix.S_IFMT != unix.S_IFREG || named.Nlink != 1 {
		return errUploadChanged
	}
	// Private directory ownership is checked at acquisition, and its fd remains
	// open even if an untrusted parent renames it. No path re-open occurs here.
	if noReplace {
		return unix.Linkat(s.dir, "data", s.parent, s.base, 0)
	}
	return unix.Renameat(s.dir, "data", s.parent, s.base)
}

func (s *fileStage) close() {
	if s.file != nil {
		s.file.Close()
		s.file = nil
	}
	unix.Unlinkat(s.dir, "data", 0)
	// Never recurse through the public name: it could now name somebody else's directory.
	var held, named unix.Stat_t
	if unix.Fstat(s.dir, &held) == nil && unix.Fstatat(s.parent, s.name, &named, unix.AT_SYMLINK_NOFOLLOW) == nil && held.Dev == named.Dev && held.Ino == named.Ino {
		unix.Unlinkat(s.parent, s.name, unix.AT_REMOVEDIR)
	}
	unix.Fsync(s.parent)
	unix.Close(s.dir)
	unix.Close(s.parent)
}

func keepFileOwner(f *os.File, prior fs.FileInfo) error {
	// Use the descriptor, not a name another directory writer can replace.
	st, ok := prior.Sys().(*syscall.Stat_t)
	if !ok {
		return nil
	}
	return f.Chown(int(st.Uid), int(st.Gid))
}

// Recovery only removes the recorded object inside an owned private stage.
func recoverFileStage(record uploadRecord) {
	path := record.Temp
	if !filepath.IsAbs(path) || filepath.Base(path) != "data" || !strings.HasPrefix(filepath.Base(filepath.Dir(path)), ".obstage-") {
		return
	}
	parent, err := unix.Open(filepath.Dir(filepath.Dir(path)), unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC, 0)
	if err != nil {
		return
	}
	defer unix.Close(parent)
	name := filepath.Base(filepath.Dir(path))
	dir, err := unix.Openat(parent, name, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return
	}
	defer unix.Close(dir)
	var st unix.Stat_t
	if unix.Fstat(dir, &st) != nil || st.Uid != uint32(os.Geteuid()) || st.Mode&0777 != 0700 {
		return
	}
	fd, err := unix.Openat(dir, "data", unix.O_RDONLY|unix.O_NONBLOCK|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return
	}
	f := os.NewFile(uintptr(fd), path)
	defer f.Close()
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() || fileIdentity(f, info) != record.Identity {
		return
	}
	unix.Unlinkat(dir, "data", 0)
	var named unix.Stat_t
	if unix.Fstatat(parent, name, &named, unix.AT_SYMLINK_NOFOLLOW) == nil && named.Dev == st.Dev && named.Ino == st.Ino {
		unix.Unlinkat(parent, name, unix.AT_REMOVEDIR)
	}
}

func openReadFile(path string) (*os.File, error) {
	fd, err := unix.Open(path, unix.O_RDONLY|unix.O_NONBLOCK|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	return os.NewFile(uintptr(fd), path), nil
}
