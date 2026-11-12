//go:build linux && amd64

package tun

import "golang.org/x/sys/cpu"

// The SIMD checksum is tailscale/wireguard-go 88b11b4a, MIT; see
// LICENSE.checksum-upstream.
var fastTCPChecksum = checksum

func init() {
	if cpu.X86.HasAVX && cpu.X86.HasAVX2 && cpu.X86.HasBMI2 {
		fastTCPChecksum = func(b []byte, initial uint64) uint16 {
			initial = (initial >> 16) + (initial & 0xffff)
			initial = (initial >> 16) + (initial & 0xffff)
			initial = (initial >> 16) + (initial & 0xffff)
			initial = (initial >> 16) + (initial & 0xffff)
			return checksumAVX2(b, uint16(initial))
		}
	}
}
