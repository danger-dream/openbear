package browser

import (
	"context"
	"encoding/base64"
	"errors"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

var (
	// downloadQuota bounds everything in the download directory; each
	// download is bounded by maxArtifactBytes as well.
	downloadQuota int64 = 1 << 30
	// downloadTTL is how long a finished download stays saveable.
	downloadTTL = time.Hour
)

// Chrome names a download by its GUID, so a valid one is also a plain file name.
var guidPattern = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)

// files is files.Files: uploads/drops through the helper world and
// downloads saved from the shared download directory.
type files struct {
	e      *Engine
	byGUID map[string]*download
	// untracked is what the download directory held besides tracked
	// downloads at the last prune.
	untracked int64
}

func (f *files) event(method string, params *Obj) {
	e := f.e
	switch method {
	case "Browser.downloadWillBegin":
		frameID := pyStr(params.Must("frameId"))
		guid := pyStr(params.Must("guid"))
		var page *Page
		for _, p := range e.pages.Values() {
			if _, ok := p.frames[frameID]; ok {
				page = p
				break
			}
		}
		if page == nil || !guidPattern.MatchString(guid) {
			return
		}
		f.prune()
		page.downloadSeq++
		record := &download{
			id: page.downloadSeq, name: params.Must("suggestedFilename"), guid: guid,
			state: "inProgress", page: page, received: 0, updated: time.Now(),
		}
		page.downloads.Set(record.id, record)
		f.byGUID[record.guid] = record
		for page.downloads.Len() > 20 {
			_, old := page.downloads.PopFirst()
			f.forget(old)
		}
	case "Browser.downloadProgress":
		guid := pyStr(params.Must("guid"))
		record := f.byGUID[guid]
		if record == nil {
			return
		}
		record.state = params.Must("state")
		record.received = params.Def("receivedBytes", 0)
		record.updated = time.Now()
		if pyEqual(record.state, "completed") {
			// Note which file it is while the browser has just written it.
			if file, err := f.openDownload(record); err == nil {
				file.Close()
			}
		}
		if !pyEqual(record.state, "inProgress") {
			return
		}
		switch {
		case mustNum(record.received) > e.cfgNum("maxArtifactBytes"):
			record.tooLarge = true
		case f.used() > downloadQuota:
			record.overQuota = true
		default:
			return
		}
		e.spawn(func(ctx context.Context) error { return f.cancel(ctx, record) })
	}
}

// forget drops a record no page lists any more, with its download.
func (f *files) forget(record *download) {
	delete(f.byGUID, record.guid)
	if pyEqual(record.state, "inProgress") {
		f.e.spawn(func(ctx context.Context) error { return f.cancel(ctx, record) })
		return
	}
	removeFile(filepath.Join(asStr(f.e.config.Must("downloadHostPath")), record.guid))
}

// used is what the download directory holds or is receiving.
func (f *files) used() int64 {
	total := f.untracked
	for _, record := range f.byGUID {
		if pyEqual(record.state, "inProgress") || pyEqual(record.state, "completed") {
			total += int64(mustNum(record.received))
		}
	}
	return total
}

// prune expires finished downloads older than downloadTTL and removes such
// files no record keeps (left by an earlier engine); the newer ones count
// as untracked.
func (f *files) prune() {
	now := time.Now()
	for _, record := range f.byGUID {
		if !pyEqual(record.state, "inProgress") && now.Sub(record.updated) > downloadTTL {
			record.page.downloads.Del(record.id)
			f.forget(record)
		}
	}
	dir := asStr(f.e.config.Must("downloadHostPath"))
	entries, _ := readDir(dir)
	f.untracked = 0
	for _, entry := range entries {
		guid := strings.TrimSuffix(entry.Name(), ".crdownload")
		if !guidPattern.MatchString(guid) || f.byGUID[guid] != nil {
			continue
		}
		info, err := entry.Info()
		if err != nil || !info.Mode().IsRegular() {
			continue
		}
		if now.Sub(info.ModTime()) > downloadTTL {
			removeFile(filepath.Join(dir, entry.Name()))
		} else {
			f.untracked += info.Size()
		}
	}
}

