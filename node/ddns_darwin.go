package node

import (
	"encoding/binary"
	"net"
	"syscall"
	"unsafe"
)

// From <netinet6/in6_var.h>: SIOCGIFAFLAG_IN6 is _IOWR('i', 73, struct
// in6_ifreq), a 16-byte name and a 272-byte union that starts with a
// sockaddr_in6 in and holds the int flags out.
const (
	siocgifaflagIn6   = 0xc1206949
	in6ifreqSize      = 288
	in6IffDeprecated  = 0x0010
	in6IffTemporary   = 0x0080
	sockaddrIn6Length = 28
)

// ddnsV6Flags asks the kernel for each IPv6 address's flags, one ioctl per
// address. Link-local addresses are skipped: their scope makes the lookup
// fiddly and no probe uses them.
func ddnsV6Flags() (map[ddnsAddrKey]ddnsAddrFlags, error) {
	fd, err := syscall.Socket(syscall.AF_INET6, syscall.SOCK_DGRAM, 0)
	if err != nil {
		return nil, err
	}
	defer syscall.Close(fd)
	list, err := net.Interfaces()
	if err != nil {
		return nil, err
	}
	out := map[ddnsAddrKey]ddnsAddrFlags{}
	for _, ifi := range list {
		for _, addr := range ddnsIfaceAddrs(&ifi) {
			if !addr.Is6() || addr.IsLinkLocalUnicast() {
				continue
			}
			var req [in6ifreqSize]byte
			copy(req[:15], ifi.Name)
			req[16], req[17] = sockaddrIn6Length, syscall.AF_INET6
			ip := addr.As16()
			copy(req[16+8:], ip[:])
			if _, _, errno := syscall.Syscall(syscall.SYS_IOCTL, uintptr(fd), siocgifaflagIn6, uintptr(unsafe.Pointer(&req[0]))); errno != 0 {
				continue
			}
			flags := binary.NativeEndian.Uint32(req[16:])
			out[ddnsAddrKey{ifi.Index, addr}] = ddnsAddrFlags{temporary: flags&in6IffTemporary != 0, deprecated: flags&in6IffDeprecated != 0}
		}
	}
	return out, nil
}

// ddnsBindDevice is not used here: binding binds the interface's address.
func ddnsBindDevice(*net.Dialer, string) bool { return false }
