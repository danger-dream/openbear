//go:build linux || darwin

package node

import (
	"errors"
	"syscall"
)

func protectedPID(pid int) bool { return pid <= 1 }

// signalProcess asks a process to end (SIGTERM), or ends it (SIGKILL).
func signalProcess(pid int, force bool, identity string) (bool, error) {
	sig := syscall.SIGTERM
	if force {
		sig = syscall.SIGKILL
	}
	if err := signalProcessInstance(pid, sig, identity); err != nil {
		if errors.Is(err, syscall.ESRCH) {
			return false, errors.New("process_not_found")
		}
		return false, err
	}
	return true, nil
}
