package browser

import (
	"context"
	"strings"
	"time"
)

// Node is a remote object handle in a frame's helper world.
type Node struct {
	frame    *Frame
	objectID string
	epoch    int
}

// argument is frames.argument: Nodes by objectId, anything else by value.
func argument(v any) *Obj {
	if n, ok := v.(*Node); ok {
		return O("objectId", n.objectID)
	}
	return O("value", v)
}

// remoteResult is frames.remote_result.
func remoteResult(ctx context.Context, result *Obj, byValue bool) (any, error) {
	if result.Has("exceptionDetails") {
		details := asObj(result.Must("exceptionDetails"))
		description := pyStr(asObj(details.Def("exception", newObj())).Def("description", ""))
		if strings.Contains(description, "strict mode violation") {
			return nil, browserError(ctx, "strict_selector_violation", "", nil)
		}
		if strings.Contains(description, "aria-ref") && (strings.Contains(description, "not found") || strings.Contains(description, "snapshot")) {
			return nil, browserError(ctx, "stale_ref: take a new snapshot", "", nil)
		}
		return nil, browserError(ctx, "javascript_error", "", nil)
	}
	remote := asObj(result.Def("result", newObj()))
	if byValue {
		return remote.Def("value", nil), nil
	}
	return remote, nil
}

func (n *Node) call(ctx context.Context, function string, args ...any) (any, error) {
	if n.epoch != n.frame.epoch {
		return nil, browserError(ctx, "stale_ref: take a new snapshot", "", nil)
	}
	return n.frame.callObject(ctx, n.objectID, function, args, true)
}

// release frees the handle; failures are ignored (0.25s budget).
func (n *Node) release(ctx context.Context) {
	n.frame.page.e.callTimeout(ctx, "Runtime.releaseObject", O("objectId", n.objectID), n.frame.session, 250*time.Millisecond)
}

// Frame is one DOM frame; OOPIF frames live on their own CDP session.
type Frame struct {
	page     *Page
	id       string
	parent   string
	session  string
	seq      int
	epoch    int
	url      any
	loader   string
	context  any // execution context id (nil when invalid)
	helper   string
	lock     *aLock
	detached bool
}

func newFrame(p *Page, id, parent, session string) *Frame {
	return &Frame{page: p, id: id, parent: parent, session: session, seq: p.e.nextFrameSeq(), url: "", lock: newALock()}
}

func (f *Frame) invalidate() {
	f.epoch++
	f.context, f.helper = nil, ""
	f.page.invalidateRefs()
}

// ready installs the licensed injected script plus bridge in an isolated world.
func (f *Frame) ready(ctx context.Context) error {
	if f.detached || f.session == "" {
		return protoErr("frame_not_ready")
	}
	e := f.page.e
	if err := e.acquire(ctx, f.lock); err != nil {
		return err
	}
	defer f.lock.release()
	if f.helper != "" {
		return nil
	}
	epoch := f.epoch
	context_, err := e.call(ctx, "Page.createIsolatedWorld", O("frameId", f.id, "worldName", "openbear-browser-utility"), f.session)
	if err != nil {
		return err
	}
	options := O(
		"isUnderTest", false,
		"sdkLanguage", "python",
		"frameSeq", f.seq,
		"testIdAttributeName", "data-testid",
		"stableRafCount", 1,
		"browserName", "chromium",
		"shouldPrependErrorPrefix", false,
		"isUtilityWorld", true,
		"customEngines", []any{},
	)
	source := "(() => {const module={};" + injectedJS + ";const h=new (module.exports.InjectedScript())(globalThis," +
		dumpsASCII(options) + ");" + bridgeJS + ";return h;})()"
	result, err := e.call(ctx, "Runtime.evaluate", O(
		"expression", source,
		"contextId", context_.Must("executionContextId"),
		"returnByValue", false,
	), f.session)
	if err != nil {
		return err
	}
	remote, err := remoteResult(ctx, result, false)
	if err != nil {
		return err
	}
	if epoch != f.epoch {
		return protoErr("document_changed")
	}
	f.context = context_.Must("executionContextId")
	f.helper = asStr(asObj(remote).Must("objectId"))
	return nil
}

