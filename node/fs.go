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
	e := fileEntry{Name: name, Size: info.Size(), Mode: fmt.Sprintf("%04o", posixMode(info.Mode())), MTime: info.ModTime().Unix()}
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
	stage        *fileStage
	request      string
	noReplace    bool
	prior        fs.FileInfo
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
	// pending holds a file per unfinished upload naming its temp file, so the
	// next run of the node removes what this one left (see recover).
	pending string
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
	if method == "fs.writeSafe" {
		p.NoReplace = true
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
			u.dropLocked(p.Upload)
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
	f, err := openReadFile(path)
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
	result := map[string]any{"data": base64.StdEncoding.EncodeToString(buf[:n]), "size": info.Size(), "offset": offset,
		"read": n, "eof": eof, "mode": fmt.Sprintf("%04o", posixMode(info.Mode())), "mtime": info.ModTime().Unix()}
	// Which file this chunk came from: a reader in chunks tells a file replaced
	// under the same name (an atomic save, a log rotation) from one that grew.
	if id := fileIdentity(f, info); id != "" {
		result["file"] = id
	}
	return result, nil
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

// write keeps the source object and destination directory pinned throughout an
// upload. A continuation cannot change its target or no-replace contract.
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
	var up *upload
	if p.Upload != "" {
		u.mu.Lock()
		up = u.open[p.Upload]
		u.mu.Unlock()
	}
	if p.Offset != 0 {
		if up == nil || up.request != path || up.size != p.Offset || up.noReplace != p.NoReplace {
			return nil, errors.New("upload_out_of_order")
		}
	} else {
		if up != nil {
			u.dropLocked(p.Upload)
		}
		if p.Mkdirs {
			if err = os.MkdirAll(filepath.Dir(path), 0755); err != nil {
				return nil, fileError(err)
			}
		}
		requested := path
		var prior fs.FileInfo
		// Do not resolve a no-replace target symlink: the existing directory entry
		// itself counts as occupied, even when its referent does not exist.
		if !p.NoReplace {
			path, prior, err = target(path)
			if err != nil {
				return nil, err
			}
		}
		stage, err := newFileStage(path)
		if err != nil {
			return nil, err
		}
		up = &upload{stage: stage, temp: stage.temp, target: path, request: requested, noReplace: p.NoReplace, prior: prior}
		if p.Upload != "" {
			u.mu.Lock()
			u.open[p.Upload] = up
			if !u.janitor {
				u.janitor = true
				go u.sweep()
			}
			u.mu.Unlock()
			u.remember(p.Upload, up)
		}
	}
	if p.Upload == "" {
		defer up.stage.close()
	}
	u.mu.Lock()
	up.touched = time.Now()
	u.mu.Unlock()
	if err = up.stage.write(data); err != nil {
		if p.Upload != "" {
			u.dropLocked(p.Upload)
		}
		return nil, err
	}
	u.mu.Lock()
	up.size += int64(len(data))
	size := up.size
	u.mu.Unlock()
	if p.Upload != "" && !p.Done {
		return map[string]any{"path": shownPath(up.target), "size": size, "done": false}, nil
	}
	if p.Upload != "" {
		defer u.dropLocked(p.Upload)
	}
	if p.SHA256 != "" {
		result, err := hashOpenFile(up.stage.file)
		if err != nil {
			return nil, err
		}
		if result.Size != p.Size || result.SHA256 != p.SHA256 {
			return nil, errors.New("checksum_mismatch")
		}
	}
	if err = up.stage.commit(p.Mode, up.prior, up.noReplace); err != nil {
		return nil, err
	}
	result := map[string]any{"path": shownPath(up.target), "size": size, "done": true}
	if p.SHA256 != "" {
		result["sha256"] = p.SHA256
	}
	return result, nil
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
		up.stage.close()
		u.forget(id)
	}
}

// recover removes the temp files of uploads a previous run of the node left
// unfinished (a restart or an update in the middle of one), then records this
// run's uploads in dir. Only the files those records name are touched, never
// others that merely look alike.
func (u *uploads) recover(dir string) {
	u.mu.Lock()
	u.pending = dir
	u.mu.Unlock()
	entries, _ := os.ReadDir(dir)
	for _, e := range entries {
		id := e.Name()
		if !uploadID.MatchString(id) {
			continue
		}
		unlock := u.lock(id)
		u.mu.Lock()
		_, open := u.open[id]
		u.mu.Unlock()
		if !open {
			data, err := os.ReadFile(filepath.Join(dir, id))
			temp := string(data)
			var record uploadRecord
			if err == nil && json.Unmarshal(data, &record) == nil && record.Identity != "" {
				recoverFileStage(record)
			} else if name := filepath.Base(temp); err == nil && filepath.IsAbs(temp) && strings.HasPrefix(name, ".") && strings.HasSuffix(name, ".obupload-"+id) {
				// N-1 records name a single legacy temporary entry; unlink only.
				os.Remove(temp)
			}
			os.Remove(filepath.Join(dir, id))
		}
		unlock()
	}
}

// remember and forget keep the record of an unfinished upload; losing one
// only costs the cleanup after a restart.
type uploadRecord struct {
	Temp     string `json:"temp"`
	Identity string `json:"identity"`
}

func (u *uploads) remember(id string, up *upload) {
	u.mu.Lock()
	dir := u.pending
	u.mu.Unlock()
	if dir == "" || os.MkdirAll(dir, 0o700) != nil {
		return
	}
	info, err := up.stage.file.Stat()
	if err != nil {
		return
	}
	data, _ := json.Marshal(uploadRecord{Temp: up.temp, Identity: fileIdentity(up.stage.file, info)})
	os.WriteFile(filepath.Join(dir, id), data, 0o600)
}

func (u *uploads) forget(id string) {
	u.mu.Lock()
	dir := u.pending
	u.mu.Unlock()
	if dir != "" {
		os.Remove(filepath.Join(dir, id))
	}
}

// recoverUploads keeps upload records next to the jobs, which also outlive
// the process; an agent with neither a config nor a jobs directory keeps none.
func (a *Agent) recoverUploads() {
	dir := a.jobDir
	if dir == "" && a.ConfigPath != "" {
		dir = jobsDir(a.ConfigPath)
	}
	if dir != "" {
		a.uploads.recover(filepath.Join(dir, ".uploads"))
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
