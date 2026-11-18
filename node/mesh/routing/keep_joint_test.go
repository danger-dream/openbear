package routing

import (
	"testing"
	"time"
)

func TestLeafKeepAgreesWithAdvertisedReturnEdge(t *testing.T) {
	now := time.Unix(1800000000, 0)
	nums := map[string]uint32{"A": 1, "B": 2, "M": 3}
	member := [][2]string{{"A", "M"}, {"B", "M"}}
	relay := [][2]string{{"A", "B"}}
	makeRouter := func(self string, relays, links, leaves []string, leaf bool) *Router {
		return New(signed(Config{Self: self, Leaf: leaf, Relays: relays, Links: links, Leaves: leaves, Numbers: nums, MemberLinks: member, RelayLinks: relay}), func() time.Time { return now }, nil)
	}
	a := makeRouter("A", []string{"B"}, []string{"B", "M"}, []string{"M"}, false)
	b := makeRouter("B", []string{"A"}, []string{"A", "M"}, []string{"M"}, false)
	m := makeRouter("M", []string{"A", "B"}, []string{"A", "B"}, nil, true)
	for range warmSamples {
		a.Probe("B", time.Millisecond)
		a.Probe("M", 30*time.Millisecond)
		b.Probe("A", time.Millisecond)
		b.Probe("M", 10*time.Millisecond)
		m.Probe("A", 30*time.Millisecond)
		m.Probe("B", 10*time.Millisecond)
	}
	budget := Budget{JSON: 4000, Compact: 4000}
	exchange := func() {
		t.Helper()
		now = now.Add(time.Second)
		for _, r := range []*Router{a, b} {
			r.sent = time.Time{}
			peer := a
			if r == a {
				peer = b
			}
			msgs := r.Advert(budget, []string{peer.cfg.Self, "M"})
			if len(msgs) == 0 {
				t.Fatal("no advert")
			}
			for _, to := range []*Router{peer, m} {
				if _, _, err := to.Receive(r.cfg.Self, msgs[0], budget); err != nil {
					t.Fatal(err)
				}
			}
		}
		a.Tick()
		b.Tick()
		m.Tick()
	}
	exchange()
	// M locally prefers B, but the relay election keeps A's return edge.
	flapped(m, "A", 30*time.Millisecond)
	flapped(m, "B", 10*time.Millisecond)
	flapped(a, "M", 30*time.Millisecond)
	flapped(b, "M", 10*time.Millisecond)
	for range 4 {
		exchange()
	}
	if linkState(a, "M") != StateUp || linkState(b, "M") != StateSuppressed {
		t.Fatalf("fixture return edges A=%s B=%s", linkState(a, "M"), linkState(b, "M"))
	}
	if linkState(m, "A") != StateUp || linkState(m, "B") != StateSuppressed {
		t.Fatalf("member disagrees: A=%s B=%s", linkState(m, "A"), linkState(m, "B"))
	}
	if hop, ok := m.NextHop("B"); !ok || hop != "A" {
		t.Fatalf("M has no usable outbound route: %q %v", hop, ok)
	}
	if hop, ok := b.NextHop("M"); !ok || hop != "A" {
		t.Fatalf("B has no usable return route: %q %v", hop, ok)
	}
	// When B's damping recovers, both sides automatically migrate together.
	b.links["M"].penalty = reuseBelow - 1
	for range 4 {
		exchange()
	}
	if hop, ok := m.NextHop("B"); !ok || hop != "B" {
		t.Fatalf("member did not follow recovered return edge: %q %v", hop, ok)
	}
}
