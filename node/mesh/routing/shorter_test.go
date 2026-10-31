package routing

import (
	"slices"
	"testing"
	"time"
)

// twoWays: A reaches D through B (A-B 10, B-D bd) until its own link to D
// comes up at ad. D's advertisement does not list A, so A-D costs what A
// measures.
func twoWays(t *testing.T, bd, ad float64) *solo {
	s := newSolo(t, []string{"B", "D"}, []string{"B", "D"}, nil)
	s.up("B", 10)
	s.advert("B", "B", map[string]float64{"A": 10, "D": bd}, map[string]string{"D": "D"})
	s.advert("B", "D", map[string]float64{"B": bd}, map[string]string{"B": "B"})
	if s.hop("D") != "B" {
		t.Fatalf("A to D through %s before its link", s.hop("D"))
	}
	s.up("D", ad)
	return s
}

// switchAfter ticks every 100 ms for up to d and says when A's next hop to
// dest became hop, -1 if it did not.
func (s *solo) switchAfter(dest, hop string, d time.Duration) time.Duration {
	start := s.now
	for s.now.Sub(start) < d {
		if s.hop(dest) == hop {
			return s.now.Sub(start)
		}
		s.now = s.now.Add(100 * time.Millisecond)
		s.r.Tick()
	}
	if s.hop(dest) == hop {
		return s.now.Sub(start)
	}
	return -1
}

// TestShorterPathRule: while the route in use works, a path through fewer
// relays is taken once it costs below 0.95 of it and at least 3 ms less,
// for 5 s; otherwise the 0.8 rule stands.
func TestShorterPathRule(t *testing.T) {
	cases := []struct {
		name   string
		bd, ad float64 // through B costs 10 + bd; direct ad
		after  time.Duration
	}{
		{"83.2 against 77.5", 73.2, 77.5, 5 * time.Second},
		{"just below 0.95", 73.2, 79.0, 5 * time.Second},   // 79.0 < 79.04, 4.2 ms
		{"not below 0.95", 73.2, 79.1, -1},                 // 79.1 > 79.04
		{"3 ms less", 40, 47.0, 5 * time.Second},           // 47 < 47.5, 3 ms
		{"less than 3 ms", 40, 47.4, -1},                   // 47.4 < 47.5, 2.6 ms
		{"0.8 rule, at once", 73.2, 60, 0},                 // 60 < 66.6, 23 ms
		{"more relays never by this rule", 73.2, 83.2, -1}, // equal
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			s := twoWays(t, c.bd, c.ad)
			got := s.switchAfter("D", "D", 20*time.Second)
			if got != c.after {
				t.Fatalf("switched after %v, want %v; %v", got, c.after, s.ev)
			}
			want := "route.changed D: B -> D (shorter)"
			if c.after == 0 {
				want = "route.changed D: B -> D (better)"
			}
			if c.after >= 0 && !slices.Contains(s.ev, want) {
				t.Fatal(s.ev)
			}
		})
	}
}

// TestShorterPathNeedsFiveSecondsOnEnd: the 5 s start again when the rule
// stops holding in between.
func TestShorterPathNeedsFiveSecondsOnEnd(t *testing.T) {
	s := twoWays(t, 73.2, 77.5)
	if s.switchAfter("D", "D", 3*time.Second) != -1 {
		t.Fatal("switched within 3 s")
	}
	// Through B gets cheaper for a second: 78 against 77.5.
	bd := func(c float64) {
		s.advert("B", "B", map[string]float64{"A": 10, "D": c}, map[string]string{"D": "D"})
		s.advert("B", "D", map[string]float64{"B": c}, map[string]string{"B": "B"})
	}
	bd(68)
	if s.switchAfter("D", "D", time.Second) != -1 {
		t.Fatal("switched while the rule did not hold")
	}
	bd(73.2)
	if got := s.switchAfter("D", "D", 10*time.Second); got != 5*time.Second {
		t.Fatalf("switched %v after the rule held again, want 5s", got)
	}
}

