// Package browser is the Go port of app/browser/pyworker: a CDP browser
// engine that executes OpenBear browser actions with the same parameters,
// results, error codes, outcomes and details as the Python engine.
package browser

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// Engine owns one CDP connection and the pages it has bound. Execute,
// Call and TargetCall may be called concurrently.
type Engine struct {
	mu        sync.Mutex // the loop lock: see loop.go
	config    *Obj
	cdp       *conn
	cdpRef    atomic.Pointer[conn]
	transport Transport
	pages     *omap[string, *Page]
	sessions  map[string]*Page
	pageLocks map[string]*aLock
	tasks     map[*task]struct{}
	frameSeq  int
	netSeq    int
	files     *files
	bg        context.Context
	bgCancel  context.CancelFunc
	wg        sync.WaitGroup
	closing   bool
}

// New returns an uninitialized engine; the first action must be initialize.
func New() *Engine {
	bg, cancel := context.WithCancel(context.Background())
	e := &Engine{
		config: newObj(), pages: newOmap[string, *Page](), sessions: map[string]*Page{},
		pageLocks: map[string]*aLock{}, tasks: map[*task]struct{}{}, bg: bg, bgCancel: cancel,
	}
	e.files = &files{e: e, byGUID: map[string]*download{}}
	return e
}

// UseTransport makes initialize use t instead of dialing its endpoint
// (for example a --remote-debugging-pipe connection). Call before initialize.
func (e *Engine) UseTransport(t Transport) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.transport = t
}

func (e *Engine) nextFrameSeq() int {
	e.frameSeq++
	return e.frameSeq
}

func (e *Engine) nextNetworkStamp() int {
	e.netSeq++
	return e.netSeq
}

// ------------------------------------------------------------ CDP helpers

// call sends a command with the operation's remaining budget (or the
// connection default outside an operation), releasing the loop lock.
func (e *Engine) call(ctx context.Context, method string, params any, session string) (*Obj, error) {
	c := e.cdp
	if c == nil || !c.connected() {
		return nil, ErrDisconnected
	}
	d := c.timeout
	if op := opOf(ctx); op != nil {
		r, err := op.remaining(ctx)
		if err != nil {
			return nil, err
		}
		d = r
	}
	var res *Obj
	var err error
	e.block(func() { res, err = c.call(ctx, method, params, session, d) })
	return res, err
}

// callTimeout is a call with its own short budget, also usable for cleanup
// after the operation deadline has passed.
func (e *Engine) callTimeout(ctx context.Context, method string, params any, session string, d time.Duration) (*Obj, error) {
	c := e.cdp
	if c == nil || !c.connected() {
		return nil, ErrDisconnected
	}
	ctx = context.WithoutCancel(ctx)
	var res *Obj
	var err error
	e.block(func() { res, err = c.call(ctx, method, params, session, d) })
	return res, err
}

// pendingCall is a command running as its own task (asyncio.create_task).
type pendingCall struct {
	ch     chan reply
	cancel context.CancelFunc
	done   chan struct{}
}

func (e *Engine) start(ctx context.Context, method string, params any, session string) *pendingCall {
	pc := &pendingCall{ch: make(chan reply, 1), done: make(chan struct{})}
	cctx, cancel := context.WithCancel(ctx)
	pc.cancel = cancel
	c := e.cdp
	var d time.Duration
	var early error
	switch {
	case c == nil || !c.connected():
		early = ErrDisconnected
	default:
		d = c.timeout
		if op := opOf(ctx); op != nil {
			r, err := op.remaining(ctx)
			if err != nil {
				early = err
			}
			d = r
		}
	}
	go func() {
		defer close(pc.done)
		if early != nil {
			pc.ch <- reply{err: early}
			return
		}
		res, err := c.call(cctx, method, params, session, d)
		pc.ch <- reply{result: res, err: err}
	}()
	return pc
}

