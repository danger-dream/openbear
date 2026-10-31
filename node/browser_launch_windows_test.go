//go:build windows

package node

import (
	"bufio"
	"context"
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"golang.org/x/sys/windows"

	"openbear/node/browser"
)

// Window checks use a separate profile in the signed-in user's AppData.
// OPENBEAR_BROWSER_MODE=window requires SYSTEM; headless tests the fallback.
func TestWindowsBrowserOverPipe(t *testing.T) {
	if os.Getenv("OPENBEAR_BROWSER_LIVE") != "1" {
		t.Skip("set OPENBEAR_BROWSER_LIVE=1 to start a real browser")
	}
	exe := os.Getenv("OPENBEAR_BROWSER_EXE")
	if exe == "" {
		if candidates := browserCandidates(); len(candidates) > 0 {
			exe = candidates[0]
		} else {
			t.Fatal("no browser found; set OPENBEAR_BROWSER_EXE")
		}
	}
	dir := t.TempDir()
	var home, profileName string
	mode := os.Getenv("OPENBEAR_BROWSER_MODE")
	token, tokenErr := sessionToken()
	if tokenErr == nil {
		t.Cleanup(func() { token.Close() })
	}
	headless := mode == "headless" || tokenErr != nil
	if mode == "window" && tokenErr != nil {
		t.Fatal("window test requires SYSTEM and a signed-in user:", tokenErr)
	}
	if !headless {
		home, profileName = liveWindowProfile(t, token, exe)
	}
	start := func() *launched { return liveLaunch(t, exe, dir, home, profileName, token, headless) }
	first := start()
	t.Logf("first browser: headless=%v pid=%d", first.headless, recordedPID(t, dir))
	liveCheck(t, first, dir)

	// A second start on the same profile ends the first (its pipe is still
	// open here, so it would not close by itself) instead of handing over to it.
	second := start()
	select {
	case <-first.done:
	case <-time.After(5 * time.Second):
		t.Fatal("first browser still running after the second started")
	}
	liveCheck(t, second, dir)

	// What a node restart leaves: the pipe closes and the browser quits by itself.
	second.transport.Close()
	select {
	case <-second.done:
	case <-time.After(20 * time.Second):
		t.Fatal("browser kept running without its pipe")
	}
	if _, ok := readBrowserRecord(dir); ok {
		t.Fatal("record of an ended browser kept")
	}

	third := start()
	defer third.stop()
	liveCheck(t, third, dir)
}

func liveWindowProfile(t *testing.T, token windows.Token, exe string) (string, string) {
	t.Helper()
	home, err := token.GetUserProfileDirectory()
	if err != nil {
		t.Fatal("user home:", err)
	}
	var suffix [16]byte
	if _, err := rand.Read(suffix[:]); err != nil {
		t.Fatal(err)
	}
	name := fmt.Sprintf("BrowserTest-%x", suffix)
	profile := filepath.Join(home, `AppData\Local\OpenBear`, name)
	t.Cleanup(func() {
		// The user can change this tree while Chrome runs; cleanup must not
		// resolve anything there with SYSTEM's privileges.
		if err := withBrowserUser(token, func() error { return os.RemoveAll(profile) }); err != nil {
			t.Errorf("cleanup test profile %q: %v", profile, err)
		}
	})
	_, downloads, err := userProfile(home, name)
	if err != nil {
		t.Fatal("prepare test profile:", err)
	}
	if err := withBrowserUser(token, func() error {
		for _, path := range []string{home, profile, downloads, exe} {
			access := uint32(windows.FILE_LIST_DIRECTORY | windows.FILE_TRAVERSE)
			if path == exe {
				access = windows.GENERIC_READ | windows.FILE_EXECUTE
			}
			p, err := windows.UTF16PtrFromString(path)
			if err != nil {
				return err
			}
			h, err := windows.CreateFile(p, access, windows.FILE_SHARE_READ|windows.FILE_SHARE_WRITE|windows.FILE_SHARE_DELETE,
				nil, windows.OPEN_EXISTING, windows.FILE_FLAG_BACKUP_SEMANTICS, 0)
			if err != nil {
				return fmt.Errorf("user access to %q: %w", path, err)
			}
			windows.CloseHandle(h)
		}
		file, err := os.CreateTemp(downloads, "access-")
		if err != nil {
			return fmt.Errorf("user create in downloads: %w", err)
		}
		_, writeErr := file.WriteString("access check")
		return errors.Join(writeErr, file.Close(), os.Remove(file.Name()))
	}); err != nil {
		t.Fatal("window filesystem preflight:", err)
	}
	t.Logf("user filesystem preflight passed: cwd=%q profile=%q exe=%q", home, profile, exe)
	return home, name
}

