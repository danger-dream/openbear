package browser

import (
	"context"
	"errors"
	"sort"
	"strings"
	"time"
	"unicode/utf8"
)

var modifierBits = map[string]int{"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}

type actions struct{ page *Page }

func (a *actions) e() *Engine { return a.page.e }

func (a *actions) modifiers() int {
	sum := 0
	for m := range a.page.modifiers {
		sum += modifierBits[m]
	}
	return sum
}

func (a *actions) scrollNode(ctx context.Context, node *Node) error {
	if _, err := a.e().call(ctx, "DOM.scrollIntoViewIfNeeded", O("objectId", node.objectID), node.frame.session); err != nil {
		return err
	}
	frame := node.frame
	for frame.parent != "" {
		owner, err := frame.owner(ctx)
		if err != nil {
			return err
		}
		_, err = a.e().call(ctx, "DOM.scrollIntoViewIfNeeded", O("objectId", owner.objectID), owner.frame.session)
		if err == nil {
			frame = owner.frame
		}
		owner.release(ctx)
		if err != nil {
			return err
		}
	}
	return nil
}

// actionable re-resolves target until it passes the element states (and,
// for pointer actions, has an unobstructed clickable point).
func (a *actions) actionable(ctx context.Context, target string, states []string, pointer bool) (*Node, *point, error) {
	for {
		node, err := a.page.resolve(ctx, target, true)
		if err != nil {
			return nil, nil, err
		}
		var found *point
		done := false
		err = func() (err error) {
			defer func() {
				if r := recover(); r != nil {
					node.release(ctx)
					panic(r)
				}
			}()
			if pointer {
				if err := a.scrollNode(ctx, node); err != nil {
					return err
				}
			}
			result, err := node.frame.run(ctx, "function(node,states){return this.checkElementStates(node,states)}", node, states)
			if err != nil {
				return err
			}
			if result != nil {
				return nil
			}
			if !pointer {
				done = true
				return nil
			}
			quads, err := a.e().call(ctx, "DOM.getContentQuads", O("objectId", node.objectID), node.frame.session)
			if err != nil {
				return err
			}
			root, err := a.page.sessionRoot(node.frame.session)
			if err != nil {
				return err
			}
			w, h, err := root.viewport(ctx)
			if err != nil {
				return err
			}
			var list [][]float64
			for _, q := range asList(quads.Def("quads", []any{})) {
				list = append(list, floats(q))
			}
			local, ok := clickablePoint(list, w, h)
			if !ok {
				return nil
			}
			p, err := root.toMain(ctx, local)
			if err != nil {
				return err
			}
			hit, err := a.hitTarget(ctx, node, p)
			if err != nil {
				return err
			}
			if hit {
				found, done = &p, true
			}
			return nil
		}()
		if err != nil {
			if _, ok := isProtocol(err); !ok || !transient(err) {
				node.release(ctx)
				return nil, nil, err
			}
		} else if done {
			return node, found, nil
		}
		node.release(ctx)
		if err := a.e().retryPause(ctx); err != nil {
			return nil, nil, err
		}
	}
}

func (a *actions) hitTarget(ctx context.Context, node *Node, p point) (hit bool, err error) {
	current := node
	defer func() {
		if current != node {
			current.release(ctx)
		}
	}()
	for {
		local, err := current.frame.fromMain(ctx, p)
		if err != nil {
			return false, err
		}
		result, err := current.frame.run(ctx, "function(node,point){return this.expectHitTarget(point,node)}", current, local.obj())
		if err != nil {
			return false, err
		}
		if !pyEqual(result, "done") {
			return false, nil
		}
		if current.frame.parent == "" {
			return true, nil
		}
		owner, err := current.frame.owner(ctx)
		if err != nil {
			return false, err
		}
		if current != node {
			current.release(ctx)
		}
		current = owner
	}
}

func (a *actions) guards(ctx context.Context, node *Node, p point, action string) (guards []*Node, err error) {
	current := node
	defer func() {
		if r := recover(); r != nil {
			a.stopGuards(ctx, guards, false)
			if current != node {
				current.release(ctx)
			}
			panic(r)
		}
		if err != nil {
			a.stopGuards(ctx, guards, false)
			guards = nil
		}
		if current != node {
			current.release(ctx)
		}
	}()
	for {
		local, err := current.frame.fromMain(ctx, p)
		if err != nil {
			return guards, err
		}
		remote, err := current.frame.runRemote(ctx,
			"function(node,args){return this.setupHitTargetInterceptor(node,args.action,args.point,false)}",
			current, O("point", local.obj(), "action", action))
		if err != nil {
			return guards, err
		}
		if !truthy(remote.Def("objectId", nil)) {
			return guards, browserError(ctx, "pointer_intercepted", "", nil)
		}
		guards = append(guards, &Node{current.frame, asStr(remote.Must("objectId")), current.frame.epoch})
		if current.frame.parent == "" {
			return guards, nil
		}
		owner, err := current.frame.owner(ctx)
		if err != nil {
			return guards, err
		}
		if current != node {
			current.release(ctx)
		}
		current = owner
	}
}

func (a *actions) stopGuards(ctx context.Context, guards []*Node, check bool) error {
	intercepted := false
	for _, guard := range guards {
		result, err := guard.call(ctx, "function(){return this.stop()}")
		guard.release(ctx)
		if err != nil {
			var be *Error
			if _, ok := isProtocol(err); ok || errors.As(err, &be) {
				// Navigation destroys the world after a successful input as well.
				continue
			}
			return err
		}
		intercepted = intercepted || !pyEqual(result, "done")
	}
	if check && intercepted {
		return browserError(ctx, "pointer_intercepted", "unknown", nil)
	}
	return nil
}

func (a *actions) mouse(ctx context.Context, kind string, p point, button string, buttons, count int) error {
	markEffect(ctx)
	a.page.mouse = p
	_, err := a.page.input(ctx, "Input.dispatchMouseEvent", O(
		"type", kind, "x", p.X, "y", p.Y, "button", button, "buttons", buttons,
		"clickCount", count, "modifiers", a.modifiers(),
	))
	return err
}

func (a *actions) platformModifier(ctx context.Context) (string, error) {
	main, err := a.page.main()
	if err != nil {
		return "", err
	}
	platform, err := main.run(ctx, "function(){return this.window.navigator.platform}")
	if err != nil {
		return "", err
	}
	if strings.HasPrefix(asStr(platform), "Mac") {
		return "Meta", nil
	}
	return "Control", nil
}

func sortedKeys(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func (a *actions) setModifiers(ctx context.Context, desired map[string]bool) error {
	want := map[string]bool{}
	for k := range desired {
		want[k] = true
	}
	if want["ControlOrMeta"] {
		delete(want, "ControlOrMeta")
		m, err := a.platformModifier(ctx)
		if err != nil {
			return err
		}
		want[m] = true
	}
	modal := false
	var release []string
	for _, k := range sortedKeys(a.page.modifiers) {
		if !want[k] {
			release = append(release, k)
		}
	}
	for _, key := range release {
		if err := a.key(ctx, key, false); err != nil {
			if errors.Is(err, errDialogOpened) {
				// Each unsent release must be queued even while a modal is open.
				modal = true
				continue
			}
			return err
		}
	}
	if modal {
		return errDialogOpened
	}
	var press []string
	for _, k := range sortedKeys(want) {
		if !a.page.modifiers[k] {
			press = append(press, k)
		}
	}
	for _, key := range press {
		if err := a.key(ctx, key, true); err != nil {
			return err
		}
	}
	return nil
}

var keyAliases = map[string]string{
	"Control": "ControlLeft", "Shift": "ShiftLeft", "Alt": "AltLeft", "Meta": "MetaLeft",
	"Esc": "Escape", "Del": "Delete", "Space": "Space",
}

type keyDescription struct {
	key, code, text string
	virtual         any
	location        any
}

func (a *actions) describeKey(ctx context.Context, key string) (*keyDescription, error) {
	rows, index := keys()
	code := key
	if alias, ok := keyAliases[key]; ok {
		code = alias
	}
	var row *Obj
	var value string
	if r, ok := index[code]; ok {
		row = r
		value = asStr(row.Must("key"))
		if a.page.modifiers["Shift"] {
			value = asStr(row.Def("shiftKey", row.Must("key")))
		}
	} else {
		found := false
		for _, kr := range rows {
			if pyEqual(kr.row.Def("key", nil), key) || pyEqual(kr.row.Def("shiftKey", nil), key) {
				code, row, found = kr.code, kr.row, true
				break
			}
		}
		if !found {
			if utf8.RuneCountInString(key) == 1 {
				return &keyDescription{key: key, code: "", virtual: 0, text: key, location: 0}, nil
			}
			return nil, browserError(ctx, "unknown_key", "", nil)
		}
		value = key
	}
	text := ""
	if utf8.RuneCountInString(value) == 1 {
		text = value
	}
	return &keyDescription{
		key:      value,
		code:     code,
		virtual:  row.Def("keyCodeWithoutLocation", row.Must("keyCode")),
		location: row.Def("location", 0),
		text:     asStr(row.Def("text", text)),
	}, nil
}

func (a *actions) key(ctx context.Context, key string, down bool) error {
	if key == "ControlOrMeta" {
		m, err := a.platformModifier(ctx)
		if err != nil {
			return err
		}
		key = m
	}
	d, err := a.describeKey(ctx, key)
	if err != nil {
		return err
	}
	if _, ok := modifierBits[d.key]; ok {
		if down {
			a.page.modifiers[d.key] = true
		} else {
			delete(a.page.modifiers, d.key)
		}
	}
	text := d.text
	for m := range a.page.modifiers {
		if m != "Shift" {
			text = ""
		}
	}
	kind := "keyUp"
	if down {
		kind = "rawKeyDown"
		if text != "" {
			kind = "keyDown"
		}
	}
	payload := O(
		"key", d.key, "code", d.code, "windowsVirtualKeyCode", d.virtual, "location", d.location,
		"type", kind, "modifiers", a.modifiers(),
		"autoRepeat", down && a.page.pressed[key],
		"isKeypad", pyEqual(d.location, 3),
	)
	if down {
		payload.Set("text", text)
		payload.Set("unmodifiedText", text)
		a.page.pressed[key] = true
	} else {
		delete(a.page.pressed, key)
	}
	markEffect(ctx)
	_, err = a.page.input(ctx, "Input.dispatchKeyEvent", payload)
	return err
}

func copySet(m map[string]bool) map[string]bool {
	out := make(map[string]bool, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}

func (a *actions) press(ctx context.Context, chord string) (err error) {
	// A trailing '+' is itself a key (Control++).
	parts := strings.Split(chord, "+")
	if parts[len(parts)-1] == "" && len(parts) > 1 {
		parts = append(parts[:len(parts)-2:len(parts)-2], "+")
	}
	modifiers, key := parts[:len(parts)-1], parts[len(parts)-1]
	previous := copySet(a.page.modifiers)
	defer func() {
		if a.page.pressed[key] {
			a.key(ctx, key, false)
		}
		if rerr := a.setModifiers(ctx, previous); rerr != nil {
			err = rerr
		}
	}()
	desired := copySet(previous)
	for _, m := range modifiers {
		desired[m] = true
	}
	if err := a.setModifiers(ctx, desired); err != nil {
		return err
	}
	if err := a.key(ctx, key, true); err != nil {
		return err
	}
	return a.key(ctx, key, false)
}

func (a *actions) click(ctx context.Context, params *Obj) (err error) {
	hover := pyEqual(params.Must("op"), "hover")
	states := []string{"visible", "stable"}
	if !hover {
		states = append(states, "enabled")
	}
	node, p, err := a.actionable(ctx, asStr(params.Must("target")), states, true)
	if err != nil {
		return err
	}
	var guards []*Node
	previous := copySet(a.page.modifiers)
	button := asStr(params.Def("button", "left"))
	bits := map[string]int{"left": 1, "right": 2, "middle": 4}
	down := false
	defer func() {
		if down {
			a.mouse(ctx, "mouseReleased", *p, button, 0, 0)
		}
		var cleanup error
		if a.page.obs.dialog != nil {
			a.page.deferredGuards = append(a.page.deferredGuards, guards...)
		} else {
			cleanup = a.stopGuards(ctx, guards, false)
		}
		node.release(ctx)
		if cleanup == nil {
			cleanup = a.setModifiers(ctx, previous)
		}
		if cleanup != nil {
			err = cleanup
		}
	}()
	wanted := map[string]bool{}
	for _, m := range asList(params.Def("modifiers", []any{})) {
		wanted[asStr(m)] = true
	}
	if err := a.setModifiers(ctx, wanted); err != nil {
		return err
	}
	kind := "mouse"
	if hover {
		kind = "hover"
	}
	if guards, err = a.guards(ctx, node, *p, kind); err != nil {
		return err
	}
	if err := a.mouse(ctx, "mouseMoved", *p, "none", 0, 0); err != nil {
		return err
	}
	if !hover {
		clicks := 1
		if truthy(params.Def("doubleClick", nil)) {
			clicks = 2
		}
		for count := 1; count <= clicks; count++ {
			down = true
			bit, ok := bits[button]
			if !ok {
				panic(keyError(button))
			}
			if err := a.mouse(ctx, "mousePressed", *p, button, bit, count); err != nil {
				return err
			}
			down = false
			if err := a.mouse(ctx, "mouseReleased", *p, button, 0, count); err != nil {
				return err
			}
		}
	}
	if a.page.obs.dialog != nil {
		// A modal pauses renderer JS. Stop the interceptors after the explicit
		// dialog handler resumes it, rather than evaluating into the modal.
		a.page.deferredGuards = append(a.page.deferredGuards, guards...)
	} else {
		completed := guards
		guards = nil
		if err := a.stopGuards(ctx, completed, true); err != nil {
			return err
		}
	}
	guards = nil
	return a.page.afterInput(ctx)
}

func (a *actions) fill(ctx context.Context, target string, text string, slowly bool) error {
	node, _, err := a.actionable(ctx, target, []string{"visible", "enabled", "editable"}, false)
	if err != nil {
		return err
	}
	defer node.release(ctx)
	markEffect(ctx)
	if slowly {
		result, err := node.frame.run(ctx, "function(node){return this.focusNode(node)}", node)
		if err != nil {
			return err
		}
		if !pyEqual(result, "done") {
			return browserError(ctx, "element_not_editable", "", nil)
		}
		rows, _ := keys()
		for _, r := range text {
			char := string(r)
			known := false
			for _, kr := range rows {
				if pyEqual(kr.row.Def("key", nil), char) || pyEqual(kr.row.Def("shiftKey", nil), char) {
					known = true
					break
				}
			}
			if known {
				err = a.press(ctx, char)
			} else {
				_, err = a.page.input(ctx, "Input.insertText", O("text", char))
			}
			if err != nil {
				return err
			}
			if err := a.e().sleep(ctx, 40*time.Millisecond); err != nil {
				return err
			}
		}
		return nil
	}
	result, err := node.frame.run(ctx, "function(node,value){return this.fill(node,value)}", node, text)
	if err != nil {
		return err
	}
	if pyEqual(result, "needsinput") {
		if text != "" {
			_, err = a.page.input(ctx, "Input.insertText", O("text", text))
			return err
		}
		return a.press(ctx, "Delete")
	}
	if !pyEqual(result, "done") {
		return browserError(ctx, "element_not_editable", "", nil)
	}
	return nil
}

func (a *actions) checked(ctx context.Context, target string) (any, error) {
	node, err := a.page.resolve(ctx, target, true)
	if err != nil {
		return nil, err
	}
	defer node.release(ctx)
	result, err := node.frame.run(ctx, "function(node){return this.elementState(node,'checked')}", node)
	if err != nil {
		return nil, err
	}
	state := asObj(result)
	if pyEqual(state.Def("received", nil), "error:notconnected") {
		return nil, browserError(ctx, "element_detached", "", nil)
	}
	return state.Must("matches"), nil
}

func (a *actions) drag(ctx context.Context, params *Obj) (err error) {
	source, start, err := a.actionable(ctx, asStr(params.Must("target")), []string{"visible", "stable"}, true)
	if err != nil {
		return err
	}
	var target *Node
	a.page.dragData = nil
	released := true
	e := a.e()
	defer func() {
		if !released {
			a.mouse(ctx, "mouseReleased", a.page.mouse, "left", 0, 0)
		}
		e.callTimeout(ctx, "Input.setInterceptDrags", O("enabled", false), a.page.session, 300*time.Millisecond)
		source.release(ctx)
		if target != nil {
			target.release(ctx)
		}
	}()
	if _, err := e.call(ctx, "Input.setInterceptDrags", O("enabled", true), a.page.session); err != nil {
		return err
	}
	if err := a.mouse(ctx, "mouseMoved", *start, "none", 0, 0); err != nil {
		return err
	}
	released = false
	if err := a.mouse(ctx, "mousePressed", *start, "left", 1, 1); err != nil {
		return err
	}
	var end *point
	if target, end, err = a.actionable(ctx, asStr(params.Must("to")), []string{"visible", "stable"}, true); err != nil {
		return err
	}
	for step := 1; step < 9; step++ {
		p := point{start.X + (end.X-start.X)*float64(step)/8, start.Y + (end.Y-start.Y)*float64(step)/8}
		if err := a.mouse(ctx, "mouseMoved", p, "left", 1, 0); err != nil {
			return err
		}
	}
	if _, err := e.call(ctx, "Page.enable", nil, a.page.session); err != nil {
		return err
	}
	if truthy(a.page.dragData) {
		for _, kind := range []string{"dragEnter", "dragOver", "drop"} {
			markEffect(ctx)
			if _, err := e.call(ctx, "Input.dispatchDragEvent", O(
				"type", kind, "x", end.X, "y", end.Y, "data", a.page.dragData, "modifiers", a.modifiers(),
			), a.page.session); err != nil {
				return err
			}
		}
	}
	// The release may have happened even if its acknowledgement is lost.
	released = true
	if err := a.mouse(ctx, "mouseReleased", *end, "left", 0, 1); err != nil {
		return err
	}
	return a.page.afterInput(ctx)
}

var mediaFeatures = [][2]string{
	{"colorScheme", "prefers-color-scheme"},
	{"reducedMotion", "prefers-reduced-motion"},
	{"forcedColors", "forced-colors"},
	{"contrast", "prefers-contrast"},
}

func (a *actions) act(ctx context.Context, params *Obj) (any, error) {
	op := params.Must("op")
	e := a.e()
	switch {
	case pyEqual(op, "click"), pyEqual(op, "hover"):
		if err := a.click(ctx, params); err != nil {
			return nil, err
		}
	case pyEqual(op, "fill"), pyEqual(op, "type"):
		var fields []any
		if f := params.Def("fields", nil); truthy(f) {
			fields = asList(f)
		} else {
			fields = []any{O("target", params.Must("target"), "value", params.Def("text", ""))}
		}
		slowly := pyEqual(op, "type") && truthy(params.Def("slowly", false))
		for _, f := range fields {
			field := asObj(f)
			if err := a.fill(ctx, asStr(field.Must("target")), asStr(field.Must("value")), slowly); err != nil {
				return nil, err
			}
		}
		if pyEqual(op, "type") && truthy(params.Def("submit", nil)) {
			if err := a.press(ctx, "Enter"); err != nil {
				return nil, err
			}
			if err := a.page.afterInput(ctx); err != nil {
				return nil, err
			}
		}
	case pyEqual(op, "press"):
		if truthy(params.Def("target", nil)) {
			node, err := a.page.resolve(ctx, asStr(params.Must("target")), true)
			if err != nil {
				return nil, err
			}
			_, err = node.frame.run(ctx, "function(node){return this.focusNode(node)}", node)
			node.release(ctx)
			if err != nil {
				return nil, err
			}
		}
		if err := a.press(ctx, asStr(params.Must("key"))); err != nil {
			return nil, err
		}
		if err := a.page.afterInput(ctx); err != nil {
			return nil, err
		}
	case pyEqual(op, "check"):
		target := asStr(params.Must("target"))
		state, err := a.checked(ctx, target)
		if err != nil {
			return nil, err
		}
		if !pyEqual(state, params.Must("checked")) {
			if err := a.click(ctx, O("op", "click", "target", target)); err != nil {
				return nil, err
			}
			if state, err = a.checked(ctx, target); err != nil {
				return nil, err
			}
			if !pyEqual(state, params.Must("checked")) {
				return nil, browserError(ctx, "checked_state_not_changed", "completed", nil)
			}
		}
	case pyEqual(op, "select"):
		for {
			node, _, err := a.actionable(ctx, asStr(params.Must("target")), []string{"visible", "enabled"}, false)
			if err != nil {
				return nil, err
			}
			markEffect(ctx)
			result, err := func() (any, error) {
				defer node.release(ctx)
				return node.frame.run(ctx,
					"function(node,values){return this.selectOptions(node,values.map(value=>({valueOrLabel:value})))}",
					node, params.Must("values"))
			}()
			if err != nil {
				return nil, err
			}
			if _, ok := result.([]any); ok {
				break
			}
			if !pyEqual(result, "error:optionsnotfound") && !pyEqual(result, "error:optionnotenabled") && !pyEqual(result, "error:notconnected") {
				return nil, browserError(ctx, "select_failed", "", nil)
			}
			if err := e.retryPause(ctx); err != nil {
				return nil, err
			}
		}
	case pyEqual(op, "drag"):
		if err := a.drag(ctx, params); err != nil {
			return nil, err
		}
	case pyEqual(op, "scroll"):
		markEffect(ctx)
		if truthy(params.Def("target", nil)) {
			node, err := a.page.resolve(ctx, asStr(params.Must("target")), true)
			if err != nil {
				return nil, err
			}
			err = a.scrollNode(ctx, node)
			node.release(ctx)
			if err != nil {
				return nil, err
			}
		} else if _, err := e.call(ctx, "Input.dispatchMouseEvent", O(
			"type", "mouseWheel", "x", a.page.mouse.X, "y", a.page.mouse.Y,
			"deltaX", params.Def("x", 0), "deltaY", params.Def("y", 0),
		), a.page.session); err != nil {
			return nil, err
		}
	case pyEqual(op, "resize"):
		markEffect(ctx)
		if _, err := e.call(ctx, "Emulation.setDeviceMetricsOverride", O(
			"width", params.Must("width"), "height", params.Must("height"),
			"deviceScaleFactor", 1, "mobile", false,
		), a.page.session); err != nil {
			return nil, err
		}
	case pyEqual(op, "emulate"):
		if a.page.media == nil {
			a.page.media = newObj()
		}
		a.page.media.Update(asObj(params.Must("media")))
		markEffect(ctx)
		features := []any{}
		for _, f := range mediaFeatures {
			value := a.page.media.Def(f[0], nil)
			if !truthy(value) {
				value = ""
			}
			features = append(features, O("name", f[1], "value", value))
		}
		media := a.page.media.Def("media", nil)
		if !truthy(media) {
			media = ""
		}
		if _, err := e.call(ctx, "Emulation.setEmulatedMedia", O("media", media, "features", features), a.page.session); err != nil {
			return nil, err
		}
	default:
		return nil, browserError(ctx, "unknown_act_operation", "", nil)
	}
	var dialog any
	if d := a.page.obs.dialog; d != nil {
		dialog = O("type", d.Must("type"), "message", d.Must("message"))
	}
	return O(
		"completed", true,
		"url", a.page.url(),
		"dialog", dialog,
		"downloads", a.page.downloadList(),
	), nil
}

func (a *actions) wait(ctx context.Context, params *Obj) (any, error) {
	e := a.e()
	if params.Has("timeMs") {
		ms := mustNum(params.Must("timeMs"))
		if err := e.sleep(ctx, time.Duration(ms*float64(time.Millisecond))); err != nil {
			return nil, err
		}
		return O("ready", true), nil
	}
	state := params.Def("state", "visible")
	for {
		if params.Has("url") {
			matched, err := fnmatchCase(asStr(a.page.url()), asStr(params.Must("url")))
			if err != nil {
				return nil, err
			}
			if matched {
				return O("ready", true), nil
			}
		} else {
			matched, err := a.waitOnce(ctx, params, state)
			if err != nil {
				if _, ok := isProtocol(err); !ok || !transient(err) {
					return nil, err
				}
			} else if matched {
				return O("ready", true), nil
			}
		}
		if err := e.retryPause(ctx); err != nil {
			return nil, err
		}
	}
}

func (a *actions) waitOnce(ctx context.Context, params *Obj, state any) (bool, error) {
	var node *Node
	var err error
	defer func() {
		if node != nil {
			node.release(ctx)
		}
	}()
	if truthy(params.Def("target", nil)) {
		node, err = a.page.resolve(ctx, asStr(params.Must("target")), false)
	} else {
		selector := "internal:text=" + dumpsASCII(params.Must("text")) + "i >> nth=0"
		var main *Frame
		if main, err = a.page.main(); err == nil {
			node, err = main.query(ctx, selector)
		}
	}
	if err != nil {
		return false, err
	}
	switch {
	case pyEqual(state, "attached") || pyEqual(state, "detached"):
		return (node != nil) == pyEqual(state, "attached"), nil
	case node == nil:
		return pyEqual(state, "hidden"), nil
	}
	result, err := node.frame.run(ctx, "function(node,state){return this.elementState(node,state)}", node, state)
	if err != nil {
		return false, err
	}
	return truthy(asObj(result).Must("matches")), nil
}
