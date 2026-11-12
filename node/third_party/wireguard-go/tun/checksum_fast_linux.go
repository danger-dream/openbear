//go:build linux

package tun

// tcpPacketChecksum is checksum for TCP segments and coalesced TCP packets,
// with SIMD where the CPU has it; small inputs keep the generic code.
func tcpPacketChecksum(b []byte, initial uint64) uint16 {
	if len(b) < 256 {
		return checksum(b, initial)
	}
	return fastTCPChecksum(b, initial)
}