func (f *files) cancel(ctx context.Context, record *download) error {
	if pyEqual(record.state, "inProgress") {
		if _, err := f.e.call(ctx, "Browser.cancelDownload", O("guid", record.guid), ""); err != nil {
			return err
		}
		record.state = "canceled"
	}
	return nil
}

func isFile(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.Mode().IsRegular()
}

var errNotRegular = errors.New("not a regular file")

// fileIdentity names one file on its volume, whatever path reaches it.
type fileIdentity struct{ volume, index uint64 }

// openDownload opens the file the browser wrote for record: a regular file
// with no other name (a hard link could be any file its creator can reach),
// of the size the browser reported and, once seen, always that same file.
func (f *files) openDownload(record *download) (*os.File, error) {
	file, err := openRegular(filepath.Join(asStr(f.e.config.Must("downloadHostPath")), record.guid))
	if err != nil {
		return nil, err
	}
	id, links, err := identify(file)
	info, serr := file.Stat()
	if err != nil || serr != nil || links != 1 || record.file != nil && id != *record.file ||
		info.Size() != int64(mustNum(record.received)) {
		file.Close()
		return nil, errNotRegular
	}
	record.file = &id
	return file, nil
}

func (f *files) upload(ctx context.Context, page *Page, params *Obj) (result any, err error) {
	e := f.e
	drop := pyEqual(params.Must("op"), "drop")
	var node *Node
	if truthy(params.Def("target", nil)) {
		if node, err = page.resolve(ctx, asStr(params.Must("target")), true); err != nil {
			return nil, err
		}
	} else {
		chooser := page.obs.chooser
		if chooser == nil {
			return nil, browserError(ctx, "file_chooser_not_open", "", nil)
		}
		frame := page.frames[pyStr(chooser.Must("frameId"))]
		if frame == nil || frame.detached {
			return nil, browserError(ctx, "file_chooser_expired", "", nil)
		}
		if node, err = frame.resolve(ctx, chooser.Must("backendNodeId")); err != nil {
			return nil, err
		}
	}
	transfer := uuidHex()
	var paths []string
	for _, p := range asList(params.Def("paths", []any{})) {
		paths = append(paths, asStr(p))
	}
	limit := e.cfgNum("maxArtifactBytes")
	defer func() {
		node.frame.run(ctx, "function(id){this.__fileTransfers.delete(id)}", transfer)
		node.release(ctx)
	}()
	for _, p := range paths {
		if !isFile(p) {
			return nil, browserError(ctx, "upload_path_not_file", "", nil)
		}
	}
	var sum int64
	for _, p := range paths {
		info, err := os.Stat(p)
		if err != nil {
			return nil, err
		}
		sum += info.Size()
	}
	if float64(sum) > limit {
		return nil, browserError(ctx, "upload_too_large", "", nil)
	}
	if _, err := node.frame.run(ctx, "function(id){this.__startFiles(id)}", transfer); err != nil {
		return nil, err
	}
	var total int64
	for _, p := range paths {
		info, err := os.Stat(p)
		if err != nil {
			return nil, err
		}
		ns := info.ModTime().UnixNano()
		mtime := float64(ns/1e9) + float64(ns%1e9)*1e-9
		name := pyBaseName(p)
		mimeType := guessType(name)
		if mimeType == "" {
			mimeType = "application/octet-stream"
		}
		meta := O("name", name, "mimeType", mimeType, "lastModified", int64(mtime*1000))
		if _, err := node.frame.run(ctx, "function(id,meta){this.__startFile(id,meta)}", transfer, meta); err != nil {
			return nil, err
		}
		if err := f.sendChunks(ctx, node, transfer, p, &total, limit); err != nil {
			return nil, err
		}
	}
	markEffect(ctx)
	data := params.Def("data", newObj())
	if _, err := node.frame.run(ctx, "function(node,args){this.__finishFiles(node,args)}", node,
		O("id", transfer, "drop", drop, "data", data)); err != nil {
		return nil, err
	}
	if !drop {
		page.obs.chooser = nil
		return O("uploaded", len(paths)), nil
	}
	return O("dropped", true), nil
}

