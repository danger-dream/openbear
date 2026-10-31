package transport

import "net"

const socketBufferSize = 256 << 10

func configureTCP(c *net.TCPConn) (int, error) {
	if err := c.SetNoDelay(true); err != nil {
		return 0, err
	}
	raw, err := c.SyscallConn()
	if err != nil {
		return 0, err
	}
	return configureSocket(raw, true)
}
