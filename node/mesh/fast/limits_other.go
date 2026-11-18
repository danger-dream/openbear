//go:build !linux && !darwin

package fast

func pendingHeaderLimit() int { return maxPendingHeaders }
