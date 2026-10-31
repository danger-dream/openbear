package transport

import (
	"io"
	"net"
	"strconv"
	"testing"
	"time"
)

// TestListenersTakeBothFamiliesAndRebindAtOnce: the TCP listener and the
// UDP socket take IPv4 and IPv6 on one port, and a restarted service binds
// the same port again straight away, though connections it accepted and
// closed first are in TIME_WAIT on this side.
func TestListenersTakeBothFamiliesAndRebindAtOnce(t *testing.T) {
	l, err := newListener("tcp", 0, "")
	if err != nil {
		t.Fatal(err)
	}
	port := l.Addr().(*net.TCPAddr).Port
	families := []string{"127.0.0.1"}
	if c, err := net.Listen("tcp6", "[::1]:0"); err == nil {
		c.Close()
		families = append(families, "::1")
	}
	for _, host := range families {
		c, err := net.DialTimeout("tcp", net.JoinHostPort(host, strconv.Itoa(port)), 2*time.Second)
		if err != nil {
			t.Fatalf("connecting over %s: %v", host, err)
		}
		s, err := l.Accept()
		if err != nil {
			t.Fatal(err)
		}
		// This side closes first, so its end of the connection waits in
		// TIME_WAIT.
		s.Write([]byte("x"))
		s.Close()
		c.SetReadDeadline(time.Now().Add(2 * time.Second))
		if b, _ := io.ReadAll(c); string(b) != "x" {
			t.Fatalf("over %s read %q", host, b)
		}
		c.Close()
	}
	l.Close()
	if l, err = newListener("tcp", port, ""); err != nil {
		t.Fatalf("rebinding TCP %d after a restart: %v", port, err)
	}
	l.Close()

	u, err := openUDP(0)
	if err != nil {
		t.Fatal(err)
	}
	port = u.LocalAddr().(*net.UDPAddr).Port
	for _, host := range families {
		c, err := net.Dial("udp", net.JoinHostPort(host, strconv.Itoa(port)))
		if err != nil {
			t.Fatal(err)
		}
		c.Write([]byte(host))
		c.Close()
		u.SetReadDeadline(time.Now().Add(2 * time.Second))
		b := make([]byte, 64)
		n, _, err := u.ReadFromUDP(b)
		if err != nil || string(b[:n]) != host {
			t.Fatalf("UDP over %s: %q, %v", host, b[:n], err)
		}
	}
	u.Close()
	if u, err = openUDP(port); err != nil {
		t.Fatalf("rebinding UDP %d after a restart: %v", port, err)
	}
	u.Close()
}
