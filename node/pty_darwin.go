//go:build darwin

package node

import (
	"bytes"
	"os"
	"os/exec"
	"os/user"
	"syscall"
	"unsafe"
)

func openPTY() (*os.File, *os.File, error) {
	master, err := os.OpenFile("/dev/ptmx", os.O_RDWR|syscall.O_NOCTTY|syscall.O_CLOEXEC, 0)
	if err != nil {
		return nil, nil, err
	}
	var name [128]byte
	if err = control(master, syscall.TIOCPTYGRANT, nil); err == nil {
		if err = control(master, syscall.TIOCPTYUNLK, nil); err == nil {
			err = control(master, syscall.TIOCPTYGNAME, unsafe.Pointer(&name))
		}
	}
	if err != nil {
		master.Close()
		return nil, nil, err
	}
	end := bytes.IndexByte(name[:], 0)
	if end < 0 {
		end = len(name)
	}
	slave, err := os.OpenFile(string(name[:end]), os.O_RDWR|syscall.O_NOCTTY|syscall.O_CLOEXEC, 0)
	if err != nil {
		master.Close()
		return nil, nil, err
	}
	return master, slave, nil
}

// loginShell is zsh, the macOS default since 10.15: root's recorded shell is
// /bin/sh, which nobody wants interactively.
func loginShell() (shell, home string) {
	for _, candidate := range []string{"/bin/zsh", "/bin/bash", "/bin/sh"} {
		if _, err := exec.LookPath(candidate); err == nil {
			shell = candidate
			break
		}
	}
	if u, err := user.Current(); err == nil && u.HomeDir != "" {
		home = u.HomeDir
	} else {
		home, _ = os.UserHomeDir()
	}
	return shell, home
}
