//go:build linux

package node

import (
	"encoding/binary"
	"os"
	"path/filepath"
	"strings"
	"syscall"
)

const (
	sockDiagByFamily = 20
	tcpListenState   = 10
	// nlmsghdr (16) + inet_diag_req_v2: family, protocol, ext, pad, states (4) and a zero sockid (48).
	sockDiagRequestLen = 16 + 8 + 48
)

// tcpListening counts listening TCP sockets with one socket-diag dump per family, which the kernel
// answers with the listeners alone: no walk over every connection as reading /proc/net/tcp would be.
// A kernel built without inet_diag (OpenWrt's default) refuses the query: the listeners are then
// counted from /proc/net/tcp{,6}, which on such small machines stay short.
func tcpListening() (int, bool) {
	if n, ok := sockDiagListening(); ok {
		return n, true
	}
	return procListening("/proc/net")
}

// procListening counts the rows in state 0A (LISTEN) of root's tcp and tcp6 tables.
func procListening(root string) (int, bool) {
	total, ok := 0, false
	for _, name := range []string{"tcp", "tcp6"} {
		raw, err := os.ReadFile(filepath.Join(root, name))
		if err != nil {
			continue
		}
		ok = true
		for _, line := range strings.Split(string(raw), "\n")[1:] {
			if f := strings.Fields(line); len(f) > 3 && f[3] == "0A" {
				total++
			}
		}
	}
	return total, ok
}

func sockDiagListening() (int, bool) {
	fd, err := syscall.Socket(syscall.AF_NETLINK, syscall.SOCK_DGRAM|syscall.SOCK_CLOEXEC, syscall.NETLINK_INET_DIAG)
	if err != nil {
		return 0, false
	}
	defer syscall.Close(fd)
	tv := syscall.Timeval{Sec: 2}
	if syscall.SetsockoptTimeval(fd, syscall.SOL_SOCKET, syscall.SO_RCVTIMEO, &tv) != nil ||
		syscall.Bind(fd, &syscall.SockaddrNetlink{Family: syscall.AF_NETLINK}) != nil {
		return 0, false
	}
	total := 0
	for i, family := range []uint8{syscall.AF_INET, syscall.AF_INET6} {
		seq := uint32(i + 1)
		req := make([]byte, sockDiagRequestLen)
		binary.NativeEndian.PutUint32(req[0:], sockDiagRequestLen)
		binary.NativeEndian.PutUint16(req[4:], sockDiagByFamily)
		binary.NativeEndian.PutUint16(req[6:], syscall.NLM_F_REQUEST|syscall.NLM_F_DUMP)
		binary.NativeEndian.PutUint32(req[8:], seq)
		req[16], req[17] = family, syscall.IPPROTO_TCP
		binary.NativeEndian.PutUint32(req[20:], 1<<tcpListenState)
		if syscall.Sendto(fd, req, 0, &syscall.SockaddrNetlink{Family: syscall.AF_NETLINK}) != nil {
			return 0, false
		}
		n, ok := sockDiagCount(fd, seq)
		// A kernel without IPv6 refuses that family's dump: none of those, the IPv4 count stands.
		if !ok && family == syscall.AF_INET {
			return 0, false
		}
		total += n
	}
	return total, true
}

func sockDiagCount(fd int, seq uint32) (int, bool) {
	buf := make([]byte, 32<<10)
	count := 0
	for {
		n, _, err := syscall.Recvfrom(fd, buf, 0)
		if err != nil {
			return 0, false
		}
		msgs, err := syscall.ParseNetlinkMessage(buf[:n])
		if err != nil {
			return 0, false
		}
		for _, m := range msgs {
			if m.Header.Seq != seq {
				continue
			}
			switch m.Header.Type {
			case syscall.NLMSG_DONE:
				return count, true
			case syscall.NLMSG_ERROR:
				return 0, false
			case sockDiagByFamily:
				count++
			}
		}
	}
}
