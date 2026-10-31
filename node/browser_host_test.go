package node

import (
	"bufio"
	"context"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"openbear/node/browser"
)

type testCloser func() error

func (f testCloser) Close() error { return f() }

// pipeBrowser is an initialized engine on a fake --remote-debugging-pipe
// browser that answers every command, or fails the methods in fail.
func pipeBrowser(t *testing.T, fail map[string]string) (*browser.Engine, func()) {
	t.Helper()
	engineIn, browserOut := io.Pipe()
	browserIn, engineOut := io.Pipe()
	var once sync.Once
	closeAll := func() {
		once.Do(func() {
			browserOut.Close()
			browserIn.Close()
			engineIn.Close()
			engineOut.Close()
		})
	}
	go func() {
		r := bufio.NewReader(browserIn)
		for {
			line, err := r.ReadBytes(0)
			if err != nil {
				return
			}
			var msg map[string]any
			if json.Unmarshal(line[:len(line)-1], &msg) != nil {
				return
			}
			reply := map[string]any{"id": msg["id"], "result": map[string]any{}}
			if text, ok := fail[msg["method"].(string)]; ok {
				reply = map[string]any{"id": msg["id"], "error": map[string]any{"message": text}}
			}
			body, _ := json.Marshal(reply)
			browserOut.Write(append(body, 0))
		}
	}()
	engine := browser.New()
	engine.UseTransport(browser.NewPipe(engineIn, engineOut, testCloser(func() error { closeAll(); return nil })))
	init, _ := json.Marshal(map[string]any{"connectTimeoutS": 5, "snapshotMaxChars": 1000, "maxEventEntries": 10,
		"maxBodyBytes": 1000, "maxArtifactBytes": 1000, "downloadHostPath": t.TempDir()})
	if _, err := engine.Execute(context.Background(), "initialize", init); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { engine.Close(); closeAll() })
	return engine, closeAll
}

// browserAgent has the browser on, pointed at an endpoint nothing answers,
// so a restart fails at once instead of launching a real browser.
func browserAgent(t *testing.T) (*Agent, *browserHost) {
	t.Helper()
	c := Config{Capabilities: Capabilities{Exec: true, Browser: true}, Browser: BrowserConfig{Endpoint: "ws://127.0.0.1:1/devtools/browser/none"}}
	a := NewAgent(c, log.New(io.Discard, "", 0))
	a.ConfigPath = filepath.Join(t.TempDir(), "config.json")
	return a, a.browserHost()
}

func attachEngine(h *browserHost, engine *browser.Engine) {
	h.mu.Lock()
	h.engine, h.epoch, h.state, h.source = engine, "epoch-1", "ready", "endpoint"
	h.mu.Unlock()
}

func TestBrowserHostDropsAnEngineWhoseBrowserWentAway(t *testing.T) {
	_, h := browserAgent(t)
	engine, closeBrowser := pipeBrowser(t, nil)
	attachEngine(h, engine)
	closeBrowser()
	for deadline := time.Now().Add(5 * time.Second); engine.Connected(); {
		if time.Now().After(deadline) {
			t.Fatal("the engine still reports a connection")
		}
		time.Sleep(10 * time.Millisecond)
	}
	// A raw call names the lost connection like the controller's own CDP client.
	reply, err := h.cdp(context.Background(), json.RawMessage(`{"method":"Browser.getVersion"}`))
	if err != nil || reply.(map[string]any)["error"] != "browser_connection_lost" {
		t.Fatalf("cdp reply %v, %v", reply, err)
	}
	// Starting again attaches anew instead of reporting the dead engine ready.
	status, err := h.ensure(context.Background())
	if err != nil || status["state"] != "failed" {
		t.Fatalf("status %v, %v", status, err)
	}
	if current, _ := h.current(); current != nil {
		t.Fatal("the dead engine is still current")
	}
}

func TestBrowserHostReportsCommandFailuresLikeTheControllerClient(t *testing.T) {
	_, h := browserAgent(t)
	engine, _ := pipeBrowser(t, map[string]string{"Target.closeTarget": "No target with given id found"})
	attachEngine(h, engine)
	reply, err := h.cdp(context.Background(), json.RawMessage(`{"method":"Target.closeTarget","params":{"targetId":"x"}}`))
	if err != nil || reply.(map[string]any)["error"] != "cdp_command_failed: No target with given id found" {
		t.Fatalf("cdp reply %v, %v", reply, err)
	}
	if current, _ := h.current(); current != engine {
		t.Fatal("a command error dropped the engine")
	}
}

