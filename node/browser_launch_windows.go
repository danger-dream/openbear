//go:build windows

package node

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"

	"openbear/node/browser"
)

var headlessArgs = []string{"--headless=new"}

func browserDataDir(config string) string { return filepath.Join(filepath.Dir(config), "browser") }

func browserCandidates() []string {
	var candidates []string
	for _, root := range []string{os.Getenv("ProgramFiles"), os.Getenv("ProgramFiles(x86)")} {
		if root != "" {
			candidates = append(candidates, filepath.Join(root, `Google\Chrome\Application\chrome.exe`))
		}
	}
	// Chrome installed only for the signed-in user.
	if token, err := sessionToken(); err == nil {
		if home, err := token.GetUserProfileDirectory(); err == nil {
			candidates = append(candidates, filepath.Join(home, `AppData\Local\Google\Chrome\Application\chrome.exe`))
		}
		token.Close()
	}
	for _, root := range []string{os.Getenv("ProgramFiles(x86)"), os.Getenv("ProgramFiles")} {
		if root != "" {
			candidates = append(candidates, filepath.Join(root, `Microsoft\Edge\Application\msedge.exe`))
		}
	}
	var out []string
	for _, path := range candidates {
		if info, err := os.Stat(path); err == nil && !info.IsDir() {
			out = append(out, path)
		}
	}
	return out
}

func browserCanRun(string) bool { return true }

// sessionToken is the token of the user whose desktop gets the window; the
// node runs as SYSTEM. The console user comes first, then a remote desktop
// user: a connected one, else one whose connection dropped (the window is
// there when they reconnect).
func sessionToken() (windows.Token, error) {
	var token windows.Token
	if session := windows.WTSGetActiveConsoleSessionId(); session != 0xFFFFFFFF && windows.WTSQueryUserToken(session, &token) == nil {
		return token, nil
	}
	var sessions *windows.WTS_SESSION_INFO
	var count uint32
	if err := windows.WTSEnumerateSessions(0, 0, 1, &sessions, &count); err != nil {
		return 0, err
	}
	defer windows.WTSFreeMemory(uintptr(unsafe.Pointer(sessions)))
	list := unsafe.Slice(sessions, count)
	for _, state := range []uint32{windows.WTSActive, windows.WTSDisconnected} {
		for _, s := range list {
			if s.SessionID != 0 && s.State == state && windows.WTSQueryUserToken(s.SessionID, &token) == nil {
				return token, nil
			}
		}
	}
	return 0, errors.New("nobody is signed in")
}

// launchBrowser opens a window on the signed-in user's desktop, as that user
// with a profile in their AppData; with nobody signed in it starts headless.
// Either way the node talks to it over a pipe, so no debugging port is open.
func launchBrowser(ctx context.Context, exe, dir string) (*launched, error) {
	token, err := sessionToken()
	if err != nil {
		return launchHeadless(ctx, exe, dir)
	}
	defer token.Close()
	home, err := token.GetUserProfileDirectory()
	if err != nil {
		return launchHeadless(ctx, exe, dir)
	}
	return launchWindow(ctx, exe, dir, home, "Browser", token)
}

