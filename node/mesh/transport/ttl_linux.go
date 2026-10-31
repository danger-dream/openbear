package transport

import (
	"net/netip"
	"unsafe"

	"golang.org/x/sys/unix"
)

// writeTTL sets the TTL of this one datagram with a control message, so
// WireGuard's packets on the same socket keep theirs.
func writeTTL(u *udpSocket, p []byte, to netip.AddrPort, ttl int) error {
	oob := make([]byte, unix.CmsgSpace(4))
	h := (*unix.Cmsghdr)(unsafe.Pointer(&oob[0]))
	h.Level, h.Type = unix.IPPROTO_IP, unix.IP_TTL
	h.SetLen(unix.CmsgLen(4))
	*(*int32)(unsafe.Pointer(&oob[unix.CmsgLen(0)])) = int32(ttl)
	_, _, err := u.WriteMsgUDPAddrPort(p, oob, to)
	return err
}