func withBrowserUser(token windows.Token, fn func() error) error {
	var impersonation windows.Token
	if err := windows.DuplicateTokenEx(token, windows.TOKEN_QUERY|windows.TOKEN_IMPERSONATE, nil,
		windows.SecurityImpersonation, windows.TokenImpersonation, &impersonation); err != nil {
		return err
	}
	defer impersonation.Close()
	done := make(chan error, 1)
	go func() {
		runtime.LockOSThread()
		if err := windows.SetThreadToken(nil, impersonation); err != nil {
			runtime.UnlockOSThread()
			done <- err
			return
		}
		var err error
		defer func() {
			if revertErr := windows.RevertToSelf(); revertErr != nil {
				err = errors.Join(err, fmt.Errorf("revert user impersonation: %w", revertErr))
				// Exiting while locked discards a thread that could not revert.
			} else {
				runtime.UnlockOSThread()
			}
			done <- err
		}()
		err = fn()
	}()
	return <-done
}

func liveLaunch(t *testing.T, exe, dir, home, profileName string, token windows.Token, headless bool) *launched {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	var l *launched
	var err error
	if headless {
		l, err = launchHeadless(ctx, exe, dir)
	} else {
		l, err = launchWindow(ctx, exe, dir, home, profileName, token)
	}
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		l.stop()
		select {
		case <-l.done:
		case <-time.After(10 * time.Second):
			t.Error("test browser did not exit before profile cleanup")
		}
	})
	if l.transport == nil || l.endpoint != "" {
		t.Fatalf("not a pipe connection: %+v", l)
	}
	return l
}