// stop cancels local waiting for the command and waits for its task.
func (pc *pendingCall) stop(e *Engine) {
	pc.cancel()
	e.block(func() { <-pc.done })
}

type callSpec struct {
	method string
	params any
}

// gather runs commands concurrently; the first failure is returned at once
// and the remaining commands continue (asyncio.gather).
func (e *Engine) gather(ctx context.Context, session string, calls []callSpec) error {
	pending := make([]*pendingCall, len(calls))
	for i, spec := range calls {
		pending[i] = e.start(ctx, spec.method, spec.params, session)
	}
	var first error
	e.block(func() {
		for _, pc := range pending {
			if r := <-pc.ch; r.err != nil {
				first = r.err
				return
			}
		}
	})
	return first
}

// ------------------------------------------------------------- lifecycle

func (e *Engine) onEvent(method string, data *Obj, session string) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if strings.HasPrefix(method, "Browser.download") {
		e.files.event(method, data)
	}
	if session != "" {
		if page := e.sessions[session]; page != nil {
			page.event(method, data, session)
		}
	}
	if method == "Target.targetDestroyed" {
		target := pyStr(data.Must("targetId"))
		if page, ok := e.pages.Get(target); ok {
			e.pages.Del(target)
			page.closed = true
			page.invalidateRefs()
			for sid, owner := range e.sessions {
				if owner == page {
					delete(e.sessions, sid)
				}
			}
			delete(e.pageLocks, page.target)
			for _, d := range page.downloads.Values() {
				e.files.forget(d)
			}
			e.spawn(func(ctx context.Context) error { page.close(ctx); return nil })
		}
	}
}

func (e *Engine) cfgNum(key string) float64 { return mustNum(e.config.Must(key)) }

func (e *Engine) initialize(ctx context.Context, config *Obj) (any, error) {
	e.config = config.Copy()
	if !truthy(e.config.Def("downloadHostPath", nil)) {
		dir, err := os.MkdirTemp("", "openbear-browser-downloads-")
		if err != nil {
			return nil, err
		}
		e.config.Set("downloadHostPath", dir)
	}
	if !e.config.Has("downloadBrowserPath") {
		e.config.Set("downloadBrowserPath", e.config.Must("downloadHostPath"))
	}
	if err := mkdirAll(asStr(e.config.Must("downloadHostPath"))); err != nil {
		return nil, err
	}
	e.files.prune()
	// Fixed hard ceiling accommodates the public maximum artifact size after
	// base64. Actual artifact/body limits are enforced separately.
	transport := e.transport
	var endpoint string
	if transport == nil || truthy(config.Def("endpoint", nil)) {
		endpoint = asStr(config.Must("endpoint"))
		transport = nil
	}
	seconds := 10.0
	if transport == nil || config.Has("connectTimeoutS") {
		seconds = mustNum(config.Must("connectTimeoutS"))
	}
	timeout := time.Duration(seconds * float64(time.Second))
	if old := e.cdp; old != nil && (transport == nil || old.t != transport) {
		e.cdp = nil
		e.block(old.close)
	}
	if e.cdp == nil {
		if transport == nil {
			var err error
			e.block(func() {
				cctx, cancel := context.WithTimeout(ctx, timeout)
				defer cancel()
				var url string
				if url, err = resolveEndpoint(cctx, endpoint); err == nil {
					transport, err = dialWebSocket(cctx, url, maxMessage)
				}
			})
			if err != nil {
				return nil, err
			}
		}
		e.cdp = newConn(transport, timeout, e.onEvent)
		e.cdpRef.Store(e.cdp)
	}
	if _, err := e.call(ctx, "Browser.getVersion", nil, ""); err != nil {
		return nil, err
	}
	if _, err := e.call(ctx, "Browser.setDownloadBehavior", O(
		"behavior", "allowAndName",
		"downloadPath", e.config.Must("downloadBrowserPath"),
		"eventsEnabled", true,
	), ""); err != nil {
		return nil, err
	}
	// Discovery events do not initialize/auto-attach unrelated pages.
	if _, err := e.call(ctx, "Target.setDiscoverTargets", O("discover", true), ""); err != nil {
		return nil, err
	}
	return O("connected", true, "engine", "go-cdp"), nil
}

