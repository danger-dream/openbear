package fast

import (
	"context"
	"encoding/binary"
	"io"
	"net"
	"net/netip"
	"sync/atomic"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/buffer"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/adapters/gonet"
	iphdr "gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/network/ipv4"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
)

func injectFallbackPacket(f *sourceFallback, b []byte) {
	pkt := stack.NewPacketBuffer(stack.PacketBufferOptions{Payload: buffer.MakeWithData(b)})
	f.ep.InjectInbound(iphdr.IPv4ProtocolNumber, pkt)
	pkt.DecRef()
}

// No OS route/NAT or Dial echo substitute: two real user-space TCP/IP stacks
// exchange actual packets in memory, while entryTCP/UDP sessions do fallback.
func TestReviewLANFallbackPacketsAndReplyDispatch(t *testing.T) {
	s, st := reviewState(t)
	lan := netip.MustParseAddr("10.10.0.50")
	target := netip.AddrPortFrom(st.cfg.Nodes["B"], 443)
	st.cfg.Targets[0].Proto = "any"
	st.targets, st.dests = targetsOf(st.cfg, "A")
	st.pool = newPool()
	s.pool = st.pool
	st.chans = &channels{svc: s, byDest: map[string]*channel{}, wake: make(chan struct{}, 1)}
	var tcpPackets, udpPackets, badSource atomic.Int64
	server, err := newSourceFallback(func(b []byte) { s.Inbound(b) }, 1280)
	if err != nil {
		t.Fatal(err)
	}
	defer server.close()
	s.hooks = Hooks{LocalSubnets: func() []netip.Prefix { return []netip.Prefix{netip.MustParsePrefix("10.10.0.0/24")} }, ToMesh: func(b []byte) {
		if netip.AddrFrom4([4]byte(b[12:16])) != lan {
			badSource.Add(1)
		}
		switch b[9] {
		case 6:
			tcpPackets.Add(1)
		case 17:
			udpPackets.Add(1)
		}
		injectFallbackPacket(server, b)
	}}
	defer func() { s.mu.Lock(); s.stopAllLocked(); s.mu.Unlock() }()
	if s.fallback.Load() != nil {
		t.Fatal("fallback eagerly created for ordinary fast config")
	}
	at := tcpip.FullAddress{NIC: 1, Addr: tcpip.AddrFrom4(target.Addr().As4()), Port: target.Port()}
	ln, err := gonet.ListenTCP(server.stack, at, ipv4.ProtocolNumber)
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	peerSource := make(chan string, 1)
	go func() {
		c, e := ln.Accept()
		if e != nil {
			return
		}
		defer c.Close()
		peerSource <- c.RemoteAddr().String()
		io.Copy(c, c)
	}()
	client, captured := clientPair(t)
	defer client.Close()
	defer captured.Close()
	done := make(chan struct{})
	go func() { defer close(done); s.entryTCP(captured, netip.AddrPortFrom(lan, 50001), true, target, nil) }()
	client.SetDeadline(time.Now().Add(3 * time.Second))
	if _, err := client.Write([]byte("lan-tcp")); err != nil {
		t.Fatal(err)
	}
	b := make([]byte, 7)
	if _, err := io.ReadFull(client, b); err != nil || string(b) != "lan-tcp" {
		t.Fatalf("TCP reply %q %v", b, err)
	}
	if from := <-peerSource; from != "10.10.0.50:50001" {
		t.Fatalf("TCP source=%s", from)
	}

	udpServer, err := gonet.DialUDP(server.stack, &at, nil, ipv4.ProtocolNumber)
	if err != nil {
		t.Fatal(err)
	}
	defer udpServer.Close()
	udpFrom := make(chan string, 1)
	go func() {
		b := make([]byte, 2048)
		n, a, e := udpServer.ReadFrom(b)
		if e == nil {
			udpFrom <- a.String()
			udpServer.WriteTo(b[:n], a)
		}
	}()
	table := newUDPSessionTable(s)
	defer table.close()
	replies := make(chan []byte, 1)
	key := udpSessionKey{client: netip.AddrPortFrom(lan, 50002), target: target, preserveLAN: true}
	if err := table.send(key, []byte("lan-udp"), func() (func([]byte) int, func(), error) {
		return func(b []byte) int { replies <- append([]byte(nil), b...); return len(b) }, nil, nil
	}, nil, nil, nil); err != nil {
		t.Fatal(err)
	}
	select {
	case b := <-replies:
		if string(b) != "lan-udp" {
			t.Fatal(string(b))
		}
	case <-time.After(3 * time.Second):
		t.Fatal("UDP reply not dispatched")
	}
	if from := <-udpFrom; from != "10.10.0.50:50002" {
		t.Fatalf("UDP source=%s", from)
	}
	if tcpPackets.Load() == 0 || udpPackets.Load() == 0 || badSource.Load() != 0 {
		t.Fatalf("actual packets tcp=%d udp=%d bad source=%d", tcpPackets.Load(), udpPackets.Load(), badSource.Load())
	}
	// An unrelated same-host UDP flow must still reach the OS/LAN.
	unrelated := make([]byte, 28)
	unrelated[0] = 0x45
	unrelated[9] = 17
	src, dst := target.Addr().As4(), lan.As4()
	copy(unrelated[12:], src[:])
	copy(unrelated[16:], dst[:])
	binary.BigEndian.PutUint16(unrelated[20:], 443)
	binary.BigEndian.PutUint16(unrelated[22:], 59999)
	if s.Inbound(unrelated) {
		t.Fatal("stole another LAN flow's reply")
	}
	// Current policy revocation closes both TCP and UDP fallback lifetimes.
	next := *st
	next.targets = map[targetKey]*Target{}
	s.openMu.Lock()
	s.state.Store(&next)
	s.openMu.Unlock()
	s.closeAllConns(func(c trackedConn) bool { return !next.allows(c) })
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("TCP fallback survived revocation")
	}
	f := s.fallback.Load()
	waitFor(t, "fallback tuples released", time.Second, func() bool { f.mu.Lock(); defer f.mu.Unlock(); return len(f.flows) == 0 })
	s.mu.Lock()
	s.stopAllLocked()
	s.mu.Unlock()
	if s.fallback.Load() != nil {
		t.Fatal("fallback stack survived disable")
	}
}

func TestReviewSourceDialCancelAndClose(t *testing.T) {
	f, err := newSourceFallback(func([]byte) {}, 1280)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		_, e := f.dial(ctx, protoTCP, netip.MustParseAddrPort("10.0.0.1:12345"), netip.MustParseAddrPort("100.96.0.2:80"))
		done <- e
	}()
	cancel()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("cancelled dial succeeded")
		}
	case <-time.After(time.Second):
		t.Fatal("cancel did not release dial")
	}
	f.close()
	if c, e := f.dial(context.Background(), protoUDP, netip.MustParseAddrPort("10.0.0.1:12346"), netip.MustParseAddrPort("100.96.0.2:80")); e == nil {
		c.Close()
		t.Fatal("closed fallback accepted flow")
	}
}

var _ net.Conn = (*fallbackConn)(nil)
