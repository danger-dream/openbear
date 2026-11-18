//go:build linux

package fast

import (
	"encoding/binary"
	"errors"
	"net"
	"net/netip"
	"runtime"
	"syscall"

	"golang.org/x/sys/unix"

	"openbear/node/mesh/bypass"
)

func withFastSocket(fn func() error) error {
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	before, err := unix.SetfsgidRetGid(bypass.GID)
	if err != nil {
		return fn()
	}
	defer unix.Setfsgid(before)
	return fn()
}

func controlFixedRcvBuf(bytes int) func(string, string, syscall.RawConn) error {
	return func(network, address string, c syscall.RawConn) error {
		if err := bypass.Control(network, address, c); err != nil {
			return err
		}
		if bytes <= 0 {
			return nil
		}
		var serr error
		if err := c.Control(func(fd uintptr) {
			serr = unix.SetsockoptInt(int(fd), unix.SOL_SOCKET, unix.SO_RCVBUF, bytes)
		}); err != nil {
			return err
		}
		return serr
	}
}

const soOriginalDst = 80 // SOL_IP SO_ORIGINAL_DST

// originalDst returns the pre-redirect IPv4 destination of a redirected TCP connection.
func originalDst(c *net.TCPConn) (netip.AddrPort, error) {
	rc, err := c.SyscallConn()
	if err != nil {
		return netip.AddrPort{}, err
	}
	var addr netip.AddrPort
	var gerr error
	err = rc.Control(func(fd uintptr) {
		raw, e := unix.GetsockoptIPv6MTUInfo(int(fd), unix.SOL_IP, soOriginalDst)
		if e != nil {
			gerr = e
			return
		}
		// struct sockaddr_in occupies the first 16 bytes of ip6_mtuinfo:
		// family (2), port big-endian (2), ipv4 (4).
		b := (*[16]byte)(rawPointer(raw))
		port := binary.BigEndian.Uint16(b[2:4])
		ip := netip.AddrFrom4([4]byte{b[4], b[5], b[6], b[7]})
		if !ip.IsValid() || port == 0 {
			gerr = errors.New("invalid original destination")
			return
		}
		addr = netip.AddrPortFrom(ip, port)
	})
	if err != nil {
		return netip.AddrPort{}, err
	}
	return addr, gerr
}

// setUDPRcvBuf sets c's receive buffer, past net.core.rmem_max where the node
// runs as root.
func setUDPRcvBuf(c net.Conn, bytes int) {
	sc, ok := c.(syscall.Conn)
	if !ok {
		return
	}
	rc, err := sc.SyscallConn()
	if err != nil {
		return
	}
	_ = rc.Control(func(fd uintptr) {
		if unix.SetsockoptInt(int(fd), unix.SOL_SOCKET, unix.SO_RCVBUFFORCE, bytes) != nil {
			_ = unix.SetsockoptInt(int(fd), unix.SOL_SOCKET, unix.SO_RCVBUF, bytes)
		}
	})
}
