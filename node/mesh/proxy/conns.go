package proxy

import (
	"net"
	"net/netip"
	"slices"
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
	// Exit policy is shared by every generation of listener and tunnel.
	exitPolicy     *policy
	exitClients    []netip.Addr
	exitConfigured bool
	closed         bool
	entryAllow     *allowList
	forwardTargets []ForwardTarget
}

type connInfo struct {
	from, to netip.Addr
	peers    []net.Conn // borrowed sockets belong to the same administrative lifetime
	fast     bool
	request  *forwardRequest
}

// add counts c in; false when the server holds as many as it may.
func (t *conns) add(c net.Conn, from netip.Addr) bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.addLocked(c, from)
}

func (t *conns) addLocked(c net.Conn, from netip.Addr) bool {
	if t.closed || t.entryAllow != nil && !t.entryAllow.has(from) {
		return false
	}
	limit := t.max
	if limit == 0 {
		limit = maxConns
	}
	if t.exitConfigured && (t.exitPolicy == nil || !slices.Contains(t.exitClients, from)) {
		return false
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
func (t *conns) reached(c net.Conn, to netip.Addr) bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	if i := t.set[c]; i != nil {
		// A dial begun before Apply must not escape its revocation pass.
		if t.exitConfigured && (t.exitPolicy == nil || !slices.Contains(t.exitClients, i.from) || !t.exitPolicy.allowed(to)) {
			c.Close()
			return false
		}
		i.to = to
		return true
	}
	return false
}

func (t *conns) allowed(to netip.Addr, fallback *policy) bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	p := fallback
	if t.exitConfigured {
		p = t.exitPolicy
	}
	return p != nil && p.allowed(to)
}

func (t *conns) updateExit(p *policy, clients []netip.Addr) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.exitConfigured, t.exitPolicy, t.exitClients = true, p, slices.Clone(clients)
	for c, i := range t.set {
		if p == nil || !slices.Contains(clients, i.from) || i.to.IsValid() && !p.allowed(i.to) {
			t.closeLocked(c, i)
		}
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
			t.closeLocked(c, i)
		}
	}
}
