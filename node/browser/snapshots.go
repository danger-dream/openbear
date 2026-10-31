package browser

import (
	"context"
	"errors"
	"regexp"
	"strings"
	"time"
)

var (
	refPattern     = regexp.MustCompile(`\[ref=([^\]]+)\]`)
	visiblePattern = regexp.MustCompile(`\[ref=[^:]+:([^\]]+)\]`)
	interactiveRe  = regexp.MustCompile(`\[ref=|heading|iframe`)
)

func snapshotRecoverable(err error) bool {
	var be *Error
	if errors.As(err, &be) || errors.Is(err, ErrDisconnected) || isTimeout(err) {
		return true
	}
	_, ok := isProtocol(err)
	return ok
}

type snapshotState struct {
	page     *Page
	params   *Obj
	refs     map[string]refEntry
	warnings []any
	epochs   *omap[string, int]
}

func (s *snapshotState) collect(ctx context.Context, frame *Frame, root *Node, depth any) (any, error) {
	page := s.page
	s.epochs.Set(frame.id, frame.epoch)
	options := O("mode", "ai", "boxes", s.params.Def("boxes", false))
	if depth != nil {
		options.Set("depth", depth)
	}
	var rootArg any
	if root != nil {
		rootArg = root
	}
	value, err := frame.run(ctx,
		"function(node,opts){return this.ariaSnapshotJSON(node || this.document.body || this.document.documentElement,opts)}",
		rootArg, options)
	if err != nil {
		return nil, err
	}
	result := asObj(value)
	data := result.Must("json")
	var index func(nodes any)
	index = func(nodes any) {
		for _, n := range asList(nodes) {
			node, ok := n.(*Obj)
			if !ok {
				continue
			}
			if ref := node.Def("ref", nil); truthy(ref) {
				s.refs[pyStr(ref)] = refEntry{frame.id, frame.epoch}
			}
			index(node.Def("children", []any{}))
		}
	}
	index(data)
	children := map[string]any{}
	depths := asObj(result.Must("iframeDepths"))
	for _, r := range asList(result.Must("iframeRefs")) {
		ref := pyStr(r)
		if !depths.Has(ref) {
			continue
		}
		var childDepth any
		if depth != nil {
			cd := mustNum(depth) - mustNum(depths.Must(ref)) - 1
			if cd < 0 {
				continue
			}
			childDepth = pyNum(cd)
		}
		err := func() error {
			op := opOf(ctx)
			limit := 2 * time.Second
			if op != nil {
				r, err := op.remaining(ctx)
				if err != nil {
					return err
				}
				limit = min(limit, r)
			}
			cctx, cancel := context.WithTimeout(ctx, limit)
			defer cancel()
			owner, err := frame.query(cctx, "aria-ref="+ref)
			if err != nil {
				return err
			}
			if owner == nil {
				return nil
			}
			defer owner.release(cctx)
			description, err := page.e.call(cctx, "DOM.describeNode", O("objectId", owner.objectID), frame.session)
			if err != nil {
				return err
			}
			node := asObj(description.Must("node"))
			fid := node.Def("frameId", nil)
			if !truthy(fid) {
				fid = asObj(node.Def("contentDocument", newObj())).Def("frameId", nil)
			}
			child := page.frames[pyStr(fid)]
			if fid == nil || child == nil || child.detached {
				return protoErr("frame_detached")
			}
			if t, ok := page.setupTasks.Get(child.session); ok {
				if err := page.e.await(cctx, t); err != nil {
					return err
				}
			}
			collected, err := s.collect(cctx, child, nil, childDepth)
			if err != nil {
				return err
			}
			children[ref] = collected
			return nil
		}()
		if err != nil {
			if !snapshotRecoverable(err) {
				return nil, err
			}
			s.warnings = append(s.warnings, O("frameRef", ref, "error", "frame_snapshot_unavailable"))
		}
	}
	var merge func(nodes any)
	merge = func(nodes any) {
		for _, n := range asList(nodes) {
			node, ok := n.(*Obj)
			if !ok {
				continue
			}
			ref := node.Def("ref", nil)
			if c, found := children[pyStr(ref)]; found && pyEqual(node.Def("role", nil), "iframe") && ref != nil {
				node.Set("children", c)
			} else {
				merge(node.Def("children", []any{}))
			}
		}
	}
	merge(data)
	return data, nil
}

