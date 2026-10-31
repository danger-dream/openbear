package browser

import (
	"context"
	"encoding/base64"
	"errors"
	"math"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"
	"time"
)

// loop runs fn holding the engine loop lock, as engine code expects.
func loop(e *Engine, fn func()) {
	e.mu.Lock()
	defer e.mu.Unlock()
	fn()
}

func TestRingCursorAndLoss(t *testing.T) {
	r := newRing(2)
	for i := 0; i < 3; i++ {
		r.add(O("value", i))
	}
	if !truthy(r.read(0).Must("dropped")) {
		t.Fatal("loss not reported")
	}
	entries := asList(r.read(2).Must("entries"))
	if len(entries) != 1 || !pyEqual(asObj(entries[0]).Must("value"), 2) {
		t.Fatalf("entries %v", entries)
	}
	if len(asList(r.read(3).Must("entries"))) != 0 {
		t.Fatal("cursor not respected")
	}
	r.trim(1)
	if !pyEqual(r.read(0).Must("cursor"), 3) {
		t.Fatal("cursor changed by trim")
	}
	// rows[-0:] keeps everything, like Python.
	r = newRing(0)
	r.add(O())
	r.add(O())
	if len(r.rows) != 2 {
		t.Fatalf("limit 0 keeps %d", len(r.rows))
	}
}

func TestFrameCoordinateMappingAndClipping(t *testing.T) {
	quad := []float64{100, 100, 300, 100, 300, 300, 100, 300}
	p, err := transform(context.Background(), point{50, 50}, viewportQuad(100, 100), quad)
	if err != nil || math.Abs(p.X-200) > 1e-9 || math.Abs(p.Y-200) > 1e-9 {
		t.Fatalf("transform %v %v", p, err)
	}
	back, err := transform(context.Background(), p, quad, viewportQuad(100, 100))
	if err != nil || math.Abs(back.X-50) > 1e-9 || math.Abs(back.Y-50) > 1e-9 {
		t.Fatalf("inverse %v %v", back, err)
	}
	c, ok := clickablePoint([][]float64{{-10, -10, 20, -10, 20, 20, -10, 20}}, 10, 10)
	if !ok || c.X < 0 || c.X > 10 || c.Y < 0 || c.Y > 10 {
		t.Fatalf("clip %v %v", c, ok)
	}
	if _, ok := clickablePoint([][]float64{{20, 20, 30, 20, 30, 30, 20, 30}}, 10, 10); ok {
		t.Fatal("offscreen quad is clickable")
	}
	_, err = transform(context.Background(), point{1, 1}, viewportQuad(100, 100), []float64{0, 0, 0, 0, 0, 0, 0, 0})
	var be *Error
	if !errors.As(err, &be) || be.Code != "element_not_visible" || be.Outcome != "not_started" {
		t.Fatalf("degenerate quad %v", err)
	}
}

func TestConfigureTrimsLiveEventsAndRequestDetailsWithoutReconnect(t *testing.T) {
	e, page, fb := testEngine(t, nil)
	cdp := e.cdp
	loop(e, func() {
		for i := 0; i < 2; i++ {
			page.obs.console.add(O("text", pyStr(i)))
			page.obs.event("Network.requestWillBeSent", O(
				"requestId", pyStr(i), "request", O("method", "GET", "url", "https://test.invalid"),
			), page.session)
		}
	})
	result, err := e.Execute(context.Background(), "configure", raw(map[string]any{
		"snapshotMaxChars": 77, "maxEventEntries": 1, "maxBodyBytes": 50, "maxArtifactBytes": 80,
	}))
	if err != nil || dumpsASCII(result) != `{"configured": true}` || e.cdp != cdp {
		t.Fatalf("configure %v %v", result, err)
	}
	loop(e, func() {
		if page.obs.records.Len() != 1 || len(page.obs.current) != 1 {
			t.Fatalf("records %d current %d", page.obs.records.Len(), len(page.obs.current))
		}
		if !truthy(page.obs.console.read(0).Must("dropped")) || len(page.obs.network.rows) != 1 {
			t.Fatal("rings not trimmed")
		}
	})
	if len(fb.methods()) != 0 {
		t.Fatalf("configure sent %v", fb.methods())
	}
}

