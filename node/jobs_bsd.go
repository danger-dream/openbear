//go:build unix && !linux && !darwin

package node

import "syscall"

// processStart cannot read a start time here; a live pid is taken as the
// recorded process.
func processStart(pid int) (string, bool) {
	return "-", syscall.Kill(pid, 0) == nil
}
