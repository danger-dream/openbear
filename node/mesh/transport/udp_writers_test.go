package transport

import (
	"errors"
	"net"
	"net/netip"
	"sync/atomic"
	"testing"
	"time"
)

// TestUDPControlBesideBusyData: a UDP path took one datagram write at a
// time and dropped any other, so a probe written while WireGuard sent a
// data batch was lost (397 of 400 beside one busy data sender). Writes now
// go out side by side, bounded per plane.
func TestUDPControlBesideBusyData(t *testing.T) {
	sink, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer sink.Close()
	go func() {
		buf := make([]byte, 2048)
		for {
			if _, _, err := sink.ReadFromUDP(buf); err != nil {
				return
			}
		}
	}()
	b := newBind(t, Options{})
	openBind(t, b)
	ep, err := b.ParseEndpoint("udp://" + sink.LocalAddr().String())
	if err != nil {
		t.Fatal(err)
	}
	stop := make(chan struct{})
	done := make(chan struct{})
	var batches, failed atomic.Int64
	go func() {
		defer close(done)
		batch := make([][]byte, 128)
		for i := range batch {
			batch[i] = make([]byte, 1400)
		}
		for {
			select {
			case <-stop:
				return
			default:
			}
			if err := b.Send(batch, ep); err != nil {
				failed.Add(1)
			}
			batches.Add(1)
		}
	}()
	drops := 0
	for i := 0; i < 200; i++ {
		if err := b.ControlBind().Send([][]byte{make([]byte, 200)}, ep); errors.Is(err, ErrBackpressure) {
			drops++
		}
		time.Sleep(2 * time.Millisecond)
	}
	close(stop)
	<-done
	if drops != 0 || failed.Load() != 0 || batches.Load() == 0 {
		t.Fatalf("control drops %d of 200, data batches %d with %d failed", drops, batches.Load(), failed.Load())
	}

	// The bound: a plane with all its writers busy drops, the other not.
	e := ep.(*endpoint)
	b.mu.Lock()
	e.udpWriters[0] = udpWriters
	b.mu.Unlock()
	if err := b.Send([][]byte{make([]byte, 100)}, ep); !errors.Is(err, ErrBackpressure) {
		t.Fatalf("data beyond its writers: %v", err)
	}
	if err := b.ControlBind().Send([][]byte{make([]byte, 100)}, ep); err != nil {
		t.Fatalf("control beside busy data writers: %v", err)
	}
	b.mu.Lock()
	e.udpWriters[0] = 0
	buffered := e.stats.TxBufferedBytes
	b.mu.Unlock()
	if buffered != 0 {
		t.Fatalf("TxBufferedBytes %d with nothing being written", buffered)
	}
}

// TestUDPIPv4OnlySocketTakesMappedAddresses: a host without IPv6 gets an
// IPv4-only socket, which refused the IPv4-mapped form a relay's address
// comes in ("non-IPv4 address"), so nothing went out over UDP.
func TestUDPIPv4OnlySocketTakesMappedAddresses(t *testing.T) {
	sink, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer sink.Close()
	c, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	u := &udpSocket{UDPConn: c, done: make(chan struct{})}
	defer u.Close()
	to := sink.LocalAddr().(*net.UDPAddr).AddrPort()
	mapped := netip.AddrPortFrom(netip.AddrFrom16(to.Addr().As16()), to.Port())
	if !mapped.Addr().Is4In6() {
		t.Fatal("not mapped", mapped)
	}
	if _, err := u.WriteToUDPAddrPort([]byte("x"), mapped); err != nil {
		t.Fatal(err)
	}
	sink.SetReadDeadline(time.Now().Add(time.Second))
	buf := make([]byte, 8)
	if n, _, err := sink.ReadFromUDPAddrPort(buf); err != nil || string(buf[:n]) != "x" {
		t.Fatalf("got %q, %v", buf[:n], err)
	}
}