func TestNotInitializedAndMalformedParams(t *testing.T) {
	e := New()
	_, err := e.Execute(context.Background(), "snapshot", raw(map[string]any{}))
	be := asError(t, err)
	if be.Code != "worker_not_initialized" || be.Outcome != "not_started" || be.Details == nil || len(be.Details) != 0 {
		t.Fatalf("%+v", be)
	}
	e2, _, _ := testEngine(t, nil)
	_, err = e2.Execute(context.Background(), "snapshot", []byte(`[1]`))
	be = asError(t, err)
	if be.Code != "browser_operation_failed" || be.Outcome != "unknown" || be.Details != nil {
		t.Fatalf("%+v", be)
	}
}

func TestStaleRefsVersionAndFrameInvalidation(t *testing.T) {
	e, page, _ := testEngine(t, nil)
	frame := page.frames[page.mainID]
	loop(e, func() {
		page.snapshot = "s1"
		page.refs = map[string]refEntry{"e1": {frame.id, frame.epoch}}
		for _, target := range []string{"s2:e1", "e1", "s1:e9"} {
			if _, err := page.resolve(context.Background(), target, true); err == nil || err.(*Error).Code != "stale_ref: take a new snapshot" {
				t.Fatalf("%s resolved: %v", target, err)
			}
		}
		frame.invalidate()
		if page.snapshot != "" || len(page.refs) != 0 {
			t.Fatal("invalidate kept refs")
		}
	})
	_, err := e.Execute(context.Background(), "act", raw(map[string]any{"targetId": "target", "op": "click", "target": "s1:e1"}))
	be := asError(t, err)
	if be.Code != "stale_ref: take a new snapshot" || be.Outcome != "failed" ||
		!reflect.DeepEqual(be.Details, map[string]any{"phase": "act", "effectsPossible": false}) {
		t.Fatalf("%+v", be)
	}
}

func TestIframeSwapInvalidatesRefsButLateOldDetachDoesNotEraseNewSession(t *testing.T) {
	e, page, _ := testEngine(t, nil)
	loop(e, func() {
		frame := child(page, "child", "child-session", "")
		page.snapshot = "s1"
		page.refs = map[string]refEntry{"e1": {frame.id, frame.epoch}}
		epoch := frame.epoch
		page.event("Page.frameDetached", O("frameId", frame.id, "reason", "swap"), page.session)
		if frame.detached || frame.epoch <= epoch || len(page.refs) != 0 {
			t.Fatal("swap not handled")
		}
		page.event("Page.frameNavigated", O("frame", O(
			"id", frame.id, "parentId", page.mainID, "loaderId", "new", "url", "https://other.test",
		)), "new-session")
		frame.helper, frame.context = "new-helper", 8
		page.event("Target.detachedFromTarget", O("sessionId", "child-session"), page.session)
		if frame.session != "new-session" || frame.helper != "new-helper" {
			t.Fatalf("late detach erased new session: %q %q", frame.session, frame.helper)
		}
		if page.frames[page.mainID].epoch != 0 {
			t.Fatal("main frame invalidated")
		}
	})
}

func TestRemovingIframeInvalidatesEntireDescendantSubtree(t *testing.T) {
	e, page, _ := testEngine(t, nil)
	loop(e, func() {
		parent := child(page, "child", "child-session", "")
		nested := child(page, "nested", "nested-session", parent.id)
		page.event("Page.frameDetached", O("frameId", parent.id, "reason", "remove"), page.session)
		if !parent.detached || !nested.detached || parent.helper != "" || nested.helper != "" {
			t.Fatal("subtree not invalidated")
		}
	})
}