func (f *Frame) callObject(ctx context.Context, objectID, function string, args []any, byValue bool) (any, error) {
	arguments := make([]any, len(args))
	for i, a := range args {
		arguments[i] = argument(a)
	}
	result, err := f.page.e.call(ctx, "Runtime.callFunctionOn", O(
		"objectId", objectID,
		"functionDeclaration", function,
		"arguments", arguments,
		"awaitPromise", true,
		"returnByValue", byValue,
		"userGesture", true,
	), f.session)
	if err != nil {
		return nil, err
	}
	return remoteResult(ctx, result, byValue)
}

func (f *Frame) run(ctx context.Context, function string, args ...any) (any, error) {
	if err := f.ready(ctx); err != nil {
		return nil, err
	}
	return f.callObject(ctx, f.helper, function, args, true)
}

func (f *Frame) runRemote(ctx context.Context, function string, args ...any) (*Obj, error) {
	if err := f.ready(ctx); err != nil {
		return nil, err
	}
	v, err := f.callObject(ctx, f.helper, function, args, false)
	if err != nil {
		return nil, err
	}
	return asObj(v), nil
}

func (f *Frame) query(ctx context.Context, selector string) (*Node, error) {
	remote, err := f.runRemote(ctx, "function(selector){return this.__query(selector)}", selector)
	if err != nil {
		return nil, err
	}
	if !truthy(remote.Def("objectId", nil)) || pyEqual(remote.Def("subtype", nil), "null") {
		return nil, nil
	}
	return &Node{f, asStr(remote.Must("objectId")), f.epoch}, nil
}

func (f *Frame) resolve(ctx context.Context, backendNode any) (*Node, error) {
	if err := f.ready(ctx); err != nil {
		return nil, err
	}
	result, err := f.page.e.call(ctx, "DOM.resolveNode", O("backendNodeId", backendNode, "executionContextId", f.context), f.session)
	if err != nil {
		return nil, err
	}
	object := asObj(result.Must("object"))
	if !truthy(object.Def("objectId", nil)) {
		return nil, protoErr("node_detached")
	}
	return &Node{f, asStr(object.Must("objectId")), f.epoch}, nil
}

func (f *Frame) viewport(ctx context.Context) (float64, float64, error) {
	v, err := f.run(ctx, "function(){return {width:this.window.innerWidth,height:this.window.innerHeight}}")
	if err != nil {
		return 0, 0, err
	}
	o := asObj(v)
	return mustNum(o.Must("width")), mustNum(o.Must("height")), nil
}

func (f *Frame) owner(ctx context.Context) (*Node, error) {
	parent := f.page.frames[f.parent]
	if f.parent == "" || parent == nil {
		return nil, protoErr("frame_detached")
	}
	owner, err := f.page.e.call(ctx, "DOM.getFrameOwner", O("frameId", f.id), parent.session)
	if err != nil {
		return nil, err
	}
	return parent.resolve(ctx, owner.Must("backendNodeId"))
}

func (f *Frame) globalQuad(ctx context.Context) (quad []float64, err error) {
	owner, err := f.owner(ctx)
	if err != nil {
		return nil, err
	}
	defer owner.release(ctx)
	model, err := f.page.e.call(ctx, "DOM.getBoxModel", O("objectId", owner.objectID), owner.frame.session)
	if err != nil {
		return nil, err
	}
	quad = floats(asObj(model.Must("model")).Must("content"))
	// CDP geometry is relative to this session's local root, not every DOM frame.
	root, err := f.page.sessionRoot(owner.frame.session)
	if err != nil {
		return nil, err
	}
	if root.id == f.page.mainID {
		return quad, nil
	}
	var result []float64
	for i := 0; i < 8; i += 2 {
		p, err := root.toMain(ctx, point{quad[i], quad[i+1]})
		if err != nil {
			return nil, err
		}
		result = append(result, p.X, p.Y)
	}
	return result, nil
}

func (f *Frame) toMain(ctx context.Context, p point) (point, error) {
	if f.id == f.page.mainID {
		return p, nil
	}
	w, h, err := f.viewport(ctx)
	if err != nil {
		return point{}, err
	}
	quad, err := f.globalQuad(ctx)
	if err != nil {
		return point{}, err
	}
	return transform(ctx, p, viewportQuad(w, h), quad)
}

func (f *Frame) fromMain(ctx context.Context, p point) (point, error) {
	if f.id == f.page.mainID {
		return p, nil
	}
	w, h, err := f.viewport(ctx)
	if err != nil {
		return point{}, err
	}
	quad, err := f.globalQuad(ctx)
	if err != nil {
		return point{}, err
	}
	return transform(ctx, p, quad, viewportQuad(w, h))
}