func (e *Engine) page(ctx context.Context, target string) (page *Page, err error) {
	lock := e.pageLocks[target]
	if lock == nil {
		lock = newALock()
		e.pageLocks[target] = lock
	}
	if err := e.acquire(ctx, lock); err != nil {
		return nil, err
	}
	defer lock.release()
	if p, ok := e.pages.Get(target); ok {
		if p.closed {
			return nil, browserError(ctx, "page_not_bound", "", nil)
		}
		return p, nil
	}
	p := newPage(e, target)
	e.pages.Set(target, p)
	cleanup := func() {
		e.pages.Del(target)
		for sid, owner := range e.sessions {
			if owner == p {
				delete(e.sessions, sid)
			}
		}
		p.close(ctx)
	}
	defer func() {
		if r := recover(); r != nil {
			cleanup()
			panic(r)
		}
	}()
	if err := p.bind(ctx); err != nil {
		cleanup()
		return nil, err
	}
	return p, nil
}

// artifact writes an owned output file (0600) within maxArtifactBytes.
func (e *Engine) artifact(ctx context.Context, filename string, body []byte) (any, error) {
	if float64(len(body)) > e.cfgNum("maxArtifactBytes") {
		return nil, browserError(ctx, "artifact_too_large", "completed", nil)
	}
	f, err := os.OpenFile(filename, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o600)
	if err != nil {
		return nil, err
	}
	_, werr := f.Write(body)
	cerr := f.Close()
	if werr != nil {
		return nil, werr
	}
	if cerr != nil {
		return nil, cerr
	}
	return O("artifact", filename, "bytes", len(body)), nil
}

func (e *Engine) capture(ctx context.Context, page *Page, params *Obj) (result any, err error) {
	var node *Node
	defer func() {
		if node != nil {
			node.release(ctx)
		}
	}()
	if truthy(params.Def("target", nil)) {
		node, _, err = (&actions{page}).actionable(ctx, asStr(params.Must("target")), []string{"visible", "stable"}, true)
		if err != nil {
			return nil, err
		}
	}
	main, err := page.main()
	if err != nil {
		return nil, err
	}
	if _, err = main.run(ctx, "async function(){if(this.document.fonts)await this.document.fonts.ready}"); err != nil {
		return nil, err
	}
	metrics, err := e.call(ctx, "Page.getLayoutMetrics", nil, page.session)
	if err != nil {
		return nil, err
	}
	layout := asObj(metrics.Def("cssLayoutViewport", metrics.Must("layoutViewport")))
	visual := asObj(metrics.Def("cssVisualViewport", metrics.Must("visualViewport")))
	if main, err = page.main(); err != nil {
		return nil, err
	}
	dprValue, err := main.run(ctx, "function(){return this.window.devicePixelRatio}")
	if err != nil {
		return nil, err
	}
	if main, err = page.main(); err != nil {
		return nil, err
	}
	width, height, err := main.viewport(ctx)
	if err != nil {
		return nil, err
	}
	dpr := mustNum(dprValue)
	clip := O("x", visual.Must("pageX"), "y", visual.Must("pageY"), "width", pyNum(width), "height", pyNum(height), "scale", 1/dpr)
	if node != nil {
		box, err := e.call(ctx, "DOM.getBoxModel", O("objectId", node.objectID), node.frame.session)
		if err != nil {
			return nil, err
		}
		border := floats(asObj(box.Must("model")).Must("border"))
		root, err := page.sessionRoot(node.frame.session)
		if err != nil {
			return nil, err
		}
		var points []point
		for i := 0; i < 8; i += 2 {
			p, err := root.toMain(ctx, point{border[i], border[i+1]})
			if err != nil {
				return nil, err
			}
			points = append(points, p)
		}
		left, top, right, bottom := points[0].X, points[0].Y, points[0].X, points[0].Y
		for _, p := range points[1:] {
			left, top = min(left, p.X), min(top, p.Y)
			right, bottom = max(right, p.X), max(bottom, p.Y)
		}
		clip.Set("x", left+mustNum(layout.Must("pageX")))
		clip.Set("y", top+mustNum(layout.Must("pageY")))
		clip.Set("width", right-left)
		clip.Set("height", bottom-top)
	} else if truthy(params.Def("fullPage", nil)) {
		content := asObj(metrics.Def("cssContentSize", metrics.Must("contentSize")))
		clip.Set("x", 0)
		clip.Set("y", 0)
		clip.Set("width", content.Must("width"))
		clip.Set("height", content.Must("height"))
	}
	shot, err := e.call(withArtifact(ctx), "Page.captureScreenshot", O(
		"format", "png", "fromSurface", true, "captureBeyondViewport", true, "clip", clip,
	), page.session)
	if err != nil {
		return nil, err
	}
	data, err := b64decode(asStr(shot.Must("data")))
	if err != nil {
		return nil, err
	}
	artifact, err := e.artifact(ctx, asStr(params.Must("outputPath")), data)
	if err != nil {
		return nil, err
	}
	out := artifact.(*Obj).Copy()
	out.Set("mime", "image/png")
	return out, nil
}

