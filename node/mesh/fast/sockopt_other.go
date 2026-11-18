//go:build !linux

package fast

import (
	"net"
	"syscall"

	"openbear/node/mesh/bypass"
)

func withFastSocket(fn func() error) error {
	return bypass.Socket(fn)
}

func controlFixedRcvBuf(_ int) func(string, string, syscall.RawConn) error {
	return bypass.Control
}

func setUDPRcvBuf(c net.Conn, bytes int) {
	if b, ok := c.(interface{ SetReadBuffer(int) error }); ok {
		_ = b.SetReadBuffer(bytes)
	}
}