func TestContextDestroyedIsScopedToOriginatingSession(t *testing.T) {
	e, page, _ := testEngine(t, nil)
	loop(e, func() {
		frame := child(page, "child", "child-session", "")
		main := page.frames[page.mainID]
		main.context, main.helper = 7, "main-helper"
		page.event("Runtime.executionContextDestroyed", O("executionContextId", 7), frame.session)
		if frame.context != nil || frame.helper != "" {
			t.Fatal("child context kept")
		}
		if !pyEqual(main.context, 7) || main.helper != "main-helper" {
			t.Fatal("main context destroyed by another session's event")
		}
	})
}

func TestSameDocumentNavigationKeepsValidRefAndFinishesPendingNavigation(t *testing.T) {
	e, page, _ := testEngine(t, nil)
	loop(e, func() {
		page.snapshot = "s1"
		page.refs = map[string]refEntry{"e1": {page.mainID, 0}}
		page.pendingNavigation = true
		page.event("Page.navigatedWithinDocument", O("frameId", page.mainID, "url", "https://test.invalid/#next"), page.session)
		if !strings.HasSuffix(asStr(page.url()), "#next") || page.pendingNavigation {
			t.Fatal("same-document navigation not applied")
		}
		if page.snapshot != "s1" || len(page.refs) != 1 {
			t.Fatal("refs invalidated")
		}
	})
}

func TestTimeoutNeverRetriesAndPreservesEffectStatus(t *testing.T) {
	hang := func(f *fakeBrowser, msg map[string]any) {
		if msg["method"] == "Page.navigate" || msg["method"] == "Runtime.evaluate" {
			return
		}
		f.reply(msg, map[string]any{})
	}
	e, _, fb := testEngine(t, hang)
	// Not started: a pure wait.
	_, err := e.Execute(context.Background(), "wait", raw(map[string]any{"targetId": "target", "timeMs": 1000, "timeoutMs": 20}))
	be := asError(t, err)
	if be.Code != "action_timeout" || be.Outcome != "failed" ||
		!reflect.DeepEqual(be.Details, map[string]any{"phase": "wait", "effectsPossible": false}) {
		t.Fatalf("wait %+v", be)
	}
	// Started: evaluate was sent once and its effect is unknown.
	_, err = e.Execute(context.Background(), "evaluate", raw(map[string]any{"targetId": "target", "expression": "1", "timeoutMs": 30}))
	be = asError(t, err)
	if be.Code != "action_timeout" || be.Outcome != "unknown" || be.Details["phase"] != "evaluate" || be.Details["effectsPossible"] != true {
		t.Fatalf("evaluate %+v", be)
	}
	// Navigation: navigation_timeout carries the navigation state.
	_, err = e.Execute(context.Background(), "navigate", raw(map[string]any{"targetId": "target", "url": "https://x.test", "timeoutMs": 30}))
	be = asError(t, err)
	nav, _ := be.Details["navigation"].(*Obj)
	if be.Code != "navigation_timeout" || be.Outcome != "unknown" || be.Details["phase"] != "navigation_response" ||
		nav == nil || dumpsASCII(nav) != `{"state": "requested", "committed": false}` {
		t.Fatalf("navigate %+v", be)
	}
	if fb.count("Runtime.evaluate") != 1 || fb.count("Page.navigate") != 1 {
		t.Fatalf("retried: %v", fb.methods())
	}
	// An already expired budget never starts.
	_, err = e.Execute(context.Background(), "evaluate", raw(map[string]any{"targetId": "target", "expression": "1", "timeoutMs": 0}))
	be = asError(t, err)
	if be.Code != "action_timeout" || be.Outcome != "failed" || fb.count("Runtime.evaluate") != 1 {
		t.Fatalf("expired %+v", be)
	}
}

