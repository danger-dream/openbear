//go:build linux

package node

import (
	"bytes"
	"context"
	"errors"
	"io"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"golang.org/x/sys/unix"

	"openbear/node/browser"
)

const browserAccount = "openbear-browser"

// headlessArgs run the ordinary browser on the display-less platform instead
// of --headless, which tells sites it is headless (HeadlessChrome in the user
// agent, an 800x600 screen). Unlike --headless it would quit with its last
// tab; keep-alive keeps it for the next page.
var headlessArgs = []string{"--ozone-platform=headless", "--ozone-override-screen-size=1920,1080", "--keep-alive-for-test"}

// browserDataDir keeps the browser outside /etc/openbear-node, which only root
// may enter; the browser account must reach its profile.
func browserDataDir(config string) string {
	if config == DefaultConfigPath {
		return "/var/lib/openbear-node/browser"
	}
	return filepath.Join(filepath.Dir(config), "browser")
}

func browserCandidates() []string {
	var out []string
	for _, name := range []string{"google-chrome-stable", "google-chrome", "chromium", "chromium-browser", "microsoft-edge-stable", "microsoft-edge", "brave-browser"} {
		// Snap packages cannot use a profile outside the user's home.
		if path, err := exec.LookPath(name); err == nil && !strings.HasPrefix(path, "/snap/") && !snapWrapper(path) {
			out = append(out, path)
		}
	}
	for _, path := range []string{"/opt/google/chrome/chrome", "/usr/lib/chromium/chromium"} {
		if info, err := os.Stat(path); err == nil && info.Mode()&0o111 != 0 {
			out = append(out, path)
		}
	}
	return out
}

// snapWrapper: a script handing over to a snap, as Ubuntu's chromium-browser package installs
// in /usr/bin since 20.04.
func snapWrapper(path string) bool {
	f, err := os.Open(path)
	if err != nil {
		return false
	}
	defer f.Close()
	head := make([]byte, 4096)
	n, _ := io.ReadFull(f, head)
	return bytes.HasPrefix(head[:n], []byte("#!")) && bytes.Contains(head[:n], []byte("/snap/"))
}

// browserCanRun tells whether the account a root node runs the browser as can
// reach and execute exe; a hardened /opt, for one, keeps it out.
func browserCanRun(exe string) bool {
	if os.Geteuid() != 0 {
		return true
	}
	name := os.Getenv("OPENBEAR_BROWSER_USER")
	if name == "" {
		name = browserAccount
	}
	uid, gid := -1, -1
	if account, err := user.Lookup(name); err == nil {
		uid, _ = strconv.Atoi(account.Uid)
		gid, _ = strconv.Atoi(account.Gid)
	}
	real, err := filepath.EvalSymlinks(exe)
	if err != nil {
		return false
	}
	for p := real; ; p = filepath.Dir(p) {
		info, err := os.Stat(p)
		if err != nil {
			return false
		}
		st, ok := info.Sys().(*syscall.Stat_t)
		mode := info.Mode().Perm()
		bit := mode & 0o001
		if ok && int(st.Uid) == uid {
			bit = mode & 0o100
		} else if ok && int(st.Gid) == gid {
			bit = mode & 0o010
		}
		if bit == 0 {
			return false
		}
		if p == "/" || p == "." {
			return true
		}
	}
}

// launchBrowser starts a headless browser connected through a pipe, so no
// port is opened. A root node runs it as an unprivileged account to keep
// Chrome's sandbox.
func launchBrowser(ctx context.Context, exe, dir string) (*launched, error) {
	credential, err := browserCredential(dir)
	if err != nil {
		return nil, err
	}
	profile, downloads, home := filepath.Join(dir, "profile"), filepath.Join(dir, "downloads"), filepath.Join(dir, "home")
	for _, d := range []string{profile, downloads, home} {
		if credential != nil {
			err = accountDir(dir, filepath.Base(d), int(credential.Uid), int(credential.Gid))
		} else {
			err = os.MkdirAll(d, 0o700)
		}
		if err != nil {
			return nil, err
		}
	}
	if credential != nil {
		os.Chmod(filepath.Dir(dir), 0o711)
	}
	uid := os.Geteuid()
	if credential != nil {
		uid = int(credential.Uid)
	}
	releaseProfile(profile, uid)
	args := append(chromeArgs(profile, true), "--remote-debugging-pipe")
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
	// Read here rather than by exec, whose Wait would also wait for every
	// helper process that inherited stderr.
	errRead, errWrite, err := os.Pipe()
	if err != nil {
		for _, f := range []*os.File{toBrowser, browserIn, browserOut, fromBrowser} {
			f.Close()
		}
		return nil, err
	}
	cmd := exec.Command(exe, args...)
	cmd.Dir = home
	cmd.Env = []string{"HOME=" + home, "TMPDIR=" + home, "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG=C.UTF-8"}
	cmd.Stderr = errWrite
	cmd.ExtraFiles = []*os.File{toBrowser, fromBrowser} // fd 3: commands in, fd 4: replies out
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true, Pdeathsig: syscall.SIGKILL, Credential: credential}
	if err = cmd.Start(); err != nil {
		for _, f := range []*os.File{toBrowser, browserIn, browserOut, fromBrowser, errRead, errWrite} {
			f.Close()
		}
		return nil, errors.New("cannot start the browser: " + err.Error())
	}
	toBrowser.Close()
	fromBrowser.Close()
	errWrite.Close()
	output := &outputTail{}
	go output.fill(errRead)
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
	return &launched{transport: browser.NewPipe(browserOut, browserIn, closer), downloads: downloads, headless: true, stop: stop, done: done,
		output: output.String}, nil
}

