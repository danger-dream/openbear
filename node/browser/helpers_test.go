package browser

import (
	"bufio"
	"encoding/json"
	"io"
	"sync"
	"testing"
	"time"
)

// fakeBrowser is a protocol double on the --remote-debugging-pipe framing.
type fakeBrowser struct {
	in       *bufio.Reader
	out      *io.PipeWriter
	wmu      sync.Mutex
	mu       sync.Mutex
	received []map[string]any
	handle   func(f *fakeBrowser, msg map[string]any)
	close    func()
}

type closerFunc func() error

func (c closerFunc) Close() error { return c() }

func newFakeBrowser(handle func(f *fakeBrowser, msg map[string]any)) (Transport, *fakeBrowser) {
	engineIn, browserOut := io.Pipe()
	browserIn, engineOut := io.Pipe()
	f := &fakeBrowser{in: bufio.NewReader(browserIn), out: browserOut, handle: handle}
	var once sync.Once
	f.close = func() {
		once.Do(func() {
			browserOut.Close()
			browserIn.Close()
			engineIn.Close()
			engineOut.Close()
		})
	}
	go f.loop()
	return NewPipe(engineIn, engineOut, closerFunc(func() error { f.close(); return nil })), f
}

func (f *fakeBrowser) loop() {
	for {
		raw, err := f.in.ReadBytes(0)
		if err != nil {
			return
		}
		var msg map[string]any
		if json.Unmarshal(raw[:len(raw)-1], &msg) != nil {
			return
		}
		f.mu.Lock()
		f.received = append(f.received, msg)
		f.mu.Unlock()
		if f.handle != nil {
			f.handle(f, msg)
		} else {
			f.reply(msg, map[string]any{})
		}
	}
}

func (f *fakeBrowser) send(v any) {
	body, _ := json.Marshal(v)
	f.wmu.Lock()
	defer f.wmu.Unlock()
	f.out.Write(append(body, 0))
}

func (f *fakeBrowser) reply(msg map[string]any, result any) {
	f.send(map[string]any{"id": msg["id"], "result": result})
}

func (f *fakeBrowser) methods() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := []string{}
	for _, m := range f.received {
		out = append(out, m["method"].(string))
	}
	return out
}

func (f *fakeBrowser) count(method string) int {
	n := 0
	for _, m := range f.methods() {
		if m == method {
			n++
		}
	}
	return n
}

// testEngine mirrors the Python engine fixture: a configured engine with one
// bound page (session "session", main frame "frame") on a fake browser.
func testEngine(t *testing.T, handle func(f *fakeBrowser, msg map[string]any)) (*Engine, *Page, *fakeBrowser) {
	t.Helper()
	tr, fb := newFakeBrowser(handle)
	e := New()
	e.config = O(
		"maxEventEntries", 2, "maxBodyBytes", 100, "maxArtifactBytes", 1000,
		"snapshotMaxChars", 1000, "downloadHostPath", t.TempDir(),
	)
	e.cdp = newConn(tr, 5*time.Second, e.onEvent)
	e.cdpRef.Store(e.cdp)
	page := newPage(e, "target")
	page.session = "session"
	frame := newFrame(page, "frame", "", page.session)
	frame.url = "https://test.invalid"
	page.frames[frame.id] = frame
	page.mainID = frame.id
	page.sessionRoots[page.session] = frame.id
	e.pages.Set(page.target, page)
	e.sessions[page.session] = page
	t.Cleanup(func() { e.Close(); fb.close() })
	return e, page, fb
}

func child(page *Page, name, session, parent string) *Frame {
	if parent == "" {
		parent = page.mainID
	}
	frame := newFrame(page, name, parent, session)
	frame.context, frame.helper = 7, "helper-"+name
	page.frames[name] = frame
	page.sessionRoots[session] = name
	return frame
}

func raw(v any) json.RawMessage {
	b, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return b
}

func asError(t *testing.T, err error) *Error {
	t.Helper()
	be, ok := err.(*Error)
	if !ok {
		t.Fatalf("expected *Error, got %T %v", err, err)
	}
	return be
}
