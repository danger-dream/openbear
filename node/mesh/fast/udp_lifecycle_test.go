package fast

import (
	"bytes"
	"io"
	"net"
	"net/netip"
	"testing"
	"time"
)

func TestReviewUDPExitDrainsHalfCloseAndBoundsTerminatedWriter(t *testing.T) {
	oldDial, oldIdle := dialAddrUDP, udpIdleDefault
	defer func() { dialAddrUDP, udpIdleDefault = oldDial, oldIdle }()
	udpIdleDefault = 250 * time.Millisecond
	for _, blocked := range []bool{false, true} {
		name := "half-close reply"
		if blocked {
			name = "terminated producer blocked writer"
		}
		t.Run(name, func(t *testing.T) {
			local, backend := net.Pipe()
			defer local.Close()
			defer backend.Close()
			dialAddrUDP = func(netip.AddrPort) (net.Conn, error) { return local, nil }
			down, client := clientPair(t)
			defer down.Close()
			defer client.Close()
			if blocked {
				down.SetWriteBuffer(1024)
				client.SetReadBuffer(1024)
			}
			var s Service
			self := netip.MustParseAddr("100.96.0.2")
			done := make(chan struct{})
			go func() { defer close(done); s.serveExitUDP(&runtimeState{selfAddr: self}, down, header{port: 443}) }()
			target := netip.AddrPortFrom(self, 443)
			produced := make(chan struct{})
			go func() {
				defer close(produced)
				request := make([]byte, 4)
				if _, e := io.ReadFull(backend, request); e != nil {
					return
				}
				// The client's FIN must not throw away a response arriving later.
				if !blocked {
					time.Sleep(30 * time.Millisecond)
					backend.Write([]byte("tail"))
					return
				}
				payload := bytes.Repeat([]byte("x"), 60000)
				for i := 0; i < 256; i++ {
					if _, e := backend.Write(payload); e != nil {
						return
					}
				}
			}()
			client.SetDeadline(time.Now().Add(3 * time.Second))
			if err := writeUDPFrame(client, target, []byte("ping")); err != nil {
				t.Fatal(err)
			}
			client.CloseWrite()
			if !blocked {
				_, payload, err := readUDPFrame(client, make([]byte, 65536))
				if err != nil || string(payload) != "tail" {
					t.Fatalf("lost delayed half-close reply: %q %v", payload, err)
				}
			}
			select {
			case <-produced:
			case <-time.After(2 * time.Second):
				t.Fatal("UDP producer blocked behind TCP writer")
			}
			select {
			case <-done:
			case <-time.After(2 * time.Second):
				t.Fatal("ended UDP session retained blocked TCP writer")
			}
		})
	}
}
