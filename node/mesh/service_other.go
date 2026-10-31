//go:build !windows

package mesh

// RunService runs the data plane until SIGINT or SIGTERM; systemd and
// launchd supervise it.
func RunService(dir string) error { return runForeground(dir) }
