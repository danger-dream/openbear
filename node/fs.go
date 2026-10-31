//go:build unix || windows

package node

import (
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const (
	fileChunkMax = 4 << 20
	listLimit    = 5000
	uploadIdle   = 15 * time.Minute
)

var uploadID = regexp.MustCompile(`^[A-Za-z0-9]{8,40}$`)

type fileParams struct {
	Path      string `json:"path"`
	Offset    int64  `json:"offset"`
	Limit     int64  `json:"limit"`
	Data      string `json:"data"`
	Mode      string `json:"mode"`
	SHA256    string `json:"sha256"`
	Size      int64  `json:"size"`
	NoReplace bool   `json:"noReplace"`
	Upload    string `json:"upload"`
	Done      bool   `json:"done"`
	Mkdirs    bool   `json:"mkdirs"`
}

type fileEntry struct {
	Name   string `json:"name"`
	Type   string `json:"type"`
	Dir    bool   `json:"dir,omitempty"`
	Size   int64  `json:"size"`
	Mode   string `json:"mode"`
	MTime  int64  `json:"mtime"`
	Target string `json:"target,omitempty"`
}

func describe(path, name string, info fs.FileInfo) fileEntry {
	e := fileEntry{Name: name, Size: info.Size(), Mode: fmt.Sprintf("%04o", info.Mode().Perm()), MTime: info.ModTime().Unix()}
	switch m := info.Mode(); {
	case m.IsDir():
		e.Type, e.Dir = "dir", true
	case m.IsRegular():
		e.Type = "file"
	case m&fs.ModeSymlink != 0:
		e.Type = "link"
		e.Target, _ = os.Readlink(path)
		if st, err := os.Stat(path); err == nil && st.IsDir() {
			e.Dir = true
		}
	case reparseDir(info):
		// A junction (mklink /J) or a cloud folder: entered like a link to a directory.
		e.Type, e.Dir = "link", true
		e.Target, _ = os.Readlink(path)
	case reparseFile(info):
		e.Type = "file"
	default:
		e.Type = "other"
	}
	return e
}

func fileError(err error) error {
	if code := platformFileError(err); code != "" {
		return errors.New(code)
	}
	switch {
	case errors.Is(err, fs.ErrNotExist):
		return errors.New("not_found")
	case errors.Is(err, fs.ErrPermission):
		return errors.New("permission_denied")
	case errors.Is(err, syscall.EISDIR):
		return errors.New("is_directory")
	case errors.Is(err, syscall.ENOTDIR):
		return errors.New("not_a_directory")
	case errors.Is(err, syscall.ENOSPC):
		return errors.New("no_space")
	case errors.Is(err, syscall.EROFS):
		return errors.New("read_only_filesystem")
	}
	return errors.New("io_error")
}

type upload struct {
	temp, target string
	size         int64
	touched      time.Time
}

// uploads tracks chunked writes; an abandoned one loses its temp file after
// uploadIdle instead of leaving it next to the target forever.
type uploads struct {
	mu      sync.Mutex
	open    map[string]*upload
	locks   map[string]*uploadLock
	janitor bool
	editMu  sync.Mutex
}

type uploadLock struct {
	mu   sync.Mutex
	refs int
}

func newUploads() *uploads {
	return &uploads{open: map[string]*upload{}, locks: map[string]*uploadLock{}}
}

func (u *uploads) handle(method string, raw json.RawMessage) (any, error) {
	var p fileParams
	if json.Unmarshal(raw, &p) != nil || strings.ContainsRune(p.Path, 0) || p.Offset < 0 {
		return nil, errors.New("invalid_params")
	}
	if method == "fs.list" && hasDrives && p.Path == "/" {
		return drives()
	}
	path, ok := localPath(p.Path)
	if !ok {
		return nil, errors.New("invalid_params")
	}
	switch method {
	case "fs.hash":
		return hashFile(path)
	case "fs.cancel":
		if !uploadID.MatchString(p.Upload) {
			return nil, errors.New("invalid_params")
		}
		unlock := u.lock(p.Upload)
		defer unlock()
		u.mu.Lock()
		up := u.open[p.Upload]
		u.mu.Unlock()
		if up != nil {
			if err := os.Remove(up.temp); err != nil && !errors.Is(err, fs.ErrNotExist) {
				return nil, fileError(err)
			}
			u.mu.Lock()
			delete(u.open, p.Upload)
			u.mu.Unlock()
		}
		return map[string]any{"cancelled": true}, nil
	case "fs.stat":
		info, err := os.Lstat(path)
		if err != nil {
			return nil, fileError(err)
		}
		return describe(path, filepath.Base(path), info), nil
	case "fs.list":
		return list(path)
	case "fs.read":
		return read(path, p.Offset, p.Limit)
	}
	if method == "fs.transfer" {
		if !uploadID.MatchString(p.Upload) || p.Size < 0 || (p.Done && !fileDigest.MatchString(p.SHA256)) {
			return nil, errors.New("invalid_params")
		}
		if info, err := os.Lstat(path); err == nil {
			if p.NoReplace {
				return nil, errors.New("file_exists")
			}
			if !info.Mode().IsRegular() {
				return nil, errors.New("not_regular_file")
			}
		} else if !errors.Is(err, fs.ErrNotExist) {
			return nil, fileError(err)
		}
	}
	return u.write(path, p)
}

func list(path string) (any, error) {
	dir, err := os.Open(path)
	if err != nil {
		return nil, fileError(err)
	}
	defer dir.Close()
	names, err := dir.Readdirnames(listLimit + 1)
	if err != nil && err != io.EOF {
		return nil, fileError(err)
	}
	truncated := len(names) > listLimit
	if truncated {
		names = names[:listLimit]
	}
	entries := make([]fileEntry, 0, len(names))
	for _, name := range names {
		full := filepath.Join(path, name)
		if info, err := os.Lstat(full); err == nil {
			entries = append(entries, describe(full, name, info))
		}
	}
	sort.Slice(entries, func(i, j int) bool {
		if entries[i].Dir != entries[j].Dir {
			return entries[i].Dir
		}
		return entries[i].Name < entries[j].Name
	})
	return map[string]any{"path": shownPath(path), "entries": entries, "truncated": truncated}, nil
}

func read(path string, offset, limit int64) (any, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, fileError(err)
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return nil, fileError(err)
	}
	if info.IsDir() {
		return nil, errors.New("is_directory")
	}
	// Devices and pipes never end; /proc files are regular and allowed.
	if !info.Mode().IsRegular() && !reparseFile(info) {
		return nil, errors.New("not_regular_file")
	}
	if limit <= 0 {
		limit = 1 << 20
	}
	buf := make([]byte, min(limit, fileChunkMax))
	n, err := f.ReadAt(buf, offset)
	if err != nil && err != io.EOF {
		return nil, fileError(err)
	}
	eof := err == io.EOF || info.Size() > 0 && offset+int64(n) >= info.Size()
	return map[string]any{"data": base64.StdEncoding.EncodeToString(buf[:n]), "size": info.Size(), "offset": offset,
		"read": n, "eof": eof, "mode": fmt.Sprintf("%04o", info.Mode().Perm()), "mtime": info.ModTime().Unix()}, nil
}

