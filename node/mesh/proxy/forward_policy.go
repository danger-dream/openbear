package proxy

import (
	"net"
	"net/netip"
	"slices"
)

// forwardRequest is the complete authority exercised by a target tunnel.
// Pending prewarmed tunnels have no request and must use the current policy.
type forwardRequest struct {
	cmd  byte
	addr Addr
}

func forwardAllowed(targets []ForwardTarget, from netip.Addr, fast bool, req *forwardRequest) bool {
	for _, t := range targets {
		if t.From != from || t.Fast != fast {
			continue
		}
		if req == nil {
			return true
		}
		kind := "tcp"
		if req.cmd == cmdUDP {
			kind = "udp"
		} else if req.cmd != cmdConnect {
			return false
		}
		if takes(t.Proto, kind) && addrOf(t.Host, t.Port) == req.addr {
			return true
		}
	}
	return false
}

// Administrative revocation is a reset, not an application FIN. Otherwise a
// remote relay cannot distinguish cancellation from a valid half-closed reader.
func abortForwardConn(c net.Conn) {
	if tcp, ok := c.(*net.TCPConn); ok {
		_ = tcp.SetLinger(0)
	}
	_ = c.Close()
}

func (t *conns) closeLocked(c net.Conn, i *connInfo) {
	delete(t.set, c)
	abortForwardConn(c)
	for _, p := range i.peers {
		abortForwardConn(p)
	}
}

// pair registers a borrowed socket or closes it if revocation won the race.
func (t *conns) pair(c, peer net.Conn) bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	i := t.set[c]
	if i == nil {
		abortForwardConn(peer)
		return false
	}
	i.peers = append(i.peers, peer)
	return true
}

func (t *conns) setEntry(l *allowList, closed bool) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.entryAllow, t.closed = l, closed
	for c, i := range t.set {
		if closed || !l.has(i.from) {
			t.closeLocked(c, i)
		}
	}
}

func (t *conns) updateForward(targets []ForwardTarget) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.forwardTargets = slices.Clone(targets)
	for c, i := range t.set {
		if !forwardAllowed(targets, i.from, i.fast, i.request) {
			t.closeLocked(c, i)
		}
	}
}

func (t *conns) addForward(c net.Conn, from netip.Addr, fast bool) bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	if !forwardAllowed(t.forwardTargets, from, fast, nil) || !t.addLocked(c, from) {
		return false
	}
	t.set[c].fast = fast
	return true
}

func (t *conns) authorizeForward(c net.Conn, cmd byte, addr Addr) bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	i := t.set[c]
	req := &forwardRequest{cmd: cmd, addr: addr}
	if i == nil || !forwardAllowed(t.forwardTargets, i.from, i.fast, req) {
		return false
	}
	i.request = req
	return true
}

// ServeFast is a trusted, in-process handoff, never a protocol field accepted
// from a local TCP client. Fast already checked this origin's access to target;
// forward still checks its own (origin, backend, protocol) policy. No extra
// framing, encryption, local re-dial or per-byte copying is added to this path.
func (s *Forwarder) ServeFast(c *net.TCPConn, origin netip.Addr, target netip.AddrPort) (bool, int64, int64) {
	s.mu.Lock()
	srv := s.target
	if srv == nil || target.Addr() != srv.self || int(target.Port()) != srv.fastPort {
		s.mu.Unlock()
		return false, 0, 0
	}
	admitted := srv.open.addForward(c, origin, true)
	s.mu.Unlock()
	if !admitted {
		srv.stats.Refused.Add(1)
		c.Close()
		return true, 0, 0
	}
	in, out := srv.serve(c, origin, true)
	return true, in, out
}
