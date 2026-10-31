package mesh

import (
	"net"
	"net/netip"
	"sync"
	"time"
)

// localAddrOf is what status shows as a path's local address: the
// socket's, or, for one bound to the wildcard address (the UDP socket),
// the address the system sends from to remote on that port. It is the
// route's source, found with a UDP socket connected to remote, which sends
// nothing; kept for sourceKeep. The socket's address stays when remote
// is not an address or has no route.
func localAddrOf(local, remote string) string {
	l, err := netip.ParseAddrPort(local)
	if err != nil || !l.Addr().IsUnspecified() {
		return local
	}
	r, err := netip.ParseAddrPort(remote)
	if err != nil || !r.Addr().IsValid() {
		return local
	}
	src, ok := sources.of(r.Addr().Unmap(), time.Now())
	if !ok {
		return local
	}
	return netip.AddrPortFrom(src, l.Port()).String()
}

const sourceKeep = 30 * time.Second

type source struct {
	addr netip.Addr
	ok   bool
	at   time.Time
}

type sourceCache struct {
	mu sync.Mutex
	m  map[netip.Addr]source
}

var sources = sourceCache{m: map[netip.Addr]source{}}

func (c *sourceCache) of(remote netip.Addr, now time.Time) (netip.Addr, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if s, ok := c.m[remote]; ok && now.Sub(s.at) < sourceKeep {
		return s.addr, s.ok
	}
	for k, s := range c.m {
		if now.Sub(s.at) >= sourceKeep {
			delete(c.m, k)
		}
	}
	s := source{at: now}
	if conn, err := net.DialUDP("udp", nil, net.UDPAddrFromAddrPort(netip.AddrPortFrom(remote, 9))); err == nil {
		if a, ok := conn.LocalAddr().(*net.UDPAddr); ok {
			s.addr = a.AddrPort().Addr().Unmap()
			s.ok = s.addr.IsValid() && !s.addr.IsUnspecified()
		}
		conn.Close()
	}
	c.m[remote] = s
	return s.addr, s.ok
}