// liveCheck attaches an engine, opens a page, captures it and checks the
// browser listens on no TCP port.
func liveCheck(t *testing.T, l *launched, dir string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	engine := browser.New()
	engine.UseTransport(l.transport)
	init, _ := json.Marshal(map[string]any{"connectTimeoutS": 30, "snapshotMaxChars": 12000, "maxEventEntries": 200,
		"maxBodyBytes": 2 << 20, "maxArtifactBytes": 32 << 20, "downloadHostPath": l.downloads, "downloadBrowserPath": l.downloads})
	if _, err := engine.Execute(ctx, "initialize", init); err != nil {
		t.Fatal("initialize:", err)
	}
	version, err := engine.Call(ctx, "Browser.getVersion", json.RawMessage(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("version: %s", version)
	created, err := engine.Call(ctx, "Target.createTarget", json.RawMessage(`{"url":"about:blank"}`))
	if err != nil {
		t.Fatal(err)
	}
	var target struct{ TargetID string }
	json.Unmarshal(created, &target)
	call := func(action string, params map[string]any) any {
		params["targetId"] = target.TargetID
		raw, _ := json.Marshal(params)
		result, err := engine.Execute(ctx, action, raw)
		if err != nil {
			t.Fatalf("%s: %v", action, err)
		}
		return result
	}
	call("navigate", map[string]any{"url": "data:text/html,<h1>OpenBear pipe check</h1>", "timeoutMs": 30000})
	out := filepath.Join(t.TempDir(), "shot.png")
	call("capture", map[string]any{"outputPath": out, "fullPage": true, "timeoutMs": 30000})
	if info, err := os.Stat(out); err != nil || info.Size() == 0 {
		t.Fatalf("screenshot: %v", err)
	}

	// A download lands in the download folder, is taken from there, and goes with its page.
	const content = "openbear download check"
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/file" {
			w.Header().Set("Content-Disposition", `attachment; filename="check.txt"`)
			w.Write([]byte(content))
			return
		}
		w.Write([]byte(`<a id="file" href="/file">file</a>`))
	}))
	defer srv.Close()
	before := dirNames(l.downloads)
	call("navigate", map[string]any{"url": srv.URL, "timeoutMs": 30000})
	call("evaluate", map[string]any{"expression": "document.getElementById('file').click()"})
	var id float64
	for deadline := time.Now().Add(30 * time.Second); id == 0; time.Sleep(200 * time.Millisecond) {
		if time.Now().After(deadline) {
			t.Fatal("download not seen")
		}
		var list struct{ Downloads []struct{ ID float64 } }
		data, _ := json.Marshal(call("files", map[string]any{"op": "list"}))
		json.Unmarshal(data, &list)
		if len(list.Downloads) > 0 {
			id = list.Downloads[0].ID
		}
	}
	saved := filepath.Join(t.TempDir(), "saved")
	call("files", map[string]any{"op": "save", "id": id, "outputPath": saved})
	if data, _ := os.ReadFile(saved); string(data) != content {
		t.Fatalf("saved %q", data)
	}
	var written []string
	for name := range dirNames(l.downloads) {
		if !before[name] {
			written = append(written, name)
		}
	}
	if len(written) == 0 {
		t.Fatalf("no file in %s", l.downloads)
	}

	engine.Call(ctx, "Target.closeTarget", json.RawMessage(`{"targetId":"`+target.TargetID+`"}`))
	for deadline := time.Now().Add(10 * time.Second); ; time.Sleep(200 * time.Millisecond) {
		left := dirNames(l.downloads)
		gone := true
		for _, name := range written {
			gone = gone && !left[name]
		}
		if gone {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("download %v kept after its page closed", written)
		}
	}
	// The engine is dropped without closing the pipe it shares with l.
	if listening := listeningPorts(t, recordedPID(t, dir)); len(listening) > 0 {
		t.Fatalf("browser listens on %v", listening)
	}
	profile := filepath.Join(dir, "profile")
	if !l.headless {
		profile = filepath.Dir(l.downloads)
	}
	if _, err := os.Stat(filepath.Join(profile, "DevToolsActivePort")); err == nil {
		t.Fatal("DevToolsActivePort written")
	}
}

func dirNames(dir string) map[string]bool {
	names := map[string]bool{}
	entries, _ := os.ReadDir(dir)
	for _, entry := range entries {
		names[entry.Name()] = true
	}
	return names
}

func recordedPID(t *testing.T, dir string) uint32 {
	t.Helper()
	r, ok := readBrowserRecord(dir)
	if !ok {
		t.Fatal("browser not recorded")
	}
	return r.PID
}

// listeningPorts lists the TCP ports pid listens on.
func listeningPorts(t *testing.T, pid uint32) []string {
	t.Helper()
	out, err := exec.Command("netstat", "-ano", "-p", "TCP").Output()
	if err != nil {
		t.Fatal(err)
	}
	var ports []string
	for _, line := range strings.Split(string(out), "\n") {
		fields := strings.Fields(line)
		if len(fields) == 5 && fields[3] == "LISTENING" && fields[4] == strconv.Itoa(int(pid)) {
			ports = append(ports, fields[1])
		}
	}
	return ports
}

