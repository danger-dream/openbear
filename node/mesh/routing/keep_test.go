package routing

import (
	"strings"
	"testing"
	"time"
)

func linkState(r *Router, peer string) string {
	for _, l := range r.Status().Links {
		if l.Peer == peer {
			return l.State
		}
	}
	return ""
}

// flapped takes a link down and back twice: suppressed, and answering.
func flapped(r *Router, peer string, rtt time.Duration) {
	for range 2 {
		r.Gone(peer)
		for range warmSamples {
			r.Probe(peer, rtt)
		}
	}
}

// TestLeafKeepsItsBestLinkWhileEveryOneIsSuppressed: a member whose links
// all flapped (its relays applying configs in turn) keeps routing through
// the best of them instead of having no route at all; once another is
// back in use, the kept one is suppressed like the rest.
func TestLeafKeepsItsBestLinkWhileEveryOneIsSuppressed(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	var events []string
	r := New(signed(Config{
		Self: "M", Leaf: true, Relays: []string{"A", "B", "C"}, Links: []string{"A", "B", "C"},
		MemberLinks: [][2]string{{"A", "M"}, {"B", "M"}, {"C", "M"}},
	}), func() time.Time { return now }, func(kind, detail string) { events = append(events, kind+" "+detail) })
	for range warmSamples {
		r.Probe("A", 30*time.Millisecond)
		r.Probe("B", 10*time.Millisecond)
		r.Probe("C", 20*time.Millisecond)
	}
	flapped(r, "B", 10*time.Millisecond)
	if linkState(r, "B") != StateSuppressed {
		t.Fatalf("B = %s, want suppressed while A and C carry the member", linkState(r, "B"))
	}
	flapped(r, "A", 30*time.Millisecond)
	flapped(r, "C", 20*time.Millisecond)
	r.Tick()
	got := []string{linkState(r, "A"), linkState(r, "B"), linkState(r, "C")}
	if got[0] != StateSuppressed || got[1] != StateUp || got[2] != StateSuppressed {
		t.Fatalf("A, B, C = %v; want the fastest, B, kept up", got)
	}
	if nh, ok := r.NextHop("A"); !ok || nh != "B" {
		t.Fatalf("NextHop(A) = %q, %v; want B", nh, ok)
	}
	if !strings.Contains(strings.Join(events, "\n"), "link.kept B:") {
		t.Fatalf("no link.kept for B: %v", events)
	}

	// C's penalty has decayed below reuse first (a minute, reuse below 750):
	// B goes back to being suppressed.
	r.links["C"].penalty = reuseBelow - 1
	r.Tick()
	if s := linkState(r, "B"); s != StateSuppressed {
		t.Fatalf("B = %s once C is back, want suppressed", s)
	}
	if s := linkState(r, "C"); s != StateUp {
		t.Fatalf("C = %s, want up", s)
	}

	// A link that is down is not kept, whatever its penalty.
	r.links["C"].penalty = suppressAt
	for range downAfter {
		r.Lost("B")
	}
	r.Tick()
	if s := linkState(r, "B"); s == StateUp || r.links["B"].kept {
		t.Fatalf("B = %s, kept %v; want a down link not kept", s, r.links["B"].kept)
	}
	if a, c := linkState(r, "A"), linkState(r, "C"); c != StateUp || a != StateSuppressed {
		t.Fatalf("A, C = %s, %s; want C kept, the faster of those answering", a, c)
	}
}

// TestRelaysKeepOneLinkToALeafAndDampTheirOwn: a relay keeps nothing among
// links to other relays, all suppressed or not; of the relays whose link to
// a member is suppressed, the first by name keeps it while no relay
// advertises one up, and lets it go once another does.
func TestRelaysKeepOneLinkToALeafAndDampTheirOwn(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	nums := map[string]uint32{"A": 1, "B": 2, "C": 3, "M": 10}
	member := [][2]string{{"A", "M"}, {"B", "M"}}
	relays := [][2]string{{"A", "B"}, {"A", "C"}, {"B", "C"}}
	rA := New(signed(Config{Self: "A", Relays: []string{"B", "C"}, Leaves: []string{"M"}, Links: []string{"B", "C", "M"}, Numbers: nums,
		RelayLinks: relays, MemberLinks: member}), func() time.Time { return now }, nil)
	rB := New(signed(Config{Self: "B", Relays: []string{"A", "C"}, Leaves: []string{"M"}, Links: []string{"A", "C", "M"}, Numbers: nums,
		RelayLinks: relays, MemberLinks: member}), func() time.Time { return now }, nil)
	for range warmSamples {
		for _, r := range []*Router{rA, rB} {
			for _, p := range []string{"A", "B", "C", "M"} {
				r.Probe(p, 10*time.Millisecond)
			}
		}
	}
	exchange := func() {
		budget := Budget{JSON: 4000, Compact: 4000}
		rA.sent, rB.sent = time.Time{}, time.Time{}
		a, b := rA.Advert(budget, []string{"B"}), rB.Advert(budget, []string{"A"})
		if len(a) == 0 || len(b) == 0 {
			t.Fatal("no advertisement")
		}
		if _, _, err := rB.Receive("A", a[0], budget); err != nil {
			t.Fatal(err)
		}
		if _, _, err := rA.Receive("B", b[0], budget); err != nil {
			t.Fatal(err)
		}
		rA.Tick()
		rB.Tick()
	}
	exchange()

	// Every link of A flapped: its relay links stay suppressed, and so does its
	// link to M, as B advertises its own up.
	for _, p := range []string{"B", "C", "M"} {
		flapped(rA, p, 10*time.Millisecond)
	}
	exchange()
	if b, c, m := linkState(rA, "B"), linkState(rA, "C"), linkState(rA, "M"); b != StateSuppressed || c != StateSuppressed || m != StateSuppressed {
		t.Fatalf("A: B %s, C %s, M %s; want all suppressed while B's link to M is up", b, c, m)
	}

	// B's link to M flaps too: A, first by name, keeps its own; B does not.
	flapped(rB, "M", 10*time.Millisecond)
	exchange()
	exchange()
	if a, b := linkState(rA, "M"), linkState(rB, "M"); a != StateUp || b != StateSuppressed {
		t.Fatalf("links to M: A %s, B %s; want A's kept, B's suppressed", a, b)
	}
	if b, c := linkState(rA, "B"), linkState(rA, "C"); b != StateSuppressed || c != StateSuppressed {
		t.Fatalf("A's relay links: B %s, C %s; want suppressed", b, c)
	}

	// B's back in use: A lets its own go.
	rB.links["M"].penalty = reuseBelow - 1
	exchange()
	exchange()
	if a, b := linkState(rA, "M"), linkState(rB, "M"); a != StateSuppressed || b != StateUp {
		t.Fatalf("links to M: A %s, B %s; want A's suppressed again, B's up", a, b)
	}
}
