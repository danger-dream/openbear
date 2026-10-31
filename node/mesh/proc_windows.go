package mesh

import (
	"time"

	"golang.org/x/sys/windows"
)

func processCPU() (time.Duration, bool) {
	var creation, exit, kernel, user windows.Filetime
	if err := windows.GetProcessTimes(windows.CurrentProcess(), &creation, &exit, &kernel, &user); err != nil {
		return 0, false
	}
	ticks := func(t windows.Filetime) int64 { return int64(t.HighDateTime)<<32 | int64(t.LowDateTime) } // 100 ns
	return time.Duration(ticks(kernel)+ticks(user)) * 100, true
}

func hostFirewall() string { return "" }

func hostFirewallIssues() []FirewallIssue { return nil }