func randomHex(n int) string {
	b := make([]byte, n)
	rand.Read(b)
	return hex.EncodeToString(b)
}

// target resolves a symlinked destination so a write replaces the file it
// points at rather than the link itself (e.g. /etc/resolv.conf).
func target(path string) (string, fs.FileInfo, error) {
	if info, err := os.Lstat(path); err == nil && info.Mode()&fs.ModeSymlink != 0 {
		if resolved, err := filepath.EvalSymlinks(path); err == nil {
			path = resolved
		}
	}
	info, err := os.Stat(path)
	if err != nil && !errors.Is(err, fs.ErrNotExist) {
		return "", nil, fileError(err)
	}
	if info != nil && info.IsDir() {
		return "", nil, errors.New("is_directory")
	}
	return path, info, nil
}

// commit gives the finished temp file the existing file's owner (or the
// requested mode) and renames it into place atomically.
func commit(temp, path, mode string, prior fs.FileInfo) error {
	perm := fs.FileMode(0o644)
	if prior != nil {
		perm = prior.Mode().Perm()
	}
	if mode != "" {
		v, err := strconv.ParseUint(mode, 8, 32)
		if err != nil || v > 0o7777 {
			os.Remove(temp)
			return errors.New("invalid_mode")
		}
		perm = fs.FileMode(v)
	}
	if err := os.Chmod(temp, perm); err != nil {
		os.Remove(temp)
		return fileError(err)
	}
	if prior != nil {
		keepOwner(temp, prior)
	}
	if err := os.Rename(temp, path); err != nil {
		os.Remove(temp)
		return fileError(err)
	}
	if d, err := os.Open(filepath.Dir(path)); err == nil {
		d.Sync()
		d.Close()
	}
	return nil
}

