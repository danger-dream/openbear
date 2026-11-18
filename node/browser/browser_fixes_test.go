package browser

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func actionFixtureReply(f *fakeBrowser, msg map[string]any) {
	p, _ := msg["params"].(map[string]any)
	result := map[string]any{}
	switch msg["method"] {
	case "DOM.getContentQuads":
		result["quads"] = [][]int{{0, 0, 10, 0, 10, 10, 0, 10}}
	case "Runtime.callFunctionOn":
		fn, _ := p["functionDeclaration"].(string)
		remote := map[string]any{"value": nil}
		switch {
		case strings.Contains(fn, "__query"):
			args := p["arguments"].([]any)
			if args[0].(map[string]any)["value"] != "css=#missing" {
				remote["objectId"] = "node"
			}
		case strings.Contains(fn, "isConnected"):
			remote["value"] = true
		case strings.Contains(fn, "innerWidth"):
			remote["value"] = map[string]any{"width": 100, "height": 100}
		case strings.Contains(fn, "expectHitTarget"), strings.Contains(fn, "this.stop()"):
			remote["value"] = "done"
		}
		result["result"] = remote
	}
	f.reply(msg, result)
}

func TestBrowserFix01DragTimeoutReleasesWithoutReplayingPress(t *testing.T) {
	e, p, fb := testEngine(t, actionFixtureReply)
	loop(e, func() { p.frames[p.mainID].helper = "helper" })
	_, err := e.Execute(context.Background(), "act", raw(map[string]any{"targetId": "target", "op": "drag", "target": "css=#source", "to": "css=#missing", "timeoutMs": 80}))
	be := asError(t, err)
	if be.Code != "action_timeout" || be.Outcome != "unknown" {
		t.Fatalf("%+v", be)
	}
	fb.mu.Lock()
	defer fb.mu.Unlock()
	counts := map[string]int{}
	for _, m := range fb.received {
		if m["method"] == "Input.dispatchMouseEvent" {
			counts[m["params"].(map[string]any)["type"].(string)]++
		}
	}
	if counts["mousePressed"] != 1 || counts["mouseReleased"] != 1 {
		t.Fatalf("input replay/missing cleanup: %v", counts)
	}
}

func TestBrowserFix01KeyTimeoutReleasesKeyAndModifiers(t *testing.T) {
	e, p, fb := testEngine(t, func(f *fakeBrowser, m map[string]any) {
		if m["method"] == "Input.dispatchKeyEvent" {
			p := m["params"].(map[string]any)
			if p["key"] == "a" && p["type"] != "keyUp" {
				return
			}
		}
		f.reply(m, map[string]any{})
	})
	_, err := e.Execute(context.Background(), "act", raw(map[string]any{"targetId": "target", "op": "press", "key": "Control+a", "timeoutMs": 40}))
	if be := asError(t, err); be.Outcome != "unknown" {
		t.Fatalf("%+v", be)
	}
	loop(e, func() {
		if len(p.pressed) != 0 || len(p.modifiers) != 0 {
			t.Fatal("pressed input retained")
		}
	})
	fb.mu.Lock()
	defer fb.mu.Unlock()
	downs, ups := 0, 0
	for _, m := range fb.received {
		if m["method"] == "Input.dispatchKeyEvent" {
			if m["params"].(map[string]any)["type"] == "keyUp" {
				ups++
			} else {
				downs++
			}
		}
	}
	if downs != 2 || ups != 2 {
		t.Fatalf("downs %d ups %d", downs, ups)
	}
}