var functionSource = regexp.MustCompile(`^(?:async[\t\n\v\f\r \x{1c}-\x{1f}\x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}]+)?function`)

// isFunctionSource is re.match(r"^(?:async\s+)?function\b", source).
func isFunctionSource(source string) bool {
	loc := functionSource.FindStringIndex(source)
	if loc == nil {
		return false
	}
	rest := source[loc[1]:]
	for _, r := range rest {
		return !pyWord(r)
	}
	return true
}

func (e *Engine) evaluate(ctx context.Context, page *Page, params *Obj) (result any, err error) {
	var node, mainNode *Node
	defer func() {
		if mainNode != nil {
			mainNode.release(ctx)
		}
		if node != nil {
			node.release(ctx)
		}
	}()
	markEffect(ctx)
	// The value goes to an artifact when it outgrows snapshotMaxChars.
	ctx = withArtifact(ctx)
	var value any
	if truthy(params.Def("target", nil)) {
		if node, err = page.resolve(ctx, asStr(params.Must("target")), true); err != nil {
			return nil, err
		}
		description, err := e.call(ctx, "DOM.describeNode", O("objectId", node.objectID), node.frame.session)
		if err != nil {
			return nil, err
		}
		// Resolve in the element's default world, not our isolated helper world.
		remote, err := e.call(ctx, "DOM.resolveNode", O(
			"backendNodeId", asObj(description.Must("node")).Must("backendNodeId"),
		), node.frame.session)
		if err != nil {
			return nil, err
		}
		mainNode = &Node{node.frame, asStr(asObj(remote.Must("object")).Must("objectId")), node.frame.epoch}
		function := "function(){const value=(" + asStr(params.Must("expression")) +
			"\n);return typeof value==='function'?value(this):value}"
		if value, err = mainNode.call(ctx, function); err != nil {
			return nil, err
		}
	} else {
		source := pyStrip(asStr(params.Must("expression")))
		if isFunctionSource(source) {
			source = "(" + source + "\n)"
		}
		expression := "(()=>{let value=(0,eval)(" + dumpsASCII(source) +
			");return typeof value==='function'?value():value})()"
		res, err := e.call(ctx, "Runtime.evaluate", O(
			"expression", expression,
			"awaitPromise", true,
			"returnByValue", true,
			"userGesture", true,
			"allowUnsafeEvalBlockedByCSP", true,
		), page.session)
		if err != nil {
			return nil, err
		}
		if value, err = remoteResult(ctx, res, true); err != nil {
			return nil, err
		}
	}
	text, err := dumps(value)
	if err != nil {
		return nil, err
	}
	if float64(pyLen(string(text))) > e.cfgNum("snapshotMaxChars") {
		return e.artifact(ctx, asStr(params.Must("outputPath")), text)
	}
	return O("value", value), nil
}

