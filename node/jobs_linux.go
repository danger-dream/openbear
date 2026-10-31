//go:build linux

package node

import (
	"os"
	"strconv"
	"strings"
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
