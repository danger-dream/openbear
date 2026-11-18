package routing

import (
	"slices"
	"testing"
	"time"
)

// RFCHost-JP2 (2026-11-16): its links answered again for moments, drew the
// other relays' routes and went down again, over and over. A link between
// relays back from down is held down for probationBase, twice as long for
// each further down within flapWindow, up to probationMax.
func TestALinkBackFromDownIsHeldLongerEachTime(t *testing.T) {
	m := newMetric(0, &DefaultTuning)
	m.probation = true
	now := time.Unix(1_800_000_000, 0)
	for range warmSamples {
		m.sample(10*time.Millisecond, false, now)
	}
	back := func() {
		for range downAfter {
			m.sample(0, true, now)
		}
		now = now.Add(time.Second)
		m.sample(10*time.Millisecond, false, now)
	}
	for i, want := range []time.Duration{30 * time.Second, time.Minute, 2 * time.Minute, 4 * time.Minute, probationMax, probationMax} {
		back()
		if got := m.heldUntil.Sub(now); !m.held || got != want {
			t.Fatalf("back after %d downs: held %v for %v, want %v", i+1, m.held, got, want)
		}
	}
	m.decay(m.heldUntil.Add(-time.Millisecond))
	if !m.held || m.state() == StateUp {
		t.Fatal("up before its hold ended")
	}
	m.decay(m.heldUntil)
	if m.held {
		t.Fatal("still held once its hold ended")
	}
	// Downs older than flapWindow no longer count.
	now = now.Add(flapWindow + probationMax)
	back()
	if got := m.heldUntil.Sub(now); got != probationBase {
		t.Fatalf("back after a down long after the others: held for %v", got)
	}
}

// The route goes back to the link once its hold is over, by the shorter
// rule's Hold after it; a link whose peer is not reachable another way is
// let up at once.
func TestARouteTakesALinkBackOnlyAfterItsHold(t *testing.T) {
	s := twoWays(t, 73.2, 77.5)
	if s.switchAfter("D", "D", 6*time.Second) != 5*time.Second {
		t.Fatal("no switch to D")
	}
	fresh := func() {
		s.advert("B", "B", map[string]float64{"A": 10, "D": 73.2}, map[string]string{"D": "D"})
		s.advert("B", "D", map[string]float64{"B": 73.2}, map[string]string{"B": "B"})
	}
	down := s.now
	for range downAfter {
		s.r.Lost("D")
	}
	s.r.Probe("D", 77500*time.Microsecond)
	if s.hop("D") != "B" || !slices.Contains(s.ev, "link.held D: back after 1 downs in 10m0s, held for 30s") {
		t.Fatalf("A to D through %s once its link is back: %v", s.hop("D"), s.ev)
	}
	var back time.Duration
	for i := 0; back == 0 && i < 600; i++ {
		if i%100 == 0 {
			fresh()
		}
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
	if want := probationBase + legacyTuning.Hold; back < want || back > want+time.Second {
		t.Fatalf("back to D %v after the link went down, want %v", back, want)
	}

	for range downAfter {
		s.r.Lost("D")
	}
	s.r.Probe("D", 77500*time.Microsecond)
	if s.hop("D") != "B" {
		t.Fatal("not held after its second down")
	}
	for range downAfter {
		s.r.Lost("B")
	}
	s.r.Tick()
	if s.hop("D") != "D" || !slices.Contains(s.ev, "link.released D: not reachable another way") {
		t.Fatalf("A to D through %s with B down: %v", s.hop("D"), s.ev)
	}
}

// A member's links and a relay's links to members are kept by keep: no hold.
func TestNoProbationOnAMembersLinks(t *testing.T) {
	relay := New(signed(Config{Self: "A", Relays: []string{"B"}, Leaves: []string{"M"}, Links: []string{"B", "M"}}), nil, nil)
	member := New(signed(Config{Self: "M", Leaf: true, Relays: []string{"A"}, Links: []string{"A"}}), nil, nil)
	for _, c := range []struct {
		r    *Router
		peer string
	}{{relay, "M"}, {member, "A"}} {
		for range warmSamples {
			c.r.Probe(c.peer, 10*time.Millisecond)
		}
		for range downAfter {
			c.r.Lost(c.peer)
		}
		c.r.Probe(c.peer, 10*time.Millisecond)
		if l := link(t, c.r, c.peer); l.State != StateUp {
			t.Fatalf("link to %s back from down: %+v", c.peer, l)
		}
	}
}
