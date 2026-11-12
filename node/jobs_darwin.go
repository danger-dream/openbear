//go:build darwin

package node

import (
	"strconv"

	"golang.org/x/sys/unix"
)

// processStart is the process's start time, and whether it is alive (a
// zombie has finished).
func processStart(pid int) (string, bool) {
	kp, err := unix.SysctlKinfoProc("kern.proc.pid", pid)
	if err != nil || kp.Proc.P_pid != int32(pid) || kp.Proc.P_stat == 5 { // SZOMB
		return "", false
	}
	t := kp.Proc.P_starttime
	return strconv.FormatInt(t.Sec, 10) + "." + strconv.FormatInt(int64(t.Usec), 10), true
}

// groupAlive tells whether a process of the group still runs; a zombie has ended.
func groupAlive(pgid int) bool {
	kps, err := unix.SysctlKinfoProcSlice("kern.proc.pgrp", pgid)
	if err != nil {
		return unix.Kill(-pgid, 0) != unix.ESRCH
	}
	for _, kp := range kps {
		if kp.Proc.P_stat != 5 { // SZOMB
			return true
		}
	}
	return false
}
