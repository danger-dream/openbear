package proxy

import (
	"io"
	"net"
	"net/netip"
	"testing"
	"time"
)

func forwardReviewDial(t *testing.T, from netip.Addr, port int) net.Conn {
	t.Helper()
	d := net.Dialer{LocalAddr: &net.TCPAddr{IP: from.AsSlice()}, Timeout: time.Second}
	c, err := d.Dial("tcp", netip.AddrPortFrom(loopback, uint16(port)).String())
	if err != nil {
		t.Fatal(err)
	}
	c.SetDeadline(time.Now().Add(4 * time.Second))
	t.Cleanup(func() { c.Close() })
	return c
}
func forwardReviewRequest(t *testing.T, c net.Conn, a Addr) byte {
	t.Helper()
	if _, err := c.Write(tunnelHeader(cmdConnect, a)); err != nil {
		return 255
	}
	var b [1]byte
	if _, err := io.ReadFull(c, b[:]); err != nil {
		return 255
	}
	return b[0]
}

func TestForwardCurrentTuplePolicyOnOldPendingAndLiveTunnels(t *testing.T) {
	one, two := echo(t), echo(t)
	from := netip.MustParseAddr("127.0.0.2")
	cfg := &ForwardConfig{Port: int(freePort(t).Port()), Targets: []ForwardTarget{{From: from, Host: "127.0.0.1", Port: int(one.Port()), Proto: "tcp"}, {From: from, Host: "127.0.0.1", Port: int(two.Port()), Proto: "tcp"}}}
	var f Forwarder
	f.Apply(cfg, loopback)
	defer f.Close()
	allowed, revoked, pending := forwardReviewDial(t, from, cfg.Port), forwardReviewDial(t, from, cfg.Port), forwardReviewDial(t, from, cfg.Port)
	if forwardReviewRequest(t, allowed, target(one)) != statusOK || forwardReviewRequest(t, revoked, target(two)) != statusOK {
		t.Fatal("fixture open")
	}
	roundTrip(t, allowed, "keep")
	roundTrip(t, revoked, "remove")
	cfg.Targets = cfg.Targets[:1]
	f.Apply(cfg, loopback)
	roundTrip(t, allowed, "still allowed across Apply")
	if !closed(revoked) {
		t.Fatal("revoked backend still live")
	}
	if st := forwardReviewRequest(t, pending, target(two)); st == statusOK {
		t.Fatal("prewarmed old server used removed rule")
	}
}

func TestForwardTupleRevocationAndLateSocketRegistration(t *testing.T) {
	from := netip.MustParseAddr("100.96.0.1")
	targets := []ForwardTarget{{From: from, Fast: true, Host: "127.0.0.1", Port: 8080, Proto: "any"}}
	var open conns
	open.updateForward(targets)
	c, client := net.Pipe()
	defer c.Close()
	defer client.Close()
	if !open.addForward(c, from, true) || !open.authorizeForward(c, cmdUDP, addrOf("127.0.0.1", 8080)) {
		t.Fatal("fixture")
	}
	targets[0].Proto = "tcp"
	open.updateForward(targets)
	late, peer := net.Pipe()
	defer late.Close()
	defer peer.Close()
	if open.pair(c, late) {
		t.Fatal("late socket resurrected revoked UDP tuple")
	}
	if !closed(peer) {
		t.Fatal("late socket leaked")
	}
	if !closed(client) {
		t.Fatal("protocol narrowing did not revoke")
	}
	// Ordinary local self is not a substitute for an authorized entry.
	if forwardAllowed(targets, netip.MustParseAddr("100.96.0.2"), true, &forwardRequest{cmdConnect, addrOf("127.0.0.1", 8080)}) {
		t.Fatal("self bypass")
	}
}

func TestForwardCloseOwnsBorrowedTunnelWithSilentBackend(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	accepted := make(chan net.Conn, 1)
	go func() {
		c, e := ln.Accept()
		if e == nil {
			accepted <- c
		}
	}()
	backend := netip.MustParseAddrPort(ln.Addr().String())
	f, cfg := fwdLab(t, Forward{Proto: "tcp", Host: "127.0.0.1", Port: int(backend.Port())}, nil)
	client, err := net.Dial("tcp", netip.AddrPortFrom(loopback, uint16(cfg.Forwards[0].Listen)).String())
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	var silent net.Conn
	select {
	case silent = <-accepted:
	case <-time.After(3 * time.Second):
		t.Fatal("backend not opened")
	}
	defer silent.Close()
	done := make(chan struct{})
	go func() {
		// Stop only the entry. The target policy/listener stays up, so target
		// cleanup must follow cancellation, not a simultaneous target revoke.
		f.Apply(&ForwardConfig{Port: cfg.Port, Targets: cfg.Targets}, loopback)
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("entry removal waited on silent borrowed tunnel")
	}
	if !closed(client) {
		t.Fatal("client not closed")
	}
	if !closed(silent) {
		t.Fatal("target socket not closed")
	}
}
