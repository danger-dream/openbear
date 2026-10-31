package browser

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"regexp"
	"strings"
	"time"
)

func transient(err error) bool {
	text := strings.ToLower(err.Error())
	for _, x := range []string{"context", "node", "frame_not_ready", "frame_detached", "document_changed", "cannot find", "target closed"} {
		if strings.Contains(text, x) {
			return true
		}
	}
	return false
}

func uuidHex() string {
	var b [16]byte
	rand.Read(b[:])
	b[6] = b[6]&0x0f | 0x40
	b[8] = b[8]&0x3f | 0x80
	return hex.EncodeToString(b[:])
}

type lifeKey struct{ frame, loader string }

type refEntry struct {
	frame string
	epoch int
}

type deferredInput struct {
	method string
	params *Obj
}

type download struct {
	id        int
	name      any
	guid      string
	state     any
	page      *Page
	received  any
	tooLarge  bool
	overQuota bool          // cancelled to keep the download directory within downloadQuota
	updated   time.Time     // last state change, for downloadTTL
	file      *fileIdentity // the file written for it, once seen
}

// Page is one bound target: control session, frames, refs and observation.
type Page struct {
	e                 *Engine
	target            string
	session           string
	mainID            string
	frames            map[string]*Frame
	sessionRoots      map[string]string
	setupTasks        *omap[string, *task]
	failedSessions    map[string]bool
	snapshot          string
	refs              map[string]refEntry
	life              *omap[lifeKey, map[string]bool]
	navigationStatus  *omap[lifeKey, any]
	navigation        *Obj
	navigationFailed  *event
	pendingNavigation bool
	navigationSerial  int
	closed            bool
	errorBinding      string
	obs               *Observations
	downloads         *omap[int, *download]
	downloadSeq       int
	dragData          any
	deferredGuards    []*Node
	deferredInputs    []deferredInput
	mouse             point
	modifiers         map[string]bool
	pressed           map[string]bool
	scripts           map[string]any
	observerSessions  *omap[string, string]
	bindingLocks      map[string]*aLock
	bindingTasks      map[string]*task
	media             *Obj
}

func newPage(e *Engine, target string) *Page {
	p := &Page{
		e: e, target: target, frames: map[string]*Frame{}, sessionRoots: map[string]string{},
		setupTasks: newOmap[string, *task](), failedSessions: map[string]bool{}, refs: map[string]refEntry{},
		life: newOmap[lifeKey, map[string]bool](), navigationStatus: newOmap[lifeKey, any](),
		navigationFailed: newEvent(), errorBinding: "__obError_" + uuidHex(),
		downloads: newOmap[int, *download](), modifiers: map[string]bool{}, pressed: map[string]bool{},
		scripts: map[string]any{}, observerSessions: newOmap[string, string](), bindingLocks: map[string]*aLock{},
		bindingTasks: map[string]*task{},
	}
	p.obs = newObservations(p)
	return p
}

func (p *Page) main() (*Frame, error) {
	f := p.frames[p.mainID]
	if f == nil {
		return nil, protoErr("frame_not_ready")
	}
	return f, nil
}

func (p *Page) url() any {
	if f := p.frames[p.mainID]; f != nil {
		return f.url
	}
	return ""
}

func (p *Page) invalidateRefs() {
	p.snapshot = ""
	p.refs = map[string]refEntry{}
}

func (p *Page) sessionRoot(session string) (*Frame, error) {
	root, ok := p.sessionRoots[session]
	if f := p.frames[root]; ok && f != nil {
		return f, nil
	}
	return nil, protoErr("frame_not_ready")
}

func (p *Page) updateFrame(data *Obj, sid string, parent *string) *Frame {
	fid := asStr(data.Must("id"))
	frame := p.frames[fid]
	if frame == nil {
		frame = newFrame(p, fid, "", "")
		p.frames[fid] = frame
	}
	frame.detached = false
	if parent != nil || truthy(data.Def("parentId", nil)) {
		if parent != nil {
			frame.parent = *parent
		} else {
			frame.parent = asStr(data.Must("parentId"))
		}
	}
	if frame.session != "" && frame.session != sid {
		frame.invalidate()
	}
	frame.session = sid
	if loader := data.Def("loaderId", nil); truthy(loader) && frame.loader != pyStr(loader) {
		frame.invalidate()
		frame.loader = pyStr(loader)
	}
	frame.url = data.Def("url", frame.url)
	return frame
}

