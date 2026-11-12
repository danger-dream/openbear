package node

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"sync/atomic"
	"testing"
	"time"
)

// chromeSite stands in for Google's version list and download host.
type chromeSite struct {
	version string
	archive []byte // nil: the download fails
	listed  atomic.Int32
	fetched atomic.Int32
	server  *httptest.Server
	hold    <-chan struct{}
}

func chromeExeName() string {
	return map[string]string{"windows": "chrome.exe", "darwin": "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"}[runtime.GOOS]
}

// fakeChrome is an archive laid out like a Chrome for Testing one; without the browser in it when empty.
func fakeChrome(t *testing.T, withBrowser bool) []byte {
	name := "chrome-" + chromePlatform() + "/"
	if exe := chromeExeName(); exe != "" {
		name += exe
	} else {
		name += "chrome"
	}
	if !withBrowser {
		name = "chrome-" + chromePlatform() + "/README"
	}
	data, err := os.ReadFile(writeZip(t, []zipEntry{{name: name, body: "#!/bin/sh\n", mode: 0o755}}))
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func newChromeSite(t *testing.T, version string, archive []byte) *chromeSite {
	t.Helper()
	if chromePlatform() == "" {
		t.Skip("no Chrome for Testing build for this platform")
	}
	site := &chromeSite{version: version, archive: archive}
	site.server = httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/versions.json":
			site.listed.Add(1)
			json.NewEncoder(w).Encode(map[string]any{"channels": map[string]any{"Stable": map[string]any{"version": site.version,
				"downloads": map[string]any{"chrome": []any{map[string]any{"platform": chromePlatform(), "url": site.server.URL + "/chrome.zip"}}}}}})
		case "/chrome.zip":
			site.fetched.Add(1)
			if site.hold != nil {
				select {
				case <-site.hold:
				case <-r.Context().Done():
					return
				}
			}
			if site.archive == nil {
				http.Error(w, "gone", http.StatusInternalServerError)
				return
			}
			w.Write(site.archive)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(site.server.Close)
	url, client := chromeVersionsURL, chromeClient
	chromeVersionsURL, chromeClient = site.server.URL+"/versions.json", site.server.Client()
	t.Cleanup(func() { chromeVersionsURL, chromeClient = url, client })
	return site
}

// installedAt puts a downloaded Chrome of version into dir, as an earlier download left it.
func installedAt(t *testing.T, dir, version string) string {
	t.Helper()
	exe := filepath.Join(dir, "chrome-"+version, "chrome-"+chromePlatform(), "chrome")
	if name := chromeExeName(); name != "" {
		exe = filepath.Join(dir, "chrome-"+version, "chrome-"+chromePlatform(), filepath.FromSlash(name))
	}
	os.MkdirAll(filepath.Dir(exe), 0o755)
	if err := os.WriteFile(exe, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	rel, _ := filepath.Rel(dir, exe)
	data, _ := json.Marshal(chromeInstall{Version: version, Executable: rel})
	os.WriteFile(filepath.Join(dir, "chrome.json"), data, 0o644)
	return exe
}

func updateCall(t *testing.T, h *browserHost, params string) map[string]any {
	t.Helper()
	out, err := h.updateChrome(t.Context(), json.RawMessage(params))
	if err != nil {
		t.Fatal(err)
	}
	return out.(map[string]any)
}

// settled waits for the background update to end and returns what browser.status says of it.
func settled(t *testing.T, h *browserHost) map[string]any {
	t.Helper()
	for deadline := time.Now().Add(20 * time.Second); time.Now().Before(deadline); time.Sleep(20 * time.Millisecond) {
		if update, _ := h.status()["update"].(map[string]any); update != nil && update["state"] != "downloading" && update["state"] != "restarting" {
			return update
		}
	}
	t.Fatal("the update never ended")
	return nil
}

func TestBrowserUpdateWhenAlreadyLatestDownloadsNothing(t *testing.T) {
	_, h := browserAgent(t)
	site := newChromeSite(t, "131.0.6778.85", fakeChrome(t, true))
	installedAt(t, h.dir, "131.0.6778.85")
	for _, params := range []string{`{"check":true}`, `{"restart":true}`} {
		out := updateCall(t, h, params)
		if out["upToDate"] != true || out["current"] != "131.0.6778.85" || out["started"] != nil {
			t.Fatalf("%s: %v", params, out)
		}
	}
	if site.fetched.Load() != 0 || h.status()["update"] != nil {
		t.Fatalf("downloaded %d times, update %v", site.fetched.Load(), h.status()["update"])
	}
}

func TestBrowserUpdateInstallsTheNewerOne(t *testing.T) {
	_, h := browserAgent(t)
	site := newChromeSite(t, "131.0.6778.85", fakeChrome(t, true))
	installedAt(t, h.dir, "130.0.6723.116")
	if out := updateCall(t, h, `{"check":true}`); out["upToDate"] != false || out["latest"] != "131.0.6778.85" || out["started"] != nil || site.fetched.Load() != 0 {
		t.Fatalf("a check: %v, downloaded %d", out, site.fetched.Load())
	}
	if out := updateCall(t, h, `{}`); out["started"] != true {
		t.Fatalf("update: %v", out)
	}
	if update := settled(t, h); update["state"] != "done" || update["version"] != "131.0.6778.85" || update["restartNeeded"] != false {
		t.Fatalf("update: %v", update)
	}
	if status := h.status(); status["chromeVersion"] != "131.0.6778.85" {
		t.Fatalf("status: %v", status)
	}
	exe, source, err := h.executable(BrowserConfig{Download: true})
	if err != nil || source != "download" || !filepath.IsAbs(exe) || filepath.Base(filepath.Dir(filepath.Dir(exe))) == "chrome-130.0.6723.116" {
		t.Fatalf("starts %q %q %v", exe, source, err)
	}

}

func TestBrowserUpdateThatFailsKeepsTheInstalledOne(t *testing.T) {
	for name, archive := range map[string][]byte{"download fails": nil, "no browser in it": fakeChrome(t, false), "not a zip": []byte("<html>")} {
		t.Run(name, func(t *testing.T) {
			_, h := browserAgent(t)
			newChromeSite(t, "131.0.6778.85", archive)
			old := installedAt(t, h.dir, "130.0.6723.116")
			if out := updateCall(t, h, `{}`); out["started"] != true {
				t.Fatalf("update: %v", out)
			}
			if update := settled(t, h); update["state"] != "failed" || update["error"] == "" {
				t.Fatalf("update: %v", update)
			}
			if installedChrome(h.dir) != "130.0.6723.116" {
				t.Fatalf("chrome.json now says %q", installedChrome(h.dir))
			}
			if _, err := os.Stat(old); err != nil {
				t.Fatalf("the installed browser is gone: %v", err)
			}
			entries, _ := os.ReadDir(h.dir)
			for _, e := range entries {
				if e.Name() != "chrome-130.0.6723.116" && e.Name() != "chrome.json" {
					t.Fatalf("left behind: %s", e.Name())
				}
			}
		})
	}
}

func TestBrowserUpdateLeavesARunningBrowserUnlessToldToRestart(t *testing.T) {
	_, h := browserAgent(t)
	newChromeSite(t, "131.0.6778.85", fakeChrome(t, true))
	installedAt(t, h.dir, "130.0.6723.116")
	engine, _ := pipeBrowser(t, nil)
	attachEngine(h, engine)
	h.mu.Lock()
	h.source = "download"
	h.mu.Unlock()
	updateCall(t, h, `{}`)
	if update := settled(t, h); update["state"] != "done" || update["restartNeeded"] != true || update["restarted"] != nil {
		t.Fatalf("update: %v", update)
	}
	if current, _ := h.current(); current != engine || !engine.Connected() {
		t.Fatal("the running browser was stopped")
	}
	// What it runs from stays until it is started again.
	if _, err := os.Stat(filepath.Join(h.dir, "chrome-130.0.6723.116")); err != nil {
		t.Fatalf("the running version was removed: %v", err)
	}

	installedAt(t, h.dir, "130.0.6723.116")
	updateCall(t, h, `{"restart":true}`)
	for deadline := time.Now().Add(20 * time.Second); ; time.Sleep(20 * time.Millisecond) {
		h.mu.Lock()
		restarted := h.update["restarted"]
		h.mu.Unlock()
		if restarted == true {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("not restarted: %v", h.status())
		}
	}
	if current, _ := h.current(); current == engine {
		t.Fatal("still the browser from before the update")
	}
}

func TestBrowserUpdateWithoutADownloadedChrome(t *testing.T) {
	_, h := browserAgent(t)
	site := newChromeSite(t, "131.0.6778.85", fakeChrome(t, true))
	if _, err := h.updateChrome(t.Context(), json.RawMessage(`{"check":true}`)); err == nil || err.Error() != "browser_not_downloaded" {
		t.Fatalf("%v", err)
	}
	if site.listed.Load() != 0 {
		t.Fatal("asked for the version list")
	}
}

func TestStartingTheDownloadedChromeDoesNotLookForANewerOne(t *testing.T) {
	_, h := browserAgent(t)
	site := newChromeSite(t, "131.0.6778.85", fakeChrome(t, true))
	exe := installedAt(t, h.dir, "130.0.6723.116")
	if !chromeRunsHere() {
		t.Skip("Chrome does not run on this system")
	}
	got, source, err := h.executable(BrowserConfig{Download: true})
	if err != nil || got != exe || source != "download" || site.listed.Load() != 0 || site.fetched.Load() != 0 {
		t.Fatalf("%q %q %v; listed %d", got, source, err, site.listed.Load())
	}
}

func TestBrowserUpdateValidationFailureKeepsTheInstalledVersion(t *testing.T) {
	_, h := browserAgent(t)
	site := newChromeSite(t, "131.0.6778.85", fakeChrome(t, true))
	old := installedAt(t, h.dir, "130.0.6723.116")
	_, err := installChrome(t.Context(), h.dir, site.version, site.server.URL+"/chrome.zip", func(int64) {}, func(string) error {
		return errors.New("missing required library")
	})
	if err == nil || installedChrome(h.dir) != "130.0.6723.116" || downloadedBrowser(h.dir) != old {
		t.Fatalf("validation replaced the old browser: %v, %v", err, h.status())
	}
}

func TestBrowserUpdateCancelledForUninstallDoesNotInstall(t *testing.T) {
	_, h := browserAgent(t)
	site := newChromeSite(t, "131.0.6778.85", fakeChrome(t, true))
	site.hold = make(chan struct{})
	installedAt(t, h.dir, "130.0.6723.116")
	updateCall(t, h, `{}`)
	for deadline := time.Now().Add(5 * time.Second); site.fetched.Load() == 0; time.Sleep(time.Millisecond) {
		if time.Now().After(deadline) {
			t.Fatal("download did not start")
		}
	}
	h.cancelUpdate()
	if h.status()["update"].(map[string]any)["state"] != "failed" || installedChrome(h.dir) != "130.0.6723.116" {
		t.Fatalf("cancelled update: %v", h.status())
	}
}

func TestExplicitBrowserRestartClearsPendingUpdateAndOldVersions(t *testing.T) {
	_, h := browserAgent(t)
	old := installedAt(t, h.dir, "130.0.6723.116")
	installedAt(t, h.dir, "131.0.6778.85")
	h.source = "download"
	h.update = map[string]any{"state": "done", "version": "131.0.6778.85", "restartNeeded": true}
	h.chromeRestarted()
	if h.status()["update"].(map[string]any)["restartNeeded"] != false {
		t.Fatal("still asks for restart")
	}
	if _, err := os.Stat(old); !os.IsNotExist(err) {
		t.Fatalf("old version after an explicit restart: %v", err)
	}
}
