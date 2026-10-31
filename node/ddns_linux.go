package node

import (
	"encoding/binary"
	"errors"
	"net"
	"net/netip"
	"syscall"

	"golang.org/x/sys/unix"
)

// ddnsV6Flags dumps the IPv6 addresses over netlink: their flags are not in
// what package net reads. IFA_FLAGS holds all 32 bits, ifa_flags only the low 8.
func ddnsV6Flags() (map[ddnsAddrKey]ddnsAddrFlags, error) {
	fd, err := unix.Socket(unix.AF_NETLINK, unix.SOCK_RAW|unix.SOCK_CLOEXEC, unix.NETLINK_ROUTE)
	if err != nil {
		return nil, err
	}
	defer unix.Close(fd)
	kernel := &unix.SockaddrNetlink{Family: unix.AF_NETLINK}
	if err = unix.Bind(fd, &unix.SockaddrNetlink{Family: unix.AF_NETLINK}); err != nil {
		return nil, err
	}
	req := make([]byte, unix.NLMSG_HDRLEN+unix.SizeofIfAddrmsg)
	binary.NativeEndian.PutUint32(req[0:], uint32(len(req)))
	binary.NativeEndian.PutUint16(req[4:], unix.RTM_GETADDR)
	binary.NativeEndian.PutUint16(req[6:], unix.NLM_F_REQUEST|unix.NLM_F_DUMP)
	binary.NativeEndian.PutUint32(req[8:], 1)
	req[unix.NLMSG_HDRLEN] = unix.AF_INET6
	if err = unix.Sendto(fd, req, 0, kernel); err != nil {
		return nil, err
	}
	out := map[ddnsAddrKey]ddnsAddrFlags{}
	buf := make([]byte, 64<<10)
	for {
		n, _, err := unix.Recvfrom(fd, buf, 0)
		if errors.Is(err, unix.EINTR) {
			continue
		}
		if err != nil {
			return nil, err
		}
		for b := buf[:n]; len(b) >= unix.NLMSG_HDRLEN; {
			size := int(binary.NativeEndian.Uint32(b[0:]))
			kind := binary.NativeEndian.Uint16(b[4:])
			if size < unix.NLMSG_HDRLEN || size > len(b) {
				return nil, errors.New("malformed netlink message")
			}
			body := b[unix.NLMSG_HDRLEN:size]
			b = b[min(nlmAlign(size), len(b)):]
			switch kind {
			case unix.NLMSG_DONE:
				return out, nil
			case unix.NLMSG_ERROR:
				if len(body) >= 4 {
					if errno := int32(binary.NativeEndian.Uint32(body)); errno != 0 {
						return nil, syscall.Errno(-errno)
					}
				}
				return out, nil
			case unix.RTM_NEWADDR:
				if key, flags, ok := parseIfAddr(body); ok {
					out[key] = flags
				}
			}
		}
	}
}

func nlmAlign(n int) int { return (n + unix.NLMSG_ALIGNTO - 1) &^ (unix.NLMSG_ALIGNTO - 1) }

func parseIfAddr(b []byte) (ddnsAddrKey, ddnsAddrFlags, bool) {
	if len(b) < unix.SizeofIfAddrmsg || b[0] != unix.AF_INET6 {
		return ddnsAddrKey{}, ddnsAddrFlags{}, false
	}
	flags := uint32(b[2])
	index := int(binary.NativeEndian.Uint32(b[4:]))
	var address, local netip.Addr
	for attrs := b[unix.SizeofIfAddrmsg:]; len(attrs) >= unix.SizeofRtAttr; {
		size := int(binary.NativeEndian.Uint16(attrs[0:]))
		kind := binary.NativeEndian.Uint16(attrs[2:])
		if size < unix.SizeofRtAttr || size > len(attrs) {
			break
		}
		value := attrs[unix.SizeofRtAttr:size]
		attrs = attrs[min(nlmAlign(size), len(attrs)):]
		switch kind {
		case unix.IFA_ADDRESS:
			address, _ = netip.AddrFromSlice(value)
		case unix.IFA_LOCAL:
			local, _ = netip.AddrFromSlice(value)
		case unix.IFA_FLAGS:
			if len(value) >= 4 {
				flags = binary.NativeEndian.Uint32(value)
			}
		}
	}
	// On a point-to-point link IFA_ADDRESS is the peer's.
	if local.IsValid() {
		address = local
	}
	if !address.Is6() {
		return ddnsAddrKey{}, ddnsAddrFlags{}, false
	}
	return ddnsAddrKey{index, address}, ddnsAddrFlags{temporary: flags&unix.IFA_F_TEMPORARY != 0, deprecated: flags&unix.IFA_F_DEPRECATED != 0}, true
}

// ddnsBindDevice sends d's connections out through iface, whatever their
// source address.
func ddnsBindDevice(d *net.Dialer, iface string) bool {
	d.Control = func(_, _ string, c syscall.RawConn) error {
		var bindErr error
		if err := c.Control(func(fd uintptr) {
			bindErr = unix.SetsockoptString(int(fd), unix.SOL_SOCKET, unix.SO_BINDTODEVICE, iface)
		}); err != nil {
			return err
		}
		return bindErr
	}
	return true
}