func (u *uploads) write(path string, p fileParams) (any, error) {
	data, err := base64.StdEncoding.DecodeString(p.Data)
	if err != nil || len(data) > fileChunkMax {
		return nil, errors.New("invalid_params")
	}
	if p.Upload != "" {
		if !uploadID.MatchString(p.Upload) {
			return nil, errors.New("invalid_params")
		}
		unlock := u.lock(p.Upload)
		defer unlock()
	}
	if p.Mkdirs {
		if err = os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			return nil, fileError(err)
		}
	}
	path, prior, err := target(path)
	if err != nil {
		return nil, err
	}
	dir, base := filepath.Dir(path), filepath.Base(path)
	if p.Upload == "" {
		temp := filepath.Join(dir, "."+base+".obtmp-"+randomHex(6))
		if err = writeFile(temp, data, os.O_WRONLY|os.O_CREATE|os.O_EXCL, true); err != nil {
			os.Remove(temp)
			return nil, err
		}
		if err = commit(temp, path, p.Mode, prior); err != nil {
			return nil, err
		}
		return map[string]any{"path": shownPath(path), "size": len(data), "done": true}, nil
	}
	u.mu.Lock()
	up := u.open[p.Upload]
	if p.Offset == 0 {
		if up != nil {
			os.Remove(up.temp)
		}
		up = &upload{temp: filepath.Join(dir, "."+base+".obupload-"+p.Upload), target: path}
		u.open[p.Upload] = up
		if !u.janitor {
			u.janitor = true
			go u.sweep()
		}
	} else if up == nil || up.target != path || up.size != p.Offset {
		u.mu.Unlock()
		return nil, errors.New("upload_out_of_order")
	}
	up.touched = time.Now()
	u.mu.Unlock()
	flags := os.O_WRONLY | os.O_APPEND
	if p.Offset == 0 {
		flags = os.O_WRONLY | os.O_CREATE | os.O_TRUNC
	}
	if err = writeFile(up.temp, data, flags, p.Done); err != nil {
		u.dropLocked(p.Upload)
		return nil, err
	}
	u.mu.Lock()
	up.size += int64(len(data))
	size := up.size
	u.mu.Unlock()
	if !p.Done {
		return map[string]any{"path": shownPath(path), "size": size, "done": false}, nil
	}
	u.mu.Lock()
	delete(u.open, p.Upload)
	u.mu.Unlock()
	if p.SHA256 != "" {
		defer os.Remove(up.temp)
		result, err := hashFile(up.temp)
		if err != nil {
			return nil, err
		}
		if result.Size != p.Size || result.SHA256 != p.SHA256 {
			return nil, errors.New("checksum_mismatch")
		}
	}
	if p.NoReplace {
		err = commitNew(up.temp, path, p.Mode)
	} else {
		err = commit(up.temp, path, p.Mode, prior)
	}
	if err != nil {
		return nil, err
	}
	result := map[string]any{"path": shownPath(path), "size": size, "done": true}
	if p.SHA256 != "" {
		result["sha256"] = p.SHA256
	}
	return result, nil
}

func writeFile(path string, data []byte, flags int, sync bool) error {
	f, err := os.OpenFile(path, flags, 0o600)
	if err != nil {
		return fileError(err)
	}
	_, err = f.Write(data)
	if err == nil && sync {
		err = f.Sync()
	}
	if closeErr := f.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return fileError(err)
	}
	return nil
}

func (u *uploads) lock(id string) func() {
	u.mu.Lock()
	lock := u.locks[id]
	if lock == nil {
		lock = &uploadLock{}
		u.locks[id] = lock
	}
	lock.refs++
	u.mu.Unlock()
	lock.mu.Lock()
	return func() {
		lock.mu.Unlock()
		u.mu.Lock()
		lock.refs--
		if lock.refs == 0 {
			delete(u.locks, id)
		}
		u.mu.Unlock()
	}
}

func (u *uploads) drop(id string) {
	unlock := u.lock(id)
	defer unlock()
	u.dropLocked(id)
}

// The ID lock stays held through deletion, including an offset-zero replacement.
func (u *uploads) dropLocked(id string) {
	u.mu.Lock()
	up := u.open[id]
	delete(u.open, id)
	u.mu.Unlock()
	if up != nil {
		os.Remove(up.temp)
	}
}

func (u *uploads) sweepIdle(now time.Time) bool {
	u.mu.Lock()
	ids := make([]string, 0, len(u.open))
	for id := range u.open {
		ids = append(ids, id)
	}
	u.mu.Unlock()
	for _, id := range ids {
		unlock := u.lock(id)
		u.mu.Lock()
		up := u.open[id]
		expired := up != nil && now.Sub(up.touched) > uploadIdle
		u.mu.Unlock()
		if expired {
			u.dropLocked(id)
		}
		unlock()
	}
	u.mu.Lock()
	defer u.mu.Unlock()
	if len(u.open) == 0 {
		u.janitor = false
		return true
	}
	return false
}

func (u *uploads) sweep() {
	for {
		time.Sleep(time.Minute)
		if u.sweepIdle(time.Now()) {
			return
		}
	}
}
