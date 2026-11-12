//go:build unix && !linux && !darwin

package node

import "syscall"

// processStart cannot read a start time here; a live pid is taken as the
// recorded process.
func processStart(pid int) (string, bool) {
	return "-", syscall.Kill(pid, 0) == nil
}

// groupAlive tells whether the group still has a process (zombies included).
func groupAlive(pgid int) bool { return syscall.Kill(-pgid, 0) != syscall.ESRCH }
