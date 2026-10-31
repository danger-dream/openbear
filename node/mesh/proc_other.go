//go:build !linux && !darwin && !windows

package mesh

import "time"

func processCPU() (time.Duration, bool) { return 0, false }

func hostFirewall() string { return "" }

func hostFirewallIssues() []FirewallIssue { return nil }
