package transport

import (
	"context"
	"encoding/binary"
	"net"
	"net/netip"
	"testing"
	"time"

	"golang.zx2c4.com/wireguard/conn"
)

func loopbackSink(t *testing.T) (*net.UDPConn, netip.AddrPort) {
	t.Helper()
	c, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { c.Close() })
	return c, c.LocalAddr().(*net.UDPAddr).AddrPort()
}

// A test arrives from a port other than the relay's listener, is counted
// only for an id the node watches, and is no larger than the request.
func TestInboundTestsFromAnotherPort(t *testing.T) {
	port := freePort(t, "udp")
	relay := newBind(t, Options{ListenUDP: port, Reflector: true})
	openBind(t, relay)
	member := newBind(t, Options{ListenUDP: freePort(t, "udp")})
	openBind(t, member)
	time.Sleep(200 * time.Millisecond)
	server := netip.AddrPortFrom(netip.MustParseAddr("127.0.0.1"), uint16(port))
	id := [8]byte{1, 2, 3}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	seen, err := member.Reflect(ctx, server, id)
	if err != nil {
		t.Fatal(err)
	}
	target, ok := relay.ReflectSeen(id)
	if !ok || target != seen {
		t.Fatalf("relay saw %v %v", target, ok)
	}

	sink, sinkAddr := loopbackSink(t)
	if err := relay.SendTest(sinkAddr, id, TestRelay); err != nil {
		t.Fatal(err)
	}
	buf := make([]byte, 2048)
	sink.SetReadDeadline(time.Now().Add(time.Second))
	n, from, err := sink.ReadFromUDPAddrPort(buf)
	if err != nil || n != reflectSize || buf[4] != reflectTest || buf[13] != TestRelay || from.Port() == uint16(port) {
		t.Fatalf("test packet %x from %v: %v; the relay listens on %d", buf[:n], from, err, port)
	}

	unwatched := [8]byte{9}
	relay.SendTest(target, unwatched, TestOther)
	if !member.WatchTests(id) {
		t.Fatal("cannot watch")
	}
	relay.SendTest(target, id, TestOther)
	deadline := time.Now().Add(2 * time.Second)
	for member.TestsSeen(id) == 0 && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if got := member.TestsSeen(id); got != 1<<TestOther {
		t.Fatalf("seen %b, want only the other relay's", got)
	}
	if member.TestsSeen(unwatched) != 0 {
		t.Fatal("a test for an id nobody watches was kept")
	}
	member.StopTests(id)
}

func TestPunchExtendSendsTheLastInitiationAtOnce(t *testing.T) {
	b := newBind(t, Options{ListenUDP: freePort(t, "udp")})
	openBind(t, b)
	id := PeerIdentity{ID: "peer", DataKey: [32]byte{1}, ControlKey: [32]byte{2}}
	if err := b.RegisterPeer(id); err != nil {
		t.Fatal(err)
	}
	lan, lanAddr := loopbackSink(t)
	public, publicAddr := loopbackSink(t)
	ep, err := b.PunchEndpoint("peer", []netip.AddrPort{lanAddr}, time.Now().Add(time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	initiation := make([]byte, 148)
	binary.LittleEndian.PutUint32(initiation, 1)
	initiation[100] = 42
	if err := b.SendWithMetadata([][]byte{initiation}, id.DataKey, conn.SendMetadata{Endpoint: ep}); err != nil {
		t.Fatal(err)
	}
	if got := replies(t, lan, 200*time.Millisecond); len(got) != 1 {
		t.Fatalf("LAN address got %d", len(got))
	}
	if got := replies(t, public, 200*time.Millisecond); len(got) != 0 {
		t.Fatal("an address not yet added got a handshake")
	}
	if err := b.PunchExtend(ep, []netip.AddrPort{lanAddr, publicAddr}); err != nil {
		t.Fatal(err)
	}
	got := replies(t, public, 200*time.Millisecond)
	if len(got) != 1 || len(got[0]) != 5+148 || got[0][5+100] != 42 {
		t.Fatalf("added address got %d packets, want the last initiation", len(got))
	}
	if again := replies(t, lan, 200*time.Millisecond); len(again) != 0 {
		t.Fatal("an address already there got the initiation twice")
	}
}

func stunAnswer(tx []byte, mapped netip.AddrPort, xor bool) []byte {
	attr := make([]byte, 12)
	binary.BigEndian.PutUint16(attr[0:2], stunMapped)
	port, ip := mapped.Port(), mapped.Addr().As4()
	if xor {
		binary.BigEndian.PutUint16(attr[0:2], stunXOR)
		port ^= stunCookie >> 16
		binary.BigEndian.PutUint32(ip[:], binary.BigEndian.Uint32(ip[:])^stunCookie)
	}
	binary.BigEndian.PutUint16(attr[2:4], 8)
	attr[5] = 1
	binary.BigEndian.PutUint16(attr[6:8], port)
	copy(attr[8:], ip[:])
	// An unknown attribute with padding first.
	other := []byte{0x80, 0x22, 0, 3, 'o', 'b', 'x', 0}
	p := make([]byte, 20, 20+len(other)+len(attr))
	binary.BigEndian.PutUint16(p[0:2], stunResponse)
	binary.BigEndian.PutUint16(p[2:4], uint16(len(other)+len(attr)))
	binary.BigEndian.PutUint32(p[4:8], stunCookie)
	copy(p[8:20], tx)
	return append(append(p, other...), attr...)
}

func TestSTUNFromTheWireGuardSocket(t *testing.T) {
	member := newBind(t, Options{ListenUDP: freePort(t, "udp")})
	openBind(t, member)
	server, serverAddr := loopbackSink(t)
	stranger, _ := loopbackSink(t)
	want := netip.MustParseAddrPort("203.0.113.9:40001")
	go func() {
		buf := make([]byte, 2048)
		server.SetReadDeadline(time.Now().Add(3 * time.Second))
		n, from, err := server.ReadFromUDPAddrPort(buf)
		if err != nil || n != 20 || binary.BigEndian.Uint16(buf[0:2]) != stunRequest {
			return
		}
		// A forged answer from elsewhere first, then the real one.
		stranger.WriteToUDPAddrPort(stunAnswer(buf[8:20], netip.MustParseAddrPort("198.51.100.1:1"), true), from)
		time.Sleep(50 * time.Millisecond)
		server.WriteToUDPAddrPort(stunAnswer(buf[8:20], want, true), from)
	}()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	got, err := member.STUN(ctx, serverAddr)
	if err != nil || got != want {
		t.Fatalf("STUN gave %v, %v; want %v", got, err, want)
	}
	if a, err := parseSTUN(stunAnswer(make([]byte, 12), want, false)); err != nil || a != want {
		t.Fatalf("MAPPED-ADDRESS: %v %v", a, err)
	}
}
