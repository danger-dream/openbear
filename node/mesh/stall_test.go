package mesh

import (
	"encoding/binary"
	"fmt"
	"io"
	"net"
	"os"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// stallProxy relays TCP to target; while paused it stops reading what the
// target sends, as a member's line that takes nothing more does, and the
// sender's socket buffers fill.
type stallProxy struct {
	ln     net.Listener
	paused atomic.Bool
}

func newStallProxy(t *testing.T, target string) *stallProxy {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	p := &stallProxy{ln: ln}
	t.Cleanup(func() { ln.Close() })
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			u, err := net.Dial("tcp", target)
			if err != nil {
				c.Close()
				continue
			}
			go func() { io.Copy(u, c); u.Close() }()
			go func() {
				defer c.Close()
				buf := make([]byte, 32<<10)
				for {
					for p.paused.Load() {
						time.Sleep(10 * time.Millisecond)
					}
					n, err := u.Read(buf)
					if n > 0 {
						if _, werr := c.Write(buf[:n]); werr != nil {
							return
						}
					}
					if err != nil {
						return
					}
				}
			}()
		}
	}()
	return p
}

// TestStalledStreamPeer measures what a relay forwards to one member while
// another member's TCP path takes nothing: the relay's writes to it block
// until the send deadline. OBLAB_STALL=1 runs it.
func TestStalledStreamPeer(t *testing.T) {
	if os.Getenv("OBLAB_STALL") != "1" {
		t.Skip("set OBLAB_STALL=1")
	}
	any := []Rule{{Proto: "any"}}
	l := newLab(t, spec{relays: []string{"R"}, members: map[string][]string{"A": {"R"}, "B": {"R"}, "C": {"R"}},
		rules: map[string][]Rule{"B": any, "C": any}})
	tcp := freeTCP(t)
	proxy := newStallProxy(t, fmt.Sprintf("127.0.0.1:%d", tcp))
	l.edit = func(name string, c *Config) {
		switch name {
		case "R":
			c.Self.Listen.TCP = tcp
		case "B":
			c.Entry.Candidates[0].Endpoint = "tcp://" + proxy.ln.Addr().String()
		}
	}
	n := l.up()
	a, b, c := n["A"], n["B"], n["C"]
	A, B, C := l.addr("A"), l.addr("B"), l.addr("C")

	var mu sync.Mutex
	var arrivals []time.Time
	stop := make(chan struct{})
	var wg sync.WaitGroup
	wg.Add(3)
	go func() {
		defer wg.Done()
		for {
			select {
			case <-c.ct.Inbound:
				mu.Lock()
				arrivals = append(arrivals, time.Now())
				mu.Unlock()
			case <-stop:
				return
			}
		}
	}()
	var toB atomic.Uint64
	go func() {
		defer wg.Done()
		for {
			select {
			case <-b.ct.Inbound:
				toB.Add(1)
			case <-stop:
				return
			}
		}
	}()
	// A sends C a small packet every 2 ms and B a flood of large ones.
	go func() {
		defer wg.Done()
		tick := time.NewTicker(time.Millisecond)
		defer tick.Stop()
		var seq uint16
		for i := 0; ; i++ {
			select {
			case <-stop:
				return
			case <-tick.C:
			}
			if i%2 == 0 {
				seq++
				p := ports(40000, 9000, 64)
				binary.BigEndian.PutUint16(p[8:], seq)
				send(a, packet(A, C, protoUDP, 64, seq, 0, false, p))
			}
			for j := 0; j < 8; j++ {
				send(a, packet(A, B, protoUDP, 64, uint16(i), 0, false, ports(40001, 9001, 1200)))
			}
		}
	}()
	window := func(d time.Duration) (count int, gap time.Duration) {
		mu.Lock()
		start := len(arrivals)
		mu.Unlock()
		from := time.Now()
		time.Sleep(d)
		mu.Lock()
		got := arrivals[start:]
		mu.Unlock()
		last := from
		for _, at := range got {
			gap = max(gap, at.Sub(last))
			last = at
		}
		gap = max(gap, time.Since(last))
		return len(got), gap
	}
	baseCount, baseGap := window(3 * time.Second)
	proxy.paused.Store(true)
	stallCount, stallGap := window(12 * time.Second)
	proxy.paused.Store(false)
	close(stop)
	wg.Wait()
	t.Logf("A to C, one packet every 2 ms: %d in 3 s with the longest gap %v while B's path took everything; %d in 12 s with the longest gap %v while it took nothing (B got %d)",
		baseCount, baseGap.Round(time.Millisecond), stallCount, stallGap.Round(time.Millisecond), toB.Load())
	if stallGap > time.Second {
		t.Errorf("C waited %v for the relay while B's path was stalled", stallGap.Round(time.Millisecond))
	}
}
