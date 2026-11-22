package routing

import (
	"strings"
	"testing"
	"time"
)

// TestTransitMoveWaitsForAdvertisements is the loop of 11-21 (replay,
// Aws-SG and OVH-SG to home): D is reachable only through C, which does not
// transit, until the link J-D comes back. Then O, which hears of it first,
// moves off C to Q; A, which hears O's new route before P's, moves off C to
// O; and O, hearing P's new route (through J) before A's move, takes A's
// path through P as cheaper. A and O point at each other until their
// advertisements cross. A move off no-transit relays while the route still
// works waits half a second, by when A has heard that P moved off C as
// well: no loop forms.
func TestTransitMoveWaitsForAdvertisements(t *testing.T) {
	ms := func(v float64) time.Duration { return time.Duration(v * float64(time.Millisecond)) }
	rtt := map[string]float64{"A-P": 38, "A-O": 4, "O-Q": 70, "Q-J": 70, "J-D": 4, "P-C": 30, "C-D": 46, "P-J": 36, "O-P": 300}
	delays := map[string]time.Duration{"A-P": ms(200), "A-O": ms(80), "O-Q": ms(35), "Q-J": ms(35), "J-D": ms(2), "P-C": ms(15), "C-D": ms(23), "P-J": ms(80), "O-P": ms(110)}
	n := newNet(t, delays, nil)
	for _, name := range n.names {
		n.r[name].Configure(n.config(name, func(c *Config) { c.NoTransit = []string{"C"} }))
	}
	down := map[string]bool{"J-D": true}
	n.cut("J", "D", true)
	settleNet(n, rtt, down, 60)
	wantPath(t, n, "A", "D", "A", "P", "C", "D")
	wantPath(t, n, "O", "D", "O", "A", "P", "C", "D")

	delete(down, "J-D")
	n.cut("J", "D", false)
	var loops []string
	for range 30 {
		for l, v := range rtt {
			a, b, _ := strings.Cut(l, "-")
			n.probe(a, b, v)
		}
		n.run(time.Second, func() {
			for _, src := range n.names {
				if src == "D" {
					continue
				}
				if p, ok := n.path(src, "D"); !ok && p[len(p)-1] != "" {
					loops = append(loops, n.now.Format("05.000")+" "+strings.Join(p, ">"))
				}
			}
		})
	}
	if len(loops) > 0 {
		t.Fatalf("routes to D looped: %v", loops)
	}
	wantPath(t, n, "A", "D", "A", "P", "J", "D")
	wantPath(t, n, "O", "D", "O", "A", "P", "J", "D")
}

// TestTransitMoveWaitsHalfASecondUnlessBroken: S reaches D through C, which
// does not transit, while the way through G is down. Once G's way is up S
// moves to it half a second after it first could, and not before; when the
// link to C breaks during that half second, S moves at once.
func TestTransitMoveWaitsHalfASecondUnlessBroken(t *testing.T) {
	rtt := map[string]float64{"S-C": 5, "C-D": 5, "S-G": 50, "G-D": 50}
	setup := func() *testNet {
		delays := map[string]time.Duration{}
		for l, v := range rtt {
			delays[l] = time.Duration(v / 2 * float64(time.Millisecond))
		}
		n := newNet(t, delays, nil)
		for _, name := range n.names {
			n.r[name].Configure(n.config(name, func(c *Config) { c.NoTransit = []string{"C"} }))
		}
		n.cut("G", "D", true)
		settleNet(n, rtt, map[string]bool{"G-D": true}, 40)
		wantPath(t, n, "S", "D", "S", "C", "D")
		n.cut("G", "D", false)
		return n
	}
	waiting := func(n *testNet) bool {
		for _, rt := range n.r["S"].Status().Routes {
			if rt.Dest == "D" {
				return rt.NextHop == "C" && rt.Best == "G" && rt.Held == HeldSettling
			}
		}
		return false
	}
	// probe every link once a second, steps of 10 ms in between, until stop says so.
	until := func(n *testNet, down map[string]bool, stop func() bool) bool {
		for sec := 0; sec < 20; sec++ {
			for l, v := range rtt {
				a, b, _ := strings.Cut(l, "-")
				if down[l] {
					v = -1
				}
				n.probe(a, b, v)
			}
			for range 100 {
				n.run(10*time.Millisecond, nil)
				if stop() {
					return true
				}
			}
		}
		return false
	}

	n := setup()
	if !until(n, nil, func() bool { return waiting(n) }) {
		t.Fatal("S never held a move off C")
	}
	from := n.now
	if !until(n, nil, func() bool { return n.hop("S", "D") == "G" }) {
		t.Fatal("S never moved off C")
	}
	if waited := n.now.Sub(from); waited < transitWait || waited > transitWait+20*time.Millisecond {
		t.Fatalf("S moved off C after %v, want %v", waited, transitWait)
	}

	n = setup()
	if !until(n, nil, func() bool { return waiting(n) }) {
		t.Fatal("S never held a move off C")
	}
	from = n.now
	for range downAfter {
		n.probe("S", "C", -1)
	}
	n.run(10*time.Millisecond, nil)
	if n.hop("S", "D") != "G" || n.now.Sub(from) >= transitWait {
		t.Fatalf("S on %s %v after its link to C broke, want G at once", n.hop("S", "D"), n.now.Sub(from))
	}
	if ev := n.eventsOf("S", "route.changed"); !strings.Contains(strings.Join(ev, "; "), "D: C -> G (down)") {
		t.Fatalf("S's move: %v", ev)
	}
}