// snapshot is snapshots.snapshot: an aria snapshot across frames (OOPIFs
// included) with refs versioned by a fresh snapshot id.
func snapshot(ctx context.Context, page *Page, action string, params *Obj) (any, error) {
	s := &snapshotState{page: page, params: params, refs: map[string]refEntry{}, epochs: newOmap[string, int]()}
	var root *Node
	if truthy(params.Def("target", nil)) {
		var err error
		if root, err = page.resolve(ctx, asStr(params.Must("target")), true); err != nil {
			return nil, err
		}
	}
	text, err := func() (string, error) {
		if root != nil {
			defer root.release(ctx)
		}
		var frame *Frame
		if root != nil {
			frame = root.frame
		} else {
			main, err := page.main()
			if err != nil {
				return "", err
			}
			frame = main
		}
		data, err := s.collect(ctx, frame, root, params.Def("depth", nil))
		if err != nil {
			return "", err
		}
		for _, fid := range s.epochs.Keys() {
			epoch, _ := s.epochs.Get(fid)
			f := page.frames[fid]
			if f == nil {
				panic(keyError(fid))
			}
			if f.epoch != epoch {
				return "", browserError(ctx, "document_changed: take a new snapshot", "", nil)
			}
		}
		rendered, err := frame.run(ctx, "function(data){return this.__renderSnapshot(data)}", data)
		if err != nil {
			return "", err
		}
		return asStr(rendered), nil
	}()
	if err != nil {
		return nil, err
	}
	if truthy(params.Def("interactive", nil)) {
		var kept []string
		for _, line := range strings.Split(text, "\n") {
			if interactiveRe.MatchString(line) {
				kept = append(kept, line)
			}
		}
		text = strings.Join(kept, "\n")
	}
	if action == "find" {
		lines := strings.Split(text, "\n")
		selected := make([]bool, len(lines))
		needle := strings.ToLower(asStr(params.Must("text")))
		for i, line := range lines {
			if strings.Contains(strings.ToLower(line), needle) {
				for j := max(0, i-2); j < min(len(lines), i+3); j++ {
					selected[j] = true
				}
			}
		}
		var kept []string
		for i, line := range lines {
			if selected[i] {
				kept = append(kept, line)
			}
		}
		text = strings.Join(kept, "\n")
	}
	version := "s" + uuidHex()[:8]
	text = refPattern.ReplaceAllString(text, "[ref="+version+":${1}]")
	limit := mustNum(params.Def("maxChars", page.e.config.Must("snapshotMaxChars")))
	limit = min(limit, page.e.cfgNum("snapshotMaxChars"))
	truncated := float64(pyLen(text)) > limit
	if truncated {
		text = pyPrefix(text, int(limit))
		if i := strings.LastIndex(text, "\n"); i >= 0 {
			text = text[:i]
		} else if !strings.HasSuffix(text, "]") {
			text = ""
		}
	}
	visible := map[string]bool{}
	for _, m := range visiblePattern.FindAllStringSubmatch(text, -1) {
		visible[m[1]] = true
	}
	page.snapshot = version
	page.refs = map[string]refEntry{}
	for ref, entry := range s.refs {
		if visible[ref] {
			page.refs[ref] = entry
		}
	}
	result := O("snapshotId", version, "text", text, "truncated", truncated, "url", page.url())
	if len(s.warnings) > 0 {
		result.Set("warnings", s.warnings)
	}
	return result, nil
}
