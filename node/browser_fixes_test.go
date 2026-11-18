package node

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"openbear/node/browser"
)

func TestNP01LegacyProbeRecoversOnlyConfirmedDeadLocalLocks(t *testing.T) {
	const host = "test-host-with-dashes"
	const address = "ws://127.0.0.1:12345/devtools/browser/old-id"
	for _, tc := range []struct {
		name, lock  string
		processErr  error
		endpoint    string
		endpointErr error
		wantErr     string
		wantAddress string
	}{
		{name: "crashed", lock: host + "-4321", processErr: syscall.ESRCH},
		{name: "crashed with recycled port", lock: host + "-4321", processErr: syscall.ESRCH, endpoint: "other browser"},
		{name: "live but unreachable", lock: host + "-4321", endpointErr: errors.New("offline"), wantErr: "browser_legacy_unreachable"},
		{name: "permission denied is alive", lock: host + "-4321", processErr: syscall.EPERM, endpointErr: errors.New("offline"), wantErr: "browser_legacy_unreachable"},
		{name: "unknown process error", lock: host + "-4321", processErr: syscall.EIO, wantErr: "browser_legacy_identity_unverified"},
		{name: "foreign hostname", lock: "other-host-4321", processErr: syscall.ESRCH, wantErr: "browser_legacy_identity_unverified"},
		{name: "malformed pid", lock: host + "-wrong", wantErr: "browser_legacy_identity_unverified"},
		{name: "recycled port with live pid", lock: host + "-4321", endpoint: "other browser", wantErr: "browser_legacy_identity_unverified"},
		{name: "live verified endpoint", lock: host + "-4321", endpoint: address, wantAddress: address},
	} {
		t.Run(tc.name, func(t *testing.T) {
			profile := t.TempDir()
			lock := filepath.Join(profile, "SingletonLock")
			if err := os.Symlink(tc.lock, lock); err != nil {
				t.Skip("symlinks unavailable:", err)
			}
			port := filepath.Join(profile, "DevToolsActivePort")
			const data = "12345\n/devtools/browser/old-id\n"
			if err := os.WriteFile(port, []byte(data), 0600); err != nil {
				t.Fatal(err)
			}
			for i := 0; i < 2; i++ {
				endpointCalls := 0
				got, pid, err := legacyBrowserEndpoint(profile, host, func(pid int) error {
					if pid != 4321 {
						t.Fatalf("unexpected PID %d", pid)
					}
					return tc.processErr
				}, func(string) (string, error) {
					endpointCalls++
					return tc.endpoint, tc.endpointErr
				})
				if tc.wantErr != "" {
					if err == nil || err.Error() != tc.wantErr {
						t.Fatalf("%q %d %v", got, pid, err)
					}
				} else if err != nil || got != tc.wantAddress {
					t.Fatalf("%q %d %v", got, pid, err)
				}
				if errors.Is(tc.processErr, syscall.ESRCH) && endpointCalls != 0 {
					t.Fatal("dead PID still required endpoint response")
				}
			}
			// Discovery (including update-only discovery) never clears unknown data.
			if got, err := os.Readlink(lock); err != nil || got != tc.lock {
				t.Fatalf("lock changed: %q %v", got, err)
			}
			if got, err := os.ReadFile(port); err != nil || string(got) != data {
				t.Fatalf("port changed: %q %v", got, err)
			}
		})
	}
}

