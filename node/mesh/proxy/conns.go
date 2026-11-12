package proxy

import (
	"net"
	"net/netip"
	"sync"
)

// maxConns is how many connections an entry or an exit holds at once,
// tunnels opened ahead included; past it a new one is refused, so a client
// cannot take what the mesh in the same process needs.
const maxConns = 4096

// conns are a server's open connections, by the address they came from and,
// once known, the one they reach.
type conns struct {
	mu  sync.Mutex
	set map[net.Conn]*connInfo
	max int
}

type connInfo struct {
	from, to netip.Addr
}

// add counts c in; false when the server holds as many as it may.
func (t *conns) add(c net.Conn, from netip.Addr) bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	limit := t.max
	if limit == 0 {
		limit = maxConns
	}
	if len(t.set) >= limit {
		return false
	}
	if t.set == nil {
		t.set = map[net.Conn]*connInfo{}
	}
	t.set[c] = &connInfo{from: from}
	return true
}

// reached records the address c's connection goes to.
func (t *conns) reached(c net.Conn, to netip.Addr) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if i := t.set[c]; i != nil {
		i.to = to
	}
}

func (t *conns) done(c net.Conn) {
	t.mu.Lock()
	defer t.mu.Unlock()
	delete(t.set, c)
}

// cut closes the connections drop says to, every one for nil.
func (t *conns) cut(drop func(from, to netip.Addr) bool) {
	t.mu.Lock()
	defer t.mu.Unlock()
	for c, i := range t.set {
		if drop == nil || drop(i.from, i.to) {
			c.Close()
		}
	}
}