// ------------------------------------------------------------- execution

var dialogActions = map[string]bool{"dialog": true, "console": true, "network": true}

func dialogPublic(dialog *Obj) *Obj {
	out := newObj()
	for _, k := range dialog.Keys() {
		if k != "session" {
			v, _ := dialog.Get(k)
			out.Set(k, v)
		}
	}
	return out
}

func (e *Engine) execute(ctx context.Context, action string, params *Obj) (any, error) {
	if action == "initialize" {
		return e.initialize(ctx, params)
	}
	if action == "configure" {
		for _, key := range []string{"snapshotMaxChars", "maxEventEntries", "maxBodyBytes", "maxArtifactBytes"} {
			e.config.Set(key, params.Must(key))
		}
		limit := int(mustNum(params.Must("maxEventEntries")))
		for _, page := range e.pages.Values() {
			page.obs.configure(limit)
		}
		return O("configured", true), nil
	}
	timeoutMs := mustNum(params.Def("timeoutMs", 20000))
	op := &operation{deadline: time.Now().Add(time.Duration(timeoutMs * float64(time.Millisecond))), phase: "initializing_page"}
	ctx = withOp(ctx, op)
	if _, err := op.remaining(ctx); err != nil {
		return nil, err
	}
	ctx, cancel := context.WithDeadline(ctx, op.deadline)
	defer cancel()
	var page *Page
	result, err := func() (any, error) {
		var err error
		page, err = e.page(ctx, asStr(params.Must("targetId")))
		if err != nil {
			return nil, err
		}
		setPhase(ctx, action)
		if page.obs.dialog != nil && !dialogActions[action] {
			return nil, browserError(ctx, "dialog_pending: inspect or handle the dialog", "", nil)
		}
		switch action {
		case "prepare":
			// Private service operation: bind only the newly owned page and prove
			// its helper execution context works before ready.
			main, err := page.main()
			if err != nil {
				return nil, err
			}
			state, err := main.run(ctx, "function(){return this.document.readyState}")
			if err != nil {
				return nil, err
			}
			if !pyEqual(state, "interactive") && !pyEqual(state, "complete") {
				return nil, browserError(ctx, "page_not_ready", "failed", nil)
			}
			return O("ready", true, "url", page.url()), nil
		case "navigate":
			return page.navigate(ctx, params)
		case "snapshot", "find":
			return snapshot(ctx, page, action, params)
		case "act":
			return (&actions{page}).act(ctx, params)
		case "wait":
			return (&actions{page}).wait(ctx, params)
		case "capture":
			return e.capture(ctx, page, params)
		case "evaluate":
			return e.evaluate(ctx, page, params)
		case "files":
			return e.files.execute(ctx, page, params)
		case "console":
			return page.obs.console.read(mustNum(params.Def("after", 0))), nil
		case "network":
			if truthy(params.Def("id", nil)) {
				return page.obs.details(ctx, params)
			}
			return page.obs.network.read(mustNum(params.Def("after", 0))), nil
		case "dialog":
			dialog := page.obs.dialog
			if dialog == nil {
				return O("dialog", nil), nil
			}
			if pyEqual(params.Must("op"), "inspect") {
				return O("dialog", dialogPublic(dialog)), nil
			}
			markEffect(ctx)
			if _, err := e.call(ctx, "Page.handleJavaScriptDialog", O(
				"accept", params.Must("accept"),
				"promptText", params.Def("text", ""),
			), asStr(dialog.Must("session"))); err != nil {
				return nil, err
			}
			if page.obs.dialog == dialog {
				page.obs.dialog = nil
			}
			page.obs.dialogOpened.Clear()
			inputs := page.deferredInputs
			page.deferredInputs = nil
			for _, in := range inputs {
				if _, err := e.call(ctx, in.method, in.params, page.session); err != nil {
					return nil, err
				}
			}
			guards := page.deferredGuards
			page.deferredGuards = nil
			if err := (&actions{page}).stopGuards(ctx, guards, false); err != nil {
				return nil, err
			}
			return O("handled", true), nil
		}
		return nil, browserError(ctx, "unknown_action", "", nil)
	}()
	if err == nil {
		return result, nil
	}
	switch {
	case errors.Is(err, errDialogOpened):
		dialog := page.obs.dialog
		var public any
		if dialog != nil {
			public = dialogPublic(dialog)
		}
		return O(
			"completed", false,
			"pendingDialog", true,
			"url", page.url(),
			"dialog", public,
			"downloads", page.downloadList(),
		), nil
	case isTimeout(err):
		details := map[string]any{}
		code := "action_timeout"
		if p, ok := e.pages.Get(pyStr(params.Def("targetId", nil))); ok && action == "navigate" && p.navigation != nil {
			details["navigation"] = p.navigationDetails()
			code = "navigation_timeout"
		}
		return nil, browserError(ctx, code, "", details)
	case errors.Is(err, ErrDisconnected):
		return nil, browserError(ctx, "browser_connection_lost", "", nil)
	}
	if _, ok := isProtocol(err); ok {
		return nil, browserError(ctx, "browser_protocol_error", "", nil)
	}
	return nil, err
}

