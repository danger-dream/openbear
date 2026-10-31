package transport

import (
	"context"
	"encoding/binary"
	"errors"
	"net"
	"net/netip"
	"slices"
	"sync/atomic"
	"time"

	"golang.zx2c4.com/wireguard/conn"
)

// Reflector messages are reflectSize bytes and anything of another size is
// ignored, so no reply is larger than what a spoofed source sent.
//
//	request: "OBR1" | 1 | id[8] | zero[19]
//	reply:   "OBR1" | 2 | id[8] | port[2] | ip length (4 or 16) | ip[16]
//	test:    "OBR1" | 3 | id[8] | sender (TestRelay or TestOther) | zero[18]
//	prime:   "OBR1" | 4 | zero[27]
//
// A test is what a relay sends, from another socket, to the address a
// request with the same id came from; a prime only opens this node's NAT
// and is sent with a TTL meant to expire before the peer's.
const (
	reflectMagic   = "OBR1"
	reflectSize    = 32
	reflectRequest = 1
	reflectReply   = 2
	reflectTest    = 3
	reflectPrime   = 4
	// Per-second limits on the replies a relay sends: per source address,
	// which members behind one NAT share, and in total.
	reflectPerSource = 8
	reflectTotal     = 1024
	// How long an answered request is remembered for a test, and its
	// source for RecentlyFrom; both maps are bounded.
	reflectKeep     = 10 * time.Second
	reflectFromKeep = 5 * time.Minute
	reflectMaxIDs   = 1024
	reflectMaxFrom  = 4096
	maxTestSenders  = 32
)

// Senders of a test packet.
const (
	TestRelay byte = 1 // the relay the request went to, from another port
	TestOther byte = 2 // another relay
)

type reflectLimiter struct {
	window time.Time
	counts map[netip.Addr]int
	total  int
}

func (l *reflectLimiter) allow(from netip.Addr, now time.Time) bool {
	if now.Sub(l.window) >= time.Second || l.counts == nil {
		l.window, l.counts, l.total = now, map[netip.Addr]int{}, 0
	}
	if l.total >= reflectTotal || l.counts[from] >= reflectPerSource {
		return false
	}
	l.total++
	l.counts[from]++
	return true
}

type reflectSeen struct {
	addr netip.AddrPort
	at   time.Time
}

func isReflect(p []byte) bool { return len(p) == reflectSize && string(p[:4]) == reflectMagic }

// reflect handles a reflector message read from u; called without b.mu.
func (b *Bind) reflect(u *udpSocket, p []byte, from netip.AddrPort) {
	var id [8]byte
	copy(id[:], p[5:13])
	switch p[4] {
	case reflectRequest:
		now := time.Now()
		b.mu.Lock()
		ok := b.opt.Reflector && b.reflectLimit.allow(from.Addr(), now)
		if ok {
			b.remember(id, from, now)
		}
		b.mu.Unlock()
		if !ok {
			return
		}
		reply := make([]byte, reflectSize)
		copy(reply, reflectMagic)
		reply[4] = reflectReply
		copy(reply[5:13], id[:])
		binary.BigEndian.PutUint16(reply[13:15], from.Port())
		ip := from.Addr().AsSlice()
		reply[15] = byte(len(ip))
		copy(reply[16:], ip)
		u.WriteToUDPAddrPort(reply, from)
	case reflectReply:
		n := int(p[15])
		if n != 4 && n != 16 {
			return
		}
		addr, _ := netip.AddrFromSlice(p[16 : 16+n])
		b.mu.Lock()
		w := b.reflectWait[id]
		if w != nil && w.server == from {
			delete(b.reflectWait, id)
			w.reply <- netip.AddrPortFrom(addr.Unmap(), binary.BigEndian.Uint16(p[13:15]))
		}
		b.mu.Unlock()
	case reflectTest:
		b.mu.Lock()
		if seen, ok := b.tests[id]; ok && (p[13] == TestRelay || p[13] == TestOther) {
			b.tests[id] = seen | 1<<p[13]
		}
		b.mu.Unlock()
	}
}

// remember keeps an answered request for a test; called with b.mu held.
func (b *Bind) remember(id [8]byte, from netip.AddrPort, now time.Time) {
	if len(b.reflectIDs) >= reflectMaxIDs {
		for k, v := range b.reflectIDs {
			if now.Sub(v.at) > reflectKeep {
				delete(b.reflectIDs, k)
			}
		}
	}
	if len(b.reflectIDs) < reflectMaxIDs {
		b.reflectIDs[id] = reflectSeen{from, now}
	}
	if len(b.reflectFrom) >= reflectMaxFrom {
		for k, at := range b.reflectFrom {
			if now.Sub(at) > reflectFromKeep {
				delete(b.reflectFrom, k)
			}
		}
	}
	if _, ok := b.reflectFrom[from.Addr()]; ok || len(b.reflectFrom) < reflectMaxFrom {
		b.reflectFrom[from.Addr()] = now
	}
}