func (p *Page) updateTree(tree *Obj, sid string, parent *string) {
	frame := p.updateFrame(asObj(tree.Must("frame")), sid, parent)
	id := frame.id
	for _, child := range asList(tree.Def("childFrames", []any{})) {
		p.updateTree(asObj(child), sid, &id)
	}
}

func (p *Page) bind(ctx context.Context) error {
	result, err := p.e.call(ctx, "Target.attachToTarget", O("targetId", p.target, "flatten", true), "")
	if err != nil {
		return err
	}
	p.session = asStr(result.Must("sessionId"))
	p.e.sessions[p.session] = p
	return p.setupSession(ctx, p.session, p.target, true)
}

func (p *Page) setupSession(ctx context.Context, sid, target string, main bool) (err error) {
	e := p.e
	defer func() {
		if r := recover(); r != nil {
			p.failedSessions[sid] = true
			panic(r)
		}
		if err != nil {
			p.failedSessions[sid] = true
		}
	}()
	if _, err = e.call(ctx, "Page.enable", nil, sid); err != nil {
		return err
	}
	treeResult, err := e.call(ctx, "Page.getFrameTree", nil, sid)
	if err != nil {
		return err
	}
	tree := asObj(treeResult.Must("frameTree"))
	root := asStr(asObj(tree.Must("frame")).Must("id"))
	p.sessionRoots[sid] = root
	if main {
		p.mainID = root
	}
	p.updateTree(tree, sid, nil)
	maxBody := mustNum(e.config.Must("maxBodyBytes"))
	if err = e.gather(ctx, sid, []callSpec{
		{"Page.setLifecycleEventsEnabled", O("enabled", true)},
		{"Runtime.enable", nil},
		{"Network.enable", O(
			"maxTotalBufferSize", pyNum(max(32*1024*1024, maxBody*2)),
			"maxResourceBufferSize", pyNum(maxBody+1024*1024),
		)},
		{"Page.setInterceptFileChooserDialog", O("enabled", true)},
	}); err != nil {
		return err
	}
	// Keep observation off the Runtime-enabled control session: CloakBrowser
	// suppresses some Runtime events there after navigation.
	attached, err := e.call(ctx, "Target.attachToTarget", O("targetId", target, "flatten", true), "")
	if err != nil {
		return err
	}
	observer := asStr(attached.Must("sessionId"))
	p.observerSessions.Set(sid, observer)
	e.sessions[observer] = p
	if _, err = e.call(ctx, "Console.enable", nil, observer); err != nil {
		return err
	}
	p.obs.consoleReady[observer] = true
	if _, err = e.call(ctx, "Runtime.addBinding", O("name", p.errorBinding), observer); err != nil {
		return err
	}
	name := dumpsASCII(p.errorBinding)
	source := "(()=>{const name=" + name + ";const key=name+'_installed';if(globalThis[key])return;" +
		"const pending=[];const flush=()=>{if(typeof globalThis[name]!=='function')return;" +
		"while(pending.length){try{globalThis[name](pending.shift())}catch{break}}};" +
		"globalThis[key]={flush};const send=text=>{pending.push(JSON.stringify({text:String(text).slice(0,8000)}));" +
		"if(pending.length>200)pending.shift();flush()};" +
		"addEventListener('error',e=>send(e.message));" +
		"addEventListener('unhandledrejection',e=>send(e.reason?.stack||e.reason));})()"
	script, err := e.call(ctx, "Page.addScriptToEvaluateOnNewDocument", O("source", source, "runImmediately", true), sid)
	if err != nil {
		return err
	}
	p.scripts[sid] = script.Must("identifier")
	// Only descendants of this owned page/session; never browser-root auto attach.
	_, err = e.call(ctx, "Target.setAutoAttach", O(
		"autoAttach", true,
		"waitForDebuggerOnStart", false,
		"flatten", true,
		"filter", []any{O("type", "iframe", "exclude", false), O("exclude", true)},
	), sid)
	return err
}

