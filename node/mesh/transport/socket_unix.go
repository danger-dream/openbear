//go:build !windows

package transport

import (
	"golang.org/x/sys/unix"
	"syscall"
)

func configureSocket(raw syscall.RawConn, tcp bool) (int, error) {
	var cost int
	var inner error
	err := raw.Control(func(fd uintptr) {
		if tcp {
			if inner = setNotSent(int(fd)); inner != nil {
				return
			}
		} else {
			allowFragments(int(fd))
		}
		var r, w int
		r, inner = unix.GetsockoptInt(int(fd), unix.SOL_SOCKET, unix.SO_RCVBUF)
		if inner != nil {
			return
		}
		w, inner = unix.GetsockoptInt(int(fd), unix.SOL_SOCKET, unix.SO_SNDBUF)
		cost = r + w
	})
	if err != nil {
		return 0, err
	}
	return cost, inner
}
