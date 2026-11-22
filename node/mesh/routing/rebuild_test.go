package routing

import (
	"maps"
	"math/rand"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"
)

// fresh fails unless r's graph is the one build makes from what r holds now
// and every chain it remembers is the one walked again.
func fresh(t *testing.T, name string, r *Router) {
	t.Helper()
	b := r.build()
	for _, n := range b.nodes {
		for _, d := range b.nodes {
			cost, ok, loops := r.graph.chain(n, d)
			wantCost, wantOK, wantLoops := b.walk(n, d)
			if cost != wantCost || ok != wantOK || loops != wantLoops {
				t.Fatalf("%s: chain %s to %s remembered as %v %v %v, walks %v %v %v", name, n, d, cost, ok, loops, wantCost, wantOK, wantLoops)
			}
			if got, want := r.graph.relays(n, d), b.count(n, d); got != want {
				t.Fatalf("%s: %s to %s remembered through %d relays, counts %d", name, n, d, got, want)
			}
		}
	}
	g := *r.graph
	g.chains, g.epoch, b.chains, b.epoch = nil, 0, nil, 0
	if !reflect.DeepEqual(&g, b) {
		t.Fatalf("%s: graph out of date", name)
	}
}

// TestGraphBuiltOnlyOnChange: a tick, a probe that leaves the advertised
// cost as it is and an advertisement listing the same links keep the
// graph; a link listed otherwise, advertisements expiring and a link going
// down build it again.
func TestGraphBuiltOnlyOnChange(t *testing.T) {
	s := diamond(t)
	g := s.r.graph
	s.r.Tick()
	s.r.Probe("B", 10*time.Millisecond)
	if _, err := s.advert("B", "B", map[string]float64{"A": 10, "D": 10}, map[string]string{"C": "A", "D": "D"}); err != nil {
		t.Fatal(err)
	}
	if s.r.graph != g {
		t.Fatal("built again with nothing changed")
	}
	fresh(t, "A", s.r)

	if _, err := s.advert("B", "D", map[string]float64{"B": 10, "C": 5}, map[string]string{"B": "B", "C": "C"}); err != nil {
		t.Fatal(err)
	}
	if s.r.graph == g {
		t.Fatal("kept after D listed C-D cheaper")
	}
	fresh(t, "A", s.r)

	g = s.r.graph
	s.now = s.now.Add(advertExpiry + time.Second)
	s.r.Tick()
	if s.r.graph == g {
		t.Fatal("kept after the advertisements expired")
	}
	fresh(t, "A", s.r)

	g = s.r.graph
	for range 10 {
		s.r.Lost("C")
	}
	if s.r.graph == g {
		t.Fatal("kept after the link to C went down")
	}
	fresh(t, "A", s.r)
}

// TestGraphFreshUnderChurn: whatever costs, losses and cut links bring,
// every relay's graph is the one built from scratch after every step.
func TestGraphFreshUnderChurn(t *testing.T) {
	links := map[string]time.Duration{"A-B": 20 * time.Millisecond, "B-C": 5 * time.Millisecond, "C-D": 30 * time.Millisecond,
		"A-D": 10 * time.Millisecond, "B-D": 40 * time.Millisecond, "D-E": 15 * time.Millisecond, "C-E": 25 * time.Millisecond}
	n := newNet(t, links, nil)
	names := slices.Sorted(maps.Keys(links))
	rng := rand.New(rand.NewSource(7))
	rtt, down := map[string]float64{}, map[string]bool{}
	last := map[string]*graph{}
	builds, steps := 0, 0
	check := func() {
		steps++
		for _, name := range n.names {
			r := n.r[name]
			if r.graph != last[name] {
				builds++
				last[name] = r.graph
			}
			fresh(t, name, r)
		}
	}
	for sec := range 300 {
		if sec%20 == 0 {
			for _, l := range names {
				rtt[l] = 5 + rng.Float64()*150
				down[l] = sec > 0 && rng.Intn(7) == 0
			}
		}
		for _, l := range names {
			a, b, _ := strings.Cut(l, "-")
			n.cut(a, b, down[l])
			if down[l] || rng.Intn(20) == 0 {
				n.probe(a, b, -1)
			} else {
				n.probe(a, b, rtt[l]*(0.9+rng.Float64()*0.2))
			}
		}
		n.run(time.Second, check)
	}
	t.Logf("%d graphs built in %d relay-steps", builds, steps*len(n.names))
}
