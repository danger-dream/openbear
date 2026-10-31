package transport

import (
	"net"
	"net/netip"
	"testing"
	"time"
)

// Connections waiting to authenticate are counted by source; with every
// slot or the socket budget taken, a newcomer takes the oldest of the
// source holding the most, and a source cannot take one back from another
// holding as many.
func TestUnauthenticatedSlotsShareBySource(t *testing.T) {
	server := newBind(t, Options{ListenTCP: freePort(t, "tcp")})
	openBind(t, server)
	address := net.JoinHostPort("127.0.0.1", portString(server.opt.ListenTCP))
	dial := func(from string) net.Conn {
		t.Helper()
		d := net.Dialer{LocalAddr: &net.TCPAddr{IP: net.ParseIP(from)}}
		c, err := d.Dial("tcp", address)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { c.Close() })
		return c
	}
	waiting := func() map[string]int {
		server.mu.Lock()
		defer server.mu.Unlock()
		out := map[string]int{}
		for ss := range server.run.sessions {
			if !ss.authenticated && !ss.evicted {
				out[ss.ep.remote.Addr().String()]++
			}
		}
		return out
	}
	total := func() int {
		n := 0
		for _, k := range waiting() {
			n += k
		}
		return n
	}
	// open: c is not closed by the other end; what it sent is skipped.
	open := func(c net.Conn) bool {
		c.SetReadDeadline(time.Now().Add(300 * time.Millisecond))
		var buf [64]byte
		for {
			if _, err := c.Read(buf[:]); err != nil {
				e, ok := err.(net.Error)
				return ok && e.Timeout()
			}
		}
	}
	// Two sources take turns until no more get in.
	var first []net.Conn
	for i := 0; ; i++ {
		before := total()
		c := dial([]string{"127.0.0.2", "127.0.0.3"}[i%2])
		time.Sleep(50 * time.Millisecond)
		if total() == before {
			break
		}
		first = append(first, c)
	}
	slots := len(first)
	if slots < 2 || slots%2 != 0 {
		t.Fatalf("%d slots", slots)
	}

	newcomer := dial("127.0.0.4")
	waitUntil(t, time.Second, "the newcomer let in", func() bool { return waiting()["127.0.0.4"] == 1 })
	if open(first[0]) {
		t.Fatal("the oldest connection of a full source was kept")
	}
	for _, c := range first[1:] {
		if !open(c) {
			t.Fatal("another connection was closed")
		}
	}
	if !open(newcomer) {
		t.Fatal("the newcomer was closed")
	}
	// 127.0.0.2 now holds one less than 127.0.0.3: it does not get it back.
	again := dial("127.0.0.2")
	if open(again) {
		t.Fatal("a source took a slot back from one holding as many")
	}
	if w := waiting(); w["127.0.0.2"] != slots/2-1 || w["127.0.0.3"] != slots/2 {
		t.Fatalf("waiting %v", w)
	}
}

func TestUnauthenticatedSourceIsAnIPv6Slash64(t *testing.T) {
	for _, c := range []struct{ a, b string }{{"2001:db8:1:2::1", "2001:db8:1:2:ffff::9"}, {"192.0.2.1", "192.0.2.1"}} {
		if sourceOf(netip.MustParseAddr(c.a)) != sourceOf(netip.MustParseAddr(c.b)) {
			t.Fatalf("%s and %s counted apart", c.a, c.b)
		}
	}
	for _, c := range []struct{ a, b string }{{"2001:db8:1:2::1", "2001:db8:1:3::1"}, {"192.0.2.1", "192.0.2.2"}} {
		if sourceOf(netip.MustParseAddr(c.a)) == sourceOf(netip.MustParseAddr(c.b)) {
			t.Fatalf("%s and %s counted together", c.a, c.b)
		}
	}
}
