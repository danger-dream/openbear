package transport

import (
	"fmt"
	"testing"
	"time"

	"golang.zx2c4.com/wireguard/conn"
)

// Members take a new network MTU one at a time, and one that is offline or
// fails to apply keeps the old one. A full-size datagram from a peer on a
// larger MTU is received, not taken for a broken frame: on a stream that
// closed the whole connection, again with every such packet.
func TestFullSizeDatagramFromALargerMTU(t *testing.T) {
	for _, transport := range []string{"udp", "tcp"} {
		t.Run(transport, func(t *testing.T) {
			o := Options{MTU: 1280}
			port := freePort(t, transport)
			if transport == "udp" {
				o.ListenUDP = port
			} else {
				o.ListenTCP = port
			}
			server := newBind(t, o)
			receive := openBind(t, server)
			client := newBind(t, Options{MTU: 1420})
			openBind(t, client)
			key := fmt.Sprintf("%s://127.0.0.1:%d", transport, port)
			target, err := client.ParseEndpoint(key)
			if err != nil {
				t.Fatal(err)
			}
			var first *session
			if transport == "tcp" {
				first = pathReady(t, client, key).current
			}
			got := make(chan int, 4)
			go func() {
				packets, sizes, eps := [][]byte{make([]byte, 65536)}, []int{0}, []conn.Endpoint{nil}
				for {
					n, err := receive(packets, sizes, eps)
					if err != nil {
						return
					}
					if n == 1 {
						got <- sizes[0]
					}
				}
			}()
			full := packet4()
			full = append(full, make([]byte, maxDatagram(1420)-len(full))...)
			for _, size := range []int{len(full), 64} {
				if err := client.Send([][]byte{full[:size]}, target); err != nil {
					t.Fatalf("send %d: %v", size, err)
				}
				select {
				case n := <-got:
					if n != size {
						t.Fatalf("received %d bytes, sent %d", n, size)
					}
				case <-time.After(3 * time.Second):
					t.Fatalf("%d-byte datagram from a peer on MTU 1420 not received on MTU 1280", size)
				}
			}
			if transport == "tcp" {
				client.mu.Lock()
				same := client.paths[key].current == first && !first.closed.Load()
				client.mu.Unlock()
				if !same {
					t.Fatal("the connection was replaced")
				}
			}
		})
	}
}
