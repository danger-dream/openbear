package proxy

import (
	"syscall"

	"golang.org/x/sys/unix"
)

// bindControl binds a socket to iface, past the routes a local proxy or TUN
// sets for this machine's own traffic; nil without one.
func bindControl(iface string) func(network, address string, c syscall.RawConn) error {
	if iface == "" {
		return nil
	}
	return func(network, address string, c syscall.RawConn) error {
		var err error
		if cerr := c.Control(func(fd uintptr) { err = unix.BindToDevice(int(fd), iface) }); cerr != nil {
			return cerr
		}
		return err
	}
}