// pyNum keeps integral arithmetic results integers in outgoing JSON.
func pyNum(f float64) any {
	if f == float64(int64(f)) {
		return int64(f)
	}
	return f
}

func (p *Page) refreshBinding(ctx context.Context, sid string) error {
	observer, ok := p.observerSessions.Get(sid)
	if !ok || observer == "" {
		return nil
	}
	lock := p.bindingLocks[sid]
	if lock == nil {
		lock = newALock()
		p.bindingLocks[sid] = lock
	}
	if err := p.e.acquire(ctx, lock); err != nil {
		return err
	}
	defer lock.release()
	// Cloak can drop the injected binding function at navigation while retaining
	// the new-document listeners. Reinstall and flush their bounded early errors.
	if _, err := p.e.call(ctx, "Runtime.addBinding", O("name", p.errorBinding), observer); err != nil {
		return err
	}
	key := dumpsASCII(p.errorBinding + "_installed")
	expression := "(()=>{const visit=w=>{try{w[" + key + "]?.flush();for(const f of w.frames)visit(f)}catch{}};visit(globalThis)})()"
	_, err := p.e.call(ctx, "Runtime.evaluate", O("expression", expression), observer)
	return err
}

// event applies one CDP event (handlers only update state and spawn work).
func (p *Page) event(method string, data *Obj, sid string) {
	if p.closed {
		return
	}
	p.obs.event(method, data, sid)
	switch method {
	case "Target.attachedToTarget":
		info := asObj(data.Must("targetInfo"))
		if !pyEqual(info.Must("type"), "iframe") {
			return
		}
		child := asStr(data.Must("sessionId"))
		target := asStr(info.Must("targetId"))
		p.e.sessions[child] = p
		p.setupTasks.Set(child, p.e.spawn(func(ctx context.Context) error {
			return p.setupSession(ctx, child, target, false)
		}))
	case "Target.detachedFromTarget":
		child := asStr(data.Must("sessionId"))
		delete(p.e.sessions, child)
		delete(p.sessionRoots, child)
		for _, frame := range p.frames {
			if frame.session == child {
				frame.invalidate()
				frame.session = ""
			}
		}
	case "Page.frameAttached":
		fid := asStr(data.Must("frameId"))
		if f := p.frames[fid]; f == nil {
			p.frames[fid] = newFrame(p, fid, asStr(data.Must("parentFrameId")), sid)
		} else {
			f.parent = asStr(data.Must("parentFrameId"))
		}
	case "Page.frameNavigated":
		frame := p.updateFrame(asObj(data.Must("frame")), sid, nil)
		if frame.id == p.mainID {
			p.navigationSerial++
			if p.navigation != nil && navActive(p.navigation) {
				loader := p.navigation.Def("loaderId", nil)
				if !truthy(loader) || pyEqual(loader, frame.loader) {
					p.navigation.Set("state", "committed")
					p.navigation.Set("committed", true)
					p.navigation.Set("loaderId", frame.loader)
				}
			}
		}
	case "Page.navigatedWithinDocument":
		if frame := p.frames[pyStr(data.Must("frameId"))]; frame != nil {
			frame.url = data.Must("url")
			if frame.id == p.mainID {
				p.pendingNavigation = false
				p.navigationSerial++
			}
		}
	case "Page.frameDetached":
		if frame := p.frames[pyStr(data.Must("frameId"))]; frame != nil {
			frame.invalidate()
			if !pyEqual(data.Def("reason", nil), "swap") {
				removed := map[string]bool{frame.id: true}
				for {
					var descendants []string
					for _, f := range p.frames {
						if removed[f.parent] && !removed[f.id] {
							descendants = append(descendants, f.id)
						}
					}
					if len(descendants) == 0 {
						break
					}
					for _, id := range descendants {
						removed[id] = true
					}
				}
				for fid := range removed {
					descendant := p.frames[fid]
					if descendant != frame {
						descendant.invalidate()
					}
					descendant.detached = true
				}
			}
		}
	case "Page.frameRequestedNavigation":
		if pyEqual(data.Def("frameId", nil), p.mainID) && pyEqual(data.Def("disposition", nil), "currentTab") {
			p.pendingNavigation = true
		}
	case "Page.frameStartedLoading":
		if pyEqual(data.Def("frameId", nil), p.mainID) {
			p.pendingNavigation = true
		}
	case "Page.frameStoppedLoading":
		if pyEqual(data.Def("frameId", nil), p.mainID) {
			p.pendingNavigation = false
		}
	case "Page.lifecycleEvent":
		name := data.Must("name")
		if pyEqual(name, "DOMContentLoaded") && p.observerSessions.Has(sid) {
			p.bindingTasks[sid] = p.e.spawn(func(ctx context.Context) error { return p.refreshBinding(ctx, sid) })
		}
		key := lifeKey{pyStr(data.Must("frameId")), loaderOf(data.Must("loaderId"))}
		names, ok := p.life.Get(key)
		if !ok {
			names = map[string]bool{}
			p.life.Set(key, names)
		}
		names[pyStr(name)] = true
		if pyEqual(data.Must("frameId"), p.mainID) && (pyEqual(name, "DOMContentLoaded") || pyEqual(name, "load")) {
			p.pendingNavigation = false
		}
		for p.life.Len() > 100 {
			p.life.PopFirst()
		}
		for p.navigationStatus.Len() > 100 {
			p.navigationStatus.PopFirst()
		}
	case "Runtime.executionContextsCleared":
		for _, frame := range p.frames {
			if frame.session == sid {
				frame.invalidate()
			}
		}
	case "Runtime.executionContextDestroyed":
		id := data.Def("executionContextId", nil)
		for _, frame := range p.frames {
			if frame.session == sid && pyEqual(frame.context, id) {
				frame.invalidate()
			}
		}
	case "Input.dragIntercepted":
		p.dragData = data.Must("data")
	case "Inspector.targetCrashed":
		p.invalidateRefs()
	}
}

