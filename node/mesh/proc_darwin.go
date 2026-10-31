package mesh

import (
	"context"
	"os/exec"
	"syscall"
	"time"
)

func processCPU() (time.Duration, bool) {
	var ru syscall.Rusage
	if err := syscall.Getrusage(syscall.RUSAGE_SELF, &ru); err != nil {
		return 0, false
	}
	return time.Duration(ru.Utime.Nano() + ru.Stime.Nano()), true
}

// The application firewall can block the TCP and TLS listeners; it is
// asked every few minutes, not on every status write.
var firewallCheck = &cachedCheck{every: 5 * time.Minute, check: func() string {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, "/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate").Output()
	if err != nil {
		return ""
	}
	return firewallState(string(out))
}}

func hostFirewall() string { return firewallCheck.get() }

func hostFirewallIssues() []FirewallIssue { return nil }
