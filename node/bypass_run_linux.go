//go:build linux

package node

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"

	"golang.org/x/sys/unix"

	"openbear/node/mesh/bypass"
)

// A command the controller asks to run bypassed (a benchmark's job, the full unlock check)
// measures this machine's own line, as the node's checks do: while the bypass is in place it
// starts through "openbear-node bypass-run", which gives it the upstream DNS servers in place of
// a proxy's (whose answers lead into the proxy) and the bypass group (see package bypass).

// bypassResolv is the resolv.conf laid over /etc/resolv.conf for a bypassed command; in RAM.
var bypassResolv = "/var/run/openbear-bypass.resolv"

// bypassedCommand makes cmd start through bypass-run; its caller asked for it while the
// bypass was in place (bypass.Active).
func bypassedCommand(cmd *exec.Cmd) {
	self, err := os.Executable()
	if err != nil {
		return
	}
	cmd.Args = append([]string{self, "bypass-run", "--", cmd.Path}, cmd.Args[1:]...)
	cmd.Path = self
}

// RunBypassed is "openbear-node bypass-run -- PROGRAM ARGS...": it becomes PROGRAM in a mount
// namespace of its own whose /etc/resolv.conf lists the upstream DNS servers, in the bypass
// group. Where either cannot be done PROGRAM still runs, as it would have otherwise.
func RunBypassed(args []string) error {
	if len(args) < 2 || args[0] != "--" {
		return errors.New("usage: openbear-node bypass-run -- PROGRAM [ARGS...]")
	}
	args = args[1:]
	path, err := exec.LookPath(args[0])
	if err != nil {
		return err
	}
	// The namespace is this thread's, which exec keeps.
	runtime.LockOSThread()
	upstreamResolv()
	syscall.Setgid(bypass.GID)
	return syscall.Exec(path, args, os.Environ())
}

// upstreamResolv lays the upstream servers over /etc/resolv.conf, in a new mount namespace that
// is made private first, so nothing of it reaches the machine's own.
func upstreamResolv() {
	var b strings.Builder
	b.WriteString("# openbear-node: the upstream DNS servers, for a command that bypasses a transparent proxy\n")
	for _, s := range bypass.Upstreams() {
		b.WriteString("nameserver " + s + "\n")
	}
	tmp := bypassResolv + "." + strconv.Itoa(os.Getpid())
	if os.MkdirAll(filepath.Dir(bypassResolv), 0o755) != nil || os.WriteFile(tmp, []byte(b.String()), 0o644) != nil || os.Rename(tmp, bypassResolv) != nil {
		return
	}
	if unix.Unshare(unix.CLONE_NEWNS) != nil || unix.Mount("", "/", "", unix.MS_REC|unix.MS_PRIVATE, "") != nil {
		return
	}
	unix.Mount(bypassResolv, "/etc/resolv.conf", "", unix.MS_BIND, "")
}