// resolve turns a css= selector or a versioned snapshot ref into a Node.
// With wait, a css= selector is retried until it matches.
func (p *Page) resolve(ctx context.Context, target string, wait bool) (*Node, error) {
	var selector string
	var frame *Frame
	expected := -1
	if strings.HasPrefix(target, "css=") {
		f, err := p.main()
		if err != nil {
			return nil, err
		}
		selector, frame = target, f
	} else {
		prefix := p.snapshot + ":"
		ref := strings.TrimPrefix(target, prefix)
		entry, ok := p.refs[ref]
		if p.snapshot == "" || !strings.HasPrefix(target, prefix) || !ok {
			return nil, browserError(ctx, "stale_ref: take a new snapshot", "", nil)
		}
		frame = p.frames[entry.frame]
		expected = entry.epoch
		if frame == nil || frame.epoch != expected || frame.detached {
			return nil, browserError(ctx, "stale_ref: take a new snapshot", "", nil)
		}
		selector = "aria-ref=" + ref
	}
	for {
		node, err := func() (*Node, error) {
			node, err := frame.query(ctx, selector)
			if err != nil {
				return nil, err
			}
			if node != nil {
				connected, err := node.call(ctx, "function(){return this.isConnected}")
				if err != nil {
					return nil, err
				}
				if !truthy(connected) {
					node.release(ctx)
					node = nil
				}
			}
			return node, nil
		}()
		if err != nil {
			if _, ok := isProtocol(err); ok {
				if expected >= 0 {
					return nil, browserError(ctx, "stale_ref: take a new snapshot", "", nil)
				}
				if !transient(err) {
					return nil, err
				}
				frame.helper, frame.context = "", nil
			} else {
				return nil, err
			}
		} else {
			if node != nil {
				return node, nil
			}
			if !wait {
				return nil, nil
			}
			if expected >= 0 {
				return nil, browserError(ctx, "stale_ref: take a new snapshot", "", nil)
			}
		}
		if err := p.e.retryPause(ctx); err != nil {
			return nil, err
		}
	}
}