func (p *Page) downloadList() []any {
	out := []any{}
	for _, d := range p.downloads.Values() {
		out = append(out, O("id", d.id, "name", d.name))
	}
	return out
}

// Execute runs one worker action (Python Engine.execute plus main.dispatch).
// Every failure is an *Error; unexpected failures are reported only as
// browser_operation_failed/unknown without source, endpoint or stack data.
func (e *Engine) Execute(ctx context.Context, action string, params json.RawMessage) (result any, err error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	defer func() {
		if r := recover(); r != nil {
			result, err = nil, &Error{Code: "browser_operation_failed", Outcome: "unknown"}
		}
	}()
	if e.config.Len() == 0 && action != "initialize" {
		return nil, &Error{Code: "worker_not_initialized", Outcome: "not_started", Details: map[string]any{}}
	}
	var p *Obj
	if parsed, perr := parseJSON(params); perr == nil {
		p, _ = parsed.(*Obj)
	}
	if p == nil {
		return nil, &Error{Code: "browser_operation_failed", Outcome: "unknown"}
	}
	result, err = e.execute(ctx, action, p)
	if err != nil {
		var be *Error
		if errors.As(err, &be) {
			return nil, be
		}
		if errors.Is(err, errCancelled) && ctx.Err() != nil {
			return nil, &Error{Code: "action_cancelled", Outcome: "unknown"}
		}
		return nil, &Error{Code: "browser_operation_failed", Outcome: "unknown"}
	}
	return result, nil
}

// ------------------------------------------------------------- raw CDP

func (e *Engine) rawConn() (*conn, error) {
	c := e.cdpRef.Load()
	if c == nil || !c.connected() {
		return nil, ErrDisconnected
	}
	return c, nil
}

// Connected tells whether the engine's browser connection is still open.
func (e *Engine) Connected() bool {
	_, err := e.rawConn()
	return err == nil
}

func rawParams(params json.RawMessage) (any, error) {
	if len(strings.TrimSpace(string(params))) == 0 {
		return newObj(), nil
	}
	v, err := parseJSON(params)
	if err != nil {
		return nil, err
	}
	if v == nil {
		return newObj(), nil
	}
	return v, nil
}