// TestUserProfileStaysInTheUsersFolder runs what SYSTEM does in a user's
// profile before a window opens, then again with the profile replaced by a
// junction: nothing outside it is read or removed.
func TestUserProfileStaysInTheUsersFolder(t *testing.T) {
	ctx := context.Background()
	home := t.TempDir()
	profile, downloads, err := userProfile(home, "Browser")
	if err != nil {
		t.Fatal(err)
	}
	if info, err := os.Stat(downloads); err != nil || !info.IsDir() {
		t.Fatal("downloads not made")
	}
	want, _ := os.Stat(filepath.Join(home, `AppData\Local\OpenBear\Browser`))
	if got, err := os.Stat(profile); err != nil || !os.SameFile(got, want) {
		t.Fatalf("profile %s", profile)
	}

	lock := filepath.Join(profile, "lockfile")
	os.WriteFile(lock, nil, 0o600)
	if !waitProfile(ctx, profile, 0) || fileExists(lock) {
		t.Fatal("stale lockfile kept")
	}
	// A running browser keeps it open without delete sharing.
	os.WriteFile(lock, nil, 0o600)
	name, _ := syscall.UTF16PtrFromString(lock)
	held, err := syscall.CreateFile(name, syscall.GENERIC_WRITE, syscall.FILE_SHARE_READ, nil, syscall.OPEN_EXISTING, 0, 0)
	if err != nil {
		t.Fatal(err)
	}
	free := waitProfile(ctx, profile, 300*time.Millisecond)
	syscall.CloseHandle(held)
	if free {
		t.Fatal("held profile taken")
	}
	port := filepath.Join(profile, "DevToolsActivePort")
	os.WriteFile(port, []byte("1\n/devtools/browser/abc"), 0o600)
	if got := profilePort(profile); got != "ws://127.0.0.1:1/devtools/browser/abc" {
		t.Fatalf("port %q", got)
	}
	if err := releaseProfile(ctx, profile, t.TempDir()); err != nil || fileExists(port) || fileExists(lock) {
		t.Fatalf("release: %v", err)
	}

	outside := t.TempDir()
	for _, name := range []string{"lockfile", "DevToolsActivePort"} {
		os.WriteFile(filepath.Join(outside, name), []byte("1\n/devtools/browser/abc"), 0o600)
	}
	browserDir := filepath.Join(home, `AppData\Local\OpenBear\Browser`)
	os.RemoveAll(browserDir)
	if err := exec.Command("cmd", "/c", "mklink", "/J", browserDir, outside).Run(); err != nil {
		t.Fatal(err)
	}
	if _, _, err := userProfile(home, "Browser"); err == nil || fileExists(filepath.Join(outside, "OpenBear Downloads")) {
		t.Fatal("profile made through a junction")
	}
	if waitProfile(ctx, profile, 0) || profilePort(profile) != "" {
		t.Fatal("profile used through a junction")
	}
	for _, name := range []string{"lockfile", "DevToolsActivePort"} {
		if !fileExists(filepath.Join(outside, name)) {
			t.Fatalf("%s removed through a junction", name)
		}
	}
}

func TestUserProfileTestDirectoryIsSeparate(t *testing.T) {
	home := t.TempDir()
	formal := filepath.Join(home, `AppData\Local\OpenBear\Browser`)
	if err := os.MkdirAll(formal, 0o700); err != nil {
		t.Fatal(err)
	}
	sentinel := filepath.Join(formal, "lockfile")
	if err := os.WriteFile(sentinel, []byte("formal profile"), 0o600); err != nil {
		t.Fatal(err)
	}
	profile, downloads, err := userProfile(home, "BrowserTest-unit")
	if err != nil {
		t.Fatal(err)
	}
	if profile != filepath.Join(filepath.Dir(formal), "BrowserTest-unit") || filepath.Dir(downloads) != profile {
		t.Fatalf("test profile %q, downloads %q", profile, downloads)
	}
	if err := releaseProfile(context.Background(), profile, t.TempDir()); err != nil {
		t.Fatal(err)
	}
	if data, err := os.ReadFile(sentinel); err != nil || string(data) != "formal profile" {
		t.Fatal("formal profile changed", err)
	}
	for _, name := range []string{"", ".", "..", `..\Browser`, "C:\\Browser", "Browser:stream"} {
		if _, _, err := userProfile(home, name); err == nil {
			t.Fatalf("invalid profile name accepted: %q", name)
		}
	}
}

