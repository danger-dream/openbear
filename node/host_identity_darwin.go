//go:build darwin

package node

import "syscall"

func signalProcessInstance(pid int, sig syscall.Signal, identity string) error {
	// macOS has no pidfd equivalent here. Check the kernel start timestamp
	// immediately before kill; this is identity checking, not a zero-race handle.
	if err := checkProcessIdentity(pid, identity); err != nil {
		return err
	}
	return syscall.Kill(pid, sig)
}
