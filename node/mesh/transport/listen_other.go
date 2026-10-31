//go:build !windows

package transport

// checkIPv4Free: Linux and macOS refuse a dual-stack bind on a port held
// for IPv4 alone, so the bind itself reports it.
func checkIPv4Free(string, int) error { return nil }
