package transport

import (
	"errors"
	"net"
	"testing"

	"golang.org/x/sys/windows"
)

// TestListenersRefuseAPortHeldForIPv4: another program with a plain IPv4
// socket on 0.0.0.0 (a .NET TcpListener, say) holds the port; a dual-stack
// bind alone would succeed beside it and lose all IPv4 to it.
func TestListenersRefuseAPortHeldForIPv4(t *testing.T) {
	other, err := net.Listen("tcp4", "0.0.0.0:0")
	if err != nil {
		t.Fatal(err)
	}
	defer other.Close()
	port := other.Addr().(*net.TCPAddr).Port
	if l, err := newListener("tcp", port, ""); err == nil {
		l.Close()
		t.Fatalf("TCP %d bound beside an IPv4 listener", port)
	} else if !errors.Is(err, windows.WSAEADDRINUSE) {
		t.Fatalf("TCP %d: %v, want address in use", port, err)
	}

	otherUDP, err := net.ListenPacket("udp4", "0.0.0.0:0")
	if err != nil {
		t.Fatal(err)
	}
	defer otherUDP.Close()
	port = otherUDP.LocalAddr().(*net.UDPAddr).Port
	if u, err := openUDP(port); err == nil {
		u.Close()
		t.Fatalf("UDP %d bound beside an IPv4 socket", port)
	} else if !errors.Is(err, windows.WSAEADDRINUSE) {
		t.Fatalf("UDP %d: %v, want address in use", port, err)
	}

	// Held dual-stack the bind itself refuses, as before.
	dual, err := net.Listen("tcp", ":0")
	if err != nil {
		t.Fatal(err)
	}
	defer dual.Close()
	if l, err := newListener("tcp", dual.Addr().(*net.TCPAddr).Port, ""); err == nil {
		l.Close()
		t.Fatal("TCP bound beside a dual-stack listener")
	}
}