func TestNP01LegacyProbeDoesNotTreatUnknownLockAsFreshProfile(t *testing.T) {
	profile := t.TempDir()
	probe := func(int) error { t.Fatal("unexpected process probe"); return nil }
	endpoint := func(string) (string, error) { t.Fatal("unexpected endpoint probe"); return "", nil }
	if address, _, err := legacyBrowserEndpoint(profile, "host", probe, endpoint); err != nil || address != "" {
		t.Fatalf("fresh profile: %q %v", address, err)
	}
	if err := os.WriteFile(filepath.Join(profile, "SingletonLock"), []byte("not a symlink"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := legacyBrowserEndpoint(profile, "host", probe, endpoint); err == nil || err.Error() != "browser_legacy_identity_unverified" {
		t.Fatalf("unknown lock: %v", err)
	}
}

// This is a protocol double only: no browser binary, profile or desktop is used.
func fixedLaunch(t *testing.T, exe, product string, reused bool, stopped *atomic.Int32) *launched {
	t.Helper()
	engineIn, browserOut := io.Pipe()
	browserIn, engineOut := io.Pipe()
	var once sync.Once
	closeAll := func() { once.Do(func() { engineIn.Close(); browserOut.Close(); browserIn.Close(); engineOut.Close() }) }
	t.Cleanup(closeAll)
	go func() {
		r := bufio.NewReader(browserIn)
		for {
			data, err := r.ReadBytes(0)
			if err != nil {
				return
			}
			var m map[string]any
			json.Unmarshal(data[:len(data)-1], &m)
			result := map[string]any{}
			if m["method"] == "Browser.getVersion" {
				result["product"] = product
			}
			out, _ := json.Marshal(map[string]any{"id": m["id"], "result": result})
			if _, err := browserOut.Write(append(out, 0)); err != nil {
				return
			}
		}
	}()
	done := make(chan struct{})
	var stopOnce sync.Once
	return &launched{transport: browser.NewPipe(engineIn, engineOut, testCloser(func() error { closeAll(); return nil })), downloads: t.TempDir(), reused: reused, executable: exe, done: done, stop: func() { stopOnce.Do(func() { stopped.Add(1); closeAll(); close(done) }) }}
}

func TestNP03UpdateReportsPendingVersionWithoutRestarting(t *testing.T) {
	a, h := browserAgent(t)
	old := installedAt(t, h.dir, "130.0.0.1")
	a.mu.Lock()
	a.Config.Browser = BrowserConfig{Download: true}
	a.mu.Unlock()
	original := launchLocalBrowser
	defer func() { launchLocalBrowser = original }()
	var launches, stops atomic.Int32
	launchLocalBrowser = func(_ context.Context, exe, _ string) (*launched, error) {
		product := "Chrome/130.0.0.1"
		if launches.Add(1) > 1 {
			product = "HeadlessChrome/131.0.0.2"
		}
		return fixedLaunch(t, exe, product, false, &stops), nil
	}
	defer h.stop()
	if s, err := h.ensure(t.Context()); err != nil || s["restartNeeded"] != false {
		t.Fatalf("initial: %v %v", s, err)
	}
	newChromeSite(t, "131.0.0.2", fakeChrome(t, true))
	updateCall(t, h, `{}`)
	update := settled(t, h)
	status := h.status()
	if update["restartNeeded"] != true || status["restartNeeded"] != true || status["runningVersion"] != "Chrome/130.0.0.1" || status["installedVersion"] != "131.0.0.2" {
		t.Fatalf("status=%v update=%v", status, update)
	}
	if launches.Load() != 1 || stops.Load() != 0 {
		t.Fatal("update restarted the browser")
	}
	if _, err := os.Stat(old); err != nil {
		t.Fatal("old runtime resources removed", err)
	}
	h.mu.Lock()
	intent := h.intentLocked()
	h.mu.Unlock()
	status, err := h.restartIntent(t.Context(), &intent)
	if err != nil || status["restartNeeded"] != false || status["runningVersion"] != "HeadlessChrome/131.0.0.2" {
		t.Fatalf("explicit restart: %v %v", status, err)
	}
	h.chromeRestarted()
	if h.status()["update"].(map[string]any)["restartNeeded"] != false || launches.Load() != 2 || stops.Load() != 1 {
		t.Fatalf("restart: %v", h.status())
	}
}

func TestNP03RestartStatusTracksActualSourceAndClearsOnStop(t *testing.T) {
	_, h := browserAgent(t)
	installedAt(t, h.dir, "131.0.0.2")
	engine, _ := pipeBrowser(t, nil)
	attachEngine(h, engine)
	for _, tc := range []struct {
		source, product string
		want            bool
	}{
		{"download", "Chrome/130.0.0.1", true},
		{"download", "Chrome/131.0.0.2", false},
		{"download", "HeadlessChrome/131.0.0.2", false},
		{"system", "Chrome/130.0.0.1", false},
		{"endpoint", "Chrome/130.0.0.1", false},
	} {
		h.mu.Lock()
		h.source, h.product = tc.source, tc.product
		h.mu.Unlock()
		if got := h.status()["restartNeeded"]; got != tc.want {
			t.Fatalf("%+v: %v", tc, got)
		}
	}
	h.mu.Lock()
	h.restartNeeded = true // pending source change must not survive a stop
	h.mu.Unlock()
	h.stop()
	if h.status()["restartNeeded"] != false {
		t.Fatal("stopped instance still requires restart")
	}
}

func TestBrowserFix05LegacyVersionIsNotInstalledVersionAndExplicitRestartApplies(t *testing.T) {
	a, h := browserAgent(t)
	old := installedAt(t, h.dir, "130.0.0.1")
	fresh := installedAt(t, h.dir, "131.0.0.2")
	a.mu.Lock()
	a.Config.Browser = BrowserConfig{Download: true}
	a.mu.Unlock()
	launch := launchLocalBrowser
	defer func() { launchLocalBrowser = launch }()
	var launches, stops atomic.Int32
	launchLocalBrowser = func(context.Context, string, string) (*launched, error) {
		if launches.Add(1) == 1 {
			return fixedLaunch(t, old, "Chrome/130.0.0.1", true, &stops), nil
		}
		return fixedLaunch(t, fresh, "Chrome/131.0.0.2", false, &stops), nil
	}
	defer h.stop()
	status, err := h.ensure(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if status["state"] != "ready" || status["installedVersion"] != "131.0.0.2" || status["runningVersion"] != "Chrome/130.0.0.1" || status["runningSource"] != "download" || status["restartNeeded"] != true || stops.Load() != 0 {
		t.Fatalf("%v stops=%d", status, stops.Load())
	}
	h.mu.Lock()
	intent := h.intentLocked()
	h.mu.Unlock()
	status, err = h.restartIntent(t.Context(), &intent)
	if err != nil || status["runningVersion"] != "Chrome/131.0.0.2" || status["restartNeeded"] != false || stops.Load() != 1 {
		t.Fatalf("%v %v stops=%d", status, err, stops.Load())
	}
}

func TestBrowserFix05LegacyUnknownSourceIsNeverReportedAsSelected(t *testing.T) {
	a, h := browserAgent(t)
	exe := installedAt(t, h.dir, "131.0.0.2")
	a.mu.Lock()
	a.Config.Browser = BrowserConfig{Executable: exe}
	a.mu.Unlock()
	old := launchLocalBrowser
	defer func() { launchLocalBrowser = old }()
	var stops atomic.Int32
	launchLocalBrowser = func(context.Context, string, string) (*launched, error) {
		return fixedLaunch(t, "", "Chrome/129.0.0.1", true, &stops), nil
	}
	defer h.stop()
	status, err := h.ensure(t.Context())
	if err != nil || status["runningSource"] != "unknown" || status["selectedSource"] != "system" || status["runningVerified"] != false || status["restartNeeded"] != true || stops.Load() != 0 {
		t.Fatalf("%v %v", status, err)
	}
}

func TestBrowserFix05UpdateDiscoversUnattachedLegacyWindow(t *testing.T) {
	for _, restart := range []bool{false, true} {
		t.Run(map[bool]string{false: "retain", true: "explicit restart"}[restart], func(t *testing.T) {
			a, h := browserAgent(t)
			oldExe := installedAt(t, h.dir, "130.0.0.1")
			a.mu.Lock()
			a.Config.Browser = BrowserConfig{Download: true}
			a.mu.Unlock()
			newChromeSite(t, "131.0.0.2", fakeChrome(t, true))
			oldProbe, oldLaunch := findLegacyBrowser, launchLocalBrowser
			defer func() { findLegacyBrowser, launchLocalBrowser = oldProbe, oldLaunch }()
			var probes, launches, stops atomic.Int32
			findLegacyBrowser = func(context.Context, string) (*launched, error) {
				probes.Add(1)
				return &launched{reused: true, executable: oldExe}, nil
			}
			launchLocalBrowser = func(_ context.Context, exe, _ string) (*launched, error) {
				if launches.Add(1) == 1 {
					return fixedLaunch(t, oldExe, "Chrome/130.0.0.1", true, &stops), nil
				}
				return fixedLaunch(t, exe, "Chrome/131.0.0.2", false, &stops), nil
			}
			defer h.stop()
			params := `{}`
			if restart {
				params = `{"restart":true}`
			}
			updateCall(t, h, params)
			update := settled(t, h)
			if probes.Load() != 1 || update["state"] != "done" {
				t.Fatalf("%v probes=%d", update, probes.Load())
			}
			if restart {
				if update["restartNeeded"] != false || stops.Load() != 1 || launches.Load() != 2 || h.status()["runningVersion"] != "Chrome/131.0.0.2" {
					t.Fatalf("%v %v stops=%d launches=%d", update, h.status(), stops.Load(), launches.Load())
				}
			} else if update["restartNeeded"] != true || h.status()["restartNeeded"] != true || launches.Load() != 0 || stops.Load() != 0 {
				t.Fatalf("unrequested restart: %v", update)
			}
		})
	}
}

func TestBrowserFix05UnappliedRestartKeepsOldInstallation(t *testing.T) {
	a, h := browserAgent(t)
	oldExe := installedAt(t, h.dir, "130.0.0.1")
	installedAt(t, h.dir, "131.0.0.2")
	a.mu.Lock()
	a.Config.Browser = BrowserConfig{Download: true}
	a.mu.Unlock()
	old := launchLocalBrowser
	defer func() { launchLocalBrowser = old }()
	var stops atomic.Int32
	launchLocalBrowser = func(context.Context, string, string) (*launched, error) {
		return fixedLaunch(t, oldExe, "Chrome/130.0.0.1", true, &stops), nil
	}
	defer h.stop()
	h.mu.Lock()
	intent := h.intentLocked()
	h.mu.Unlock()
	status, err := h.restartIntent(t.Context(), &intent)
	if err == nil || status["restartNeeded"] != true {
		t.Fatalf("claimed applied: %v %v", status, err)
	}
	h.chromeRestarted()
	if _, err := os.Stat(oldExe); err != nil {
		t.Fatal("deleted surviving browser resources", err)
	}
}

func TestBrowserFix06UpdateCannotCancelNewConfigurationStart(t *testing.T) {
	a, h := browserAgent(t)
	installedAt(t, h.dir, "130.0.0.1")
	site := newChromeSite(t, "131.0.0.2", fakeChrome(t, true))
	release := make(chan struct{})
	site.hold = release
	updateCall(t, h, `{"restart":true}`)
	// A configuration edit invalidates the update intent while its archive is held.
	if _, err := a.configureBrowser(json.RawMessage(`{"enabled":true,"endpoint":"ws://127.0.0.1:2/devtools/browser/new"}`)); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	h.mu.Lock()
	h.starting = make(chan struct{})
	h.startCancel = cancel
	h.state = "starting"
	generation := h.generation
	h.mu.Unlock()
	close(release)
	update := settled(t, h)
	h.mu.Lock()
	same := h.generation == generation && h.starting != nil && h.state == "starting"
	h.starting = nil
	h.startCancel = nil
	h.mu.Unlock()
	if !same || ctx.Err() != nil || update["configurationChanged"] != true || update["restarted"] != nil {
		t.Fatalf("old update interrupted new start: %v", update)
	}
	if installedChrome(h.dir) != "131.0.0.2" {
		t.Fatal("installation was unnecessarily cancelled")
	}
}

func TestBrowserFix06StaleIntentCannotStopOrStart(t *testing.T) {
	_, h := browserAgent(t)
	engine, _ := pipeBrowser(t, nil)
	attachEngine(h, engine)
	h.mu.Lock()
	intent := h.intentLocked()
	h.generation++
	currentGen := h.generation
	h.mu.Unlock()
	if h.stopIntent(&intent) {
		t.Fatal("stale stop accepted")
	}
	if _, err := h.ensureIntent(t.Context(), &intent); err == nil {
		t.Fatal("stale start accepted")
	}
	current, _ := h.current()
	if current != engine || !engine.Connected() || h.generation != currentGen {
		t.Fatal("new instance modified")
	}
}

func TestBrowserFix08StatusDetectsDisconnectedEndpointWithoutStarting(t *testing.T) {
	_, h := browserAgent(t)
	engine, disconnect := pipeBrowser(t, nil)
	attachEngine(h, engine)
	disconnect()
	for deadline := time.Now().Add(2 * time.Second); engine.Connected(); time.Sleep(time.Millisecond) {
		if time.Now().After(deadline) {
			t.Fatal("reader not stopped")
		}
	}
	generation := h.generation
	for i := 0; i < 3; i++ {
		s := h.status()
		if s["state"] != "stopped" || s["runningVerified"] != false || s["error"] == nil {
			t.Fatalf("%v", s)
		}
	}
	if h.generation != generation || h.starting != nil || h.engine != engine {
		t.Fatal("status performed lifecycle work")
	}
	if _, err := os.Stat(filepath.Join(h.dir, "profile")); !os.IsNotExist(err) {
		t.Fatal("status created browser profile")
	}
}
