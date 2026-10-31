package routing

import (
	"fmt"
	"strings"
	"testing"
	"time"
)

func TestLiveDestinationConvergence(t *testing.T) {
	for _, delay := range []time.Duration{500 * time.Microsecond, 20 * time.Millisecond, 50 * time.Millisecond} {
		for _, mode := range []string{"simultaneous", "staggered", "direct advertisements lost"} {
			t.Run(fmt.Sprintf("%s/%s", delay, mode), func(t *testing.T) {
				rtt := map[string]float64{"A-B": 1, "A-D": 10, "B-D": 10, "A-C": 50, "B-C": 50, "C-D": 50}
				delays := map[string]time.Duration{}
				for l, v := range rtt {
					delays[l] = time.Duration(v / 2 * float64(time.Millisecond))
				}
				delays["A-B"] = delay
				n := newNet(t, delays, nil)
				for range 40 {
					for l, v := range rtt {
						a, b, _ := strings.Cut(l, "-")
						n.probe(a, b, v)
					}
					n.run(time.Second, nil)
				}
				if n.hop("A", "D") != "D" || n.hop("B", "D") != "D" {
					t.Fatal("not converged")
				}
				n.cut("A", "D", true)
				n.cut("B", "D", true)
				n.r["A"].Gone("D")
				skew := time.Duration(0)
				if mode == "staggered" {
					skew = delay + 20*time.Millisecond
				}
				if mode == "direct advertisements lost" {
					n.cut("A", "B", true)
				}
				start := n.now
				detected := false
				loops := 0
				lastLoop := time.Duration(0)
				for range 100 {
					elapsed := n.now.Sub(start)
					if !detected && elapsed >= skew {
						detected = true
						n.r["B"].Gone("D")
					}
					n.now = n.now.Add(10 * time.Millisecond)
					n.step()
					if _, ok := n.path("A", "D"); !ok {
						loops++
						lastLoop = n.now.Sub(start)
					}
				}
				bound := delay + 10*time.Millisecond
				if mode == "direct advertisements lost" {
					bound = 60 * time.Millisecond
				}
				if mode == "staggered" && loops != 0 {
					t.Fatalf("staggered failures looped: %d", loops)
				}
				if mode != "staggered" && (loops == 0 || lastLoop > bound) {
					t.Fatalf("fixture loop duration %v, bound %v (steps %d)", lastLoop, bound, loops)
				}
				if n.hop("B", "D") != "C" || (mode != "staggered" && n.hop("A", "D") != "C") {
					t.Fatalf("remaining path unused: A=%s B=%s", n.hop("A", "D"), n.hop("B", "D"))
				}
				if mode != "staggered" && !strings.Contains(strings.Join(n.eventsOf("A", "route.changed"), " ")+strings.Join(n.eventsOf("B", "route.changed"), " "), "(loop)") {
					t.Fatal("no loop recovery event")
				}
			})
		}
	}
}
