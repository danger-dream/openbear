package transport

import (
	"net"
	"net/netip"
	"testing"
	"time"
	"unsafe"

	"golang.org/x/sys/unix"
)

// A prime leaves with the TTL asked for, and WireGuard's datagrams on the
// same socket keep the default one.
func TestPrimeTTLIsPerDatagram(t *testing.T) {
	b := newBind(t, Options{ListenUDP: freePort(t, "udp")})
	openBind(t, b)
	sink, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer sink.Close()
	raw, _ := sink.SyscallConn()
	raw.Control(func(fd uintptr) { unix.SetsockoptInt(int(fd), unix.IPPROTO_IP, unix.IP_RECVTTL, 1) })
	to := sink.LocalAddr().(*net.UDPAddr).AddrPort()
	read := func() (int, []byte) {
		buf, oob := make([]byte, 2048), make([]byte, 128)
		sink.SetReadDeadline(time.Now().Add(time.Second))
		n, oobn, _, _, err := sink.ReadMsgUDPAddrPort(buf, oob)
		if err != nil {
			t.Fatal(err)
		}
		msgs, _ := unix.ParseSocketControlMessage(oob[:oobn])
		for _, m := range msgs {
			if m.Header.Level == unix.IPPROTO_IP && m.Header.Type == unix.IP_TTL && len(m.Data) >= 4 {
				return int(*(*int32)(unsafe.Pointer(&m.Data[0]))), buf[:n]
			}
		}
		t.Fatal("no TTL")
		return 0, nil
	}
	for _, ttl := range []int{4, 3, 6} {
		if err := b.Prime([]netip.AddrPort{to}, ttl); err != nil {
			t.Fatal(err)
		}
		got, p := read()
		if got != ttl || len(p) != reflectSize || p[4] != reflectPrime {
			t.Fatalf("prime with TTL %d arrived with %d: %x", ttl, got, p)
		}
	}
	b.mu.Lock()
	u := b.run.udp
	b.mu.Unlock()
	u.WriteToUDPAddrPort([]byte("plain"), to)
	if got, _ := read(); got <= 6 {
		t.Fatalf("a datagram after the primes left with TTL %d", got)
	}
}