// input sends one input command once. A dialog opened by it (or already
// open) returns errDialogOpened; releases blocked by an open dialog are
// deferred until the dialog is handled.
func (p *Page) input(ctx context.Context, method string, params *Obj) (*Obj, error) {
	if p.obs.dialog != nil {
		t := params.Def("type", nil)
		if pyEqual(t, "mouseReleased") || pyEqual(t, "keyUp") {
			p.deferredInputs = append(p.deferredInputs, deferredInput{method, params})
		}
		return nil, errDialogOpened
	}
	command := p.e.start(ctx, method, params, p.session)
	modal := p.obs.dialogOpened.wait()
	var r reply
	commandDone := false
	p.e.block(func() {
		select {
		case r = <-command.ch:
			commandDone = true
		case <-modal:
			select {
			case r = <-command.ch:
				commandDone = true
			default:
			}
		}
	})
	if commandDone {
		if r.err != nil {
			return nil, r.err
		}
		if p.obs.dialog != nil {
			return nil, errDialogOpened
		}
		return r.result, nil
	}
	// The input was sent once; the concrete dialog event is its acknowledgement.
	// Cancel only local waiting, never repeat that input after handling the dialog.
	command.stop(p.e)
	return nil, errDialogOpened
}

func (p *Page) afterInput(ctx context.Context) error {
	if p.obs.dialog != nil {
		return nil
	}
	if _, err := p.e.call(ctx, "Page.enable", nil, p.session); err != nil {
		return err
	}
	for p.pendingNavigation && p.obs.dialog == nil {
		if err := p.e.retryPause(ctx); err != nil {
			return err
		}
	}
	if t := p.bindingTasks[p.session]; t != nil {
		return p.e.await(ctx, t)
	}
	return nil
}

func (p *Page) navigationDetails() *Obj {
	out := newObj()
	for _, key := range []string{"state", "committed", "loaderId", "networkError"} {
		if v, ok := p.navigation.Get(key); ok {
			out.Set(key, v)
		}
	}
	return out
}

var netErrorRe = regexp.MustCompile(`net::ERR_[A-Z0-9_]+`)

func (p *Page) failNavigation(text string) {
	if p.navigation == nil {
		return
	}
	// Preserve the useful Chromium error code, not arbitrary protocol text/URLs.
	code := "network_failure"
	if m := netErrorRe.FindString(text); m != "" {
		code = m
	}
	p.navigation.Set("state", "failed")
	p.navigation.Set("networkError", code)
	p.navigationFailed.Set()
	p.pendingNavigation = false
}

func (p *Page) checkNavigationFailure(ctx context.Context) error {
	if p.navigationFailed.IsSet() {
		return browserError(ctx, "navigation_failed", "failed", map[string]any{"navigation": p.navigationDetails()})
	}
	return nil
}

func (p *Page) navigationCommand(ctx context.Context, method string, params *Obj) (*Obj, error) {
	setPhase(ctx, "navigation_response")
	markEffect(ctx)
	command := p.e.start(ctx, method, params, p.session)
	failed := p.navigationFailed.wait()
	var r reply
	commandDone := false
	p.e.block(func() {
		select {
		case r = <-command.ch:
			commandDone = true
		case <-failed:
			select {
			case r = <-command.ch:
				commandDone = true
			default:
			}
		}
	})
	if err := p.checkNavigationFailure(ctx); err != nil {
		if !commandDone {
			command.stop(p.e)
		}
		return nil, err
	}
	if !commandDone {
		p.e.block(func() { r = <-command.ch })
	}
	if r.err != nil {
		return nil, r.err
	}
	if text := r.result.Def("errorText", nil); truthy(text) {
		p.failNavigation(pyStr(text))
		if err := p.checkNavigationFailure(ctx); err != nil {
			return nil, err
		}
	}
	return r.result, nil
}

