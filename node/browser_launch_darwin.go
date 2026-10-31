//go:build darwin

package node

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"strconv"
	"syscall"
	"time"

	"openbear/node/browser"
)

var headlessArgs = []string{"--headless=new"}

func browserDataDir(config string) string { return filepath.Join(filepath.Dir(config), "browser") }

func browserCandidates() []string {
	apps := []string{"Google Chrome.app/Contents/MacOS/Google Chrome", "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
		"Chromium.app/Contents/MacOS/Chromium", "Brave Browser.app/Contents/MacOS/Brave Browser"}
	var out []string
	for _, app := range apps {
		path := filepath.Join("/Applications", app)
		if info, err := os.Stat(path); err == nil && !info.IsDir() {
			out = append(out, path)
		}
	}
	return out
}

func browserCanRun(string) bool { return true }

// launchBrowser opens a window on the logged-in user's desktop, running as
// that user with a profile in their home; with nobody logged in it starts
// headless. A window left by an earlier node process is reused.
func launchBrowser(ctx context.Context, exe, dir string) (*launched, error) {
	uid := consoleUser()
	if uid <= 0 || os.Geteuid() != 0 {
		return launchHeadless(exe, dir)
	}
	account, err := user.LookupId(strconv.Itoa(uid))
	if err != nil {
		return launchHeadless(exe, dir)
	}
	gid, _ := strconv.Atoi(account.Gid)
	parts := []string{"Library", "Application Support", "OpenBear", "Browser", "OpenBear Downloads"}
	profile := filepath.Join(append([]string{account.HomeDir}, parts[:4]...)...)
	downloads := filepath.Join(profile, parts[4])
	if err = makeUserDirs(account.HomeDir, parts, 2, uid, gid); err != nil {
		return nil, err
	}
	if address := readDevToolsPort(profile); address != "" && devToolsAlive(ctx, address) {
		return reusedBrowser(address, profile, downloads), nil
	}
	clearDevToolsPort(profile)
	args := append([]string{"asuser", strconv.Itoa(uid), "/usr/bin/sudo", "-u", "#" + strconv.Itoa(uid), "-H", "--", exe},
		append(chromeArgs(profile, false), "--remote-debugging-port=0")...)
	cmd := exec.Command("/bin/launchctl", args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err = cmd.Start(); err != nil {
		return nil, errors.New("cannot start the browser: " + err.Error())
	}
	done := make(chan struct{})
	go func() { cmd.Wait(); close(done) }()
	address, err := devToolsPort(ctx, profile, 30*time.Second, done)
	if err != nil {
		syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		return nil, err
	}
	stop := func() {
		syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		}
	}
	return &launched{endpoint: address, downloads: downloads, stop: stop, done: done}, nil
}

func launchHeadless(exe, dir string) (*launched, error) {
	profile, downloads := filepath.Join(dir, "profile"), filepath.Join(dir, "downloads")
	for _, d := range []string{profile, downloads} {
		if err := os.MkdirAll(d, 0o700); err != nil {
			return nil, err
		}
	}
	for _, name := range []string{"SingletonLock", "SingletonSocket", "SingletonCookie"} {
		os.Remove(filepath.Join(profile, name))
	}
	toBrowser, browserIn, err := os.Pipe()
	if err != nil {
		return nil, err
	}
	browserOut, fromBrowser, err := os.Pipe()
	if err != nil {
		toBrowser.Close()
		browserIn.Close()
		return nil, err
	}
	cmd := exec.Command(exe, append(chromeArgs(profile, true), "--remote-debugging-pipe")...)
	cmd.ExtraFiles = []*os.File{toBrowser, fromBrowser}
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err = cmd.Start(); err != nil {
		for _, f := range []*os.File{toBrowser, browserIn, browserOut, fromBrowser} {
			f.Close()
		}
		return nil, errors.New("cannot start the browser: " + err.Error())
	}
	toBrowser.Close()
	fromBrowser.Close()
	done := make(chan struct{})
	go func() { cmd.Wait(); close(done) }()
	stop := func() {
		browserIn.Close()
		syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		}
		browserOut.Close()
	}
	closer := closerFunc(func() error { browserIn.Close(); return browserOut.Close() })
	return &launched{transport: browser.NewPipe(browserOut, browserIn, closer), downloads: downloads, headless: true, stop: stop, done: done}, nil
}

// consoleUser is the uid owning the login window session, 0 when nobody is logged in.
func consoleUser() int {
	var st syscall.Stat_t
	if syscall.Stat("/dev/console", &st) != nil {
		return 0
	}
	return int(st.Uid)
}

func checkLibraries(string) error { return nil }

type closerFunc func() error

func (f closerFunc) Close() error { return f() }
