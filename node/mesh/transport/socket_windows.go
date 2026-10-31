package transport

import (
	"golang.org/x/sys/windows"
	"syscall"
	"unsafe"
)

func configureSocket(raw syscall.RawConn, tcp bool) (int, error) {
	var cost int
	var inner error
	err := raw.Control(func(fd uintptr) {
		if !tcp {
			// Windows turns an ICMP port unreachable for an earlier datagram
			// into an error on the next read, as if the socket were
			// connected; one relay restarting would stop all UDP here.
			for _, code := range []uint32{windows.SIO_UDP_CONNRESET, windows.SIO_UDP_NETRESET} {
				off := uint32(0)
				var ret uint32
				if inner = windows.WSAIoctl(windows.Handle(fd), code, (*byte)(unsafe.Pointer(&off)), uint32(unsafe.Sizeof(off)), nil, 0, &ret, nil, 0); inner != nil {
					return
				}
			}
		}
		var r, w int
		r, inner = windows.GetsockoptInt(windows.Handle(fd), windows.SOL_SOCKET, windows.SO_RCVBUF)
		if inner != nil {
			return
		}
		w, inner = windows.GetsockoptInt(windows.Handle(fd), windows.SOL_SOCKET, windows.SO_SNDBUF)
		cost = r + w
	})
	if err != nil {
		return 0, err
	}
	return cost, inner
}