// outputTail keeps the last lines a browser wrote, to explain a failed start.
type outputTail struct {
	mu  sync.Mutex
	buf []byte
}

func (t *outputTail) fill(r *os.File) {
	defer r.Close()
	chunk := make([]byte, 4096)
	for {
		n, err := r.Read(chunk)
		t.mu.Lock()
		t.buf = append(t.buf, chunk[:n]...)
		if len(t.buf) > 2048 {
			t.buf = append([]byte(nil), t.buf[len(t.buf)-1024:]...)
		}
		t.mu.Unlock()
		if err != nil {
			return
		}
	}
}

func (t *outputTail) String() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	lines := strings.Split(strings.TrimSpace(strings.ToValidUTF8(string(t.buf), "")), "\n")
	if len(lines) > 4 {
		lines = lines[len(lines)-4:]
	}
	out := strings.TrimSpace(strings.Join(lines, " | "))
	if len(out) > 600 {
		out = strings.ToValidUTF8(out[len(out)-600:], "")
	}
	return out
}

// browserCredential is the account the browser runs as: openbear-browser
// (created when missing), else nobody. A node not running as root keeps its own.
func browserCredential(dir string) (*syscall.Credential, error) {
	if os.Geteuid() != 0 {
		return nil, nil
	}
	return browserCredentialFor(dir, user.Lookup, func(name string, args ...string) error { return exec.Command(name, args...).Run() })
}

func browserCredentialFor(dir string, lookup func(string) (*user.User, error), run func(string, ...string) error) (*syscall.Credential, error) {
	// OPENBEAR_BROWSER_USER names an existing account to use instead.
	if name := os.Getenv("OPENBEAR_BROWSER_USER"); name != "" {
		account, err := lookup(name)
		if err != nil {
			return nil, errors.New("browser_account_unavailable: OPENBEAR_BROWSER_USER names no account")
		}
		return accountCredential(account)
	}
	account, err := lookup(browserAccount)
	if err != nil {
		nologin := "/usr/sbin/nologin"
		if _, err := os.Stat(nologin); err != nil {
			nologin = "/bin/false"
		}
		if run("useradd", "--system", "--user-group", "--no-create-home", "--home-dir", filepath.Join(dir, "home"), "--shell", nologin, browserAccount) != nil {
			run("adduser", "-S", "-D", "-H", "-h", filepath.Join(dir, "home"), "-s", nologin, browserAccount)
		}
		if account, err = lookup(browserAccount); err != nil {
			if account, err = lookup("nobody"); err != nil {
				return nil, errors.New("browser_account_unavailable")
			}
		}
	}
	return accountCredential(account)
}

func accountCredential(account *user.User) (*syscall.Credential, error) {
	uid, uidErr := strconv.ParseUint(account.Uid, 10, 32)
	gid, gidErr := strconv.ParseUint(account.Gid, 10, 32)
	if uidErr != nil || gidErr != nil || uid == 0 {
		return nil, errors.New("browser_account_unavailable: a non-root account is required")
	}
	// Groups stays empty so setgroups drops root's supplementary groups.
	return &syscall.Credential{Uid: uint32(uid), Gid: uint32(gid)}, nil
}