type reflectWaiter struct {
	server netip.AddrPort
	reply  chan netip.AddrPort
}

// Reflect asks the relay at server which address this node's UDP socket,
// the one WireGuard uses, is seen from. The relay keeps id for a while so
// that a reachability test can be sent to that address.
func (b *Bind) Reflect(ctx context.Context, server netip.AddrPort, id [8]byte) (netip.AddrPort, error) {
	server = netip.AddrPortFrom(server.Addr().Unmap(), server.Port())
	w := &reflectWaiter{server: server, reply: make(chan netip.AddrPort, 1)}
	b.mu.Lock()
	u := b.udp()
	if u == nil || len(b.reflectWait) >= 16 || b.reflectWait[id] != nil {
		b.mu.Unlock()
		return netip.AddrPort{}, net.ErrClosed
	}
	b.reflectWait[id] = w
	b.mu.Unlock()
	defer func() {
		b.mu.Lock()
		delete(b.reflectWait, id)
		b.mu.Unlock()
	}()
	req := make([]byte, reflectSize)
	copy(req, reflectMagic)
	req[4] = reflectRequest
	copy(req[5:13], id[:])
	if _, err := u.WriteToUDPAddrPort(req, server); err != nil {
		return netip.AddrPort{}, err
	}
	select {
	case a := <-w.reply:
		return a, nil
	case <-ctx.Done():
		return netip.AddrPort{}, ctx.Err()
	}
}

// udp is the WireGuard socket; called with b.mu held.
func (b *Bind) udp() *udpSocket {
	if b.run == nil {
		return nil
	}
	return b.run.udp
}

// UDPPort is the local port of the WireGuard UDP socket, 0 without one.
func (b *Bind) UDPPort() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return udpPort(b.udp())
}

// ReflectSeen is where an answered request with id came from.
func (b *Bind) ReflectSeen(id [8]byte) (netip.AddrPort, bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	s, ok := b.reflectIDs[id]
	if !ok || time.Since(s.at) > reflectKeep {
		return netip.AddrPort{}, false
	}
	return s.addr, true
}

// RecentlyFrom says whether ip may hold NAT or firewall state toward this
// node: it asked this reflector, or has a UDP path that this node dials or
// that authenticated, within the last while.
func (b *Bind) RecentlyFrom(ip netip.Addr, within time.Duration) bool {
	ip = ip.Unmap()
	b.mu.Lock()
	defer b.mu.Unlock()
	if at, ok := b.reflectFrom[ip]; ok && time.Since(at) < within {
		return true
	}
	for _, ep := range b.ids {
		if ep.transport != "udp" || ep.retired || ep.remote.Addr().Unmap() != ip {
			continue
		}
		if ep.outbound || time.Since(ep.lastAuthenticated[0]) < within || time.Since(ep.lastAuthenticated[1]) < within {
			return true
		}
	}
	return false
}

var testSenders atomic.Int32

// SendTest sends three test packets for id to target from a socket of its
// own, so that they do not match any NAT state the target opened toward
// this node's listener.
func (b *Bind) SendTest(target netip.AddrPort, id [8]byte, sender byte) error {
	if testSenders.Add(1) > maxTestSenders {
		testSenders.Add(-1)
		return ErrBackpressure
	}
	network := "udp4"
	if !target.Addr().Unmap().Is4() {
		network = "udp6"
	}
	c, err := net.ListenUDP(network, nil)
	if err != nil {
		testSenders.Add(-1)
		return err
	}
	p := make([]byte, reflectSize)
	copy(p, reflectMagic)
	p[4] = reflectTest
	copy(p[5:13], id[:])
	p[13] = sender
	to := netip.AddrPortFrom(target.Addr().Unmap(), target.Port())
	go func() {
		defer testSenders.Add(-1)
		defer c.Close()
		for i := 0; i < 3; i++ {
			if i > 0 {
				time.Sleep(300 * time.Millisecond)
			}
			c.WriteToUDPAddrPort(p, to)
		}
	}()
	return nil
}

// WatchTests counts test packets for id until StopTests.
func (b *Bind) WatchTests(id [8]byte) bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	if len(b.tests) >= 16 {
		return false
	}
	b.tests[id] = 0
	return true
}

// TestsSeen is a bit 1<<sender for each sender a test arrived from.
func (b *Bind) TestsSeen(id [8]byte) byte {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.tests[id]
}

