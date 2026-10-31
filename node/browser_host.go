package node

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"openbear/node/browser"
)

const (
	// Browser operations running at once; they never take command slots.
	maxBrowserCalls = 32
	browserChunk    = 4 << 20
	uploadTTL       = time.Hour
	// An artifact the controller never fetched (it went away mid-transfer).
	artifactTTL = time.Hour
)

// The controller drops a channel message over 16MB, and with it the session.
var maxBrowserReply = 15 << 20

// The controller service sends these itself; page work goes through the engine.
var (
	browserCDPMethods    = map[string]bool{"Target.getTargets": true, "Target.createTarget": true, "Target.closeTarget": true, "Browser.getVersion": true}
	browserTargetMethods = map[string]bool{"Runtime.evaluate": true, "Runtime.terminateExecution": true}
)

// launched is a browser this node started.
type launched struct {
	transport browser.Transport // pipe mode
	endpoint  string            // port mode: the browser's ws:// address
	downloads string            // where the browser saves files, readable by the node
	headless  bool
	reused    bool // left running by an earlier node process
	stop      func()
	done      <-chan struct{} // closed when the process exits
	output    func() string   // the end of the browser's stderr, when captured
}

// reusedBrowser is a window an earlier node process left on a desktop. This
// node holds no process handle, so stopping it waits for the CDP close.
func reusedBrowser(address, profile, downloads string) *launched {
	done := make(chan struct{})
	var once sync.Once
	return &launched{endpoint: address, downloads: downloads, reused: true, done: done, stop: func() {
		for deadline := time.Now().Add(10 * time.Second); time.Now().Before(deadline) && devToolsAlive(context.Background(), address); {
			time.Sleep(200 * time.Millisecond)
		}
		// The DevTools port closes before the process lets go of the profile; a
		// browser started on it sooner would hand itself to the exiting one.
		for deadline := time.Now().Add(5 * time.Second); time.Now().Before(deadline) && profileLocked(profile); {
			time.Sleep(200 * time.Millisecond)
		}
		once.Do(func() { close(done) })
	}}
}

// profileLocked tells whether a browser still holds the profile: Windows
// keeps a delete-on-close lockfile, macOS and Linux a SingletonLock link.
func profileLocked(profile string) bool {
	for _, name := range []string{"lockfile", "SingletonLock"} {
		if _, err := os.Lstat(filepath.Join(profile, name)); err == nil {
			return true
		}
	}
	return false
}

// browserHost owns the engine and the browser behind Capabilities.Browser. It
// outlives controller sessions so pages and logins survive reconnects.
type browserHost struct {
	agent *Agent
	dir   string
	slots chan struct{}

	mu             sync.Mutex
	engine         *browser.Engine
	launch         *launched
	epoch          string
	state          string // stopped, installing, starting, ready, failed
	source         string // system, download, endpoint
	product        string
	headless       bool
	err            string
	progress       int64
	starting       chan struct{}
	stopping       chan struct{}
	generation     uint64
	startCancel    context.CancelFunc
	downloadCancel context.CancelFunc
	downloading    chan struct{}
}

func (a *Agent) browserHost() *browserHost {
	a.browserOnce.Do(func() {
		dir := browserDataDir(a.ConfigPath)
		if a.ConfigPath == "" {
			dir = filepath.Join(os.TempDir(), "openbear-node-browser")
		}
		a.browser = &browserHost{agent: a, dir: dir, slots: make(chan struct{}, maxBrowserCalls), state: "stopped"}
	})
	return a.browser
}

func (h *browserHost) statusLocked() map[string]any {
	c := h.agent.config()
	out := map[string]any{"enabled": c.Capabilities.Browser, "state": h.state, "source": h.source, "product": h.product,
		"headless": h.headless, "epoch": h.epoch, "endpoint": c.Browser.Endpoint, "download": c.Browser.Download}
	if c.Browser.Endpoint != "" && !loopbackEndpoint(c.Browser.Endpoint) {
		// Files it saves are on its own machine; the node cannot hand them over.
		out["downloadsUnavailable"] = true
	}
	if h.err != "" {
		out["error"] = h.err
	}
	if h.state == "installing" {
		out["downloaded"] = h.progress
	}
	return out
}

