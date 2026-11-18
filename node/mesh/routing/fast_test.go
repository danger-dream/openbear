package routing

import (
	"slices"
	"testing"
	"time"
)

func TestFastPathsRouteFirstThenRunnersUpSkippingLegacy(t *testing.T) {
	// Topology:
	//   A --10ms--> B --10ms--> D
	//   A --35ms--> C --35ms--> D
	//   A --5ms---> L --5ms---> D   (the route; L may be an older release)
	rtt := map[string]float64{
		"A-B": 10, "B-D": 10,
		"A-C": 35, "C-D": 35,
		"A-L": 5, "L-D": 5,
	}
	delays := map[string]time.Duration{}
	for l, v := range rtt {
		delays[l] = time.Duration(v / 2 * float64(time.Millisecond))
	}
	n := newNet(t, delays, nil)
	settleNet(n, rtt, nil, 30)
	self := map[string]bool{"A": true}

	got := n.r["A"].FastPaths("D", 3, nil, self)
	if want := [][]string{{"L", "D"}, {"B", "D"}, {"C", "D"}}; !slices.EqualFunc(got, want, slices.Equal) {
		t.Fatalf("FastPaths = %v, want %v", got, want)
	}
	if got := n.r["A"].FastPaths("D", 2, nil, self); len(got) != 2 {
		t.Fatalf("FastPaths with k=2 = %v", got)
	}

	legacy := func(id string) bool { return id != "L" }
	got = n.r["A"].FastPaths("D", 3, legacy, self)
	if want := [][]string{{"B", "D"}, {"C", "D"}}; !slices.EqualFunc(got, want, slices.Equal) {
		t.Fatalf("FastPaths around a legacy relay = %v, want %v", got, want)
	}

	got = n.r["A"].FastPaths("D", 3, legacy, map[string]bool{"A": true, "B": true})
	if want := [][]string{{"C", "D"}}; !slices.EqualFunc(got, want, slices.Equal) {
		t.Fatalf("FastPaths avoiding B = %v, want %v", got, want)
	}
}