func (f *files) sendChunks(ctx context.Context, node *Node, transfer, path string, total *int64, limit float64) error {
	stream, err := os.Open(path)
	if err != nil {
		return err
	}
	defer stream.Close()
	buf := make([]byte, 256*1024)
	for {
		n, err := io.ReadFull(stream, buf)
		if n > 0 {
			*total += int64(n)
			if float64(*total) > limit {
				return browserError(ctx, "upload_too_large", "", nil)
			}
			if _, err := node.frame.run(ctx, "function(id,chunk){this.__fileChunk(id,chunk)}", transfer,
				base64.StdEncoding.EncodeToString(buf[:n])); err != nil {
				return err
			}
		}
		if err != nil {
			if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
				return nil
			}
			return err
		}
	}
}

func (f *files) execute(ctx context.Context, page *Page, params *Obj) (any, error) {
	op := params.Must("op")
	if pyEqual(op, "upload") || pyEqual(op, "drop") {
		return f.upload(ctx, page, params)
	}
	f.prune()
	if pyEqual(op, "list") {
		return O("downloads", page.downloadList()), nil
	}
	var record *download
	if id, ok := num(params.Must("id")); ok && id == float64(int(id)) {
		record, _ = page.downloads.Get(int(id))
	}
	if record == nil {
		return nil, browserError(ctx, "download_expired", "", nil)
	}
	if pyEqual(op, "cancel") {
		markEffect(ctx)
		if err := f.cancel(ctx, record); err != nil {
			return nil, err
		}
		return O("cancelled", true), nil
	}
	for pyEqual(record.state, "inProgress") {
		if err := f.e.retryPause(ctx); err != nil {
			return nil, err
		}
	}
	if record.tooLarge {
		return nil, browserError(ctx, "artifact_too_large", "", nil)
	}
	if record.overQuota {
		return nil, browserError(ctx, "download_quota_exceeded", "", nil)
	}
	if !pyEqual(record.state, "completed") {
		return nil, browserError(ctx, "download_cancelled", "", nil)
	}
	source, err := f.openDownload(record)
	if err != nil {
		return nil, browserError(ctx, "download_file_unavailable: configure shared download paths for a remote browser", "", nil)
	}
	defer source.Close()
	dest := pyPath(asStr(params.Must("outputPath")))
	written, err := f.copy(ctx, source, dest)
	if err != nil {
		return nil, err
	}
	return O("artifact", dest, "bytes", written, "name", record.name), nil
}

func (f *files) copy(ctx context.Context, reader io.Reader, dest string) (written int64, err error) {
	writer, err := os.OpenFile(dest, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o666)
	if err != nil {
		return 0, err
	}
	defer func() {
		cerr := writer.Close()
		if err == nil && cerr != nil {
			err = cerr
		}
		if r := recover(); r != nil {
			os.Remove(dest)
			panic(r)
		}
		if err != nil {
			os.Remove(dest)
		}
	}()
	if err := os.Chmod(dest, 0o600); err != nil {
		return 0, err
	}
	limit := f.e.cfgNum("maxArtifactBytes")
	buf := make([]byte, 256*1024)
	for {
		n, rerr := io.ReadFull(reader, buf)
		if n > 0 {
			written += int64(n)
			if float64(written) > limit {
				return written, browserError(ctx, "artifact_too_large", "", nil)
			}
			if _, err := writer.Write(buf[:n]); err != nil {
				return written, err
			}
			f.e.yield()
			if ctx.Err() != nil {
				return written, ctxErr(ctx)
			}
		}
		if rerr != nil {
			if errors.Is(rerr, io.EOF) || errors.Is(rerr, io.ErrUnexpectedEOF) {
				return written, nil
			}
			return written, rerr
		}
	}
}
