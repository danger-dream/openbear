package transport

import (
	"net"
	"testing"

	"golang.org/x/sys/unix"
)

func TestLinuxSocketAutotuning(t *testing.T) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	accepted := make(chan net.Conn, 1)
	go func() {
		c, err := l.Accept()
		if err == nil {
			accepted <- c
		}
	}()
	c, err := net.Dial("tcp", l.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	other := <-accepted
	defer other.Close()
	raw, err := c.(*net.TCPConn).SyscallConn()
	if err != nil {
		t.Fatal(err)
	}
	before, err := configureSocket(raw, false)
	if err != nil {
		t.Fatal(err)
	}
	cost, err := configureTCP(c.(*net.TCPConn))
	if err != nil {
		t.Fatal(err)
	}
	var low, snd, rcv int
	var inner error
	err = raw.Control(func(fd uintptr) {
		low, inner = unix.GetsockoptInt(int(fd), unix.IPPROTO_TCP, unix.TCP_NOTSENT_LOWAT)
		if inner != nil {
			return
		}
		snd, inner = unix.GetsockoptInt(int(fd), unix.SOL_SOCKET, unix.SO_SNDBUF)
		if inner != nil {
			return
		}
		rcv, inner = unix.GetsockoptInt(int(fd), unix.SOL_SOCKET, unix.SO_RCVBUF)
	})
	if err != nil || inner != nil {
		t.Fatalf("getsockopt %v %v", err, inner)
	}
	if low != 16<<10 || cost != snd+rcv || cost != before || snd <= 0 || rcv <= 0 {
		t.Fatalf("low=%d snd=%d rcv=%d cost=%d before=%d", low, snd, rcv, cost, before)
	}
}

// The data socket sends IPv4 without DF; TestPathMTU (mesh, OBLAB_MTU=1)
// shows full-size packets crossing a smaller link that way.
func TestUDPSocketLetsRoutersFragment(t *testing.T) {
	u, err := openUDP(0)
	if err != nil {
		t.Fatal(err)
	}
	defer u.Close()
	raw, err := u.SyscallConn()
	if err != nil {
		t.Fatal(err)
	}
	var mode int
	var inner error
	if err = raw.Control(func(fd uintptr) { mode, inner = unix.GetsockoptInt(int(fd), unix.IPPROTO_IP, unix.IP_MTU_DISCOVER) }); err != nil || inner != nil {
		t.Fatalf("getsockopt %v %v", err, inner)
	}
	if mode != unix.IP_PMTUDISC_DONT {
		t.Fatalf("IP_MTU_DISCOVER = %d, want IP_PMTUDISC_DONT", mode)
	}
}