func launchWindow(ctx context.Context, exe, dir, home, profileName string, token windows.Token) (*launched, error) {
	profile, downloads, err := userProfile(home, profileName)
	if err != nil {
		return nil, err
	}
	if err = releaseProfile(ctx, profile, dir); err != nil {
		return nil, err
	}
	var env *uint16
	if err = windows.CreateEnvironmentBlock(&env, token, false); err != nil {
		return nil, err
	}
	defer windows.DestroyEnvironmentBlock(env)
	pipes, err := newBrowserPipes()
	if err != nil {
		return nil, err
	}
	line, _ := windows.UTF16PtrFromString(windows.ComposeCommandLine(append([]string{exe}, append(chromeArgs(profile, false), pipes.args()...)...)))
	app, _ := windows.UTF16PtrFromString(exe)
	desktop, _ := windows.UTF16PtrFromString(`winsta0\default`)
	cwd, _ := windows.UTF16PtrFromString(home)
	// Only the pipe ends are inherited, not whatever else the node has open.
	attrs, err := windows.NewProcThreadAttributeList(1)
	if err != nil {
		pipes.close()
		return nil, err
	}
	defer attrs.Delete()
	inherit := pipes.handles()
	if err = attrs.Update(windows.PROC_THREAD_ATTRIBUTE_HANDLE_LIST, unsafe.Pointer(&inherit[0]), uintptr(len(inherit))*unsafe.Sizeof(inherit[0])); err != nil {
		pipes.close()
		return nil, err
	}
	si := windows.StartupInfoEx{StartupInfo: windows.StartupInfo{Desktop: desktop}, ProcThreadAttributeList: attrs.List()}
	si.Cb = uint32(unsafe.Sizeof(si))
	var pi windows.ProcessInformation
	if err = windows.CreateProcessAsUser(token, app, line, nil, nil, true,
		windows.CREATE_UNICODE_ENVIRONMENT|windows.CREATE_NEW_PROCESS_GROUP|windows.EXTENDED_STARTUPINFO_PRESENT,
		env, cwd, &si.StartupInfo, &pi); err != nil {
		pipes.close()
		return nil, fmt.Errorf("cannot start the browser on the user's desktop (cwd=%q profile=%q): %w", home, profile, err)
	}
	pipes.started()
	windows.CloseHandle(pi.Thread)
	recordBrowser(dir, pi.Process, pi.ProcessId, exe)
	// The handle is closed once the process ends; terminating after that
	// could hit a handle the system has since given to something else.
	var handleMu sync.Mutex
	handleOpen := true
	terminate := func() {
		handleMu.Lock()
		defer handleMu.Unlock()
		if handleOpen {
			windows.TerminateProcess(pi.Process, 1)
		}
	}
	done := make(chan struct{})
	go func() {
		windows.WaitForSingleObject(pi.Process, windows.INFINITE)
		handleMu.Lock()
		handleOpen = false
		windows.CloseHandle(pi.Process)
		handleMu.Unlock()
		forgetBrowser(dir, pi.ProcessId)
		close(done)
	}()
	stop := func() {
		// Without its pipe the browser closes its windows itself.
		pipes.toBrowser.Close()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			terminate()
		}
		pipes.fromBrowser.Close()
	}
	return &launched{transport: pipes.transport(), downloads: downloads, stop: stop, done: done}, nil
}

// Directories inherit the user's access, but no part of home may redirect
// SYSTEM's file operations through a reparse point.
func userProfile(home, name string) (profile, downloads string, err error) {
	if name == "" || name == "." || name == ".." || strings.ContainsAny(name, `\/:`) {
		return "", "", windows.ERROR_INVALID_NAME
	}
	profile = filepath.Join(home, `AppData\Local\OpenBear`, name)
	downloads = filepath.Join(profile, "OpenBear Downloads")
	return profile, downloads, browser.MkdirAllPlain(downloads)
}

func launchHeadless(ctx context.Context, exe, dir string) (*launched, error) {
	profile, downloads := filepath.Join(dir, "profile"), filepath.Join(dir, "downloads")
	for _, d := range []string{profile, downloads} {
		if err := browser.MkdirAllPlain(d); err != nil {
			return nil, err
		}
	}
	if err := releaseProfile(ctx, profile, dir); err != nil {
		return nil, err
	}
	pipes, err := newBrowserPipes()
	if err != nil {
		return nil, err
	}
	cmd := exec.Command(exe, append(chromeArgs(profile, true), pipes.args()...)...)
	cmd.SysProcAttr = &syscall.SysProcAttr{AdditionalInheritedHandles: []syscall.Handle{syscall.Handle(pipes.childRead), syscall.Handle(pipes.childWrite)}}
	if err := cmd.Start(); err != nil {
		pipes.close()
		return nil, errors.New("cannot start the browser: " + err.Error())
	}
	pipes.started()
	// Unlike a window on a user's desktop, a headless browser ends with the
	// node: an orphan would keep the profile and fail the next node's start.
	job, jobErr := killJob(cmd.Process.Pid)
	if jobErr == nil {
		var limit windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION
		limit.BasicLimitInformation.LimitFlags = windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
		windows.SetInformationJobObject(job, windows.JobObjectExtendedLimitInformation, uintptr(unsafe.Pointer(&limit)), uint32(unsafe.Sizeof(limit)))
	}
	if process, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(cmd.Process.Pid)); err == nil {
		recordBrowser(dir, process, uint32(cmd.Process.Pid), exe)
		windows.CloseHandle(process)
	}
	done := make(chan struct{})
	go func() {
		cmd.Wait()
		if jobErr == nil {
			windows.CloseHandle(job)
		}
		forgetBrowser(dir, uint32(cmd.Process.Pid))
		close(done)
	}()
	stop := func() {
		pipes.toBrowser.Close()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			cmd.Process.Kill()
		}
		pipes.fromBrowser.Close()
	}
	return &launched{transport: pipes.transport(), downloads: downloads, headless: true, stop: stop, done: done}, nil
}

