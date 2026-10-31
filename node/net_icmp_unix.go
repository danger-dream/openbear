//go:build linux || darwin

package node

import (
	"context"
	"encoding/binary"
	"errors"
	"net"
	"net/netip"
	"sync"
	"syscall"
	"time"

	"golang.org/x/net/icmp"
	"golang.org/x/net/ipv4"
	"golang.org/x/net/ipv6"
)

// rawEcho pings over a raw ICMP socket, which also receives the time
// exceeded and unreachable errors routers send back; replies are told apart
// by the echo ID and matched to their request by sequence number.
type rawEcho struct {
	c   *icmp.PacketConn
	p4  *ipv4.PacketConn
	p6  *ipv6.PacketConn
	dst net.Addr
	id  int

	wmu sync.Mutex // the ttl and the write that uses it

	mu     sync.Mutex
	waits  map[int]chan echoReply
	closed chan struct{}
	once   sync.Once
}

func openEcho(dst netip.Addr, id int) (echoConn, error) {
	e := &rawEcho{dst: &net.IPAddr{IP: dst.AsSlice(), Zone: dst.Zone()}, id: id, waits: map[int]chan echoReply{}, closed: make(chan struct{})}
	var err error
	if dst.Is4() {
		if e.c, err = icmp.ListenPacket("ip4:icmp", "0.0.0.0"); err == nil {
			e.p4 = e.c.IPv4PacketConn()
			err = e.p4.SetControlMessage(ipv4.FlagTTL, true)
		}
	} else if e.c, err = icmp.ListenPacket("ip6:ipv6-icmp", "::"); err == nil {
		e.p6 = e.c.IPv6PacketConn()
		var f ipv6.ICMPFilter
		f.SetAll(true)
		for _, t := range []ipv6.ICMPType{ipv6.ICMPTypeEchoReply, ipv6.ICMPTypeTimeExceeded, ipv6.ICMPTypeDestinationUnreachable} {
			f.Accept(t)
		}
		if err = e.p6.SetICMPFilter(&f); err == nil {
			err = e.p6.SetControlMessage(ipv6.FlagHopLimit, true)
		}
	}
	if err != nil {
		if e.c != nil {
			e.c.Close()
		}
		return nil, err
	}
	go e.read()
	return e, nil
}

func (e *rawEcho) Close() error {
	e.once.Do(func() { close(e.closed) })
	return e.c.Close()
}

func (e *rawEcho) echo(ctx context.Context, seq, ttl int, timeout time.Duration) echoReply {
	ch := make(chan echoReply, 1)
	e.mu.Lock()
	e.waits[seq] = ch
	e.mu.Unlock()
	defer func() {
		e.mu.Lock()
		delete(e.waits, seq)
		e.mu.Unlock()
	}()
	var typ icmp.Type = ipv4.ICMPTypeEcho
	if e.p6 != nil {
		typ = ipv6.ICMPTypeEchoRequest
	}
	// The kernel fills in the ICMPv6 checksum.
	b, err := (&icmp.Message{Type: typ, Body: &icmp.Echo{ID: e.id, Seq: seq, Data: probePayload}}).Marshal(nil)
	if ttl == 0 {
		ttl = 64
	}
	e.wmu.Lock()
	if err == nil && e.p4 != nil {
		err = e.p4.SetTTL(ttl)
	} else if err == nil {
		err = e.p6.SetHopLimit(ttl)
	}
	sent := time.Now()
	if err == nil {
		_, err = e.c.WriteTo(b, e.dst)
	}
	e.wmu.Unlock()
	if err != nil {
		return echoReply{kind: replyFailed, err: err}
	}
	t := time.NewTimer(timeout)
	defer t.Stop()
	select {
	case r := <-ch:
		r.rtt = r.at.Sub(sent)
		return r
	case <-t.C:
	case <-ctx.Done():
	case <-e.closed:
	}
	return echoReply{kind: replyTimeout}
}

func (e *rawEcho) read() {
	b := make([]byte, 1500)
	for {
		var n, ttl int
		var src net.Addr
		var err error
		if e.p4 != nil {
			var cm *ipv4.ControlMessage
			if n, cm, src, err = e.p4.ReadFrom(b); cm != nil {
				ttl = cm.TTL
			}
		} else {
			var cm *ipv6.ControlMessage
			if n, cm, src, err = e.p6.ReadFrom(b); cm != nil {
				ttl = cm.HopLimit
			}
		}
		if err != nil {
			return
		}
		at := time.Now()
		kind, id, seq, ok := parseEchoResponse(e.p6 != nil, b[:n])
		if !ok || id != e.id {
			continue
		}
		r := echoReply{kind: kind, at: at}
		if a, ok := src.(*net.IPAddr); ok {
			r.from, _ = netip.AddrFromSlice(a.IP)
			r.from = r.from.Unmap()
		}
		if kind == replyEcho {
			r.ttl = ttl
		}
		e.mu.Lock()
		ch := e.waits[seq]
		delete(e.waits, seq)
		e.mu.Unlock()
		if ch != nil {
			ch <- r
		}
	}
}

// parseEchoResponse reads an echo reply, or an error that quotes one of our
// echo requests, and returns the echo's ID and sequence number.
func parseEchoResponse(v6 bool, b []byte) (replyKind, int, int, bool) {
	proto := 1
	if v6 {
		proto = 58
	}
	m, err := icmp.ParseMessage(proto, b)
	if err != nil {
		return 0, 0, 0, false
	}
	var quoted []byte
	kind := replyExceeded
	switch body := m.Body.(type) {
	case *icmp.Echo:
		if m.Type != ipv4.ICMPTypeEchoReply && m.Type != ipv6.ICMPTypeEchoReply {
			return 0, 0, 0, false
		}
		return replyEcho, body.ID, body.Seq, true
	case *icmp.TimeExceeded:
		quoted = body.Data
	case *icmp.DstUnreach:
		quoted, kind = body.Data, replyUnreachable
	default:
		return 0, 0, 0, false
	}
	// The quoted datagram: our IP header, then at least the echo's first 8 bytes.
	var echo []byte
	switch {
	case !v6 && len(quoted) >= 20 && quoted[9] == 1:
		if hl := int(quoted[0]&0x0f) * 4; len(quoted) >= hl+8 && quoted[hl] == 8 {
			echo = quoted[hl:]
		}
	case v6 && len(quoted) >= 48 && quoted[6] == 58 && quoted[40] == 128:
		echo = quoted[40:]
	}
	if echo == nil {
		return 0, 0, 0, false
	}
	return kind, int(binary.BigEndian.Uint16(echo[4:])), int(binary.BigEndian.Uint16(echo[6:])), true
}

func dialErrorKind(err error) string {
	switch {
	case errors.Is(err, syscall.ECONNREFUSED):
		return "refused"
	case errors.Is(err, syscall.ENETUNREACH), errors.Is(err, syscall.EHOSTUNREACH), errors.Is(err, syscall.EHOSTDOWN):
		return "unreachable"
	}
	return ""
}

// noRoute: the system has no route or source address for the target, or
// none of its family at all.
func noRoute(err error) bool {
	return errors.Is(err, syscall.EADDRNOTAVAIL) || errors.Is(err, syscall.ENETUNREACH) || errors.Is(err, syscall.EHOSTUNREACH) ||
		errors.Is(err, syscall.EAFNOSUPPORT)
}
