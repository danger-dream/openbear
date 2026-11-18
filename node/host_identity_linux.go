//go:build linux

package node

import (
	"errors"
	"golang.org/x/sys/unix"
	"syscall"
)

func signalProcessInstance(pid int, sig syscall.Signal, identity string) error {
	fd, err := unix.PidfdOpen(pid, 0)
	if err == nil {
		defer unix.Close(fd)
		// Acquiring the pidfd first means that exit/reuse after this check can only
		// make signalling fail, never redirect it to the new occupant of the PID.
		if err := checkProcessIdentity(pid, identity); err != nil {
			return err
		}
		return unix.PidfdSendSignal(fd, unix.Signal(sig), nil, 0)
	}
	if !errors.Is(err, unix.ENOSYS) && !errors.Is(err, unix.EINVAL) {
		return err
	}
	// Older kernels lack pidfd. Keep the startup identity check immediately next
	// to kill; unlike pidfd this retains a narrow check/signal race.
	if err := checkProcessIdentity(pid, identity); err != nil {
		return err
	}
	return syscall.Kill(pid, sig)
}