// browserPipes are the anonymous pipes of --remote-debugging-pipe. Windows
// has no descriptors 3 and 4 to hand over, so the browser's ends are
// inherited and named by value in --remote-debugging-io-pipes (Chrome 117+).
type browserPipes struct {
	childRead, childWrite windows.Handle // the browser's ends
	toBrowser             *os.File
	fromBrowser           *os.File
}

func newBrowserPipes() (*browserPipes, error) {
	var commandsRead, commandsWrite, repliesRead, repliesWrite windows.Handle
	if err := windows.CreatePipe(&commandsRead, &commandsWrite, nil, 1<<16); err != nil {
		return nil, err
	}
	if err := windows.CreatePipe(&repliesRead, &repliesWrite, nil, 1<<16); err != nil {
		windows.CloseHandle(commandsRead)
		windows.CloseHandle(commandsWrite)
		return nil, err
	}
	p := &browserPipes{childRead: commandsRead, childWrite: repliesWrite,
		toBrowser: os.NewFile(uintptr(commandsWrite), "browser-commands"), fromBrowser: os.NewFile(uintptr(repliesRead), "browser-replies")}
	for _, h := range p.handles() {
		if err := windows.SetHandleInformation(h, windows.HANDLE_FLAG_INHERIT, windows.HANDLE_FLAG_INHERIT); err != nil {
			p.close()
			return nil, err
		}
	}
	return p, nil
}

func (p *browserPipes) handles() []windows.Handle { return []windows.Handle{p.childRead, p.childWrite} }

func (p *browserPipes) args() []string {
	return []string{"--remote-debugging-pipe", fmt.Sprintf("--remote-debugging-io-pipes=%d,%d", p.childRead, p.childWrite)}
}

// started closes the browser's ends here, so its exit ends the node's reads.
func (p *browserPipes) started() {
	for _, h := range p.handles() {
		windows.CloseHandle(h)
	}
	p.childRead, p.childWrite = 0, 0
}

func (p *browserPipes) close() {
	if p.childRead != 0 {
		p.started()
	}
	p.toBrowser.Close()
	p.fromBrowser.Close()
}

func (p *browserPipes) transport() browser.Transport {
	return browser.NewPipe(p.fromBrowser, p.toBrowser, closerFunc(func() error {
		p.toBrowser.Close()
		return p.fromBrowser.Close()
	}))
}

// releaseProfile ends a browser still running on profile before another
// starts there: a second one would hand its command line to the first and
// exit, and a pipe cannot be attached to a browser already running. One an
// earlier node started on a pipe closes itself when that pipe goes away;
// one an older node started with a debugging port is asked to close over
// it; one that lingers is ended only if this node recorded starting it.
func releaseProfile(ctx context.Context, profile, dir string) error {
	if address := profilePort(profile); address != "" && devToolsAlive(ctx, address) {
		closeDevTools(ctx, address)
	}
	held := !waitProfile(ctx, profile, 10*time.Second)
	if held && ctx.Err() == nil && endRecordedBrowser(dir) {
		held = !waitProfile(ctx, profile, 5*time.Second)
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	if held {
		return errors.New("another browser is running on the profile " + profile + "; close it and try again")
	}
	if err := browser.RemovePlain(filepath.Join(profile, "DevToolsActivePort")); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return err
	}
	return nil
}

