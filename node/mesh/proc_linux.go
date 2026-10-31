package mesh

import (
	"os"
	"strconv"
	"strings"
	"time"
)

// processCPU is user plus system time from /proc/self/stat, which counts
// in USER_HZ: 100 per second on every Linux.
func processCPU() (time.Duration, bool) {
	raw, err := os.ReadFile("/proc/self/stat")
	if err != nil {
		return 0, false
	}
	// The command name in parentheses may contain spaces.
	i := strings.LastIndexByte(string(raw), ')')
	if i < 0 {
		return 0, false
	}
	f := strings.Fields(string(raw[i+1:]))
	if len(f) < 13 { // state is field 3, utime 14 and stime 15
		return 0, false
	}
	user, err1 := strconv.ParseUint(f[11], 10, 64)
	system, err2 := strconv.ParseUint(f[12], 10, 64)
	if err1 != nil || err2 != nil {
		return 0, false
	}
	return time.Duration(user+system) * (time.Second / 100), true
}