func (b *Bind) StopTests(id [8]byte) {
	b.mu.Lock()
	defer b.mu.Unlock()
	delete(b.tests, id)
}

// Prime sends one prime to each address with the given IP TTL.
func (b *Bind) Prime(addrs []netip.AddrPort, ttl int) error {
	if ttl < 1 || ttl > 255 {
		return errors.New("invalid TTL")
	}
	b.mu.Lock()
	u := b.udp()
	b.mu.Unlock()
	if u == nil {
		return net.ErrClosed
	}
	p := make([]byte, reflectSize)
	copy(p, reflectMagic)
	p[4] = reflectPrime
	var firstErr error
	for _, a := range addrs {
		if err := writeTTL(u, p, netip.AddrPortFrom(a.Addr().Unmap(), a.Port()), ttl); err != nil && firstErr == nil {
			firstErr = err
		}
	}
	return firstErr
}

// punchEndpoint is where a WireGuard handshake initiation goes to every
// address a peer may be reachable at. Only that message may use it, and only
// until it expires: WireGuard repeats its initiation to the same endpoint.
type punchEndpoint struct {
	b     *Bind
	peer  string
	until time.Time
	first netip.Addr
	addrs []netip.AddrPort // under b.mu
	last  []byte           // the last initiation sent, under b.mu
}

var (
	errPunchMessage = errors.New("only handshake initiations go to every punch address")
	errPunchOver    = errors.New("the punch attempt is over")
)

const maxPunchAddrs = 8

// PunchEndpoint is for Peer.SendHandshakeInitiationTo on the business plane.
func (b *Bind) PunchEndpoint(peer string, addrs []netip.AddrPort, until time.Time) (conn.Endpoint, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.peers[peer] == nil {
		return nil, errors.New("unknown peer")
	}
	if len(addrs) == 0 || len(addrs) > maxPunchAddrs {
		return nil, errors.New("one to eight punch addresses")
	}
	return &punchEndpoint{b: b, peer: peer, first: addrs[0].Addr(), addrs: slices.Clone(addrs), until: until}, nil
}

// PunchExtend adds addresses to a punch endpoint and sends them the last
// initiation at once, instead of after WireGuard's next retry.
func (b *Bind) PunchExtend(e conn.Endpoint, addrs []netip.AddrPort) error {
	p, ok := e.(*punchEndpoint)
	if !ok || p.b != b {
		return errors.New("not a punch endpoint")
	}
	b.mu.Lock()
	var added []netip.AddrPort
	for _, a := range addrs {
		if !slices.Contains(p.addrs, a) && len(p.addrs) < maxPunchAddrs {
			p.addrs = append(p.addrs, a)
			added = append(added, a)
		}
	}
	last := p.last
	u := b.udp()
	b.mu.Unlock()
	if len(added) == 0 || last == nil || u == nil || time.Now().After(p.until) {
		return nil
	}
	for _, a := range added {
		u.WriteToUDPAddrPort(last, a)
	}
	return nil
}

func (b *Bind) sendPunch(bufs [][]byte, key [32]byte, e *punchEndpoint, plane uint8) error {
	if len(bufs) != 1 || len(bufs[0]) != 148 || binary.LittleEndian.Uint32(bufs[0]) != 1 || plane != 0 {
		return errPunchMessage
	}
	b.mu.Lock()
	p := b.peers[e.peer]
	u := b.udp()
	addrs := slices.Clone(e.addrs)
	head := preambleFor(b.datagramVersion(e.peer, 0))
	b.mu.Unlock()
	if p == nil || p.identity.DataKey != key {
		return ErrSecurity
	}
	if u == nil {
		return net.ErrClosed
	}
	if time.Now().After(e.until) {
		// WireGuard then sends its retry the peer's own way.
		return errPunchOver
	}
	packet := make([]byte, 5+len(bufs[0]))
	copy(packet, head[:])
	copy(packet[5:], bufs[0])
	b.mu.Lock()
	e.last = packet
	b.mu.Unlock()
	var firstErr error
	sent := false
	for _, a := range addrs {
		if _, err := u.WriteToUDPAddrPort(packet, a); err != nil {
			firstErr = err
		} else {
			sent = true
		}
	}
	if sent {
		return nil
	}
	return firstErr
}

func (e *punchEndpoint) ClearSrc()           {}
func (e *punchEndpoint) SrcToString() string { return "" }
func (e *punchEndpoint) DstToString() string { return "punch://" + e.peer }
func (e *punchEndpoint) DstToBytes() []byte  { return []byte(e.DstToString()) }
func (e *punchEndpoint) DstIP() netip.Addr   { return e.first }
func (e *punchEndpoint) SrcIP() netip.Addr   { return netip.Addr{} }