func TestBrowserHostRefusesRepliesTheChannelCannotCarry(t *testing.T) {
	_, h := browserAgent(t)
	engine, _ := pipeBrowser(t, nil)
	attachEngine(h, engine)
	params := json.RawMessage(`{"action":"configure","params":{"snapshotMaxChars":1000,"maxEventEntries":10,"maxBodyBytes":1000,"maxArtifactBytes":1000}}`)
	reply, err := h.call(context.Background(), params)
	if raw, ok := reply.(json.RawMessage); err != nil || !ok || string(raw) != `{"result":{"configured":true}}` {
		t.Fatalf("reply %s, %v", reply, err)
	}
	defer func(limit int) { maxBrowserReply = limit }(maxBrowserReply)
	maxBrowserReply = 10
	reply, err = h.call(context.Background(), params)
	if m, ok := reply.(map[string]any); err != nil || !ok || m["error"] != "worker_response_too_large" || m["outcome"] != "unknown" {
		t.Fatalf("reply %v, %v", reply, err)
	}
}

func TestBrowserHostPrunesArtifactsNeverFetched(t *testing.T) {
	_, h := browserAgent(t)
	engine, _ := pipeBrowser(t, nil)
	attachEngine(h, engine)
	dir := filepath.Join(h.dir, "artifacts")
	os.MkdirAll(dir, 0o700)
	stale, fresh := filepath.Join(dir, "stale"), filepath.Join(dir, "fresh")
	os.WriteFile(stale, []byte("x"), 0o600)
	os.WriteFile(fresh, []byte("x"), 0o600)
	old := time.Now().Add(-2 * artifactTTL)
	os.Chtimes(stale, old, old)
	params := json.RawMessage(`{"action":"configure","params":{"outputPath":"*","snapshotMaxChars":1000,"maxEventEntries":10,"maxBodyBytes":1000,"maxArtifactBytes":1000}}`)
	if _, err := h.call(context.Background(), params); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(stale); !os.IsNotExist(err) {
		t.Fatal("a stale artifact was kept")
	}
	if _, err := os.Stat(fresh); err != nil {
		t.Fatal("a fresh artifact was removed")
	}
}

func TestBrowserConfigChangeClearsAnOldFailure(t *testing.T) {
	a, h := browserAgent(t)
	h.mu.Lock()
	h.state, h.err = "failed", "browser_not_runnable: /opt/browser/chrome"
	h.mu.Unlock()
	status, err := a.configureBrowser(json.RawMessage(`{"enabled":true,"download":true}`))
	if err != nil || status["state"] != "stopped" || status["error"] != nil || status["download"] != true {
		t.Fatalf("status %v, %v", status, err)
	}
}

func TestReusedBrowserStopWaitsForTheProfile(t *testing.T) {
	profile := t.TempDir()
	lock := filepath.Join(profile, "SingletonLock")
	if err := os.Symlink("host-1", lock); err != nil {
		t.Skip("no symlinks here")
	}
	l := reusedBrowser("ws://127.0.0.1:1/devtools/browser/none", profile, filepath.Join(profile, "downloads"))
	go func() {
		time.Sleep(300 * time.Millisecond)
		os.Remove(lock)
	}()
	started := time.Now()
	l.stop()
	if time.Since(started) < 250*time.Millisecond {
		t.Fatal("stop returned while the profile was still locked")
	}
	select {
	case <-l.done:
	default:
		t.Fatal("done stays open after stop")
	}
	l.stop() // a second stop is harmless
}

func TestFetchFileEndsAStalledDownload(t *testing.T) {
	defer func(d time.Duration) { fetchStall = d }(fetchStall)
	fetchStall = 300 * time.Millisecond
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("partial"))
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	}))
	defer server.Close()
	path := filepath.Join(t.TempDir(), "chrome.zip")
	err := fetchFile(context.Background(), server.Client(), server.URL, path, func(int64) {})
	if err == nil || !strings.Contains(err.Error(), "stalled") {
		t.Fatalf("err %v", err)
	}
}

// From the 10-30 check: a browser reached through an endpoint saved into the node's own
// directory, which its account cannot write; downloads always failed.
func TestAnEndpointBrowserOnThisMachineSavesWhereItCan(t *testing.T) {
	dir := t.TempDir()
	if got, err := endpointDownloads("ws://10.0.0.5:9222/devtools/browser/x", dir); err != nil || got != filepath.Join(dir, "downloads") {
		t.Fatalf("remote endpoint: %s %v", got, err)
	}
	if _, err := os.Stat(sharedDownloads()); os.IsNotExist(err) {
		t.Cleanup(func() { os.Remove(sharedDownloads()) })
	}
	got, err := endpointDownloads("ws://127.0.0.1:9222/devtools/browser/x", dir)
	if err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(got)
	if err != nil || got != sharedDownloads() || !info.IsDir() {
		t.Fatalf("local endpoint: %s %v %v", got, info, err)
	}
	// Windows: the Public folder lets every account write; there are no mode bits.
	if runtime.GOOS != "windows" && (info.Mode().Perm() != 0o777 || info.Mode()&os.ModeSticky == 0) {
		t.Fatalf("local endpoint mode %v", info.Mode())
	}
	if !loopbackEndpoint("http://localhost:9222") || loopbackEndpoint("http://192.168.1.2:9222") {
		t.Fatal("loopback not told apart")
	}
}