func publicErr(err error, ctx context.Context) error {
	if p, ok := isProtocol(err); ok {
		return &CommandError{Message: p.msg}
	}
	if errors.Is(err, errCancelled) && ctx.Err() != nil {
		return ctx.Err()
	}
	return err
}

func (e *Engine) rawCall(ctx context.Context, c *conn, method string, params any, session string, d time.Duration) (json.RawMessage, error) {
	res, err := c.call(ctx, method, params, session, d)
	if err != nil {
		return nil, publicErr(err, ctx)
	}
	body, err := dumps(res)
	if err != nil {
		return nil, err
	}
	return body, nil
}

// Call sends one raw browser-level CDP command on the engine's connection
// (app/browser/cdp.py call). Without a ctx deadline the connect timeout is used.
func (e *Engine) Call(ctx context.Context, method string, params json.RawMessage) (json.RawMessage, error) {
	c, err := e.rawConn()
	if err != nil {
		return nil, err
	}
	p, err := rawParams(params)
	if err != nil {
		return nil, err
	}
	d := c.timeout
	if deadline, ok := ctx.Deadline(); ok {
		d = time.Until(deadline)
	}
	return e.rawCall(ctx, c, method, p, "", d)
}

// TargetCall attaches a separate flattened session to targetID, runs one
// command there and detaches (cdp.py target_call). The engine's own page
// sessions are not touched. Without a ctx deadline the budget is 2s.
func (e *Engine) TargetCall(ctx context.Context, targetID, method string, params json.RawMessage) (json.RawMessage, error) {
	c, err := e.rawConn()
	if err != nil {
		return nil, err
	}
	p, err := rawParams(params)
	if err != nil {
		return nil, err
	}
	d := 2 * time.Second
	if deadline, ok := ctx.Deadline(); ok {
		d = time.Until(deadline)
	}
	ctx, cancel := context.WithTimeout(ctx, d)
	defer cancel()
	attached, err := c.call(ctx, "Target.attachToTarget", O("targetId", targetID, "flatten", true), "", d)
	if err != nil {
		return nil, publicErr(err, ctx)
	}
	session, _ := attached.Def("sessionId", nil).(string)
	if session == "" {
		return nil, &CommandError{Message: "attach_failed"}
	}
	defer func() {
		if c.connected() {
			c.call(context.Background(), "Target.detachFromTarget", O("sessionId", session), "", 250*time.Millisecond)
		}
	}()
	return e.rawCall(ctx, c, method, p, session, time.Until(deadlineOf(ctx)))
}

func deadlineOf(ctx context.Context) time.Time {
	d, _ := ctx.Deadline()
	return d
}

// Close cancels background work, detaches bound pages and closes the
// connection. The browser itself is never closed.
func (e *Engine) Close() error {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.closing {
		return nil
	}
	e.closing = true
	e.bgCancel()
	tasks := make([]*task, 0, len(e.tasks))
	for t := range e.tasks {
		tasks = append(tasks, t)
	}
	e.cancelAndWait(tasks)
	e.block(e.wg.Wait)
	if e.cdp != nil {
		ctx := context.Background()
		var wg sync.WaitGroup
		for _, page := range e.pages.Values() {
			wg.Add(1)
			e.spawnDirect(&wg, func() { page.close(ctx) })
		}
		e.block(wg.Wait)
		c := e.cdp
		e.block(c.close)
	} else if e.transport != nil {
		e.block(func() { e.transport.Close() })
	}
	return nil
}

// spawnDirect runs fn holding the loop lock in its own goroutine.
func (e *Engine) spawnDirect(wg *sync.WaitGroup, fn func()) {
	go func() {
		defer wg.Done()
		e.mu.Lock()
		defer e.mu.Unlock()
		defer func() { recover() }()
		fn()
	}()
}
