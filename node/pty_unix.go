//go:build linux || darwin

package node

import (
	"errors"
	"os"
	"os/exec"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

// The ioctls go through SyscallConn so the master stays in Go's poller and
// Close interrupts a pending Read.
func control(f *os.File, req uintptr, arg unsafe.Pointer) error {
	raw, err := f.SyscallConn()
	if err != nil {
		return err
	}
	var errno syscall.Errno
	if err = raw.Control(func(fd uintptr) {
		_, _, errno = syscall.Syscall(syscall.SYS_IOCTL, fd, req, uintptr(arg))
	}); err != nil {
		return err
	}
	if errno != 0 {
		return errno
	}
	return nil
}

type ptyConsole struct {
	master *os.File
	cmd    *exec.Cmd
}

func (c *ptyConsole) Read(p []byte) (int, error)  { return c.master.Read(p) }
func (c *ptyConsole) Write(p []byte) (int, error) { return c.master.Write(p) }
func (c *ptyConsole) Wait()                       { c.cmd.Wait() }

func (c *ptyConsole) Resize(cols, rows int) error {
	if cols <= 0 || rows <= 0 || cols > 1000 || rows > 1000 {
		return errors.New("invalid size")
	}
	size := struct{ Row, Col, X, Y uint16 }{uint16(rows), uint16(cols), 0, 0}
	return control(c.master, syscall.TIOCSWINSZ, unsafe.Pointer(&size))
}

// Hangup signals the whole process group; anything that ignores SIGHUP is
// killed shortly after.
func (c *ptyConsole) Hangup() {
	pid := c.cmd.Process.Pid
	syscall.Kill(-pid, syscall.SIGHUP)
	time.AfterFunc(2*time.Second, func() { syscall.Kill(-pid, syscall.SIGKILL) })
	c.master.Close()
}

// startShell runs a login shell as the session leader of a new terminal.
func startShell(cols, rows int) (console, error) {
	shell, home := loginShell()
	if shell == "" {
		return nil, errors.New("no_shell")
	}
	master, slave, err := openPTY()
	if err != nil {
		return nil, err
	}
	defer slave.Close()
	c := &ptyConsole{master: master}
	c.Resize(cols, rows)
	c.cmd = exec.Command(shell, "-l")
	c.cmd.Dir = home
	env := []string{"TERM=xterm-256color", "HOME=" + home, "SHELL=" + shell}
	for _, kv := range os.Environ() {
		if k, _, _ := strings.Cut(kv, "="); k != "TERM" && k != "HOME" && k != "SHELL" && k != "INVOCATION_ID" && k != "JOURNAL_STREAM" {
			env = append(env, kv)
		}
	}
	if os.Getenv("LANG") == "" {
		env = append(env, "LANG="+utf8Locale())
	}
	c.cmd.Env = env
	c.cmd.Stdin, c.cmd.Stdout, c.cmd.Stderr = slave, slave, slave
	c.cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true, Setctty: true, Ctty: 0}
	if err = c.cmd.Start(); err != nil {
		master.Close()
		return nil, err
	}
	return c, nil
}
