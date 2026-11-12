//go:build linux

package node

import (
	"os"
	"strconv"
	"strings"
	"syscall"
)

// processStart is the process's start time in clock ticks since boot, and
// whether it is alive (a zombie has finished).
func processStart(pid int) (string, bool) {
	data, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/stat")
	if err != nil {
		return "", false
	}
	// The command name may hold spaces and parentheses; fields follow the last ")".
	end := strings.LastIndexByte(string(data), ')')
	if end < 0 {
		return "", false
	}
	fields := strings.Fields(string(data[end+1:]))
	if len(fields) < 20 || fields[0] == "Z" || fields[0] == "X" {
		return "", false
	}
	return fields[19], true
}

// groupAlive tells whether a process of the group still runs. A zombie has
// ended: one whose parent never reaps it (a container's PID 1 without an
// init) must not hold a stop open.
func groupAlive(pgid int) bool {
	if syscall.Kill(-pgid, 0) == syscall.ESRCH {
		return false
	}
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return true
	}
	group := strconv.Itoa(pgid)
	for _, e := range entries {
		if name := e.Name(); name[0] < '0' || name[0] > '9' {
			continue
		}
		data, err := os.ReadFile("/proc/" + e.Name() + "/stat")
		if err != nil {
			continue
		}
		end := strings.LastIndexByte(string(data), ')')
		if end < 0 {
			continue
		}
		// state, ppid, pgrp
		fields := strings.Fields(string(data[end+1:]))
		if len(fields) > 2 && fields[2] == group && fields[0] != "Z" && fields[0] != "X" {
			return true
		}
	}
	return false
}