func TestUserProfileRejectsHomeJunction(t *testing.T) {
	base, outside := t.TempDir(), t.TempDir()
	home := filepath.Join(base, "home")
	if out, err := exec.Command("cmd", "/c", "mklink", "/J", home, outside).CombinedOutput(); err != nil {
		t.Fatalf("junction setup: %v: %s", err, out)
	}
	if _, _, err := userProfile(home, "Browser"); err == nil {
		t.Fatal("resolved home junction before validating it")
	}
	if entries, err := os.ReadDir(outside); err != nil || len(entries) != 0 {
		t.Fatal("created profile outside home", err)
	}
}

func TestProfilePortCleanupRefusesLeafReparse(t *testing.T) {
	home, outside := t.TempDir(), t.TempDir()
	profile, _, err := userProfile(home, "Browser")
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(profile, "DevToolsActivePort")
	if out, err := exec.Command("cmd", "/c", "mklink", "/J", path, outside).CombinedOutput(); err != nil {
		t.Fatalf("junction setup: %v: %s", err, out)
	}
	if err := releaseProfile(context.Background(), profile, t.TempDir()); err == nil {
		t.Fatal("ignored unsafe DevToolsActivePort cleanup")
	}
	if !fileExists(path) || !fileExists(outside) {
		t.Fatal("removed reparse point or target")
	}
}

func fileExists(path string) bool {
	_, err := os.Lstat(path)
	return err == nil
}

// TestBrowserPipeChild is the child half of
// TestBrowserPipesReachAChildByHandleValue: it finds its ends the way Chrome
// does, from --remote-debugging-io-pipes, and echoes one message.
func TestBrowserPipeChild(t *testing.T) {
	if os.Getenv("OPENBEAR_PIPE_CHILD") != "1" {
		return
	}
	for _, arg := range os.Args {
		if value, ok := strings.CutPrefix(arg, "--remote-debugging-io-pipes="); ok {
			ends := strings.Split(value, ",")
			read, _ := strconv.ParseUint(ends[0], 10, 32)
			write, _ := strconv.ParseUint(ends[1], 10, 32)
			in, out := os.NewFile(uintptr(read), "in"), os.NewFile(uintptr(write), "out")
			message, _ := bufio.NewReader(in).ReadBytes(0)
			out.Write(append([]byte("echo:"), message...))
			os.Exit(0)
		}
	}
	os.Exit(2)
}

func TestBrowserPipesReachAChildByHandleValue(t *testing.T) {
	pipes, err := newBrowserPipes()
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(os.Args[0], append([]string{"-test.run=^TestBrowserPipeChild$", "--"}, pipes.args()...)...)
	cmd.Env = append(os.Environ(), "OPENBEAR_PIPE_CHILD=1")
	cmd.SysProcAttr = &syscall.SysProcAttr{AdditionalInheritedHandles: []syscall.Handle{syscall.Handle(pipes.childRead), syscall.Handle(pipes.childWrite)}}
	if err := cmd.Start(); err != nil {
		pipes.close()
		t.Fatal(err)
	}
	pipes.started()
	transport := pipes.transport()
	defer transport.Close()
	if err := transport.Send(context.Background(), []byte(`{"id":1}`)); err != nil {
		t.Fatal(err)
	}
	reply, err := transport.Receive()
	if err != nil || string(reply) != `echo:{"id":1}` {
		t.Fatalf("%q %v", reply, err)
	}
	cmd.Wait()
	// The child's exit ends the node's reads: no other copy of its end is open.
	if _, err := transport.Receive(); err == nil {
		t.Fatal("read past the child's exit")
	}
}