func (p *Page) navigate(ctx context.Context, params *Obj) (any, error) {
	op := params.Def("op", "goto")
	p.navigation = O("state", "requested", "committed", false)
	p.navigationFailed.Clear()
	e := p.e
	switch {
	case pyEqual(op, "back"):
		setPhase(ctx, "navigation_history")
		history, err := e.call(ctx, "Page.getNavigationHistory", nil, p.session)
		if err != nil {
			return nil, err
		}
		index := int(mustNum(history.Must("currentIndex")))
		if index == 0 {
			p.navigation.Set("state", "completed")
			return O("url", p.url(), "statusCode", nil), nil
		}
		p.pendingNavigation = true
		entries := asList(history.Must("entries"))
		i := index - 1
		if i < 0 {
			i += len(entries)
		}
		if _, err := p.navigationCommand(ctx, "Page.navigateToHistoryEntry", O("entryId", asObj(entries[i]).Must("id"))); err != nil {
			return nil, err
		}
	case pyEqual(op, "reload"):
		p.pendingNavigation = true
		if _, err := p.navigationCommand(ctx, "Page.reload", nil); err != nil {
			return nil, err
		}
	default:
		result, err := p.navigationCommand(ctx, "Page.navigate", O("url", params.Must("url")))
		if err != nil {
			return nil, err
		}
		loader := result.Def("loaderId", nil)
		if truthy(result.Def("isDownload", nil)) {
			p.navigation.Set("state", "download")
		} else if truthy(loader) {
			p.navigation.Set("loaderId", loader)
			setPhase(ctx, "dom_content_loaded")
			key := lifeKey{pyStr(result.Must("frameId")), loaderOf(loader)}
			for {
				if names, _ := p.life.Get(key); names["DOMContentLoaded"] {
					break
				}
				if err := p.checkNavigationFailure(ctx); err != nil {
					return nil, err
				}
				if p.obs.dialog != nil {
					break
				}
				if err := e.retryPause(ctx); err != nil {
					return nil, err
				}
			}
		}
	}
	if pyEqual(op, "back") || pyEqual(op, "reload") {
		setPhase(ctx, "dom_content_loaded")
		for p.pendingNavigation && p.obs.dialog == nil {
			if err := p.checkNavigationFailure(ctx); err != nil {
				return nil, err
			}
			if err := e.retryPause(ctx); err != nil {
				return nil, err
			}
		}
	}
	if err := p.checkNavigationFailure(ctx); err != nil {
		return nil, err
	}
	setPhase(ctx, "navigation_bindings")
	if t := p.bindingTasks[p.session]; t != nil {
		if err := e.await(ctx, t); err != nil {
			return nil, err
		}
	}
	if !pyEqual(p.navigation.Must("state"), "download") {
		if p.obs.dialog != nil {
			p.navigation.Set("state", "dialog")
		} else {
			p.navigation.Set("state", "completed")
		}
	}
	main, err := p.main()
	if err != nil {
		return nil, err
	}
	status, _ := p.navigationStatus.Get(lifeKey{p.mainID, main.loader})
	return O("url", p.url(), "statusCode", status), nil
}

// close detaches every session this page attached; the target stays open.
func (p *Page) close(ctx context.Context) {
	p.closed = true
	var tasks []*task
	for _, t := range p.setupTasks.Values() {
		if !t.finished() {
			tasks = append(tasks, t)
		}
	}
	for _, t := range p.bindingTasks {
		if !t.finished() {
			tasks = append(tasks, t)
		}
	}
	if len(tasks) > 0 {
		p.e.cancelAndWait(tasks)
	}
	e := p.e
	short := 200 * time.Millisecond
	for _, observer := range p.observerSessions.Values() {
		if _, err := e.callTimeout(ctx, "Runtime.removeBinding", O("name", p.errorBinding), observer, short); err == nil {
			e.callTimeout(ctx, "Target.detachFromTarget", O("sessionId", observer), "", short)
		}
	}
	// Initialization can fail before getFrameTree registers the root session.
	sessions := map[string]bool{}
	for sid := range p.sessionRoots {
		sessions[sid] = true
	}
	for _, sid := range p.setupTasks.Keys() {
		sessions[sid] = true
	}
	if p.session != "" {
		sessions[p.session] = true
	}
	for sid := range sessions {
		if script, ok := p.scripts[sid]; ok {
			if _, err := e.callTimeout(ctx, "Page.removeScriptToEvaluateOnNewDocument", O("identifier", script), sid, short); err != nil {
				continue
			}
		}
		if _, err := e.callTimeout(ctx, "Runtime.removeBinding", O("name", p.errorBinding), sid, short); err != nil {
			continue
		}
		e.callTimeout(ctx, "Target.detachFromTarget", O("sessionId", sid), "", short)
	}
}
