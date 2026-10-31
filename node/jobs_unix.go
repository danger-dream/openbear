//go:build unix

package node

import (
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"syscall"
)

// jobsDir keeps jobs out of /etc/openbear-node for an installed Linux node,
// next to its browser profile.
func jobsDir(config string) string {
	if config == DefaultConfigPath && runtime.GOOS == "linux" {
		return "/var/lib/openbear-node/jobs"
	}
	return filepath.Join(filepath.Dir(config), "jobs")
}

func jobShell() string { return execShell() }

// runnerPath is the node binary itself: replacing it on update leaves a
// running runner its own copy.
func runnerPath(exe, _, _ string) (string, error) { return exe, nil }

// startRunner puts the runner in a session of its own, so neither the
// node's process group nor its service unit (KillMode=process) takes it down.
func startRunner(cmd *exec.Cmd) error {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	return cmd.Start()
}

func signalRunner(pid int) { syscall.Kill(pid, syscall.SIGTERM) }

// killTree kills a process and the process group it leads.
func killTree(pid int) {
	syscall.Kill(-pid, syscall.SIGKILL)
	syscall.Kill(pid, syscall.SIGKILL)
}

// jobCommand runs the job in a process group of its own, so a stop reaches
// everything it started and nothing else.
func jobCommand(spec jobSpec) (*exec.Cmd, func(), error) {
	cmd := shellCommand(spec.Shell, spec.Command)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	return cmd, func() {}, nil
}

func watchJob(cmd *exec.Cmd) (func(force bool), func()) {
	pgid := cmd.Process.Pid
	return func(force bool) {
		if force {
			syscall.Kill(-pgid, syscall.SIGKILL)
		} else {
			syscall.Kill(-pgid, syscall.SIGTERM)
		}
	}, func() {}
}

// jobStderr shares stdout's pipe, which keeps the two streams in order.
func jobStderr(out *jobLog) (io.Writer, func()) { return out, func() {} }

// exitInfo reports a command killed by a signal as a shell would: 128+n.
func exitInfo(state *os.ProcessState) (int, string) {
	if ws, ok := state.Sys().(syscall.WaitStatus); ok && ws.Signaled() {
		return 128 + int(ws.Signal()), signalName(ws.Signal())
	}
	return state.ExitCode(), ""
}

// signalName names the signals a stop or the system sends; Linux builds stay
// free of golang.org/x/sys.
func signalName(s syscall.Signal) string {
	switch s {
	case syscall.SIGHUP:
		return "SIGHUP"
	case syscall.SIGINT:
		return "SIGINT"
	case syscall.SIGQUIT:
		return "SIGQUIT"
	case syscall.SIGABRT:
		return "SIGABRT"
	case syscall.SIGKILL:
		return "SIGKILL"
	case syscall.SIGSEGV:
		return "SIGSEGV"
	case syscall.SIGPIPE:
		return "SIGPIPE"
	case syscall.SIGTERM:
		return "SIGTERM"
	}
	return "signal " + strconv.Itoa(int(s))
}
