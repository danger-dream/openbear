//go:build linux || darwin

package fast

import "syscall"

// Keep file descriptors available for established business and the mesh even
// under a pre-auth flood on hosts with a low soft fd limit. This is global, not
// per source IP: many legitimate nodes may share one NAT egress.
func pendingHeaderLimit() int {
	var r syscall.Rlimit
	if syscall.Getrlimit(syscall.RLIMIT_NOFILE, &r) == nil {
		return int(max(1, min(uint64(maxPendingHeaders), uint64(r.Cur)/4)))
	}
	return maxPendingHeaders
}
