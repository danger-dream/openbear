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

func reflectRequestBytes(id byte, size int) []byte {
	p := make([]byte, size)
	copy(p, reflectMagic)
	p[4] = reflectRequest
	p[5] = id
	return p
}

// replies collects what the reflector sends back to c until the wait ends.
func replies(t *testing.T, c *net.UDPConn, wait time.Duration) [][]byte {
	t.Helper()
	var out [][]byte
	buf := make([]byte, 2048)
	c.SetReadDeadline(time.Now().Add(wait))
	for {
		n, err := c.Read(buf)
		if err != nil {
			return out
		}
		out = append(out, append([]byte(nil), buf[:n]...))
	}
}

func TestReflectorRepliesNoLargerAndLimitsEachSource(t *testing.T) {
	port := freePort(t, "udp")
	relay := newBind(t, Options{ListenUDP: port, Reflector: true})
	openBind(t, relay)
	server := netip.AddrPortFrom(netip.MustParseAddr("127.0.0.1"), uint16(port))
	c, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	self := c.LocalAddr().(*net.UDPAddr).AddrPort()

	c.WriteToUDPAddrPort(reflectRequestBytes(1, reflectSize), server)
	got := replies(t, c, 300*time.Millisecond)
	if len(got) != 1 || len(got[0]) > reflectSize || got[0][4] != reflectReply || got[0][5] != 1 {
		t.Fatalf("one reply of at most %d bytes expected, got %d: %x", reflectSize, len(got), got)
	}
	ip, _ := netip.AddrFromSlice(got[0][16 : 16+int(got[0][15])])
	if seen := netip.AddrPortFrom(ip, binary.BigEndian.Uint16(got[0][13:15])); seen != self {
		t.Fatalf("reflected %v, sent from %v", seen, self)
	}

	// Anything but an exact-size request, padded or cut, gets no answer.
	for _, size := range []int{reflectSize - 1, reflectSize + 1, 1200} {
		c.WriteToUDPAddrPort(reflectRequestBytes(2, size), server)
	}
	if got := replies(t, c, 300*time.Millisecond); len(got) != 0 {
		t.Fatalf("requests of another size answered: %d replies", len(got))
	}

	time.Sleep(time.Second) // a fresh one-second window
	for i := 0; i < 3*reflectPerSource; i++ {
		c.WriteToUDPAddrPort(reflectRequestBytes(byte(10+i), reflectSize), server)
	}
	if got := replies(t, c, 300*time.Millisecond); len(got) != reflectPerSource {
		t.Fatalf("%d requests from one source within a second: %d replies, want %d", 3*reflectPerSource, len(got), reflectPerSource)
	}

	member := newBind(t, Options{ListenUDP: freePort(t, "udp")})
	openBind(t, member)
	plain := newBind(t, Options{ListenUDP: freePort(t, "udp")})
	openBind(t, plain)
	time.Sleep(time.Second)
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	id := [8]byte{7, 7}
	seen, err := member.Reflect(ctx, server, id)
	if err != nil || seen.Port() != uint16(member.opt.ListenUDP) {
		t.Fatalf("member reflected as %v, %v; listens on %d", seen, err, member.opt.ListenUDP)
	}
	if kept, ok := relay.ReflectSeen(id); !ok || kept != seen {
		t.Fatalf("the relay kept %v %v for the request, the member is %v", kept, ok, seen)
	}
	if !relay.RecentlyFrom(seen.Addr(), time.Minute) || relay.RecentlyFrom(netip.MustParseAddr("192.0.2.1"), time.Minute) {
		t.Fatal("RecentlyFrom does not follow the requests answered")
	}
	ctx2, cancel2 := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel2()
	notReflector := netip.AddrPortFrom(server.Addr(), uint16(plain.opt.ListenUDP))
	if _, err := member.Reflect(ctx2, notReflector, [8]byte{8}); err == nil {
		t.Fatal("a bind that is not a reflector answered")
	}
}

func TestPunchEndpointSendsOnlyHandshakesToEveryAddress(t *testing.T) {
	b := newBind(t, Options{ListenUDP: freePort(t, "udp")})
	openBind(t, b)
	id := PeerIdentity{ID: "peer", DataKey: [32]byte{1}, ControlKey: [32]byte{2}}
	if err := b.RegisterPeer(id); err != nil {
		t.Fatal(err)
	}
	var sinks []*net.UDPConn
	var addrs []netip.AddrPort
	for i := 0; i < 3; i++ {
		c, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
		if err != nil {
			t.Fatal(err)
		}
		defer c.Close()
		sinks = append(sinks, c)
		addrs = append(addrs, c.LocalAddr().(*net.UDPAddr).AddrPort())
	}
	ep, err := b.PunchEndpoint("peer", addrs, time.Now().Add(time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	initiation := make([]byte, 148)
	binary.LittleEndian.PutUint32(initiation, 1)
	if err := b.SendWithMetadata([][]byte{initiation}, id.DataKey, conn.SendMetadata{Endpoint: ep}); err != nil {
		t.Fatal(err)
	}
	for i, c := range sinks {
		got := replies(t, c, 200*time.Millisecond)
		if len(got) != 1 || len(got[0]) != 5+148 || string(got[0][:4]) != preamble || got[0][4] != 0 {
			t.Fatalf("address %d got %d packets", i, len(got))
		}
	}
	keepalive := make([]byte, 32)
	binary.LittleEndian.PutUint32(keepalive, 4)
	if err := b.SendWithMetadata([][]byte{keepalive}, id.DataKey, conn.SendMetadata{Endpoint: ep}); err != errPunchMessage {
		t.Fatalf("a transport packet to a punch endpoint: %v", err)
	}
	if err := b.SendWithMetadata([][]byte{initiation}, id.ControlKey, conn.SendMetadata{Endpoint: ep}); err != ErrSecurity {
		t.Fatalf("another key through the punch endpoint: %v", err)
	}
	// Over, it says so: WireGuard then retries through the peer's own endpoint.
	expired, _ := b.PunchEndpoint("peer", addrs, time.Now().Add(-time.Second))
	if err := b.SendWithMetadata([][]byte{initiation}, id.DataKey, conn.SendMetadata{Endpoint: expired}); err != errPunchOver {
		t.Fatalf("an initiation after the attempt: %v", err)
	}
	for i, c := range sinks {
		if got := replies(t, c, 200*time.Millisecond); len(got) != 0 {
			t.Fatalf("address %d got %d packets after the attempt ended", i, len(got))
		}
	}
}