func TestDialogInterruptsInputWithoutReplayAndHandleIsExplicit(t *testing.T) {
	var e *Engine
	var page *Page
	e, page, fb := testEngine(t, func(f *fakeBrowser, msg map[string]any) {
		if strings.HasPrefix(msg["method"].(string), "Input.") {
			f.send(map[string]any{"method": "Page.javascriptDialogOpening", "sessionId": "session",
				"params": map[string]any{"type": "prompt", "message": "value?", "defaultPrompt": "x"}})
			return // the input never acknowledges while the modal is open
		}
		f.reply(msg, map[string]any{})
	})
	loop(e, func() {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		if _, err := page.input(ctx, "Input.dispatchMouseEvent", O("type", "mouseReleased")); !errors.Is(err, errDialogOpened) {
			t.Fatalf("input: %v", err)
		}
	})
	result, err := e.Execute(context.Background(), "dialog", raw(map[string]any{"targetId": "target", "op": "inspect"}))
	if err != nil || dumpsASCII(result) != `{"dialog": {"type": "prompt", "message": "value?", "defaultValue": "x"}}` {
		t.Fatalf("inspect %v %v", dumpsASCII(result), err)
	}
	_, err = e.Execute(context.Background(), "snapshot", raw(map[string]any{"targetId": "target"}))
	if be := asError(t, err); be.Code != "dialog_pending: inspect or handle the dialog" || be.Outcome != "failed" {
		t.Fatalf("%+v", be)
	}
	// console/network stay available while a dialog is pending.
	if _, err := e.Execute(context.Background(), "console", raw(map[string]any{"targetId": "target"})); err != nil {
		t.Fatal(err)
	}
	result, err = e.Execute(context.Background(), "dialog", raw(map[string]any{"targetId": "target", "op": "handle", "accept": true, "text": "answer"}))
	if err != nil || dumpsASCII(result) != `{"handled": true}` {
		t.Fatalf("handle %v %v", result, err)
	}
	loop(e, func() {
		if page.obs.dialog != nil {
			t.Fatal("dialog kept")
		}
	})
	if fb.count("Input.dispatchMouseEvent") != 1 {
		t.Fatalf("input replayed: %v", fb.methods())
	}
	fb.mu.Lock()
	last := fb.received[len(fb.received)-1]
	fb.mu.Unlock()
	if last["method"] != "Page.handleJavaScriptDialog" || !reflect.DeepEqual(last["params"], map[string]any{"accept": true, "promptText": "answer"}) {
		t.Fatalf("last %v", last)
	}
}

func TestReleaseNotSentBeforeDialogIsDeferredOnce(t *testing.T) {
	e, page, fb := testEngine(t, nil)
	loop(e, func() {
		page.obs.event("Page.javascriptDialogOpening", O("type", "alert", "message", "pause"), page.session)
		if _, err := page.input(context.Background(), "Input.dispatchKeyEvent", O("type", "keyUp", "key", "Enter")); !errors.Is(err, errDialogOpened) {
			t.Fatalf("input: %v", err)
		}
	})
	if len(fb.methods()) != 0 {
		t.Fatal("input sent during modal")
	}
	if _, err := e.Execute(context.Background(), "dialog", raw(map[string]any{"targetId": "target", "op": "handle", "accept": true})); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(fb.methods(), []string{"Page.handleJavaScriptDialog", "Input.dispatchKeyEvent"}) {
		t.Fatalf("methods %v", fb.methods())
	}
	loop(e, func() {
		if len(page.deferredInputs) != 0 {
			t.Fatal("deferred inputs kept")
		}
	})
}