func (h *browserHost) status() map[string]any {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.statusLocked()
}

// ensure starts or attaches the browser when needed. A browser still being
// downloaded is reported, not waited for: the download can take minutes.
func (h *browserHost) ensure(ctx context.Context) (map[string]any, error) {
	for {
		h.mu.Lock()
		c := h.agent.config()
		if !c.Capabilities.Browser {
			defer h.mu.Unlock()
			return h.statusLocked(), nil
		}
		if wait := h.stopping; wait != nil {
			h.mu.Unlock()
			select {
			case <-wait:
				continue
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}
		if h.state == "ready" && h.engine != nil && !h.engine.Connected() {
			// An attached browser that went away (closed window, restarted
			// endpoint) is attached again rather than reported ready.
			engine := h.engine
			h.mu.Unlock()
			h.drop(engine, "connection to the browser was lost")
			continue
		}
		if h.state == "ready" && h.engine != nil || h.state == "installing" {
			defer h.mu.Unlock()
			return h.statusLocked(), nil
		}
		if wait := h.starting; wait != nil {
			h.mu.Unlock()
			select {
			case <-wait:
				continue
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}
		if wait := h.downloading; wait != nil {
			h.mu.Unlock()
			select {
			case <-wait:
				continue
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}
		h.starting = make(chan struct{})
		generation := h.generation
		startCtx, cancel := context.WithCancel(ctx)
		h.startCancel = cancel
		h.state, h.err = "starting", ""
		h.mu.Unlock()
		err := h.start(startCtx, c, generation)
		cancel()
		h.mu.Lock()
		close(h.starting)
		h.starting, h.startCancel = nil, nil
		if err != nil && generation == h.generation && h.state != "installing" {
			h.state, h.err = "failed", err.Error()
		}
		defer h.mu.Unlock()
		return h.statusLocked(), nil
	}
}

func (h *browserHost) start(ctx context.Context, c Config, generation uint64) error {
	// Passable but not listable: a browser account may need to reach its profile inside.
	if err := os.MkdirAll(h.dir, 0o711); err != nil {
		return err
	}
	// Files of an earlier engine: its download records and transfers are gone.
	pruneOld(filepath.Join(h.dir, "uploads"), 0)
	pruneOld(filepath.Join(h.dir, "artifacts"), 0)
	pruneOld(filepath.Join(h.dir, "downloads"), 0)
	engine := browser.New()
	var l *launched
	init := map[string]any{"connectTimeoutS": 15, "snapshotMaxChars": 12000, "maxEventEntries": 200,
		"maxBodyBytes": 2 << 20, "maxArtifactBytes": 32 << 20}
	source := "endpoint"
	if c.Browser.Endpoint != "" {
		address, err := devtoolsAddress(ctx, c.Browser.Endpoint)
		if err != nil {
			return err
		}
		downloads, err := endpointDownloads(address, h.dir)
		if err != nil {
			return err
		}
		init["endpoint"], init["downloadHostPath"], init["downloadBrowserPath"] = address, downloads, downloads
	} else {
		exe, found, err := h.executable(c.Browser)
		if err != nil {
			return err
		}
		if exe == "" {
			h.download(generation)
			return errors.New("browser_installing")
		}
		source = found
		if l, err = launchBrowser(ctx, exe, h.dir); err != nil {
			return err
		}
		if l.transport != nil {
			engine.UseTransport(l.transport)
		}
		init["endpoint"], init["downloadHostPath"], init["downloadBrowserPath"] = l.endpoint, l.downloads, l.downloads
	}
	raw, _ := json.Marshal(init)
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	if l != nil && l.transport != nil {
		stop := context.AfterFunc(ctx, func() { l.transport.Close() })
		defer stop()
	}
	if _, err := engine.Execute(ctx, "initialize", raw); err != nil {
		closeBrowserEngine(engine, l)
		if l != nil {
			// What the browser printed, such as a sandbox or library failure.
			if l.output != nil {
				if tail := l.output(); tail != "" {
					return fmt.Errorf("%v; browser output: %s", browserFailure(err), tail)
				}
			}
		}
		return browserFailure(err)
	}
	product := ""
	if version, err := engine.Call(ctx, "Browser.getVersion", json.RawMessage(`{}`)); err == nil {
		var v struct{ Product string }
		json.Unmarshal(version, &v)
		product = v.Product
	}
	h.mu.Lock()
	if generation != h.generation || ctx.Err() != nil {
		h.mu.Unlock()
		closeBrowserEngine(engine, l)
		return context.Canceled
	}
	h.engine, h.launch, h.epoch = engine, l, randomHex(8)
	h.state, h.source, h.product, h.headless, h.err = "ready", source, product, l != nil && l.headless, ""
	h.mu.Unlock()
	if l != nil {
		go func() {
			<-l.done
			h.drop(engine, "browser exited")
		}()
	}
	h.agent.Logger.Printf("browser ready: %s (%s)", product, source)
	return nil
}

// drop forgets an engine whose browser went away; the next call starts again.
func (h *browserHost) drop(engine *browser.Engine, why string) {
	h.mu.Lock()
	if h.engine != engine {
		h.mu.Unlock()
		return
	}
	l := h.launch
	h.engine, h.launch, h.state, h.err = nil, nil, "stopped", why
	h.stopping = make(chan struct{})
	h.mu.Unlock()
	h.finishStop(engine, l)
}

func (h *browserHost) finishStop(engine *browser.Engine, l *launched) {
	closeBrowserEngine(engine, l)
	h.mu.Lock()
	close(h.stopping)
	h.stopping = nil
	h.mu.Unlock()
}

func closeBrowserEngine(engine *browser.Engine, l *launched) {
	if l != nil {
		// A clean exit gets a chance; closing our pipe also unblocks a stuck send.
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		done := make(chan struct{})
		go func() {
			engine.Call(ctx, "Browser.close", json.RawMessage(`{}`))
			close(done)
		}()
		select {
		case <-done:
		case <-ctx.Done():
		}
		cancel()
		if l.transport != nil {
			l.transport.Close()
		}
		<-done
	}
	engine.Close()
	if l != nil {
		l.stop()
	}
}

func (h *browserHost) reused() bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.launch != nil && h.launch.reused
}

func (h *browserHost) stop() {
	h.mu.Lock()
	h.generation++
	if h.startCancel != nil {
		h.startCancel()
	}
	if h.downloadCancel != nil {
		h.downloadCancel()
	}
	h.state, h.err = "stopped", ""
	if wait := h.stopping; wait != nil {
		h.mu.Unlock()
		<-wait
		return
	}
	engine, l := h.engine, h.launch
	h.engine, h.launch = nil, nil
	if engine != nil {
		h.stopping = make(chan struct{})
	}
	h.mu.Unlock()
	if engine != nil {
		h.finishStop(engine, l)
	}
}

func (h *browserHost) current() (*browser.Engine, string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.engine, h.epoch
}

// executable picks the browser to start; "" with no error means download one.
// An installed browser the node cannot run is reported rather than silently
// replaced: the machine's owner chooses a download or an existing browser.
func (h *browserHost) executable(c BrowserConfig) (string, string, error) {
	if c.Executable != "" {
		if _, err := os.Stat(c.Executable); err != nil {
			return "", "", errors.New("configured browser executable not found")
		}
		return c.Executable, "system", nil
	}
	candidates := browserCandidates()
	if !c.Download {
		for _, exe := range candidates {
			if browserCanRun(exe) {
				return exe, "system", nil
			}
		}
	}
	if exe := downloadedBrowser(h.dir); exe != "" {
		// Reported, not downloaded again: installing the libraries fixes it.
		if err := checkLibraries(exe); err != nil {
			return "", "", err
		}
		return exe, "download", nil
	}
	if len(candidates) > 0 && !c.Download {
		// The real file: a launcher link elsewhere would point at the wrong folder.
		exe := candidates[0]
		if real, err := filepath.EvalSymlinks(exe); err == nil {
			exe = real
		}
		return "", "", errors.New("browser_not_runnable: " + exe)
	}
	return "", "", nil
}

func (h *browserHost) download(generation uint64) {
	h.mu.Lock()
	if generation != h.generation || h.downloading != nil {
		h.mu.Unlock()
		return
	}
	h.state, h.progress, h.err = "installing", 0, ""
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
	h.downloadCancel, h.downloading = cancel, make(chan struct{})
	h.mu.Unlock()
	go func() {
		defer cancel()
		err := downloadChrome(ctx, h.dir, func(n int64) {
			h.mu.Lock()
			if generation == h.generation {
				h.progress = n
			}
			h.mu.Unlock()
		})
		h.mu.Lock()
		close(h.downloading)
		h.downloading, h.downloadCancel = nil, nil
		if generation != h.generation {
			h.mu.Unlock()
			return
		}
		if err != nil {
			h.state, h.err = "failed", "download: "+err.Error()
		} else {
			h.state = "stopped"
		}
		h.mu.Unlock()
		if err != nil {
			h.agent.Logger.Printf("browser download failed: %v", err)
		}
	}()
}

func (h *browserHost) acquire() error {
	select {
	case h.slots <- struct{}{}:
		return nil
	default:
		return errors.New("node_busy")
	}
}

// call runs one engine action. Engine failures are results, not call errors,
// so the controller sees the same error, outcome and details as locally.
func (h *browserHost) call(ctx context.Context, raw json.RawMessage) (any, error) {
	var p struct {
		Action string         `json:"action"`
		Params map[string]any `json:"params"`
	}
	if json.Unmarshal(raw, &p) != nil || p.Action == "" || p.Action == "initialize" {
		return nil, errors.New("invalid_params")
	}
	if err := h.acquire(); err != nil {
		return nil, err
	}
	defer func() { <-h.slots }()
	engine, _ := h.current()
	if engine == nil {
		return nil, errors.New("browser_not_started")
	}
	if p.Params == nil {
		p.Params = map[string]any{}
	}
	if p.Params["outputPath"] == "*" {
		dir := filepath.Join(h.dir, "artifacts")
		if err := os.MkdirAll(dir, 0o700); err != nil {
			return nil, err
		}
		pruneOld(dir, artifactTTL)
		p.Params["outputPath"] = filepath.Join(dir, randomHex(12))
	} else if _, ok := p.Params["outputPath"]; ok {
		return nil, errors.New("invalid_params")
	}
	if paths, ok := p.Params["paths"].([]any); ok {
		for _, v := range paths {
			if s, _ := v.(string); !h.within(filepath.Join(h.dir, "uploads"), s) {
				return nil, errors.New("upload_path_outside_node")
			}
		}
	}
	params, _ := json.Marshal(p.Params)
	result, err := engine.Execute(ctx, p.Action, params)
	if err != nil {
		var be *browser.Error
		if !errors.As(err, &be) {
			be = &browser.Error{Code: "browser_operation_failed", Outcome: "unknown"}
		}
		if be.Code == "browser_connection_lost" {
			go h.drop(engine, "connection to the browser was lost")
		}
		return map[string]any{"error": be.Code, "outcome": be.Outcome, "details": be.Details}, nil
	}
	// Like the local worker's frame limit: an oversized reply is an error,
	// not a message that would end the whole node session.
	body, err := json.Marshal(map[string]any{"result": result})
	if err != nil || len(body) > maxBrowserReply {
		return map[string]any{"error": "worker_response_too_large", "outcome": "unknown", "details": map[string]any{}}, nil
	}
	return json.RawMessage(body), nil
}

func (h *browserHost) cdp(ctx context.Context, raw json.RawMessage) (any, error) {
	var p struct {
		Method string          `json:"method"`
		Params json.RawMessage `json:"params"`
		Target string          `json:"target"`
	}
	if json.Unmarshal(raw, &p) != nil {
		return nil, errors.New("invalid_params")
	}
	if p.Target == "" && !browserCDPMethods[p.Method] || p.Target != "" && !browserTargetMethods[p.Method] {
		return nil, errors.New("method_not_forwarded")
	}
	if len(p.Params) == 0 {
		p.Params = json.RawMessage(`{}`)
	}
	if err := h.acquire(); err != nil {
		return nil, err
	}
	defer func() { <-h.slots }()
	engine, epoch := h.current()
	if engine == nil {
		return nil, errors.New("browser_not_started")
	}
	var result json.RawMessage
	var err error
	if p.Target != "" {
		result, err = engine.TargetCall(ctx, p.Target, p.Method, p.Params)
	} else {
		result, err = engine.Call(ctx, p.Method, p.Params)
	}
	if err != nil {
		code := cdpError(err)
		if code == "browser_connection_lost" {
			go h.drop(engine, "connection to the browser was lost")
		}
		return map[string]any{"error": code, "epoch": epoch}, nil
	}
	return map[string]any{"result": result, "epoch": epoch}, nil
}

// artifact hands back a file the engine wrote, in chunks; the last read removes it.
func (h *browserHost) artifact(raw json.RawMessage) (any, error) {
	var p struct {
		Path   string `json:"path"`
		Offset int64  `json:"offset"`
		Limit  int    `json:"limit"`
	}
	if json.Unmarshal(raw, &p) != nil || p.Offset < 0 || !h.within(filepath.Join(h.dir, "artifacts"), p.Path) {
		return nil, errors.New("invalid_params")
	}
	limit := min(max(p.Limit, 1), browserChunk)
	f, err := os.Open(p.Path)
	if err != nil {
		return nil, errors.New("not_found")
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return nil, err
	}
	buf := make([]byte, limit)
	n, err := f.ReadAt(buf, p.Offset)
	if err != nil && !errors.Is(err, io.EOF) {
		return nil, err
	}
	eof := p.Offset+int64(n) >= info.Size()
	if eof {
		f.Close()
		os.Remove(p.Path)
	}
	return map[string]any{"data": base64.StdEncoding.EncodeToString(buf[:n]), "eof": eof, "size": info.Size()}, nil
}

// upload stores a file the controller sends for a page's file chooser.
func (h *browserHost) upload(raw json.RawMessage) (any, error) {
	var p struct {
		Upload string `json:"upload"`
		Name   string `json:"name"`
		Offset int64  `json:"offset"`
		Data   string `json:"data"`
		Done   bool   `json:"done"`
	}
	if json.Unmarshal(raw, &p) != nil || p.Offset < 0 || !validUploadID(p.Upload) {
		return nil, errors.New("invalid_params")
	}
	name := filepath.Base(strings.ReplaceAll(p.Name, "\\", "/"))
	if name == "." || name == "/" || name == "" || len(name) > 255 {
		return nil, errors.New("invalid_params")
	}
	data, err := base64.StdEncoding.DecodeString(p.Data)
	if err != nil || len(data) > browserChunk {
		return nil, errors.New("invalid_params")
	}
	root := filepath.Join(h.dir, "uploads")
	if p.Offset == 0 {
		pruneOld(root, uploadTTL)
	}
	dir := filepath.Join(root, p.Upload)
	if err = os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	path := filepath.Join(dir, name)
	flags := os.O_WRONLY | os.O_CREATE
	if p.Offset == 0 {
		flags |= os.O_TRUNC
	}
	f, err := os.OpenFile(path, flags, 0o600)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	if info, _ := f.Stat(); info == nil || info.Size() != p.Offset {
		return nil, errors.New("upload_offset_mismatch")
	}
	if _, err = f.WriteAt(data, p.Offset); err != nil {
		return nil, err
	}
	if p.Done {
		return map[string]any{"path": path}, nil
	}
	return map[string]any{}, nil
}

// pruneOld removes what root holds that is older than ttl (everything for 0).
func pruneOld(root string, ttl time.Duration) {
	entries, _ := os.ReadDir(root)
	for _, e := range entries {
		if info, err := e.Info(); err == nil && time.Since(info.ModTime()) >= ttl {
			os.RemoveAll(filepath.Join(root, e.Name()))
		}
	}
}

func (h *browserHost) within(root, path string) bool {
	rel, err := filepath.Rel(root, filepath.Clean(path))
	return err == nil && rel != "." && !strings.HasPrefix(rel, "..") && !filepath.IsAbs(rel)
}

// configure turns the browser on or off and sets its endpoint. Only nodes that
// run commands accept it: those already give the controller full control.
func (a *Agent) configureBrowser(raw json.RawMessage) (map[string]any, error) {
	var p struct {
		Enabled  bool   `json:"enabled"`
		Endpoint string `json:"endpoint"`
		Download bool   `json:"download"`
	}
	if json.Unmarshal(raw, &p) != nil || len(p.Endpoint) > 300 {
		return nil, errors.New("invalid_params")
	}
	if p.Endpoint != "" {
		if u, err := url.Parse(p.Endpoint); err != nil || u.Host == "" || !map[string]bool{"ws": true, "wss": true, "http": true, "https": true}[u.Scheme] {
			return nil, errors.New("invalid_browser_endpoint")
		}
	}
	a.mu.Lock()
	c := a.Config
	c.Capabilities.Browser, c.Browser.Endpoint, c.Browser.Download = p.Enabled, p.Endpoint, p.Download
	if a.ConfigPath != "" {
		if err := SaveConfig(a.ConfigPath, c); err != nil {
			a.mu.Unlock()
			return nil, errors.New("config_write_failed")
		}
	}
	changed := a.Config.Browser != c.Browser || !p.Enabled
	a.Config = c
	a.mu.Unlock()
	h := a.browserHost()
	if changed {
		h.stop()
		// A failure under the old settings no longer applies.
		h.mu.Lock()
		if h.state == "failed" {
			h.state, h.err = "stopped", ""
		}
		h.mu.Unlock()
	}
	return h.status(), nil
}

// devToolsAlive tells whether a browser still answers at a ws:// DevTools address.
func devToolsAlive(ctx context.Context, address string) bool {
	u, err := url.Parse(address)
	if err != nil {
		return false
	}
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, "http://"+u.Host+"/json/version", nil)
	resp, err := (&http.Client{Transport: &http.Transport{Proxy: nil}}).Do(req)
	if err != nil {
		return false
	}
	resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

// endpointDownloads is where a browser reached at address saves files. One on this machine
// runs as another account, which cannot write below the node's own directory: it gets a
// directory every account can write (sticky, like /tmp; Windows: below the Public folder).
func endpointDownloads(address, dir string) (string, error) {
	if !loopbackEndpoint(address) {
		downloads := filepath.Join(dir, "downloads")
		return downloads, os.MkdirAll(downloads, 0o700)
	}
	downloads := sharedDownloads()
	return downloads, prepareSharedDownloads(downloads)
}

func loopbackEndpoint(raw string) bool {
	u, err := url.Parse(raw)
	if err != nil {
		return false
	}
	host := u.Hostname()
	if host == "localhost" {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func devtoolsAddress(ctx context.Context, raw string) (string, error) {
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" {
		return "", errors.New("invalid_browser_endpoint")
	}
	if u.Scheme == "ws" || u.Scheme == "wss" {
		return raw, nil
	}
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	u.Path = strings.TrimRight(u.Path, "/") + "/json/version"
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	resp, err := (&http.Client{Transport: &http.Transport{Proxy: nil}}).Do(req)
	if err != nil {
		return "", fmt.Errorf("browser endpoint unreachable: %w", err)
	}
	defer resp.Body.Close()
	var v struct {
		WebSocketDebuggerURL string `json:"webSocketDebuggerUrl"`
	}
	if resp.StatusCode != http.StatusOK || json.NewDecoder(io.LimitReader(resp.Body, 65536)).Decode(&v) != nil || v.WebSocketDebuggerURL == "" {
		return "", errors.New("browser endpoint is not a DevTools address")
	}
	return v.WebSocketDebuggerURL, nil
}

func browserFailure(err error) error {
	var be *browser.Error
	if errors.As(err, &be) {
		return errors.New(be.Code)
	}
	return err
}

// cdpError names a raw CDP failure as the controller's own CDP client does.
func cdpError(err error) string {
	var be *browser.Error
	var ce *browser.CommandError
	switch {
	case errors.As(err, &be):
		return be.Code
	case errors.Is(err, browser.ErrDisconnected):
		return "browser_connection_lost"
	case errors.As(err, &ce):
		return ce.Error()
	case errors.Is(err, context.DeadlineExceeded):
		return "cdp_timeout"
	}
	return "cdp_call_failed"
}

func validUploadID(s string) bool {
	if len(s) < 8 || len(s) > 64 {
		return false
	}
	for _, r := range s {
		if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '-' || r == '_') {
			return false
		}
	}
	return true
}