// profilePort is readDevToolsPort for a profile SYSTEM must read through a handle.
func profilePort(profile string) string {
	file, err := browser.OpenPlain(filepath.Join(profile, "DevToolsActivePort"))
	if err != nil {
		return ""
	}
	defer file.Close()
	data, _ := io.ReadAll(io.LimitReader(file, 4096))
	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	if len(lines) < 2 {
		return ""
	}
	port, path := strings.TrimSpace(lines[0]), strings.TrimSpace(lines[1])
	if n, err := strconv.Atoi(port); err != nil || n < 1 || n > 65535 || strconv.Itoa(n) != port || !devToolsPath.MatchString(path) {
		return ""
	}
	return "ws://127.0.0.1:" + port + path
}

// waitProfile waits until no browser holds profile. A running browser keeps
// its lockfile open without delete sharing; one left by a crash is removed.
func waitProfile(ctx context.Context, profile string, wait time.Duration) bool {
	lock := filepath.Join(profile, "lockfile")
	for deadline := time.Now().Add(wait); ; {
		if err := browser.RemovePlain(lock); err == nil || errors.Is(err, fs.ErrNotExist) {
			return true
		}
		if time.Now().After(deadline) || ctx.Err() != nil {
			return false
		}
		time.Sleep(200 * time.Millisecond)
	}
}

// closeDevTools asks the browser at a DevTools address to quit.
func closeDevTools(ctx context.Context, address string) {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	t, err := browser.DialWebSocket(ctx, address)
	if err != nil {
		return
	}
	defer t.Close()
	if t.Send(ctx, []byte(`{"id":1,"method":"Browser.close"}`)) != nil {
		return
	}
	replied := make(chan struct{})
	go func() { t.Receive(); close(replied) }()
	select {
	case <-replied:
	case <-ctx.Done():
	}
}

// browserRecord identifies the browser process this node started last. It is
// kept in the node's own directory: a user who could edit it would choose
// what SYSTEM ends.
type browserRecord struct {
	PID        uint32 `json:"pid"`
	Created    int64  `json:"created"`
	Executable string `json:"executable"`
}

func browserRecordPath(dir string) string { return filepath.Join(dir, "browser-process.json") }

func recordBrowser(dir string, process windows.Handle, pid uint32, exe string) {
	var created, exited, kernel, user windows.Filetime
	if windows.GetProcessTimes(process, &created, &exited, &kernel, &user) != nil {
		return
	}
	data, _ := json.Marshal(browserRecord{PID: pid, Created: created.Nanoseconds(), Executable: exe})
	os.WriteFile(browserRecordPath(dir), data, 0o600)
}

func readBrowserRecord(dir string) (browserRecord, bool) {
	var r browserRecord
	data, err := os.ReadFile(browserRecordPath(dir))
	return r, err == nil && json.Unmarshal(data, &r) == nil && r.PID != 0
}

// forgetBrowser drops the record of pid once that process has ended.
func forgetBrowser(dir string, pid uint32) {
	if r, ok := readBrowserRecord(dir); ok && r.PID == pid {
		os.Remove(browserRecordPath(dir))
	}
}

// endRecordedBrowser ends the recorded browser if that very process (same
// start time and program) still runs.
func endRecordedBrowser(dir string) bool {
	r, ok := readBrowserRecord(dir)
	if !ok {
		return false
	}
	process, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION|windows.PROCESS_TERMINATE|windows.SYNCHRONIZE, false, r.PID)
	if err != nil {
		return false
	}
	defer windows.CloseHandle(process)
	var created, exited, kernel, user windows.Filetime
	if windows.GetProcessTimes(process, &created, &exited, &kernel, &user) != nil || created.Nanoseconds() != r.Created {
		return false
	}
	name := make([]uint16, windows.MAX_LONG_PATH)
	size := uint32(len(name))
	if windows.QueryFullProcessImageName(process, 0, &name[0], &size) != nil || !strings.EqualFold(windows.UTF16ToString(name[:size]), r.Executable) {
		return false
	}
	if windows.TerminateProcess(process, 1) != nil {
		return false
	}
	windows.WaitForSingleObject(process, 5000)
	return true
}

func checkLibraries(string) error { return nil }

type closerFunc func() error

func (f closerFunc) Close() error { return f() }