func TestEvaluateSentToBrowserAndLargeResultIsArtifact(t *testing.T) {
	e, _, fb := testEngine(t, func(f *fakeBrowser, msg map[string]any) {
		f.reply(msg, map[string]any{"result": map[string]any{"value": map[string]any{"answer": 42}}})
	})
	result, err := e.Execute(context.Background(), "evaluate", raw(map[string]any{
		"targetId": "target", "expression": `function(){return fetch("/submit",{method:"POST"})}`,
	}))
	if err != nil || dumpsASCII(result) != `{"value": {"answer": 42}}` {
		t.Fatalf("%v %v", result, err)
	}
	fb.mu.Lock()
	sent := fb.received[len(fb.received)-1]
	fb.mu.Unlock()
	if sent["method"] != "Runtime.evaluate" || !strings.Contains(sent["params"].(map[string]any)["expression"].(string), `function(){return fetch`) {
		t.Fatalf("sent %v", sent)
	}
	loop(e, func() { e.config.Set("snapshotMaxChars", 3) })
	out := filepath.Join(t.TempDir(), "value.json")
	result, err = e.Execute(context.Background(), "evaluate", raw(map[string]any{"targetId": "target", "expression": "42", "outputPath": out}))
	if err != nil {
		t.Fatal(err)
	}
	body, _ := os.ReadFile(out)
	if pyStr(asObj(result).Must("artifact")) != out || string(body) != `{"answer":42}` || asObj(result).Has("value") {
		t.Fatalf("%v %q", dumpsASCII(result), body)
	}
	info, _ := os.Stat(out)
	if runtime.GOOS != "windows" && info.Mode().Perm() != 0o600 { // Windows has no mode bits: the directory's ACL protects it.
		t.Fatalf("artifact mode %v", info.Mode())
	}
	// A JavaScript exception reports only a code, never source or message.
	e2, _, _ := testEngine(t, func(f *fakeBrowser, msg map[string]any) {
		f.reply(msg, map[string]any{"result": map[string]any{}, "exceptionDetails": map[string]any{"text": "secret source"}})
	})
	_, err = e2.Execute(context.Background(), "evaluate", raw(map[string]any{"targetId": "target", "expression": "secret()"}))
	be := asError(t, err)
	if be.Code != "javascript_error" || be.Outcome != "unknown" || strings.Contains(dumpsASCII(be.Details), "secret") {
		t.Fatalf("%+v", be)
	}
}

func TestNetworkExtraHeadersBodiesLimitsAndEventCursors(t *testing.T) {
	body := base64.StdEncoding.EncodeToString([]byte("raw"))
	e, page, fb := testEngine(t, func(f *fakeBrowser, msg map[string]any) {
		f.reply(msg, map[string]any{"body": body, "base64Encoded": true})
	})
	dir := t.TempDir()
	ctx := context.Background()
	loop(e, func() {
		obs := page.obs
		obs.event("Network.requestWillBeSentExtraInfo", O("requestId", "r", "headers", O("X-Actual", "yes")), page.session)
		obs.event("Network.requestWillBeSent", O("requestId", "r", "request", O(
			"method", "POST", "url", "https://test.invalid/data", "postData", "request")), page.session)
		obs.event("Network.responseReceived", O("requestId", "r", "response", O(
			"url", "https://test.invalid/data", "status", 200, "headers", O("Content-Length", "101"))), page.session)
		headers, err := obs.details(ctx, O("id", 1, "part", "request-headers"))
		if err != nil || dumpsASCII(asObj(headers).Must("headers")) != `{"x-actual": "yes"}` {
			t.Fatalf("headers %v %v", headers, err)
		}
		if _, err := obs.details(ctx, O("id", 1, "part", "response-body")); err == nil || err.(*Error).Code != "network_body_too_large" {
			t.Fatalf("expected too large: %v", err)
		}
		if len(fb.methods()) != 0 {
			t.Fatal("body fetched despite limit")
		}
		result, err := obs.details(ctx, O("id", 1, "part", "request-body", "outputPath", filepath.Join(dir, "request")))
		if err != nil {
			t.Fatal(err)
		}
		if data, _ := os.ReadFile(asStr(asObj(result).Must("artifact"))); string(data) != "request" {
			t.Fatalf("request body %q", data)
		}
		obs.event("Network.responseReceivedExtraInfo", O("requestId", "r", "headers", O("Content-Length", "3")), page.session)
		obs.event("Network.loadingFinished", O("requestId", "r"), page.session)
		result, err = obs.details(ctx, O("id", 1, "part", "response-body", "outputPath", filepath.Join(dir, "body")))
		if err != nil {
			t.Fatal(err)
		}
		if data, _ := os.ReadFile(asStr(asObj(result).Must("artifact"))); string(data) != "raw" {
			t.Fatalf("response body %q", data)
		}
		obs.consoleReady[page.session] = true
		for i := 0; i < 3; i++ {
			obs.event("Console.messageAdded", O("message", O("source", "console-api", "text", pyStr(i))), page.session)
		}
		if !truthy(obs.console.read(0).Must("dropped")) || !pyEqual(asObj(asList(obs.console.read(2).Must("entries"))[0]).Must("text"), "2") {
			t.Fatal("console cursor")
		}
	})
}