func TestBrowserFix01UploadTimeoutDeletesTransfer(t *testing.T) {
	chunks, cleans, finishes := 0, 0, 0
	e, p, _ := testEngine(t, func(f *fakeBrowser, m map[string]any) {
		if m["method"] == "Runtime.callFunctionOn" {
			fn := m["params"].(map[string]any)["functionDeclaration"].(string)
			if strings.Contains(fn, "__fileChunk") {
				chunks++
				return
			}
			if strings.Contains(fn, "__fileTransfers.delete") {
				cleans++
			}
			if strings.Contains(fn, "__finishFiles") {
				finishes++
			}
		}
		actionFixtureReply(f, m)
	})
	loop(e, func() { p.frames[p.mainID].helper = "helper" })
	path := filepath.Join(t.TempDir(), "upload.txt")
	if err := os.WriteFile(path, []byte("one chunk"), 0600); err != nil {
		t.Fatal(err)
	}
	_, err := e.Execute(context.Background(), "files", raw(map[string]any{"targetId": "target", "op": "upload", "target": "css=#file", "paths": []string{path}, "timeoutMs": 40}))
	if be := asError(t, err); be.Code != "action_timeout" {
		t.Fatalf("%+v", be)
	}
	// The cleanup reply synchronizes with the handler's writes.
	if chunks != 1 || cleans != 1 || finishes != 0 {
		t.Fatalf("chunks=%d cleans=%d finishes=%d", chunks, cleans, finishes)
	}
}

func TestBrowserFix01ExpiredGuardCleanupStillStopsInterceptor(t *testing.T) {
	e, p, fb := testEngine(t, actionFixtureReply)
	loop(e, func() {
		ctx, cancel := context.WithCancel(withOp(context.Background(), &operation{deadline: time.Now().Add(-time.Second), started: true}))
		cancel()
		guard := &Node{frame: p.frames[p.mainID], objectID: "guard", epoch: 0}
		if err := (&actions{p}).stopGuards(ctx, []*Node{guard}, false); err != nil {
			t.Fatal(err)
		}
	})
	if fb.count("Runtime.callFunctionOn") != 1 || fb.count("Runtime.releaseObject") != 1 {
		t.Fatal("interceptor cleanup was not sent")
	}
}

type signaledWriter struct {
	io.Writer
	entered chan struct{}
}

func (w signaledWriter) Write(b []byte) (int, error) {
	select {
	case w.entered <- struct{}{}:
	default:
	}
	return w.Writer.Write(b)
}

func TestBrowserFix02QueuedCancelPreservesHealthyPipe(t *testing.T) {
	r, w := io.Pipe()
	entered := make(chan struct{}, 2)
	p := NewPipe(bytes.NewReader(nil), signaledWriter{w, entered}, closerFunc(func() error { r.Close(); return w.Close() })).(*pipeTransport)
	defer p.Close()
	first := make(chan error, 1)
	go func() { first <- p.Send(context.Background(), []byte("first")) }()
	<-entered
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := p.Send(ctx, []byte("cancelled")); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	ctx, cancel = context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	if err := p.Send(ctx, []byte("queued")); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal(err)
	}
	select {
	case <-p.closed:
		t.Fatal("queued cancel closed active writer")
	default:
	}
	data := make([]byte, 6)
	if _, err := io.ReadFull(r, data); err != nil {
		t.Fatal(err)
	}
	if err := <-first; err != nil || string(data) != "first\x00" {
		t.Fatalf("%q %v", data, err)
	}
	go func() { _, _ = io.ReadFull(r, data) }()
	if err := p.Send(context.Background(), []byte("again")); err != nil {
		t.Fatal(err)
	}
}

