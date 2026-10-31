package node

import (
	"archive/zip"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"
)

var chromeVersionsURL = "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json"

// chromePlatform is the Chrome for Testing build for this machine; Windows on
// ARM runs the x64 build.
func chromePlatform() string {
	return map[string]string{"linux/amd64": "linux64", "linux/arm64": "linux-arm64", "darwin/arm64": "mac-arm64", "darwin/amd64": "mac-x64",
		"windows/amd64": "win64", "windows/arm64": "win64"}[runtime.GOOS+"/"+runtime.GOARCH]
}

type chromeInstall struct {
	Version    string `json:"version"`
	Executable string `json:"executable"` // relative to the browser directory
}

func downloadedBrowser(dir string) string {
	data, err := os.ReadFile(filepath.Join(dir, "chrome.json"))
	var c chromeInstall
	if err != nil || json.Unmarshal(data, &c) != nil || c.Executable == "" || filepath.IsAbs(c.Executable) || strings.Contains(c.Executable, "..") {
		return ""
	}
	exe := filepath.Join(dir, c.Executable)
	if _, err = os.Stat(exe); err != nil {
		return ""
	}
	return exe
}

// downloadChrome installs the current stable Chrome for Testing into dir and
// removes older ones. progress gets the bytes downloaded so far.
func downloadChrome(ctx context.Context, dir string, progress func(int64)) error {
	platform := chromePlatform()
	if platform == "" {
		return fmt.Errorf("no Chrome for Testing build for %s/%s", runtime.GOOS, runtime.GOARCH)
	}
	client := &http.Client{Timeout: 0}
	listCtx, cancel := context.WithTimeout(ctx, time.Minute)
	defer cancel()
	req, _ := http.NewRequestWithContext(listCtx, http.MethodGet, chromeVersionsURL, nil)
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	var listing struct {
		Channels map[string]struct {
			Version   string `json:"version"`
			Downloads map[string][]struct {
				Platform string `json:"platform"`
				URL      string `json:"url"`
			} `json:"downloads"`
		} `json:"channels"`
	}
	err = json.NewDecoder(io.LimitReader(resp.Body, 4<<20)).Decode(&listing)
	resp.Body.Close()
	if err != nil || resp.StatusCode != http.StatusOK {
		return errors.New("cannot read the Chrome for Testing version list")
	}
	stable := listing.Channels["Stable"]
	link := ""
	for _, d := range stable.Downloads["chrome"] {
		if d.Platform == platform {
			link = d.URL
		}
	}
	if stable.Version == "" || !strings.HasPrefix(link, "https://") || strings.ContainsAny(stable.Version, `/\`) {
		return errors.New("no stable Chrome for Testing build for " + platform)
	}
	if err = os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	archive := filepath.Join(dir, "chrome-download.zip")
	defer os.Remove(archive)
	if err = fetchFile(ctx, client, link, archive, progress); err != nil {
		return err
	}
	target := filepath.Join(dir, "chrome-"+stable.Version)
	staging := target + ".partial"
	os.RemoveAll(staging)
	if err = unzip(archive, staging); err != nil {
		os.RemoveAll(staging)
		return err
	}
	exe := findChromeIn(staging)
	if exe == "" {
		os.RemoveAll(staging)
		return errors.New("the download has no browser executable")
	}
	os.RemoveAll(target)
	if err = os.Rename(staging, target); err != nil {
		return err
	}
	exe = filepath.Join(target, strings.TrimPrefix(exe, staging+string(filepath.Separator)))
	rel, _ := filepath.Rel(dir, exe)
	data, _ := json.Marshal(chromeInstall{Version: stable.Version, Executable: rel})
	if err = os.WriteFile(filepath.Join(dir, "chrome.json"), data, 0o644); err != nil {
		return err
	}
	entries, _ := os.ReadDir(dir)
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), "chrome-") && e.IsDir() && e.Name() != filepath.Base(target) {
			os.RemoveAll(filepath.Join(dir, e.Name()))
		}
	}
	// Kept installed either way: once the libraries are added it starts
	// without another download.
	return checkLibraries(exe)
}

// fetchStall ends a download that has received nothing for this long, so a
// dead connection does not hold the browser in "installing" for half an hour.
var fetchStall = time.Minute

func fetchFile(ctx context.Context, client *http.Client, link, path string, progress func(int64)) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	stalled := false
	var stallMu sync.Mutex
	watchdog := time.AfterFunc(fetchStall, func() {
		stallMu.Lock()
		stalled = true
		stallMu.Unlock()
		cancel()
	})
	defer watchdog.Stop()
	err := fetchBody(ctx, client, link, path, progress, func() { watchdog.Reset(fetchStall) })
	stallMu.Lock()
	defer stallMu.Unlock()
	if err != nil && stalled {
		return errors.New("the download stalled: nothing received for " + fetchStall.String())
	}
	return err
}

func fetchBody(ctx context.Context, client *http.Client, link, path string, progress func(int64), alive func()) error {
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, link, nil)
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("download failed: HTTP %d", resp.StatusCode)
	}
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o600)
	if err != nil {
		return err
	}
	defer f.Close()
	var done int64
	buf := make([]byte, 256<<10)
	last := time.Now()
	for {
		n, err := resp.Body.Read(buf)
		if n > 0 {
			alive()
			if _, werr := f.Write(buf[:n]); werr != nil {
				return werr
			}
			done += int64(n)
			if time.Since(last) > time.Second {
				progress(done)
				last = time.Now()
			}
		}
		if errors.Is(err, io.EOF) {
			progress(done)
			return nil
		}
		if err != nil {
			return err
		}
	}
}

// unzip keeps executable bits and the symlinks inside macOS app bundles, and
// refuses entries that would land outside dest. Chrome for Testing publishes
// no checksums, so the archive itself is not trusted: nothing is written
// through a link, and every link must resolve inside dest.
func unzip(archive, dest string) error {
	r, err := zip.OpenReader(archive)
	if err != nil {
		return err
	}
	defer r.Close()
	root, _ := filepath.Abs(dest)
	if err = os.MkdirAll(root, 0o755); err != nil {
		return err
	}
	var links []string
	for _, f := range r.File {
		path := filepath.Join(root, filepath.FromSlash(f.Name))
		if rel, err := filepath.Rel(root, path); err != nil || strings.HasPrefix(rel, "..") || throughLink(root, path) {
			return errors.New("unsafe path in archive")
		}
		mode := f.Mode()
		switch {
		case mode.IsDir():
			if err = os.MkdirAll(path, 0o755); err != nil {
				return err
			}
			continue
		case mode&os.ModeSymlink != 0:
			rc, err := f.Open()
			if err != nil {
				return err
			}
			link, err := io.ReadAll(io.LimitReader(rc, 4096))
			rc.Close()
			if err != nil {
				return err
			}
			resolved := filepath.Join(filepath.Dir(path), string(link))
			if rel, err := filepath.Rel(root, resolved); err != nil || strings.HasPrefix(rel, "..") || filepath.IsAbs(string(link)) {
				return errors.New("unsafe link in archive")
			}
			os.MkdirAll(filepath.Dir(path), 0o755)
			if err = os.Symlink(string(link), path); err != nil {
				return err
			}
			links = append(links, path)
			continue
		}
		if err = os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			return err
		}
		perm := os.FileMode(0o644)
		if mode&0o111 != 0 {
			perm = 0o755
		}
		if err = extractFile(f, path, perm); err != nil {
			return err
		}
	}
	// A target checked by its text can still leave dest through another
	// link (a/b -> "../x" where a is itself a link); check what each resolves to.
	realRoot, err := filepath.EvalSymlinks(root)
	if err != nil {
		return err
	}
	for _, link := range links {
		real, err := filepath.EvalSymlinks(link)
		if err != nil {
			return errors.New("unsafe link in archive")
		}
		if rel, err := filepath.Rel(realRoot, real); err != nil || strings.HasPrefix(rel, "..") {
			return errors.New("unsafe link in archive")
		}
	}
	return nil
}

// throughLink tells whether path, below root, already exists as a link or
// lies under one: writing there would follow it.
func throughLink(root, path string) bool {
	rel, err := filepath.Rel(root, path)
	if err != nil {
		return true
	}
	current := root
	for _, part := range strings.Split(rel, string(filepath.Separator)) {
		current = filepath.Join(current, part)
		info, err := os.Lstat(current)
		if err != nil {
			return false
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return true
		}
	}
	return false
}

func extractFile(f *zip.File, path string, perm os.FileMode) error {
	rc, err := f.Open()
	if err != nil {
		return err
	}
	defer rc.Close()
	out, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, perm)
	if err != nil {
		return err
	}
	_, err = io.Copy(out, rc)
	if cerr := out.Close(); err == nil {
		err = cerr
	}
	return err
}

// findChromeIn locates the executable of an unpacked Chrome for Testing.
func findChromeIn(dir string) string {
	names := map[string]bool{"chrome": runtime.GOOS == "linux", "chrome.exe": runtime.GOOS == "windows",
		"Google Chrome for Testing": runtime.GOOS == "darwin"}
	found := ""
	filepath.WalkDir(dir, func(path string, d os.DirEntry, err error) error {
		if err != nil || found != "" {
			return nil
		}
		if !d.IsDir() && names[d.Name()] && (runtime.GOOS != "darwin" || filepath.Base(filepath.Dir(path)) == "MacOS") {
			found = path
		}
		return nil
	})
	return found
}

// chromeArgs are shared by every browser the node starts.
func chromeArgs(profile string, headless bool) []string {
	// Remote debugging alone sets navigator.webdriver, even in a window on a desktop.
	args := []string{"--user-data-dir=" + profile, "--no-first-run", "--no-default-browser-check", "--disable-search-engine-choice-screen",
		"--password-store=basic", "--window-size=1366,900", "--disable-blink-features=AutomationControlled"}
	if headless {
		args = append(args, headlessArgs...)
	}
	return args
}

// clearDevToolsPort removes a previous browser's address so a new one is not
// mistaken for it.
func clearDevToolsPort(profile string) { os.Remove(filepath.Join(profile, "DevToolsActivePort")) }

// devToolsPort waits for a browser started with --remote-debugging-port=0 to
// write its address.
func devToolsPort(ctx context.Context, profile string, wait time.Duration, exited <-chan struct{}) (string, error) {
	deadline := time.Now().Add(wait)
	for time.Now().Before(deadline) {
		if address := readDevToolsPort(profile); address != "" {
			return address, nil
		}
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-exited:
			return "", errors.New("the browser exited while starting")
		case <-time.After(200 * time.Millisecond):
		}
	}
	return "", errors.New("the browser did not open its DevTools port")
}

var devToolsPath = regexp.MustCompile(`^/devtools/browser/[A-Za-z0-9-]+$`)

// readDevToolsPort reads the address only as a port on 127.0.0.1: the file
// sits in a profile its user can write.
func readDevToolsPort(profile string) string {
	data, err := os.ReadFile(filepath.Join(profile, "DevToolsActivePort"))
	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	if err != nil || len(lines) < 2 {
		return ""
	}
	port, path := strings.TrimSpace(lines[0]), strings.TrimSpace(lines[1])
	if n, err := strconv.Atoi(port); err != nil || n < 1 || n > 65535 || strconv.Itoa(n) != port || !devToolsPath.MatchString(path) {
		return ""
	}
	return "ws://127.0.0.1:" + port + path
}
