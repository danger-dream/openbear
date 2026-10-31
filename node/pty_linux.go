//go:build linux

package node

import (
	"bufio"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
	"unsafe"
)

func openPTY() (*os.File, *os.File, error) {
	master, err := os.OpenFile("/dev/ptmx", os.O_RDWR|syscall.O_NOCTTY|syscall.O_CLOEXEC, 0)
	if err != nil {
		return nil, nil, err
	}
	var unlock int32
	var n uint32
	if err = control(master, syscall.TIOCSPTLCK, unsafe.Pointer(&unlock)); err == nil {
		err = control(master, syscall.TIOCGPTN, unsafe.Pointer(&n))
	}
	if err != nil {
		master.Close()
		return nil, nil, err
	}
	slave, err := os.OpenFile("/dev/pts/"+strconv.Itoa(int(n)), os.O_RDWR|syscall.O_NOCTTY|syscall.O_CLOEXEC, 0)
	if err != nil {
		master.Close()
		return nil, nil, err
	}
	return master, slave, nil
}

// loginShell is the account's shell from /etc/passwd, else bash or sh.
func loginShell() (shell, home string) {
	uid := strconv.Itoa(os.Getuid())
	if f, err := os.Open("/etc/passwd"); err == nil {
		s := bufio.NewScanner(f)
		for s.Scan() {
			p := strings.Split(s.Text(), ":")
			if len(p) >= 7 && p[2] == uid {
				shell, home = p[6], p[5]
				break
			}
		}
		f.Close()
	}
	if _, err := exec.LookPath(shell); shell == "" || err != nil || strings.HasSuffix(shell, "nologin") || strings.HasSuffix(shell, "false") {
		shell = ""
		for _, candidate := range []string{"bash", "sh"} {
			if path, err := exec.LookPath(candidate); err == nil {
				shell = path
				break
			}
		}
	}
	if home == "" {
		home, _ = os.UserHomeDir()
	}
	return shell, home
}
