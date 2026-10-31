package transport

import (
	"errors"
	"fmt"
	"io"
	"net"
	"strconv"

	"golang.org/x/sys/windows"
)

// checkIPv4Free reports port as taken when another socket holds it for
// IPv4 alone. The listeners bind [::] dual-stack, and Windows lets that
// bind succeed next to a plain 0.0.0.0 socket on the same port (a .NET
// TcpListener, for one), which then keeps every IPv4 connection or
// datagram: the conflict would pass unnoticed. A default IPv4 bind does
// fail there, so one is made and closed before the real bind.
//
// SO_EXCLUSIVEADDRUSE on the listener was not used: an exclusive bind also
// fails while connections this service accepted before a restart are still
// in TIME_WAIT, which would keep a restarted relay off its own port for
// minutes. A default bind, this check's included, is not held up by them.
//
// Only "in use" and "access denied" (an exclusive holder, or a port in a
// reserved range) count; a host without IPv4 leaves the decision to the
// real bind.
func checkIPv4Free(network string, port int) error {
	if port == 0 {
		return nil
	}
	addr := net.JoinHostPort("0.0.0.0", strconv.Itoa(port))
	var c io.Closer
	var err error
	if network == "tcp" {
		c, err = net.Listen("tcp4", addr)
	} else {
		c, err = net.ListenPacket("udp4", addr)
	}
	if err == nil {
		c.Close()
		return nil
	}
	if errors.Is(err, windows.WSAEADDRINUSE) || errors.Is(err, windows.WSAEACCES) {
		return fmt.Errorf("port held for IPv4 by another program: %w", err)
	}
	return nil
}