func TestBrowserFix02BlockedOSPipeExpiresAndInvalidates(t *testing.T) {
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	p := NewPipe(r, w, closerFunc(func() error { r.Close(); return w.Close() })).(*pipeTransport)
	defer p.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 40*time.Millisecond)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- p.Send(ctx, make([]byte, 2<<20)) }()
	select {
	case err := <-done:
		if !errors.Is(err, errWriteInterrupted) {
			t.Fatal(err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("pipe Write did not unblock")
	}
	select {
	case <-p.closed:
	default:
		t.Fatal("partial transport still alive")
	}
	if err := p.Send(context.Background(), []byte("never replay")); err == nil {
		t.Fatal("accepted next frame")
	}
}

// A browser may consume a complete frame and emit its modal event before the
// writer goroutine is scheduled to return. Hold that exact boundary explicitly.
type heldReturnWriter struct {
	io.Writer
	written chan struct{}
	release <-chan struct{}
}

func (w heldReturnWriter) Write(b []byte) (int, error) {
	n, err := w.Writer.Write(b)
	select {
	case w.written <- struct{}{}:
	default:
	}
	<-w.release
	return n, err
}

type captureSendContext struct {
	Transport
	seen chan context.Context
}

func (t captureSendContext) Send(ctx context.Context, b []byte) error {
	select {
	case t.seen <- ctx:
	default:
	}
	return t.Transport.Send(ctx, b)
}

func TestBrowserFix02LocalReplyStopDuringWriteReturnKeepsConnection(t *testing.T) {
	e := New()
	transport, fb := newFakeBrowser(func(f *fakeBrowser, msg map[string]any) {
		if msg["method"] == "Input.dispatchMouseEvent" {
			f.send(map[string]any{"method": "Page.javascriptDialogOpening", "sessionId": "session",
				"params": map[string]any{"type": "alert", "message": "already consumed"}})
			return // the modal blocks the reply, not the completed command frame
		}
		f.reply(msg, map[string]any{})
	})
	release := make(chan struct{})
	defer func() {
		select {
		case <-release:
		default:
			close(release)
		}
	}()
	written := make(chan struct{}, 1)
	seen := make(chan context.Context, 2)
	pipe := transport.(*pipeTransport)
	pipe.w = heldReturnWriter{pipe.w, written, release}
	e.cdp = newConn(captureSendContext{pipe, seen}, 3*time.Second, e.onEvent)
	e.cdpRef.Store(e.cdp)
	t.Cleanup(func() { e.Close(); fb.close() })
	var command *pendingCall
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	loop(e, func() {
		command = e.start(ctx, "Input.dispatchMouseEvent", O("type", "mouseReleased"), "session")
	})
	select {
	case <-written:
	case <-ctx.Done():
		t.Fatal("frame not consumed")
	}
	sendCtx := <-seen
	// This is the same local-stop signal used by Page.input on dialog opening.
	// It must not propagate to the physical writer even before Send has returned.
	command.cancel()
	if sendCtx.Err() != nil {
		t.Fatal("local reply stop cancelled physical writing")
	}
	select {
	case <-pipe.closed:
		t.Fatal("healthy frame invalidated")
	default:
	}
	close(release)
	loop(e, func() { command.stop(e) })
	if r := <-command.ch; !errors.Is(r.err, errCancelled) {
		t.Fatalf("reply wait: %v", r.err)
	}
	loop(e, func() {
		if _, err := e.call(ctx, "Page.handleJavaScriptDialog", O("accept", true), "session"); err != nil {
			t.Fatal(err)
		}
	})
	if !e.Connected() || fb.count("Input.dispatchMouseEvent") != 1 || fb.count("Page.handleJavaScriptDialog") != 1 {
		t.Fatalf("disconnected or replayed: %v", fb.methods())
	}
}

func TestBrowserFix02LocalReplyStopStillHonorsRealWriteCancellation(t *testing.T) {
	for _, explicit := range []bool{false, true} {
		t.Run(map[bool]string{false: "deadline", true: "cancel"}[explicit], func(t *testing.T) {
			e, page, _ := testEngine(t, nil)
			r, w, err := os.Pipe()
			if err != nil {
				t.Fatal(err)
			}
			defer r.Close()
			defer w.Close()
			entered := make(chan struct{}, 1)
			ctx, cancel := context.WithTimeout(context.Background(), time.Second)
			defer cancel()
			var command *pendingCall
			var pipe *pipeTransport
			loop(e, func() {
				pipe = e.cdp.t.(*pipeTransport)
				oldCloser := pipe.closer
				pipe.closer = closerFunc(func() error { r.Close(); w.Close(); return oldCloser.Close() })
				pipe.w = signaledWriter{w, entered}
				command = e.start(ctx, "Runtime.evaluate", O("expression", strings.Repeat("x", 2<<20)), page.session)
			})
			select {
			case <-entered:
			case <-time.After(3 * time.Second):
				t.Fatal("write not entered")
			}
			command.cancel() // only reply waiting; the actual write remains bounded
			if explicit {
				cancel()
			}
			select {
			case <-command.done:
			case <-time.After(3 * time.Second):
				t.Fatal("writer not joined")
			}
			if r := <-command.ch; !errors.Is(r.err, errWriteInterrupted) {
				t.Fatalf("lost write uncertainty: %v", r.err)
			}
			select {
			case <-pipe.closed:
			default:
				t.Fatal("interrupted frame remained usable")
			}
		})
	}
}

type shortWriter struct{}

func (shortWriter) Write(b []byte) (int, error) { return len(b) / 2, nil }
func TestBrowserFix02ShortFrameInvalidates(t *testing.T) {
	p := NewPipe(bytes.NewReader(nil), shortWriter{}, nil).(*pipeTransport)
	if err := p.Send(context.Background(), []byte("command")); !errors.Is(err, errWriteInterrupted) {
		t.Fatal(err)
	}
	select {
	case <-p.closed:
	default:
		t.Fatal("short frame remained usable")
	}
}

func TestBrowserFix02PartialCommandReportsUnknownWithoutReplay(t *testing.T) {
	e, _, fb := testEngine(t, nil)
	loop(e, func() { e.cdp.t.(*pipeTransport).w = shortWriter{} })
	_, err := e.Execute(context.Background(), "evaluate", raw(map[string]any{"targetId": "target", "expression": "oneEffect()"}))
	be := asError(t, err)
	if be.Code != "browser_connection_lost" || be.Outcome != "unknown" || be.Details["effectsPossible"] != true {
		t.Fatalf("%+v", be)
	}
	if fb.count("Runtime.evaluate") != 0 {
		t.Fatal("command replayed onto another transport")
	}
}

func TestBrowserFix03IframeChurnReclaimsMapsAndTasks(t *testing.T) {
	e, p, _ := testEngine(t, nil)
	for i := 0; i < 50; i++ {
		loop(e, func() {
			sid, fid := fmt.Sprintf("s%d", i), fmt.Sprintf("f%d", i)
			frame := child(p, fid, sid, "")
			p.sessionTargets[sid] = fid
			e.sessions[sid] = p
			p.observerSessions.Set(sid, "obs"+sid)
			e.sessions["obs"+sid] = p
			p.obs.consoleReady["obs"+sid] = true
			p.scripts[sid] = "script"
			p.failedSessions[sid] = true
			p.bindingLocks[sid] = newALock()
			p.setupTasks.Set(sid, e.spawn(func(ctx context.Context) error { e.block(func() { <-ctx.Done() }); return ctx.Err() }))
			p.bindingTasks[sid] = e.spawn(func(ctx context.Context) error { e.block(func() { <-ctx.Done() }); return ctx.Err() })
			p.life.Set(lifeKey{fid, "loader"}, map[string]bool{"load": true})
			p.navigationStatus.Set(lifeKey{fid, "loader"}, 200)
			p.event("Page.frameDetached", O("frameId", fid, "reason", "remove"), p.session)
			if !frame.detached {
				t.Fatal("old references remain live")
			}
			e.block(e.wg.Wait)
			if len(p.frames) != 1 || len(p.sessionRoots) != 1 || len(p.sessionTargets) != 0 || p.setupTasks.Len() != 0 || len(p.bindingTasks) != 0 || p.observerSessions.Len() != 0 || len(p.scripts) != 0 || len(p.failedSessions) != 0 || len(p.bindingLocks) != 0 || len(p.obs.consoleReady) != 0 || len(e.sessions) != 1 || p.life.Len() != 0 || p.navigationStatus.Len() != 0 || len(e.tasks) != 0 {
				t.Fatalf("iteration %d leaked frame/session metadata", i)
			}
		})
	}
}

func TestBrowserFix03RemovedFrameCannotBeResurrectedBySetupReply(t *testing.T) {
	e, p, _ := testEngine(t, func(f *fakeBrowser, m map[string]any) {
		if m["method"] == "Page.getFrameTree" {
			f.send(map[string]any{"method": "Page.frameDetached", "sessionId": "session", "params": map[string]any{"frameId": "child", "reason": "remove"}})
			f.reply(m, map[string]any{"frameTree": map[string]any{"frame": map[string]any{"id": "child"}}})
			return
		}
		f.reply(m, map[string]any{})
	})
	loop(e, func() {
		// No frame has been registered yet: removal precedes getFrameTree.
		p.event("Target.attachedToTarget", O("sessionId", "child-session", "targetInfo", O("type", "iframe", "targetId", "child")), p.session)
		e.block(e.wg.Wait)
		if p.frames["child"] != nil || len(p.sessionRoots) != 1 || p.setupTasks.Len() != 0 || len(p.failedSessions) != 0 || len(e.sessions) != 1 {
			t.Fatal("late setup restored removed state")
		}
	})
}

func TestBrowserFix03LateOldRemovePreservesSwappedSession(t *testing.T) {
	e, p, _ := testEngine(t, nil)
	loop(e, func() {
		frame := child(p, "child", "old", "")
		p.event("Page.frameDetached", O("frameId", "child", "reason", "swap"), p.session)
		p.updateFrame(O("id", "child", "parentId", p.mainID), "new", nil)
		frame.helper = "new-helper"
		p.sessionRoots["new"] = frame.id
		p.event("Page.frameDetached", O("frameId", "child", "reason", "remove"), "old")
		p.event("Target.detachedFromTarget", O("sessionId", "old"), p.session)
		if p.frames["child"] != frame || frame.session != "new" || frame.helper != "new-helper" || p.sessionRoots["new"] != frame.id {
			t.Fatal("late detach erased new owner")
		}
	})
}

func TestBrowserFix04CompletedRechecksBothQuotas(t *testing.T) {
	for _, total := range []bool{false, true} {
		t.Run(fmt.Sprint(total), func(t *testing.T) {
			old := downloadQuota
			defer func() { downloadQuota = old }()
			downloadQuota = 1000
			e, p, _ := testEngine(t, nil)
			dir := asStr(e.config.Must("downloadHostPath"))
			size := 1001
			if total {
				size = 700
				os.WriteFile(filepath.Join(dir, guidB), make([]byte, 400), 0600)
			}
			loop(e, func() {
				e.files.event("Browser.downloadWillBegin", O("frameId", p.mainID, "guid", guidA, "suggestedFilename", "a"))
				os.WriteFile(filepath.Join(dir, guidA), make([]byte, size), 0600)
				e.files.event("Browser.downloadProgress", O("guid", guidA, "state", "completed", "receivedBytes", size))
				e.block(e.wg.Wait)
				record := e.files.byGUID[guidA]
				if !pyEqual(record.state, "canceled") || total && !record.overQuota || !total && !record.tooLarge {
					t.Fatalf("%+v", record)
				}
			})
			if _, err := os.Stat(filepath.Join(dir, guidA)); !os.IsNotExist(err) {
				t.Fatal("completed oversized file kept", err)
			}
			if total {
				if info, err := os.Stat(filepath.Join(dir, guidB)); err != nil || info.Size() != 400 {
					t.Fatal("unowned file altered")
				}
			}
		})
	}
}

func TestBrowserFix04CancelCompletedInterleaveAndReplacement(t *testing.T) {
	e, p, _ := testEngine(t, func(f *fakeBrowser, m map[string]any) {
		if m["method"] == "Browser.cancelDownload" {
			f.send(map[string]any{"method": "Browser.downloadProgress", "params": map[string]any{"guid": guidA, "state": "completed", "receivedBytes": 1001}})
		}
		f.reply(m, map[string]any{})
	})
	dir := asStr(e.config.Must("downloadHostPath"))
	path := filepath.Join(dir, guidA)
	loop(e, func() {
		e.files.event("Browser.downloadWillBegin", O("frameId", p.mainID, "guid", guidA, "suggestedFilename", "a"))
		os.WriteFile(path, make([]byte, 1001), 0600)
		r := e.files.byGUID[guidA]
		r.tooLarge = true
		if err := e.files.cancel(context.Background(), r); err != nil {
			t.Fatal(err)
		}
		e.block(e.wg.Wait)
		if _, err := os.Stat(path); !os.IsNotExist(err) {
			t.Fatal("completion raced past cancellation")
		}
		os.WriteFile(path, []byte("mine"), 0600)
		r.state = "completed"
		r.received = 4
		r.file = nil
		f, err := e.files.openDownload(r)
		if err != nil {
			t.Fatal(err)
		}
		f.Close()
		os.Rename(path, path+".old")
		os.WriteFile(path, []byte("user"), 0600)
		if err := e.files.cancel(context.Background(), r); err == nil {
			t.Fatal("replacement accepted")
		}
		if data, _ := os.ReadFile(path); string(data) != "user" {
			t.Fatal("replacement deleted")
		}
	})
}