// TestShorterPathIsLoopFree: the shorter rule picks only a loop-free next
// hop, like every other rule.
func TestShorterPathIsLoopFree(t *testing.T) {
	// A-B 10, B-E 10, E-D 63.2: through B 83.2, two relays. A-C 10, C-D
	// 67.5: through C 77.5, one relay; but C routes to D through A.
	s := newSolo(t, []string{"B", "C", "D", "E"}, []string{"B", "C"}, nil)
	s.up("B", 10)
	s.advert("B", "B", map[string]float64{"A": 10, "E": 10}, map[string]string{"D": "E", "E": "E", "C": "A"})
	s.advert("B", "E", map[string]float64{"B": 10, "D": 63.2}, map[string]string{"D": "D", "B": "B"})
	s.advert("B", "D", map[string]float64{"E": 63.2}, map[string]string{"E": "E"})
	if s.hop("D") != "B" {
		t.Fatalf("A to D through %s", s.hop("D"))
	}
	s.up("C", 10)
	s.advert("C", "C", map[string]float64{"A": 10, "D": 67.5}, map[string]string{"D": "A", "B": "A"})
	s.advert("C", "D", map[string]float64{"E": 63.2, "C": 67.5}, map[string]string{"E": "E", "C": "C"})
	if s.switchAfter("D", "C", 10*time.Second) != -1 {
		t.Fatal("took C, whose path to D comes back here")
	}
	s.advert("C", "C", map[string]float64{"A": 10, "D": 67.5}, map[string]string{"D": "D", "B": "A"})
	if got := s.switchAfter("D", "C", 10*time.Second); got != 5*time.Second {
		t.Fatalf("switched to C %v after its path was loop-free", got)
	}
}

// TestShorterPathWaitsForPenalty: a first link that went down lately does
// not take the shorter rule until its penalty is below 750; the 0.8 rule
// and loop-freedom are as ever.
func TestShorterPathWaitsForPenalty(t *testing.T) {
	s := twoWays(t, 73.2, 77.5)
	if s.switchAfter("D", "D", 6*time.Second) != 5*time.Second {
		t.Fatal("no switch to D")
	}
	down := s.now
	for range 3 {
		s.r.Lost("D")
	}
	if s.hop("D") != "B" {
		t.Fatal("still on D, down")
	}
	// Up again at the next probe, with a penalty of 1000 that halves every
	// 30 s: below 750 12.45 s after the link went down. Ten good probes
	// clear its loss well before.
	var back time.Duration
	for i := 0; back == 0 && i < 300; i++ {
		if i%10 == 0 {
			s.r.Probe("D", 77500*time.Microsecond)
			s.r.Probe("B", 10*time.Millisecond)
		}
		s.now = s.now.Add(100 * time.Millisecond)
		s.r.Tick()
		if s.hop("D") == "D" {
			back = s.now.Sub(down)
		}
	}
	if back < 17400*time.Millisecond || back > 18*time.Second || !slices.Contains(s.ev, "route.changed D: B -> D (shorter)") {
		t.Fatalf("back to D %v after the link went down, want 5 s after 12.45 s: %v", back, s.ev)
	}
}

// TestShorterPathBothWays is section 0's case on routers exchanging
// advertisements: T reaches S through H for 83.2 ms (two links) when the
// direct T-S link comes up at 77.5 ms. Both ends move to it a little over
// 5 s later. Going the other way, from the direct 77.5 to a detour of 74,
// takes the 0.8 rule and does not happen.
func TestShorterPathBothWays(t *testing.T) {
	n := newNet(t, map[string]time.Duration{"T-H": 1500 * time.Microsecond, "H-S": 40 * time.Millisecond, "T-S": 39 * time.Millisecond}, nil)
	probe := func(ts float64) {
		n.probe("T", "H", 3)
		n.probe("H", "S", 80.2)
		if ts > 0 {
			n.probe("T", "S", ts)
		}
	}
	n.cut("T", "S", true)
	for range 20 {
		probe(-1)
		n.run(time.Second, nil)
	}
	if p, _ := n.path("T", "S"); !slices.Equal(p, []string{"T", "H", "S"}) {
		t.Fatalf("T to S: %v", p)
	}
	n.cut("T", "S", false)
	start := n.now
	var ts, st time.Duration
	for range 12 {
		probe(77.5)
		n.run(time.Second, func() {
			if ts == 0 && n.hop("T", "S") == "S" {
				ts = n.now.Sub(start)
			}
			if st == 0 && n.hop("S", "T") == "T" {
				st = n.now.Sub(start)
			}
		})
	}
	// Up at the third probe, at 2 s.
	t.Logf("T to S direct %v, S to T %v after the first probe", ts, st)
	for _, d := range []time.Duration{ts, st} {
		if d < 7*time.Second || d > 7500*time.Millisecond {
			t.Fatalf("T to S direct after %v, S to T after %v; want 5 s and a little after the link came up at 2 s", ts, st)
		}
	}
	if !slices.Contains(n.eventsOf("T", "route.changed"), "S: H -> S (shorter)") {
		t.Fatal(n.events["T"])
	}

	// H-S drops to 71: the detour costs 74 against 77.5 direct.
	before := len(n.eventsOf("T", "route.changed"))
	for range 60 {
		n.probe("T", "H", 3)
		n.probe("H", "S", 71)
		n.probe("T", "S", 77.5)
		n.run(time.Second, nil)
	}
	if n.hop("T", "S") != "S" || n.hop("S", "T") != "T" || len(n.eventsOf("T", "route.changed")) != before {
		t.Fatalf("moved to the detour: %v", n.events["T"])
	}
}
