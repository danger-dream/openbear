//go:build !windows

package browser

import (
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"

	"golang.org/x/sys/unix"
)

// identify returns which file f is and how many names it has.
func identify(f *os.File) (fileIdentity, uint64, error) {
	info, err := f.Stat()
	if err != nil {
		return fileIdentity{}, 0, err
	}
	st, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return fileIdentity{}, 0, errNotRegular
	}
	return fileIdentity{uint64(st.Dev), uint64(st.Ino)}, uint64(st.Nlink), nil
}

// OpenDownloadDir traverses each component without following user-controlled links.
func OpenDownloadDir(path string, create bool) (*os.File, error) {
	path, err := filepath.Abs(path)
	if err != nil {
		return nil, err
	}
	// macOS exposes these system-owned aliases even in its default temporary paths.
	if runtime.GOOS == "darwin" {
		for _, alias := range []string{"/tmp", "/var", "/etc"} {
			if path == alias || strings.HasPrefix(path, alias+"/") {
				path = "/private" + path
				break
			}
		}
	}
	fd, err := unix.Open("/", unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	for _, name := range strings.Split(strings.TrimPrefix(path, "/"), "/") {
		if name == "" {
			continue
		}
		if create {
			if err = unix.Mkdirat(fd, name, 0o777); err != nil && !errors.Is(err, unix.EEXIST) {
				unix.Close(fd)
				return nil, &os.PathError{Op: "mkdir", Path: path, Err: err}
			}
		}
		next, err := unix.Openat(fd, name, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
		unix.Close(fd)
		if err != nil {
			return nil, &os.PathError{Op: "open", Path: path, Err: err}
		}
		fd = next
	}
	return os.NewFile(uintptr(fd), path), nil
}

func removeFile(path string) error {
	dir, err := OpenDownloadDir(filepath.Dir(path), false)
	if err != nil {
		return err
	}
	defer dir.Close()
	var st unix.Stat_t
	if err = unix.Fstatat(int(dir.Fd()), filepath.Base(path), &st, unix.AT_SYMLINK_NOFOLLOW); err != nil {
		return err
	}
	if st.Mode&unix.S_IFMT != unix.S_IFREG {
		return errNotRegular
	}
	return unix.Unlinkat(int(dir.Fd()), filepath.Base(path), 0)
}

func mkdirAll(path string) error {
	dir, err := OpenDownloadDir(path, true)
	if err != nil {
		return err
	}
	return dir.Close()
}

func readDir(path string) ([]os.DirEntry, error) {
	dir, err := OpenDownloadDir(path, false)
	if err != nil {
		return nil, err
	}
	defer dir.Close()
	entries, err := dir.ReadDir(-1)
	if err != nil {
		return nil, err
	}
	out := make([]os.DirEntry, 0, len(entries))
	for _, entry := range entries {
		file, err := openRegularAt(dir, entry.Name())
		if err != nil {
			continue
		}
		info, err := file.Stat()
		file.Close()
		if err == nil {
			out = append(out, regularEntry{info})
		}
	}
	return out, nil
}

type regularEntry struct{ os.FileInfo }

func (e regularEntry) Type() os.FileMode          { return e.Mode().Type() }
func (e regularEntry) Info() (os.FileInfo, error) { return e.FileInfo, nil }

func openRegular(path string) (*os.File, error) {
	dir, err := OpenDownloadDir(filepath.Dir(path), false)
	if err != nil {
		return nil, err
	}
	defer dir.Close()
	return openRegularAt(dir, filepath.Base(path))
}

func openRegularAt(dir *os.File, name string) (*os.File, error) {
	fd, err := unix.Openat(int(dir.Fd()), name, unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_NONBLOCK|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	file := os.NewFile(uintptr(fd), name)
	if info, err := file.Stat(); err != nil || !info.Mode().IsRegular() {
		file.Close()
		return nil, errNotRegular
	}
	return file, nil
}