func TestDownloadUsesOriginalGUIDBytesAndRemoteMappingIsExplicit(t *testing.T) {
	e, page, fb := testEngine(t, nil)
	host := asStr(e.config.Must("downloadHostPath"))
	out := filepath.Join(t.TempDir(), "saved")
	guid := "6b1f3c1e-0d2a-4c4e-9a51-1f2e3d4c5b6a"
	loop(e, func() {
		e.files.event("Browser.downloadWillBegin", O("frameId", page.mainID, "guid", guid, "suggestedFilename", "original.bin"))
		e.files.event("Browser.downloadProgress", O("guid", guid, "state", "completed", "receivedBytes", 3))
		params := O("op", "save", "id", 1, "outputPath", out)
		if _, err := e.files.execute(context.Background(), page, params); err == nil ||
			err.(*Error).Code != "download_file_unavailable: configure shared download paths for a remote browser" {
			t.Fatalf("expected unavailable: %v", err)
		}
		os.WriteFile(filepath.Join(host, guid), []byte("raw"), 0o600)
		result, err := e.files.execute(context.Background(), page, params)
		if err != nil || !pyEqual(asObj(result).Must("bytes"), 3) {
			t.Fatalf("%v %v", result, err)
		}
	})
	if data, _ := os.ReadFile(out); string(data) != "raw" || len(fb.methods()) != 0 {
		t.Fatalf("%q %v", data, fb.methods())
	}
	// Unowned frames keep their download without being adopted.
	loop(e, func() {
		e.files.event("Browser.downloadWillBegin", O("frameId", "other", "guid", "7c2a4d2f-1e3b-4d5f-8b62-2a3f4e5d6c7b", "suggestedFilename", "x"))
		if page.downloads.Len() != 1 {
			t.Fatal("unowned download adopted")
		}
		e.block(e.wg.Wait)
	})
	if fb.count("Browser.cancelDownload") != 0 {
		t.Fatal("unowned download cancelled")
	}
}

