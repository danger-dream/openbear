//go:build !windows

package browser

import (
	"crypto/rand"
	"errors"
	"fmt"
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

func removeFile(path string) error { return removeCheckedFile(path, nil) }

func removeIdentifiedFile(path string, id fileIdentity) error { return removeCheckedFile(path, &id) }

func removeCheckedFile(path string, id *fileIdentity) error {
	dir, err := OpenDownloadDir(filepath.Dir(path), false)
	if err != nil {
		return err
	}
	defer dir.Close()
	file, err := openRegularAt(dir, filepath.Base(path))
	if err != nil {
		return err
	}
	defer file.Close() // also prevents inode reuse during isolation/verification
	checked, links, err := identify(file)
	if err != nil {
		return err
	}
	if id != nil && (links != 1 || *id != checked) {
		return errNotRegular
	}
	// Even the untracked/prune path pins the identity it just inspected. The
	// shared directory entry may be replaced at any time after this open.
	isolated, err := isolateDownload(dir, filepath.Base(path))
	if err != nil {
		return err
	}
	defer isolated.private.Close()
	return isolated.remove(checked, id != nil)
}

// Reuse a process-private fallback if someone has occupied the conventional
// name; a static foreign entry must not create one new directory per download.
var downloadRemovalSuffix = rand.Text()

// The shared directory's owner may rename the private directory, but cannot
// change its children. All operations on those children use its verified FD.
// Same-UID processes (which can control the node itself) are not a security
// boundary. Keep this small directory instead of racing an rmdir by public name.
func openDownloadRemovalDir(dir *os.File) (*os.File, error) {
	name := fmt.Sprintf(".openbear-cleanup-%d", os.Geteuid())
	for attempt := 0; attempt < 8; attempt++ {
		if attempt == 1 {
			name = fmt.Sprintf(".openbear-cleanup-%d-%s", os.Geteuid(), downloadRemovalSuffix)
		} else if attempt > 1 {
			name = ".openbear-cleanup-" + rand.Text()
		}
		err := unix.Mkdirat(int(dir.Fd()), name, 0o700)
		if err != nil && !errors.Is(err, unix.EEXIST) {
			return nil, err
		}
		fd, err := unix.Openat(int(dir.Fd()), name, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
		if err != nil {
			if errors.Is(err, unix.ELOOP) || errors.Is(err, unix.ENOTDIR) || errors.Is(err, unix.ENOENT) || errors.Is(err, unix.EACCES) {
				continue // a foreign/symlink entry cannot reserve our cleanup name
			}
			return nil, err
		}
		var st unix.Stat_t
		err = unix.Fstat(fd, &st)
		if err != nil || st.Uid != uint32(os.Geteuid()) || st.Mode&0o777 != 0o700 {
			unix.Close(fd)
			if err != nil {
				return nil, err
			}
			continue // never chmod or take ownership of someone else's directory
		}
		private := os.NewFile(uintptr(fd), filepath.Join(dir.Name(), name))
		if err := secureDownloadRemovalDir(private); err != nil {
			private.Close()
			return nil, err
		}
		return private, nil
	}
	return nil, errors.New("cannot reserve private download cleanup directory")
}

type isolatedDownload struct {
	dir, private   *os.File
	original, name string
}

func isolateDownload(dir *os.File, name string) (*isolatedDownload, error) {
	private, err := openDownloadRemovalDir(dir)
	if err != nil {
		return nil, err
	}
	q := &isolatedDownload{dir: dir, private: private, original: name, name: rand.Text()}
	// The destination is in a private namespace; an external writer can only
	// replace the source. We deliberately inspect AFTER this atomic move.
	if err := unix.Renameat(int(dir.Fd()), name, int(private.Fd()), q.name); err != nil {
		private.Close()
		return nil, err
	}
	return q, nil
}

func (q *isolatedDownload) remove(expected fileIdentity, singleLink bool) error {
	var st unix.Stat_t
	err := unix.Fstatat(int(q.private.Fd()), q.name, &st, unix.AT_SYMLINK_NOFOLLOW)
	if err == nil && st.Mode&unix.S_IFMT == unix.S_IFREG &&
		expected == (fileIdentity{uint64(st.Dev), uint64(st.Ino)}) && (!singleLink || st.Nlink == 1) {
		err = unix.Unlinkat(int(q.private.Fd()), q.name, 0)
		if err == nil {
			return nil
		}
	}
	if err == nil {
		err = errNotRegular
	}
	// Do not discard mismatches or leave a recoverable object hidden. In
	// particular a raced symlink/directory must be restored without following it.
	return q.restore(err)
}

func (q *isolatedDownload) restore(cause error) error {
	name := q.original
	for attempt := 0; attempt < 8; attempt++ {
		err := renameDownloadNoReplace(int(q.private.Fd()), q.name, int(q.dir.Fd()), name)
		if err == nil {
			if name == q.original {
				return cause
			}
			return fmt.Errorf("%w; replacement preserved at %s", cause, filepath.Join(q.dir.Name(), name))
		}
		if !errors.Is(err, unix.EEXIST) {
			return fmt.Errorf("%w; restore failed (%v), preserved at %s", cause, err, filepath.Join(q.private.Name(), q.name))
		}
		// A new download/user file now occupies the original name. Keep BOTH;
		// never replace it and never delete the displaced, unverified object.
		name = ".openbear-recovered-" + rand.Text()
	}
	return fmt.Errorf("%w; recovery names occupied, preserved at %s", cause, filepath.Join(q.private.Name(), q.name))
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
