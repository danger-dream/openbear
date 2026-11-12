package proxy

import (
	"net"
	"net/netip"
	"sync"
	"time"
)

// Addresses an exit never dials: the mesh's (traffic between members goes
// over the mesh itself), unspecified, multicast and reserved ones.
var neverNets = mustPrefixes("0.0.0.0/8", "224.0.0.0/4", "240.0.0.0/4", "ff00::/8", "::/128")

// Private and local addresses, which only an exit with LAN may dial.
var lanNets = mustPrefixes("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16", "127.0.0.0/8",
	"fc00::/7", "fe80::/10", "::1/128")

func mustPrefixes(s ...string) []netip.Prefix {
	out := make([]netip.Prefix, len(s))
	for i, v := range s {
		out[i] = netip.MustParsePrefix(v)
	}
	return out
}

// policy decides which addresses an exit dials.
type policy struct {
	lan  bool
	mesh []netip.Prefix // the mesh's and its control plane's
	self *localAddrs
}

func (p *policy) allowed(a netip.Addr) bool {
	a = a.Unmap()
	for _, n := range p.mesh {
		if n.Contains(a) {
			return false
		}
	}
	for _, n := range neverNets {
		if n.Contains(a) {
			return false
		}
	}
	if p.lan {
		return true
	}
	for _, n := range lanNets {
		if n.Contains(a) {
			return false
		}
	}
	return !p.self.has(a)
}

// localAddrs are this machine's own addresses, its public ones included,
// refreshed now and then.
type localAddrs struct {
	mu    sync.Mutex
	addrs map[netip.Addr]bool
	at    time.Time
}

func (l *localAddrs) has(a netip.Addr) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	if time.Since(l.at) > 30*time.Second {
		l.addrs, l.at = map[netip.Addr]bool{}, time.Now()
		if list, err := net.InterfaceAddrs(); err == nil {
			for _, x := range list {
				if n, ok := x.(*net.IPNet); ok {
					if ip, ok := netip.AddrFromSlice(n.IP); ok {
						l.addrs[ip.Unmap()] = true
					}
				}
			}
		}
	}
	return l.addrs[a]
}
