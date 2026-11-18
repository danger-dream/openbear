package routing

import (
	"slices"
	"strings"
	"testing"
	"time"
)

// settleNet probes every link once a second for rounds seconds, a link in
// down losing its probes.
func settleNet(n *testNet, rtt map[string]float64, down map[string]bool, rounds int) {
	for range rounds {
		for l, v := range rtt {
			a, b, _ := strings.Cut(l, "-")
			if down[l] {
				v = -1
			}
			n.probe(a, b, v)
		}
		n.run(time.Second, nil)
	}
}

func wantPath(t *testing.T, n *testNet, src, dst string, want ...string) {
	t.Helper()
	got, ok := n.path(src, dst)
	if !ok || !slices.Equal(got, want) {
		t.Fatalf("%s -> %s goes %v (ok %v), want %v", src, dst, got, ok, want)
	}
}

// TestNoTransitRelays: home (H) and office (C) are 2 ms apart and on the
// cheapest paths between the others; once they do not transit, the others
// go around them, they still reach each other directly, and a relay left
// with no other way out goes through one until its own link is back and has
// held for its probation (probationBase).
func TestNoTransitRelays(t *testing.T) {
	rtt := map[string]float64{"H-C": 2, "H-G": 60, "C-G": 28, "G-K": 7, "H-A": 18, "C-A": 15, "A-G": 45, "N-C": 3, "N-A": 20}
	delays := map[string]time.Duration{}
	for l, v := range rtt {
		delays[l] = time.Duration(v / 2 * float64(time.Millisecond))
	}
	n := newNet(t, delays, nil)
	down := map[string]bool{}
	settleNet(n, rtt, down, 40)
	wantPath(t, n, "H", "K", "H", "C", "G", "K")
	wantPath(t, n, "N", "K", "N", "C", "G", "K")

	for _, name := range n.names {
		n.r[name].Configure(n.config(name, func(c *Config) { c.NoTransit = []string{"C", "H"} }))
	}
	settleNet(n, rtt, down, 20)
	wantPath(t, n, "H", "K", "H", "G", "K")
	wantPath(t, n, "N", "K", "N", "A", "G", "K")
	wantPath(t, n, "K", "N", "K", "G", "A", "N")
	wantPath(t, n, "N", "H", "N", "A", "H")
	wantPath(t, n, "H", "N", "H", "A", "N")
	wantPath(t, n, "H", "C", "H", "C")
	wantPath(t, n, "N", "C", "N", "C")
	if ev := strings.Join(n.eventsOf("H", "route.changed"), "; "); !strings.Contains(ev, "K: C -> G (better)") {
		t.Fatalf("H's move off C: %s", ev)
	}

	down["N-A"] = true
	n.cut("N", "A", true)
	settleNet(n, rtt, down, 10)
	wantPath(t, n, "N", "K", "N", "C", "G", "K")
	wantPath(t, n, "K", "N", "K", "G", "C", "N")

	delete(down, "N-A")
	n.cut("N", "A", false)
	settleNet(n, rtt, down, 40)
	wantPath(t, n, "N", "K", "N", "A", "G", "K")
	wantPath(t, n, "K", "N", "K", "G", "A", "N")
}

// TestNoTransitCarriesItsMembers: member M enters by C, which does not
// transit, and by G. C still carries M's traffic both ways, while relay R,
// linked to C as cheaply, goes around it.
func TestNoTransitCarriesItsMembers(t *testing.T) {
	rtt := map[string]float64{"C-G": 20, "G-K": 7, "M-C": 0.5, "M-G": 60, "R-C": 1, "R-G": 60}
	delays := map[string]time.Duration{}
	for l, v := range rtt {
		delays[l] = time.Duration(v / 2 * float64(time.Millisecond))
	}
	n := newNet(t, delays, nil)
	memberLinks := [][2]string{{"C", "M"}, {"G", "M"}}
	for _, name := range n.names {
		name := name
		n.r[name] = New(n.config(name, func(c *Config) {
			c.Relays = slices.DeleteFunc(c.Relays, func(x string) bool { return x == "M" })
			c.Leaf = name == "M"
			if !c.Leaf {
				c.Leaves = []string{"M"}
			}
			c.MemberLinks = memberLinks
			c.NoTransit = []string{"C"}
		}), func() time.Time { return n.now }, func(kind, detail string) {
			n.events[name] = append(n.events[name], kind+" "+detail)
		})
	}
	settleNet(n, rtt, nil, 40)
	wantPath(t, n, "M", "K", "M", "C", "G", "K")
	wantPath(t, n, "K", "M", "K", "G", "C", "M")
	wantPath(t, n, "R", "K", "R", "G", "K")
	wantPath(t, n, "R", "C", "R", "C")
}
