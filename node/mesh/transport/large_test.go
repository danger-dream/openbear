package transport

import (
	"errors"
	"fmt"
	"testing"
	"time"

	"golang.zx2c4.com/wireguard/conn"

	"openbear/node/mesh/wire"
)

// A large frame goes out only on a TCP or TLS stream of wire.LargeFrames
// or later, to a peer that said it takes them, from a node that takes
// them; it is dropped before it is written otherwise, so an older node, a
// macOS or Windows one or a UDP path never gets one and the connection
// stays. One that goes out arrives whole on the receiver's large queue.
// LargeLinks asks for them only when this node sends them too.
func TestLargeFramesOnlyToPeersThatTakeThem(t *testing.T) {
	for _, c := range []struct {
		name               string
		transport          string
		server             wire.Range
		serverLarge        bool
		clientLarge, send  bool
		peerLarge, arrives bool
		asked              bool
	}{
		{"both take them", "tcp", wire.Local(), true, true, true, true, true, true},
		{"protocol 3 node", "tcp", wire.Range{Min: 2, Max: 3}, false, true, true, true, false, false},
		{"protocol 2 node", "tcp", wire.Range{Min: 2, Max: 2}, false, true, true, true, false, false},
		{"peer did not say", "tcp", wire.Local(), true, true, true, false, false, false},
		{"switched off here", "tcp", wire.Local(), true, true, false, true, true, false},
		{"no large pool here", "tcp", wire.Local(), true, false, true, true, false, false},
		{"udp", "udp", wire.Local(), true, true, true, true, false, false},
	} {
		t.Run(c.name, func(t *testing.T) {
			port := freePort(t, c.transport)
			o := Options{Protocol: c.server, LargeFrames: c.serverLarge}
			if c.transport == "udp" {
				o.ListenUDP = port
			} else {
				o.ListenTCP = port
			}
			server := newBind(t, o)
			fns, _, err := server.Open(0)
			if err != nil {
				t.Fatal(err)
			}
			client := newBind(t, Options{LargeFrames: c.clientLarge, SendLarge: c.send})
			openBind(t, client)
			key := fmt.Sprintf("%s://127.0.0.1:%d", c.transport, port)
			target, err := client.ParseEndpoint(key)
			if err != nil {
				t.Fatal(err)
			}
			var first *session
			if c.transport != "udp" {
				first = pathReady(t, client, key).current
			}
			client.mu.Lock()
			ep := client.paths[key]
			ep.peer = "z"
			client.peers["z"] = &peerPaths{active: PathRef{ep.id, ep.generation}}
			client.mu.Unlock()
			client.SetPeerLarge("z", c.peerLarge)
			if links := client.LargeLinks(); links["z"] != c.asked {
				t.Fatalf("large links %v", links)
			}
			got := make(chan [2]int, 4)
			for i, f := range fns {
				go func(i int, f conn.ReceiveFunc) {
					packets, sizes, eps := [][]byte{make([]byte, 65536)}, []int{0}, []conn.Endpoint{nil}
					for {
						n, err := f(packets, sizes, eps)
						if err != nil {
							return
						}
						if n == 1 {
							got <- [2]int{i, sizes[0]}
						}
					}
				}(i, f)
			}
			large := append(packet4(), make([]byte, largeLimit-64)...)
			client.mu.Lock()
			dropped := ep.stats.Dropped
			client.mu.Unlock()
			if err = client.Send([][]byte{large}, target); err != nil {
				t.Fatalf("large frame: %v", err)
			}
			client.mu.Lock()
			dropped = ep.stats.Dropped - dropped
			client.mu.Unlock()
			if c.arrives && (c.transport == "udp" || len(fns) != 3) {
				t.Fatal("no large queue")
			}
			if (dropped == 1) == c.arrives {
				t.Fatalf("large frame dropped %d", dropped)
			}
			if err := client.Send([][]byte{append(large, 0)}, target); !errors.Is(err, ErrFrame) {
				t.Fatalf("a frame over any limit: %v", err)
			}
			if err := client.Send([][]byte{packet4()}, target); err != nil {
				t.Fatal(err)
			}
			// By receive function: the large frame on the large queue's.
			want := [][2]int{{0, 64}}
			if c.arrives {
				want = append(want, [2]int{2, len(large)})
			}
			seen := map[[2]int]bool{}
			for range want {
				select {
				case n := <-got:
					seen[n] = true
				case <-time.After(3 * time.Second):
					t.Fatalf("received %v, want %v", seen, want)
				}
			}
			for _, n := range want {
				if !seen[n] {
					t.Fatalf("received %v, want %v", seen, want)
				}
			}
			if first != nil {
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

// A node takes large frames only on a stream of wire.LargeFrames or later
// and only with the large pool; LargeLinks are the peers a large frame
// would go to now.
func TestLargeReceiveLimit(t *testing.T) {
	for _, c := range []struct {
		large   bool
		version int
		want    int
	}{{true, wire.LargeFrames, largeLimit}, {true, 3, maxDatagram(1420)}, {false, wire.LargeFrames, maxDatagram(1420)}} {
		b := newBind(t, Options{MTU: 1400, LargeFrames: c.large})
		b.mu.Lock()
		got := b.streamReceiveLimit(&session{version: c.version})
		b.mu.Unlock()
		if got != c.want {
			t.Fatalf("large %v, version %d: limit %d, want %d", c.large, c.version, got, c.want)
		}
	}
	if largeLimit < maxDatagram(wire.LargeMTU) || largeLimit > largeMessageBuffer-16 {
		t.Fatalf("large frames of %d bytes, buffers of %d", largeLimit, largeMessageBuffer)
	}
}
