//go:build !linux

package transport

import (
	"errors"
	"net/netip"
	"sync"
)

var ttlMu sync.Mutex

// writeTTL changes the socket's TTL around one write: these systems take no
// per-datagram TTL. A WireGuard packet written in that moment leaves with
// the low TTL too and is lost like any other dropped datagram.
func writeTTL(u *udpSocket, p []byte, to netip.AddrPort, ttl int) error {
	raw, err := u.SyscallConn()
	if err != nil {
		return err
	}
	ttlMu.Lock()
	defer ttlMu.Unlock()
	var old4, old6 int
	var err4, err6 error
	if err = raw.Control(func(fd uintptr) {
		old4, err4 = getTTL(fd, false)
		old6, err6 = getTTL(fd, true)
		if err4 == nil {
			err4 = setTTL(fd, false, ttl)
		}
		if err6 == nil {
			err6 = setTTL(fd, true, ttl)
		}
	}); err != nil {
		return err
	}
	if err4 != nil && err6 != nil {
		return errors.Join(err4, err6)
	}
	_, werr := u.WriteToUDPAddrPort(p, to)
	raw.Control(func(fd uintptr) {
		if err4 == nil {
			setTTL(fd, false, old4)
		}
		if err6 == nil {
			setTTL(fd, true, old6)
		}
	})
	return werr
}