// Every component is opened without following links, including dir's parents.
// The top owner marks a completed handover, so it is changed only after success.
func accountDir(dir, name string, uid, gid int) error {
	if dir == "" {
		return unix.ENOENT
	}
	if name == "" || name == "." || name == ".." || strings.ContainsRune(name, '/') {
		return unix.EINVAL
	}
	start := "."
	if filepath.IsAbs(dir) {
		start = "/"
	}
	parent, err := unix.Open(start, unix.O_PATH|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return err
	}
	for _, part := range strings.Split(dir, "/") {
		if part == "" || part == "." {
			continue
		}
		next, err := unix.Openat(parent, part, unix.O_PATH|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
		unix.Close(parent)
		if err != nil {
			return err
		}
		parent = next
	}
	defer unix.Close(parent)
	if err = unix.Mkdirat(parent, name, 0o700); err != nil && !errors.Is(err, unix.EEXIST) {
		return err
	}
	fd, err := unix.Openat(parent, name, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return &os.PathError{Op: "open", Path: filepath.Join(dir, name), Err: err}
	}
	defer unix.Close(fd)
	var st unix.Stat_t
	if err = unix.Fstat(fd, &st); err != nil {
		return err
	}
	if int(st.Uid) == uid && int(st.Gid) == gid {
		return nil
	}
	if err = handOver(fd, uid, gid); err != nil {
		return err
	}
	return unix.Fchown(fd, uid, gid)
}

func handOver(fd, uid, gid int) error {
	var names []string
	buf := make([]byte, 8192)
	for {
		n, err := unix.Getdents(fd, buf)
		if err != nil {
			return err
		}
		if n == 0 {
			break
		}
		_, _, names = unix.ParseDirent(buf[:n], -1, names)
	}
	for _, name := range names {
		if err := handOverEntry(fd, name, uid, gid); err != nil && !errors.Is(err, unix.ENOENT) {
			return err
		}
	}
	return nil
}

func handOverEntry(fd int, name string, uid, gid int) error {
	// O_PATH opens nothing a device or FIFO would notice.
	entry, err := unix.Openat(fd, name, unix.O_PATH|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return err
	}
	defer unix.Close(entry)
	var st unix.Stat_t
	if err = unix.Fstat(entry, &st); err != nil {
		return err
	}
	switch st.Mode & unix.S_IFMT {
	case unix.S_IFDIR:
		sub, err := unix.Openat(entry, ".", unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
		if err != nil {
			return err
		}
		err = handOver(sub, uid, gid)
		unix.Close(sub)
		if err != nil {
			return err
		}
	case unix.S_IFREG:
		// Another hard link could be outside the browser's tree.
		if st.Nlink != 1 {
			return nil
		}
	default:
		return nil
	}
	return unix.Fchownat(entry, "", uid, gid, unix.AT_EMPTY_PATH|unix.AT_SYMLINK_NOFOLLOW)
}

// releaseProfile ends a browser left on this profile by an earlier node
// process (the profile is the node's own) and removes its lock.
func releaseProfile(profile string, uid int) {
	lock := filepath.Join(profile, "SingletonLock")
	if target, err := os.Readlink(lock); err == nil {
		if i := strings.LastIndex(target, "-"); i >= 0 {
			if pid, err := strconv.Atoi(target[i+1:]); err == nil && pid > 1 {
				if browserOnProfile(filepath.Join("/proc", strconv.Itoa(pid)), profile, uid) {
					syscall.Kill(pid, syscall.SIGKILL)
					time.Sleep(500 * time.Millisecond)
				}
			}
		}
	}
	for _, name := range []string{"SingletonLock", "SingletonSocket", "SingletonCookie"} {
		os.Remove(filepath.Join(profile, name))
	}
}

func browserOnProfile(proc, profile string, uid int) bool {
	exe, err := os.Readlink(filepath.Join(proc, "exe"))
	if err != nil || !strings.Contains(strings.ToLower(filepath.Base(exe)), "chrom") {
		return false
	}
	info, err := os.Stat(proc)
	if err != nil {
		return false
	}
	st, ok := info.Sys().(*syscall.Stat_t)
	if !ok || st.Uid != uint32(uid) {
		return false
	}
	data, err := os.ReadFile(filepath.Join(proc, "cmdline"))
	if err != nil {
		return false
	}
	args := strings.Split(string(data), "\x00")
	for i, arg := range args {
		if arg == "--user-data-dir="+profile || arg == "--user-data-dir" && i+1 < len(args) && args[i+1] == profile {
			return true
		}
	}
	return false
}

// checkLibraries reports shared libraries a downloaded Chrome needs but the
// system lacks, which minimal server images often do.
func checkLibraries(exe string) error {
	out, err := exec.Command("ldd", exe).Output()
	if err != nil {
		return nil
	}
	var missing []string
	for _, line := range strings.Split(string(out), "\n") {
		if strings.Contains(line, "not found") {
			missing = append(missing, strings.TrimSpace(strings.SplitN(line, "=>", 2)[0]))
		}
	}
	if len(missing) > 0 {
		return errors.New("missing system libraries: " + strings.Join(missing, " ") +
			" (Debian/Ubuntu: apt-get install -y libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2)")
	}
	return nil
}

type closerFunc func() error

func (f closerFunc) Close() error { return f() }
