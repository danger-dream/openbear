//go:build windows

package node

import (
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"syscall"

	"golang.org/x/sys/windows"
)

func jobsDir(config string) string { return filepath.Join(filepath.Dir(config), "jobs") }

func jobShell() string { return "powershell" }

// runnerPath is a copy of the node binary in the jobs directory: Windows
// cannot replace or delete a running executable, and a runner started from
// the installed one would block the next update. Copies of older builds go
// once no runner uses them.
func runnerPath(exe, dir, build string) (string, error) {
	tag := build
	if len(tag) > 16 {
		tag = tag[:16]
	}
	if tag == "" {
		tag = "dev"
	}
	path := filepath.Join(dir, "runner-"+tag+".exe")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", err
	}
	old, _ := filepath.Glob(filepath.Join(dir, "runner-*.exe"))
	for _, name := range old {
		if name != path {
			os.Remove(name)
		}
	}
	if _, err := os.Stat(path); err == nil {
		return path, nil
	}
	src, err := os.Open(exe)
	if err != nil {
		return "", err
	}
	defer src.Close()
	temp := path + "." + randomHex(4) + ".tmp"
	dst, err := os.OpenFile(temp, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o700)
	if err != nil {
		return "", err
	}
	_, err = io.Copy(dst, src)
	if closeErr := dst.Close(); err == nil {
		err = closeErr
	}
	if err == nil {
		err = os.Rename(temp, path)
	}
	if err != nil {
		os.Remove(temp)
		return "", err
	}
	return path, nil
}

// startRunner starts the runner outside any job object the node is in, so
// stopping the service does not end it; a job that forbids breaking away
// leaves only the plain start.
func startRunner(cmd *exec.Cmd) error {
	flags := uint32(windows.CREATE_NEW_PROCESS_GROUP | windows.CREATE_NO_WINDOW)
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: flags | windows.CREATE_BREAKAWAY_FROM_JOB}
	if err := cmd.Start(); err == nil {
		return nil
	}
	retry := exec.Command(cmd.Path, cmd.Args[1:]...)
	retry.SysProcAttr = &syscall.SysProcAttr{CreationFlags: flags}
	if err := retry.Start(); err != nil {
		return err
	}
	*cmd = *retry
	return nil
}

// The runner checks for the stop file itself.
func signalRunner(int) {}

func killTree(pid int) {
	kill := exec.Command(filepath.Join(os.Getenv("SystemRoot"), `System32\taskkill.exe`), "/T", "/F", "/PID", strconv.Itoa(pid))
	kill.SysProcAttr = &syscall.SysProcAttr{CreationFlags: windows.CREATE_NO_WINDOW}
	kill.Run()
}

// processStart is the process's creation time, and whether it still runs.
func processStart(pid int) (string, bool) {
	h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(pid))
	if err != nil {
		return "", false
	}
	defer windows.CloseHandle(h)
	var code uint32
	if windows.GetExitCodeProcess(h, &code) != nil || code != 259 { // STILL_ACTIVE
		return "", false
	}
	var created, exited, kernel, user windows.Filetime
	if windows.GetProcessTimes(h, &created, &exited, &kernel, &user) != nil {
		return "", false
	}
	return strconv.FormatInt(created.Nanoseconds(), 10), true
}

func jobCommand(spec jobSpec) (*exec.Cmd, func(), error) {
	return powershellCommand(psPrologue + spec.Command + psEpilogue)
}

// watchJob holds the command and everything it starts in a job object of
// the runner's own; closing it does not end them, a stop does.
func watchJob(cmd *exec.Cmd) (func(force bool), func()) {
	job, err := killJob(cmd.Process.Pid)
	if err != nil {
		pid := cmd.Process.Pid
		return func(bool) { killTree(pid) }, func() {}
	}
	return func(bool) { windows.TerminateJobObject(job, 1) }, func() { windows.CloseHandle(job) }
}

// jobStderr turns the CLIXML that Windows PowerShell writes to a redirected
// stderr back into text as it arrives.
func jobStderr(out *jobLog) (io.Writer, func()) {
	w := &clixmlWriter{out: out}
	return w, w.flush
}

func exitInfo(state *os.ProcessState) (int, string) { return state.ExitCode(), "" }