// waitMethod waits for the fake browser to receive method.
func waitMethod(t *testing.T, fb *fakeBrowser, method string) {
	t.Helper()
	for deadline := time.Now().Add(2 * time.Second); fb.count(method) == 0; {
		if time.Now().After(deadline) {
			t.Fatalf("%s not sent: %v", method, fb.methods())
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func TestUseTransportInitializeWithoutEndpointAndCallsShareTheConnection(t *testing.T) {
	release := make(chan struct{})
	tr, fb := newFakeBrowser(func(f *fakeBrowser, msg map[string]any) {
		switch msg["method"] {
		case "Target.attachToTarget":
			f.reply(msg, map[string]any{"sessionId": "probe"})
		case "Page.enable":
			// Engine's own page binding hangs until released.
			go func() { <-release; f.reply(msg, map[string]any{}) }()
		case "Runtime.evaluate":
			f.reply(msg, map[string]any{"result": map[string]any{"value": msg["sessionId"]}})
		default:
			f.reply(msg, map[string]any{"product": "Fake/1"})
		}
	})
	e := New()
	defer fb.close()
	defer e.Close()
	e.UseTransport(tr)
	result, err := e.Execute(context.Background(), "initialize", raw(map[string]any{
		"downloadHostPath": t.TempDir(), "snapshotMaxChars": 1000, "maxEventEntries": 10,
		"maxBodyBytes": 100, "maxArtifactBytes": 1000,
	}))
	if err != nil || dumpsASCII(result) != `{"connected": true, "engine": "go-cdp"}` {
		t.Fatalf("%v %v", result, err)
	}
	if !reflect.DeepEqual(fb.methods(), []string{"Browser.getVersion", "Browser.setDownloadBehavior", "Target.setDiscoverTargets"}) {
		t.Fatalf("methods %v", fb.methods())
	}
	// An Execute blocked on the browser does not block raw calls.
	done := make(chan error, 1)
	go func() {
		_, err := e.Execute(context.Background(), "snapshot", raw(map[string]any{"targetId": "T", "timeoutMs": 5000}))
		done <- err
	}()
	for fb.count("Page.enable") == 0 {
		time.Sleep(time.Millisecond)
	}
	version, err := e.Call(context.Background(), "Browser.getVersion", nil)
	if err != nil || string(version) != `{"product":"Fake/1"}` {
		t.Fatalf("Call %s %v", version, err)
	}
	value, err := e.TargetCall(context.Background(), "T2", "Runtime.evaluate", raw(map[string]any{"expression": "1"}))
	if err != nil || string(value) != `{"result":{"value":"probe"}}` {
		t.Fatalf("TargetCall %s %v", value, err)
	}
	for fb.count("Target.detachFromTarget") == 0 {
		time.Sleep(time.Millisecond)
	}
	close(release)
	<-done
}

func TestConcurrentExecuteCallAndEvents(t *testing.T) {
	e, page, fb := testEngine(t, func(f *fakeBrowser, msg map[string]any) {
		f.send(map[string]any{"method": "Console.messageAdded", "sessionId": "session",
			"params": map[string]any{"message": map[string]any{"source": "console-api", "text": "x"}}})
		f.reply(msg, map[string]any{"result": map[string]any{"value": 1}})
	})
	loop(e, func() { page.obs.consoleReady["session"] = true })
	done := make(chan error, 60)
	for i := 0; i < 20; i++ {
		go func() {
			_, err := e.Execute(context.Background(), "evaluate", raw(map[string]any{"targetId": "target", "expression": "1"}))
			done <- err
		}()
		go func() {
			_, err := e.Execute(context.Background(), "console", raw(map[string]any{"targetId": "target"}))
			done <- err
		}()
		go func() {
			_, err := e.Call(context.Background(), "Browser.getVersion", nil)
			done <- err
		}()
	}
	for i := 0; i < 60; i++ {
		if err := <-done; err != nil {
			t.Fatal(err)
		}
	}
	if fb.count("Runtime.evaluate") != 20 {
		t.Fatalf("evaluate count %d", fb.count("Runtime.evaluate"))
	}
}

func TestRawCallErrorsAreFormattedLikeCDPModule(t *testing.T) {
	tr, fb := newFakeBrowser(func(f *fakeBrowser, msg map[string]any) {
		if msg["method"] == "Target.closeTarget" {
			f.send(map[string]any{"id": msg["id"], "error": map[string]any{"message": "No target with given id"}})
			return
		}
		f.reply(msg, map[string]any{})
	})
	defer fb.close()
	e := New()
	defer e.Close()
	e.UseTransport(tr)
	if _, err := e.Call(context.Background(), "Browser.getVersion", nil); !errors.Is(err, ErrDisconnected) {
		t.Fatalf("before initialize: %v", err)
	}
	if _, err := e.Execute(context.Background(), "initialize", raw(map[string]any{"downloadHostPath": t.TempDir()})); err != nil {
		t.Fatal(err)
	}
	_, err := e.Call(context.Background(), "Target.closeTarget", raw(map[string]any{"targetId": "x"}))
	var ce *CommandError
	if !errors.As(err, &ce) || err.Error() != "cdp_command_failed: No target with given id" {
		t.Fatalf("%T %v", err, err)
	}
}
