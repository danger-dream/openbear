package routing

import (
	"encoding/json"
	"fmt"
	"math"
	"slices"
	"sort"
	"strings"
	"testing"
	"time"
)

func near(t *testing.T, what string, got, want float64) {
	t.Helper()
	if math.Abs(got-want) > 0.05 {
		t.Fatalf("%s = %v, want %v", what, got, want)
	}
}

func link(t *testing.T, r *Router, peer string) LinkStatus {
	t.Helper()
	for _, l := range r.Status().Links {
		if l.Peer == peer {
			return l
		}
	}
	t.Fatalf("no link %s", peer)
	return LinkStatus{}
}

// TestLinkMetrics checks the round trip, jitter, loss, down, the penalty and its
// half-life, suppression and reuse, the cap and bias.
func TestLinkMetrics(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	var events []string
	r := New(signed(Config{Self: "A", Relays: []string{"B"}, Links: []string{"B"}, Bias: map[string]float64{"B": 5}}), func() time.Time { return now },
		func(k, d string) { events = append(events, k+" "+d) })
	ms := func(v float64) time.Duration { return time.Duration(v * float64(time.Millisecond)) }
	if l := link(t, r, "B"); l.State != StateDown || !math.IsInf(l.Cost, 1) {
		t.Fatalf("before any probe: %+v", l)
	}
	// Up after three answers, from the fastest of them.
	r.Probe("B", ms(12))
	r.Probe("B", ms(10))
	if l := link(t, r, "B"); l.State != StateDown {
		t.Fatalf("up after two probes: %+v", l)
	}
	r.Probe("B", ms(11))
	l := link(t, r, "B")
	near(t, "rtt", l.RTTMs, 10)
	near(t, "jitter", l.JitterMs, 0)
	near(t, "cost", l.Cost, 10+5+DefaultTuning.HopCostMs) // rtt 10 + bias 5 + hopCost 2
	r.Probe("B", ms(20))
	// Medians: of the round trips 12, 10, 11, 20 the upper middle 12; of
	// the distances from it, |20-12| alone so far.
	l = link(t, r, "B")
	near(t, "rtt", l.RTTMs, 12)
	near(t, "jitter", l.JitterMs, 8)
	near(t, "cost", l.Cost, 12+2*8+5+DefaultTuning.HopCostMs)
	r.Lost("B")
	l = link(t, r, "B")
	// First single loss in 100 probes is ignored.
	near(t, "loss", l.Loss, 0)
	near(t, "cost", l.Cost, 12+2*8+5+DefaultTuning.HopCostMs)
	if l.State != StateUp {
		t.Fatal("one lost probe is not down")
	}
	r.Lost("B")
	if link(t, r, "B").State != StateUp {
		t.Fatal("two lost probes are not down")
	}
	r.Lost("B")
	l = link(t, r, "B")
	if l.State != StateDown || !math.IsInf(l.Cost, 1) || l.Penalty != 1000 {
		t.Fatalf("three lost probes: %+v", l)
	}
	r.Lost("B")
	if link(t, r, "B").Penalty != 1000 {
		t.Fatal("staying down added to the penalty")
	}
	r.Probe("B", ms(13))
	// The four probes lost in the outage leave the loss rate once it answers:
	// the outage costs through the penalty, not as loss.
	if l := link(t, r, "B"); l.State != StateUp || l.Loss != 0 {
		t.Fatalf("back up: %+v", l)
	}
	// Losses before the outage stay: one stray, then three more scattered.
	for _, lost := range []bool{true, false, true, false, true, false, true, false} {
		if lost {
			r.Lost("B")
		} else {
			r.Probe("B", ms(13))
		}
	}
	// 4 of the last 10 lost -> short window (0.4).
	near(t, "loss of scattered losses", link(t, r, "B").Loss, 0.4)
	for i := 0; i < 10; i++ {
		r.Probe("B", ms(13))
	}
	// Short window cleared; 4 losses still in 100 -> (4-1)/100 = 0.03.
	near(t, "loss after ten good probes", link(t, r, "B").Loss, 0.03)
	now = now.Add(30 * time.Second)
	r.Tick()
	near(t, "penalty after a half-life", link(t, r, "B").Penalty, 500)

	// Down again at once: 500 + 1000 is not yet suppressed; once more is.
	for i := 0; i < 3; i++ {
		r.Lost("B")
	}
	r.Probe("B", ms(13))
	if l := link(t, r, "B"); l.State != StateUp || l.Penalty != 1500 {
		t.Fatalf("penalty 1500: %+v", l)
	}
	for i := 0; i < 3; i++ {
		r.Lost("B")
	}
	r.Probe("B", ms(13))
	if l := link(t, r, "B"); l.State != StateSuppressed || l.Penalty != 2500 || !math.IsInf(l.Cost, 1) {
		t.Fatalf("penalty 2500: %+v", l)
	}
	if !slices.Contains(events, "link.suppressed B: penalty 2500") {
		t.Fatal("no link.suppressed event", events)
	}
	// Reused below 750: 2500 * 0.5^(t/30) < 750 after 52.1 s.
	now = now.Add(52 * time.Second)
	r.Tick()
	if link(t, r, "B").State != StateSuppressed {
		t.Fatal("reused above 750")
	}
	now = now.Add(200 * time.Millisecond)
	r.Tick()
	if l := link(t, r, "B"); l.State != StateUp || l.Penalty >= 750 {
		t.Fatalf("not reused below 750: %+v", l)
	}
	if !slices.ContainsFunc(events, func(e string) bool { return strings.HasPrefix(e, "link.reused B: penalty 7") }) {
		t.Fatal("no link.reused event", events)
	}
	for i := 0; i < 6; i++ {
		r.Gone("B")
		r.Probe("B", ms(13))
	}
	if p := link(t, r, "B").Penalty; p != 4000 {
		t.Fatalf("penalty %v is above the cap", p)
	}

	// A negative bias cannot make a link cost nothing.
	r.Configure(signed(Config{Self: "A", Relays: []string{"B"}, Links: []string{"B"}, Bias: map[string]float64{"B": -500}}))
	now = now.Add(10 * time.Minute)
	r.Tick()
	if l := link(t, r, "B"); l.Cost != minCost || l.Advertised != minCost {
		t.Fatalf("negative bias: %+v", l)
	}
}

// TestAdvertisedCostThreshold: the cost a link advertises updates once the
// measured cost moves by at least max(2% of the larger, 2 ms), with no 5s
// hold; state changes go out after advertGap (100 ms), while pure cost changes
// are rate-limited to once every 2 seconds per origin.
func TestAdvertisedCostThreshold(t *testing.T) {
	for _, c := range []struct {
		measured, advertised float64
		want                 bool
	}{
		{100, 100, false},
		{101.9, 100, false},
		{102.1, 100, true},
		{98.1, 100, false},
		{98.0, 100, true},
		{11.9, 10, false},
		{12.0, 10, true},
		{8.1, 10, false},
		{8.0, 10, true},
	} {
		if got := costDiffers(c.measured, c.advertised); got != c.want {
			t.Errorf("costDiffers(%v, %v) = %v, want %v", c.measured, c.advertised, got, c.want)
		}
	}
	now := time.Unix(1_800_000_000, 0)
	r := New(signed(Config{Self: "A", Relays: []string{"B"}, Links: []string{"B"}}), func() time.Time { return now }, nil)
	for range warmSamples {
		r.Probe("B", 10*time.Millisecond)
	}
	if msgs := r.Advert(Budget{JSON: 1000, Compact: 1000}, []string{"B"}); len(msgs) != 1 {
		t.Fatalf("initial advert: %d", len(msgs))
	}
	// Pure cost change: not sent before costAdvertGap (2s), sent at 2s.
	for range 5 {
		r.Probe("B", 40*time.Millisecond)
	}
	now = now.Add(time.Second)
	if msgs := r.Advert(Budget{JSON: 1000, Compact: 1000}, []string{"B"}); len(msgs) != 0 {
		t.Fatalf("pure cost advert sent after 1s")
	}
	now = now.Add(time.Second)
	if msgs := r.Advert(Budget{JSON: 1000, Compact: 1000}, []string{"B"}); len(msgs) != 1 {
		t.Fatalf("pure cost advert not sent after 2s")
	}
	// State change (down) goes out after advertGap (100ms), not waiting 1s.
	r.Lost("B")
	r.Lost("B")
	r.Lost("B")
	now = now.Add(advertGap)
	if msgs := r.Advert(Budget{JSON: 1000, Compact: 1000}, []string{"B"}); len(msgs) != 1 {
		t.Fatalf("state change advert not sent after advertGap")
	}
}

// triangle: A-B direct 10 ms against A-C-B 20 ms.
func triangle(t *testing.T) *testNet {
	n := newNet(t, map[string]time.Duration{"A-B": 5 * time.Millisecond, "A-C": 5 * time.Millisecond, "B-C": 5 * time.Millisecond}, nil)
	for i := 0; i < 3; i++ {
		n.probe("A", "B", 10)
		n.probe("A", "C", 10)
		n.probe("B", "C", 10)
		n.run(time.Second, nil)
	}
	if n.hop("A", "B") != "B" || n.hop("B", "A") != "A" {
		t.Fatalf("A-B not direct: %s, %s", n.hop("A", "B"), n.hop("B", "A"))
	}
	return n
}

// TestBetterPathSmoothSwitch: small noise does not move the route; a sustained
// large increase of A-B moves A<->B to C once smoothed costs cross the margin.
func TestBetterPathSmoothSwitch(t *testing.T) {
	n := triangle(t)
	second := func(ab float64) {
		n.probe("A", "B", ab)
		n.probe("A", "C", 10)
		n.probe("B", "C", 10)
	}
	// Settle initial start penalty on both paths.
	for i := 0; i < 400; i++ {
		second(10)
		n.run(time.Second, nil)
	}
	// Small increase (15 ms direct vs 24 ms via C): still cheaper, no switch.
	for i := 0; i < 20; i++ {
		second(15)
		n.run(time.Second, nil)
	}
	if n.hop("A", "B") != "B" {
		t.Fatal("a small increase moved the route")
	}
	start := n.now
	var moved, back time.Duration
	for i := 0; i < 60; i++ {
		second(60)
		n.run(time.Second, func() {
			if moved == 0 && n.hop("A", "B") == "C" {
				moved = n.now.Sub(start)
			}
			if back == 0 && n.hop("B", "A") == "C" {
				back = n.now.Sub(start)
			}
		})
	}
	if moved == 0 || moved > 45*time.Second || back == 0 || back > 45*time.Second {
		t.Fatalf("A moved after %v, B after %v; want within 45s", moved, back)
	}
	if e := n.eventsOf("A", "route.changed"); !slices.Contains(e, "B: B -> C (better)") {
		t.Fatal(e)
	}
}

// TestDownSwitchesAtOnce: the third lost probe moves the route in the same
// call; the far end follows as soon as the advertisement reaches it.
func TestDownSwitchesAtOnce(t *testing.T) {
	n := triangle(t)
	n.cut("A", "B", true)
	n.r["A"].Lost("B")
	n.r["A"].Lost("B")
	if n.hop("A", "B") != "B" {
		t.Fatal("moved after two lost probes")
	}
	n.r["A"].Lost("B")
	if n.hop("A", "B") != "C" {
		t.Fatal("not moved at the third lost probe")
	}
	start := n.now
	var follows time.Duration
	n.run(time.Second, func() {
		if follows == 0 && n.hop("B", "A") == "C" {
			follows = n.now.Sub(start)
		}
	})
	// A's advertisement may wait for the 100 ms gap, then crosses C.
	if follows == 0 || follows > advertGap+20*time.Millisecond {
		t.Fatalf("B followed after %v", follows)
	}
	if e := n.eventsOf("A", "route.changed"); !slices.Contains(e, "B: B -> C (down)") {
		t.Fatal(e)
	}
}

// solo is one relay A, linked to B and C, fed hand-made advertisements.
type solo struct {
	t   *testing.T
	now time.Time
	r   *Router
	seq map[string]uint64
	ev  []string
}

func newSolo(t *testing.T, relays, links []string, hops map[string]string) *solo {
	return newSoloTuned(t, relays, links, hops, &DefaultTuning)
}

func newSoloTuned(t *testing.T, relays, links []string, hops map[string]string, tune *Tuning) *solo {
	s := &solo{t: t, now: time.Unix(1_800_000_000, 0), seq: map[string]uint64{}}
	s.r = New(signed(Config{Self: "A", Relays: relays, Links: links, NextHops: hops, Tuning: tune}), func() time.Time { return s.now }, func(k, d string) { s.ev = append(s.ev, k+" "+d) })
	s.r.setRandSeed(0x123456789abcdef0, 0xfedcba9876543210)
	return s
}

func (s *solo) advance(d time.Duration) {
	steps := int(d / time.Second)
	for range steps {
		s.now = s.now.Add(time.Second)
		s.r.Tick()
	}
	if rem := d - time.Duration(steps)*time.Second; rem > 0 {
		s.now = s.now.Add(rem)
		s.r.Tick()
	}
}

// advert sends origin's advertisement through from: links are peer:cost
// (cost < 0 is down), routes dest:nextHop.
func (s *solo) advert(from, origin string, links map[string]float64, routes map[string]string) ([]string, error) {
	s.t.Helper()
	s.seq[origin]++
	var order []string
	state := map[string]advLink{}
	for p, c := range links {
		if c < 0 {
			state[p] = advLink{0, StateDown}
		} else {
			state[p] = advLink{c, StateUp}
		}
	}
	for d := range routes {
		order = append(order, d)
	}
	sort.Strings(order)
	raw := encodeJSON(&advert{origin: origin, seq: s.seq[origin], links: state, routes: routes}, order, routes != nil)
	fwd, _, err := s.recv(from, raw)
	return fwd, err
}

// ownJSON is A's own advertisement, if due: JSON, as A's config has no
// control host numbers.
func (s *solo) ownJSON(budget int) []byte {
	for _, m := range s.r.Advert(Budget{JSON: budget, Compact: budget}, []string{"B"}) {
		if m.Format == FormatJSON {
			return m.Raw
		}
	}
	return nil
}

// recv gives A a JSON advertisement from a neighbor, signed by the origin
// it names; forward lists the neighbors it goes on to.
func (s *solo) recv(from string, raw []byte) (forward []string, changed bool, err error) {
	var head struct {
		O string `json:"o"`
	}
	json.Unmarshal(raw, &head)
	msgs, changed, err := s.r.Receive(from, signedBy(head.O, FormatJSON, raw), Budget{JSON: 1000, Compact: 1000})
	for _, m := range msgs {
		if !slices.Contains(forward, m.Peer) {
			forward = append(forward, m.Peer)
		}
	}
	return forward, changed, err
}

// up gives the link to peer the answers it needs to be up, ms each.
func (s *solo) up(peer string, ms float64) {
	for i := 0; i < warmSamples; i++ {
		s.r.Probe(peer, time.Duration(ms*float64(time.Millisecond)))
	}
}

// diamond: A-B 10, A-C 10, B-D 10, C-D 30; A reaches D through B for 20
// or C for 40.
func diamond(t *testing.T) *solo {
	s := newSolo(t, []string{"B", "C", "D"}, []string{"B", "C"}, nil)
	s.up("B", 10)
	s.up("C", 10)
	s.advert("B", "B", map[string]float64{"A": 10, "D": 10}, map[string]string{"C": "A", "D": "D"})
	s.advert("C", "C", map[string]float64{"A": 10, "D": 30}, map[string]string{"B": "A", "D": "D"})
	s.advert("B", "D", map[string]float64{"B": 10, "C": 30}, map[string]string{"B": "B", "C": "C"})
	if h, _ := s.r.NextHop("D"); h != "B" {
		t.Fatalf("A to D through %s", h)
	}
	return s
}

func (s *solo) hop(d string) string {
	h, _ := s.r.NextHop(d)
	return h
}

// TestRetainedNextHopMustStayLoopFree: when the next hop in use starts
// routing back through this relay, the route moves at once.
func TestRetainedNextHopMustStayLoopFree(t *testing.T) {
	s := diamond(t)
	// Let both paths settle their initial start penalty before the fault.
	for range 60 {
		s.advance(10 * time.Second)
		s.advert("B", "B", map[string]float64{"A": 10, "D": 10}, map[string]string{"C": "A", "D": "D"})
		s.advert("C", "C", map[string]float64{"A": 10, "D": 30}, map[string]string{"B": "A", "D": "D"})
		s.advert("B", "D", map[string]float64{"B": 10, "C": 30}, map[string]string{"B": "B", "C": "C"})
	}
	// B now says it reaches D through A, while its link to D still shows up.
	s.advert("B", "B", map[string]float64{"A": 10, "D": 10}, map[string]string{"C": "A", "D": "A"})
	if s.hop("D") != "C" {
		t.Fatalf("A kept %s", s.hop("D"))
	}
	if !slices.Contains(s.ev, "route.changed D: B -> C (loop)") {
		t.Fatal(s.ev)
	}
	// B's path is loop-free again and cheaper: because it was loop-unsafe,
	// it starts with StartPenaltyMs and settles back to B.
	s.advert("B", "B", map[string]float64{"A": 10, "D": 10}, map[string]string{"C": "A", "D": "D"})
	if s.hop("D") != "C" {
		t.Fatalf("A jumped back to B immediately despite start penalty: %s", s.hop("D"))
	}
	if st := routeTo(s.r, "D"); st.Held != HeldSettling || st.Best != "B" {
		t.Fatalf("want B settling, got %+v", st)
	}
	// Refresh adverts periodically while smoothing settles so lsdb does not expire.
	for range 50 {
		s.advance(10 * time.Second)
		s.advert("B", "B", map[string]float64{"A": 10, "D": 10}, map[string]string{"C": "A", "D": "D"})
		s.advert("C", "C", map[string]float64{"A": 10, "D": 30}, map[string]string{"B": "A", "D": "D"})
		s.advert("B", "D", map[string]float64{"B": 10, "C": 30}, map[string]string{"B": "B", "C": "C"})
	}
	if s.hop("D") != "B" || !slices.Contains(s.ev, "route.changed D: C -> B (better)") {
		t.Fatal(s.hop("D"), s.ev)
	}
}

// TestNextHopWaitsForLoopFreePath: the cheaper next hop is taken only once its
// own advertised path no longer comes back here and its smoothed cost catches
// up; until then the route in use stays.
func TestNextHopWaitsForLoopFreePath(t *testing.T) {
	s := diamond(t)
	// B-D goes down: A moves to C, and B, until it hears of it, still says D through D.
	s.advert("B", "D", map[string]float64{"B": -1, "C": 30}, map[string]string{"B": "C", "C": "C"})
	if s.hop("D") != "C" {
		t.Fatalf("A to D through %s", s.hop("D"))
	}
	// B takes A's path: D through A.
	s.advert("B", "B", map[string]float64{"A": 10, "D": -1}, map[string]string{"C": "A", "D": "A"})
	// B-D comes back up. The shortest path is through B again, but B still routes through A.
	s.advert("B", "D", map[string]float64{"B": 10, "C": 30}, map[string]string{"B": "B", "C": "C"})
	if s.hop("D") != "C" {
		t.Fatalf("A moved to %s while B routes through A", s.hop("D"))
	}
	// B advertises D direct: loop-free again, settles back to B.
	s.advert("B", "B", map[string]float64{"A": 10, "D": 10}, map[string]string{"C": "A", "D": "D"})
	for range 50 {
		s.advance(10 * time.Second)
		s.advert("B", "B", map[string]float64{"A": 10, "D": 10}, map[string]string{"C": "A", "D": "D"})
		s.advert("C", "C", map[string]float64{"A": 10, "D": 30}, map[string]string{"B": "A", "D": "D"})
		s.advert("B", "D", map[string]float64{"B": 10, "C": 30}, map[string]string{"B": "B", "C": "C"})
	}
	if s.hop("D") != "B" {
		t.Fatalf("A to D through %s once B's path is loop-free and settled", s.hop("D"))
	}
}

// TestCurrentCostBoundsTheNewNextHop: a neighbor whose advertised path
// costs at least as much as the route in use is not taken.
func TestCurrentCostBoundsTheNewNextHop(t *testing.T) {
	s := diamond(t)
	// C's link to D gets very cheap, but C routes to D through B's long way.
	s.advert("C", "D", map[string]float64{"B": 10, "C": 1}, map[string]string{"B": "B", "C": "C"})
	s.advert("C", "C", map[string]float64{"A": 10, "D": 1}, map[string]string{"B": "A", "D": "B"})
	s.advert("C", "B", map[string]float64{"A": 10, "D": 10, "C": 40}, map[string]string{"C": "C", "D": "D"})
	// Shortest: A-C-D; C's own path C-B-D costs 50, above A's 22.
	if s.hop("D") != "B" {
		t.Fatalf("A took %s", s.hop("D"))
	}
	// C's path is now loop-free and cheaper (13 vs 22): settles and switches to C.
	s.advert("C", "C", map[string]float64{"A": 10, "D": 1}, map[string]string{"B": "A", "D": "D"})
	for range 50 {
		s.advance(10 * time.Second)
		s.advert("C", "D", map[string]float64{"B": 10, "C": 1}, map[string]string{"B": "B", "C": "C"})
		s.advert("C", "C", map[string]float64{"A": 10, "D": 1}, map[string]string{"B": "A", "D": "D"})
		s.advert("C", "B", map[string]float64{"A": 10, "D": 10, "C": 40}, map[string]string{"C": "C", "D": "D"})
	}
	if s.hop("D") != "C" || !slices.Contains(s.ev, "route.changed D: B -> C (better)") {
		t.Fatalf("A to D through %s: %v", s.hop("D"), s.ev)
	}
}

// TestPathSwitchThreshold: with the route in use working, a cheaper path
// moves only when both real and smoothed costs beat max(SwitchPct% * cur, SwitchMinMs).
func TestPathSwitchThreshold(t *testing.T) {
	// A-B and A-C cost 12 (rtt 10 + hopCost 2); A reaches D through B for 12 + bd,
	// through C for 12 + cd. With defaults 5% / 2ms:
	cases := []struct {
		cd, bd float64
		want   string
	}{
		{47.5, 50, "B"}, // 59.5 vs 62: 2.5 ms < max(5%=3.1, 2ms)=3.1ms
		{46, 50, "C"},   // 58 vs 62: 4 ms >= 3.1ms
		{188, 200, "C"}, // 200 vs 212: 12 ms >= max(5%=10.6, 5ms)=10.6ms
		{192, 200, "B"}, // 204 vs 212: 8 ms < 10.6ms
	}
	for _, c := range cases {
		s := newSolo(t, []string{"B", "C", "D"}, []string{"B", "C"}, map[string]string{"D": "B"})
		s.up("B", 10)
		s.up("C", 10)
		refresh := func() {
			s.advert("B", "B", map[string]float64{"A": 12, "D": c.bd}, map[string]string{"C": "A", "D": "D"})
			s.advert("B", "D", map[string]float64{"B": c.bd, "C": c.cd}, map[string]string{"B": "B", "C": "C"})
			s.advert("C", "C", map[string]float64{"A": 12, "D": c.cd}, map[string]string{"B": "A", "D": "D"})
		}
		// Start with B alone so B is current, then bring C up and let smoothing settle.
		s.advert("B", "B", map[string]float64{"A": 12, "D": c.bd}, map[string]string{"C": "A", "D": "D"})
		s.advert("B", "D", map[string]float64{"B": c.bd, "C": c.cd}, map[string]string{"B": "B", "C": "C"})
		if s.hop("D") != "B" {
			t.Fatalf("%+v: A to D through %s before C's advertisement", c, s.hop("D"))
		}
		for range 80 {
			refresh()
			s.advance(10 * time.Second)
		}
		if s.hop("D") != c.want {
			t.Errorf("%+v: A to D through %s, want %s", c, s.hop("D"), c.want)
		}
	}
}

// TestAdvertSeqForwardAndExpiry: a new seq is stored and forwarded to the
// other neighbors, an old or repeated one neither; 30 s without a refresh
// drops it, and without any advertisement the configured next hops are back.
func TestAdvertSeqForwardAndExpiry(t *testing.T) {
	s := newSolo(t, []string{"B", "C", "D"}, []string{"B", "C", "D"}, map[string]string{"B": "D", "C": "D", "D": "D"})
	s.up("B", 10)
	s.up("C", 10)
	s.up("D", 10)
	if s.hop("C") != "D" {
		t.Fatal("before advertisements:", s.hop("C"))
	}
	fwd, err := s.advert("B", "C", map[string]float64{"A": 10, "B": 1}, map[string]string{"A": "A", "B": "B"})
	if err != nil || !slices.Equal(fwd, []string{"D"}) {
		t.Fatalf("forwarded to %v: %v", fwd, err)
	}
	if s.hop("C") != "C" {
		t.Fatal("after an advertisement:", s.hop("C"))
	}
	// The same seq again, through D: not stored or forwarded.
	s.seq["C"] = 0
	if fwd, _ := s.advert("D", "C", map[string]float64{"A": -1, "B": 1}, map[string]string{"B": "B"}); len(fwd) != 0 || s.hop("C") != "C" {
		t.Fatal("a repeated seq was taken:", fwd, s.hop("C"))
	}
	s.seq["C"] = 10
	if fwd, _ := s.advert("D", "C", map[string]float64{"A": 10, "B": 1}, map[string]string{"B": "B"}); !slices.Equal(fwd, []string{"B"}) {
		t.Fatal("a newer seq was not forwarded:", fwd)
	}
	s.seq["C"] = 5
	if fwd, _ := s.advert("D", "C", map[string]float64{"A": -1, "B": 1}, map[string]string{"B": "B"}); len(fwd) != 0 || s.hop("C") != "C" {
		t.Fatal("an older seq was taken:", fwd, s.hop("C"))
	}
	// Its own advertisement coming back is ignored.
	own := s.ownJSON(1000)
	if fwd, _, err := s.recv("B", own); err != nil || fwd != nil {
		t.Fatal("own advertisement:", fwd, err)
	}
	s.now = s.now.Add(30 * time.Second)
	s.r.Tick()
	if len(s.r.lsdb) != 1 || s.hop("C") != "C" {
		t.Fatal("expired at 30 s")
	}
	s.now = s.now.Add(10 * time.Millisecond)
	s.r.Tick()
	if len(s.r.lsdb) != 0 || s.hop("C") != "D" {
		t.Fatalf("not expired after 30 s: %d, %s", len(s.r.lsdb), s.hop("C"))
	}
}

// TestStaticAndFallback: static mode uses nextHops whatever is advertised;
// in dynamic mode a destination keeps its configured next hop until the
// advertisements give it a route.
func TestStaticAndFallback(t *testing.T) {
	hops := map[string]string{"B": "B", "C": "B", "D": "B"}
	s := newSolo(t, []string{"B", "C", "D"}, []string{"B", "C"}, hops)
	if s.hop("C") != "B" || s.hop("D") != "B" {
		t.Fatal("start:", s.hop("C"), s.hop("D"))
	}
	s.up("C", 10)
	s.advert("C", "C", map[string]float64{"A": 10}, map[string]string{"B": "A"})
	if s.hop("C") != "C" {
		t.Fatal("C advertised: A to C through", s.hop("C"))
	}
	if s.hop("D") != "B" {
		t.Fatal("D is not in any advertisement yet, A to D through", s.hop("D"))
	}
	if st := s.r.Status(); st.Mode != ModeDynamic {
		t.Fatal(st.Mode)
	}
	s.r.Configure(signed(Config{Self: "A", Relays: []string{"B", "C", "D"}, Links: []string{"B", "C"}, NextHops: hops, Mode: ModeStatic}))
	if s.hop("C") != "B" || s.r.Status().Mode != ModeStatic {
		t.Fatal("static mode: A to C through", s.hop("C"))
	}
	// Static mode still advertises its links and the hops it uses.
	if raw := string(s.ownJSON(1000)); !strings.Contains(raw, `["C",12,"u"]`) || !strings.Contains(raw, `["C","B"]`) {
		t.Fatal(raw)
	}
}

// TestAdvertLimits: what an advertisement may hold and how often.
func TestAdvertLimits(t *testing.T) {
	s := newSolo(t, []string{"B", "C"}, []string{"B"}, nil)
	s.up("B", 10)
	bad := []string{
		`{"o":"X","s":1,"l":[]}`,                                                // unknown origin
		`{"o":"C","s":0,"l":[]}`,                                                // no seq
		`{"o":"C","s":1,"l":[["B",1,"u"],["B",2,"u"]]}`,                         // repeated link
		`{"o":"C","s":1,"l":[["B",-1,"u"]]}`,                                    // negative cost
		`{"o":"C","s":1,"l":[["B",1e9,"u"]]}`,                                   // cost too large
		`{"o":"C","s":1,"l":[["B",1,"up"]]}`,                                    // unknown state
		`{"o":"C","s":1,"l":[["C",1,"u"]]}`,                                     // link to itself
		`{"o":"C","s":1,"l":[["A",1,"u"],["B",1,"u"],["X",1,"u"],["Y",1,"u"]]}`, // more links than relays
		`{"o":"C","s":1,"l":[],"r":[["Q"]]}`,                                    // unknown destination
		`{"o":"C","s":1,"l":[],"r":[["B","C"]]}`,                                // route through itself
	}
	for _, raw := range bad {
		if _, _, err := s.recv("B", []byte(raw)); err == nil {
			t.Errorf("accepted %s", raw)
		}
	}
	if _, _, err := s.recv("C", []byte(`{"o":"C","s":1,"l":[]}`)); err == nil {
		t.Error("accepted an advertisement from a relay without a link")
	}
	// One origin: 40 at once, then 20 a second.
	taken := 0
	for i := 0; i < 60; i++ {
		if _, err := s.advert("B", "C", map[string]float64{"B": 1}, nil); err == nil {
			taken++
		}
	}
	if taken != originBurst {
		t.Fatalf("took %d of 60 at once", taken)
	}
	s.now = s.now.Add(time.Second)
	if _, err := s.advert("B", "C", map[string]float64{"B": 1}, nil); err != nil {
		t.Fatal("refused after a second:", err)
	}
	// One neighbor: sound messages, an old one again included, cost it
	// nothing; 100 invalid ones at once, then 50 a second, are read, and past
	// them its messages are dropped unread, a sound one too.
	s.now = s.now.Add(time.Minute)
	for i := 0; i < 200; i++ {
		if _, _, err := s.recv("B", []byte(`{"o":"C","s":1,"l":[]}`)); err != nil {
			t.Fatalf("an old advertisement again, %d: %v", i, err)
		}
	}
	verified := s.r.Verified()
	forged := []byte(`{"o":"C","s":999,"l":[]}`)
	n := 0
	for i := 0; i < 200; i++ {
		_, _, err := s.r.Receive("B", Message{Format: FormatJSON, Raw: forged, Sig: advertInput("n", FormatJSON, forged).Sign(testKey("B"))}, Budget{1000, 1000})
		if err == ErrSignature {
			n++
		} else if err != errThrottled {
			t.Fatalf("forged %d: %v", i, err)
		}
	}
	if n != senderBurst || s.r.Verified() != verified {
		t.Fatalf("read %d of 200 forged at once, want %.0f; verified %d", n, senderBurst, s.r.Verified()-verified)
	}
	if _, err := s.advert("B", "C", map[string]float64{"B": 1}, nil); err != errThrottled {
		t.Fatalf("a sound one while throttled: %v", err)
	}
	s.now = s.now.Add(time.Second)
	if _, err := s.advert("B", "C", map[string]float64{"B": 1}, nil); err != nil {
		t.Fatal("refused after a second:", err)
	}
	if d := s.r.Status().Drops; d.Signature != senderBurst || d.Throttled != 101 || d.Unlinked != 1 || d.OriginRate != 20 || d.Malformed != uint64(len(bad)) {
		t.Fatalf("drops %+v", d)
	}
	// This relay's own advertisements: at once when something changed, but
	// at most every 100 ms; otherwise every 5 s.
	s.now = s.now.Add(time.Minute)
	if s.ownJSON(1000) == nil {
		t.Fatal("no advertisement after a minute")
	}
	s.up("B", 100)
	s.r.Lost("B")
	s.r.Lost("B")
	s.r.Lost("B") // down: a change
	if s.ownJSON(1000) != nil {
		t.Fatal("advertised again at once")
	}
	s.now = s.now.Add(advertGap)
	if s.ownJSON(1000) == nil {
		t.Fatal("change not advertised after 100 ms")
	}
	s.now = s.now.Add(4 * time.Second)
	if s.ownJSON(1000) != nil {
		t.Fatal("advertised without a change within 5 s")
	}
	s.now = s.now.Add(time.Second)
	if s.ownJSON(1000) == nil {
		t.Fatal("not advertised after 5 s")
	}
	// Too large for the channel: routes are left out, then nothing goes.
	s.now = s.now.Add(10 * time.Second)
	full := s.ownJSON(1000)
	s.now = s.now.Add(10 * time.Second)
	short := s.ownJSON(len(full) - 1)
	if short == nil || strings.Contains(string(short), `"r"`) {
		t.Fatalf("budget %d: %s", len(full)-1, short)
	}
	s.now = s.now.Add(10 * time.Second)
	if s.ownJSON(10) != nil || !slices.ContainsFunc(s.ev, func(e string) bool { return strings.HasPrefix(e, "routing.error") }) {
		t.Fatal("an advertisement over the budget")
	}
}

// TestFourRelaysNoLoops: four relays in a ring with a diagonal and
// different delays. Links fail, flap and come back while every relay's
// next hops are followed every 10 ms: no loop ever, and once settled
// every pair uses one path both ways.
func TestFourRelaysNoLoops(t *testing.T) {
	links := map[string]time.Duration{"A-B": 20 * time.Millisecond, "B-C": 5 * time.Millisecond, "C-D": 30 * time.Millisecond, "A-D": 10 * time.Millisecond, "B-D": 40 * time.Millisecond}
	rtt := map[string]float64{"A-B": 40, "B-C": 10, "C-D": 60, "A-D": 20, "B-D": 80}
	n := newNet(t, links, nil)
	down := map[string]bool{}
	names := []string{"A", "B", "C", "D"}
	loops, steps := 0, 0
	check := func() {
		steps++
		for _, a := range names {
			for _, b := range names {
				if a == b {
					continue
				}
				if p, ok := n.path(a, b); !ok && len(p) > 1 && p[len(p)-1] != "" {
					loops++
					t.Errorf("loop %s at %v", strings.Join(p, ">"), n.now)
				}
			}
		}
	}
	second := func() {
		for l, v := range rtt {
			a, b, _ := strings.Cut(l, "-")
			n.cut(a, b, down[l])
			if down[l] {
				n.probe(a, b, -1)
			} else {
				n.probe(a, b, v)
			}
		}
		n.run(time.Second, check)
	}
	settled := func(what string) {
		for _, a := range names {
			for _, b := range names {
				if a == b {
					continue
				}
				there, ok := n.path(a, b)
				back, _ := n.path(b, a)
				slices.Reverse(back)
				if !ok || !slices.Equal(there, back) {
					t.Fatalf("%s: %s to %s along %v, back along %v", what, a, b, there, back)
				}
			}
		}
	}
	for i := 0; i < 10; i++ {
		second()
	}
	settled("start")
	if p, _ := n.path("A", "C"); !slices.Equal(p, []string{"A", "B", "C"}) {
		t.Fatal("A to C along", p)
	}
	for _, f := range []struct {
		link    string
		seconds int
		flap    bool
	}{{"B-C", 20, false}, {"A-B", 20, false}, {"A-D", 30, true}, {"B-C", 30, true}} {
		for i := 0; i < f.seconds; i++ {
			down[f.link] = !f.flap || (i/2)%2 == 0
			second()
		}
		down[f.link] = false
		for i := 0; i < 900; i++ { // past 100-probe loss window, suppression, and cascaded 60s smoothing
			for l, v := range rtt {
				a, b, _ := strings.Cut(l, "-")
				n.cut(a, b, false)
				n.probe(a, b, v)
			}
			n.runFastSecond()
			check()
		}
		settled(f.link + " back")
	}
	if p, _ := n.path("A", "C"); !slices.Equal(p, []string{"A", "B", "C"}) {
		t.Fatal("A to C along", p)
	}
	t.Logf("%d steps of 10 ms, %d loops; events at A: %s", steps, loops, fmt.Sprint(n.eventsOf("A", "route.changed")))
}

// TestNoMicroloopWhileConverging: B-D fails; A hears of it from B within
// 1 ms and C only 30 ms later. A's new shortest path is through C, which
// until then still routes through A: plain shortest-path routing would
// loop A>C>A for those 30 ms. A waits instead, then takes C.
func TestNoMicroloopWhileConverging(t *testing.T) {
	n := newNet(t, map[string]time.Duration{"A-B": time.Millisecond, "B-D": time.Millisecond, "A-C": 30 * time.Millisecond, "C-D": 30 * time.Millisecond}, nil)
	rtt := map[string]float64{"A-B": 10, "B-D": 10, "A-C": 10, "C-D": 50}
	down := ""
	loops := 0
	held := time.Duration(0)
	check := func() {
		for _, a := range []string{"A", "B", "C"} {
			if p, ok := n.path(a, "D"); !ok {
				if p[len(p)-1] != "" {
					loops++
				} else if down != "" {
					held += 10 * time.Millisecond
				}
			}
		}
	}
	second := func() {
		for l, v := range rtt {
			a, b, _ := strings.Cut(l, "-")
			n.cut(a, b, l == down)
			if l == down {
				n.probe(a, b, -1)
			} else {
				n.probe(a, b, v)
			}
		}
		n.run(time.Second, check)
	}
	for i := 0; i < 400; i++ {
		for l, v := range rtt {
			a, b, _ := strings.Cut(l, "-")
			n.cut(a, b, false)
			n.probe(a, b, v)
		}
		if i < 5 {
			n.run(time.Second, check)
		} else {
			n.runFastSecond()
			check()
		}
		if p, _ := n.path("C", "D"); slices.Equal(p, []string{"C", "A", "B", "D"}) {
			break
		}
	}
	if p, _ := n.path("C", "D"); !slices.Equal(p, []string{"C", "A", "B", "D"}) {
		t.Fatal("C to D along", p)
	}
	down = "B-D"
	for i := 0; i < 5; i++ {
		second()
	}
	if p, _ := n.path("A", "D"); !slices.Equal(p, []string{"A", "C", "D"}) {
		t.Fatal("A to D along", p)
	}
	if loops > 0 {
		t.Fatalf("%d relay-steps on a loop", loops)
	}
	t.Logf("after B-D went down, A, B and C were without a route for %v in all", held)
}

// TestForwardedAdvertKeepsRoutes: T forwards B's advertisement to H before
// B's own copy arrives. It must keep B's route to T: without it H took B
// for not routing to T at all, and B, once it moved its route to T onto
// H, found that path broken and moved back at once (reason down), as the
// relays did whenever one end of a long link lost a probe or two.
func TestForwardedAdvertKeepsRoutes(t *testing.T) {
	n := newNet(t, map[string]time.Duration{"T-B": 79500 * time.Microsecond, "T-H": 1250 * time.Microsecond, "H-B": 87500 * time.Microsecond},
		map[string]string{"T>B": "B", "T>H": "H", "B>T": "T", "B>H": "H", "H>T": "T", "H>B": "B"})
	for sec := 0; sec < 120; sec++ {
		if sec == 60 || sec == 61 { // B alone loses two probes of T
			n.r["B"].Lost("T")
		} else {
			n.r["B"].Probe("T", 159*time.Millisecond)
		}
		n.r["T"].Probe("B", 159*time.Millisecond)
		n.probe("H", "B", 175)
		n.probe("T", "H", 2.5)
		n.run(time.Second, nil)
		if sec == 30 {
			for _, x := range []string{"T", "H", "B"} {
				for o, a := range n.r[x].lsdb {
					if len(a.routes) != 2 {
						t.Fatalf("%s holds %s's advertisement with routes %v", x, o, a.routes)
					}
				}
			}
		}
	}
	for _, e := range n.eventsOf("B", "route.changed") {
		if strings.HasSuffix(e, "(down)") {
			t.Fatalf("B's route broke without a link down: %v", n.eventsOf("B", "route.changed"))
		}
	}
	t.Logf("B: %v", n.eventsOf("B", "route.changed"))
}

// TestLeafRouting checks that:
//  1. Leaves are never used as transit hops in Dijkstra even if a path through a leaf is cheaper.
//  2. Relays advertise links to leaves in "m" (and compact section 2) and route to leaves via the cheapest relay.
//  3. A leaf router (Leaf: true) never sends or forwards advertisements, receives relay advertisements,
//     and picks the upstream relay (or direct member link) with the shortest path to each destination.
func TestLeafRouting(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	ms := func(v float64) time.Duration { return time.Duration(v * float64(time.Millisecond)) }
	nums := map[string]uint32{"A": 1, "B": 2, "M": 10, "N": 11}
	relayLinks := [][2]string{{"A", "B"}}
	memberLinks := [][2]string{{"A", "M"}, {"B", "M"}, {"A", "N"}, {"B", "N"}}

	rA := New(signed(Config{
		Self: "A", Relays: []string{"B"}, Leaves: []string{"M", "N"},
		Links: []string{"B", "M", "N"}, Numbers: nums,
		RelayLinks: relayLinks, MemberLinks: memberLinks,
	}), func() time.Time { return now }, nil)

	rB := New(signed(Config{
		Self: "B", Relays: []string{"A"}, Leaves: []string{"M", "N"},
		Links: []string{"A", "M", "N"}, Numbers: nums,
		RelayLinks: relayLinks, MemberLinks: memberLinks,
	}), func() time.Time { return now }, nil)

	rM := New(signed(Config{
		Self: "M", Leaf: true, Relays: []string{"A", "B"}, Leaves: []string{"N"},
		Links: []string{"A", "B"}, Numbers: nums,
		RelayLinks: relayLinks, MemberLinks: memberLinks,
	}), func() time.Time { return now }, nil)

	// Warm up links:
	// A-B is 100ms, while A-M is 5ms and B-M is 5ms (path A-M-B would be 10ms if M could transit, which it must not).
	// A-N is 80ms, B-N is 10ms.
	for i := 0; i < warmSamples; i++ {
		rA.Probe("B", ms(100))
		rA.Probe("M", ms(5))
		rA.Probe("N", ms(80))
		rB.Probe("A", ms(100))
		rB.Probe("M", ms(5))
		rB.Probe("N", ms(10))
		rM.Probe("A", ms(5))
		rM.Probe("B", ms(5))
	}

	// Leaf router must never emit advertisements.
	if msgs := rM.Advert(Budget{JSON: 1000, Compact: 1000}, []string{"A", "B"}); len(msgs) != 0 {
		t.Fatalf("leaf emitted adverts: %+v", msgs)
	}

	// Exchange advertisements between A, B, and M.
	advA := rA.Advert(Budget{JSON: 1000, Compact: 1000}, []string{"B", "M"})
	advB := rB.Advert(Budget{JSON: 1000, Compact: 1000}, []string{"A", "M"})
	if len(advA) == 0 || len(advB) == 0 {
		t.Fatalf("expected relay adverts, got A=%d B=%d", len(advA), len(advB))
	}
	if _, _, err := rB.Receive("A", advA[0], Budget{JSON: 1000, Compact: 1000}); err != nil {
		t.Fatalf("rB receive A: %v", err)
	}
	if _, _, err := rA.Receive("B", advB[0], Budget{JSON: 1000, Compact: 1000}); err != nil {
		t.Fatalf("rA receive B: %v", err)
	}
	if fwd, _, err := rM.Receive("A", advA[0], Budget{JSON: 1000, Compact: 1000}); err != nil || len(fwd) != 0 {
		t.Fatalf("rM receive A: fwd=%v err=%v", fwd, err)
	}
	if fwd, _, err := rM.Receive("B", advB[0], Budget{JSON: 1000, Compact: 1000}); err != nil || len(fwd) != 0 {
		t.Fatalf("rM receive B: fwd=%v err=%v", fwd, err)
	}

	// 1. A->B must go directly to B (100ms), never through leaf M (5+5=10ms).
	if nh, ok := rA.NextHop("B"); !ok || nh != "B" {
		t.Fatalf("rA NextHop(B) = %q, %v; want B", nh, ok)
	}
	// 2. A->N: direct A-N is 80ms, via B is 100+10=110ms -> A chooses N directly.
	if nh, ok := rA.NextHop("N"); !ok || nh != "N" {
		t.Fatalf("rA NextHop(N) = %q, %v; want N", nh, ok)
	}
	// Make A-B 20ms so A-B-N is 20+10 < 80ms; advance time so smoothed costs settle.
	for range 360 {
		now = now.Add(time.Second)
		rA.Probe("B", ms(20))
		rB.Probe("A", ms(20))
		rA.Probe("N", ms(80))
		rA.Probe("M", ms(5))
		rB.Probe("N", ms(10))
		rB.Probe("M", ms(5))
		rA.Tick()
		rB.Tick()
		rM.Tick()
		for _, m := range rA.Advert(Budget{JSON: 1000, Compact: 1000}, []string{"B"}) {
			rB.Receive("A", m, Budget{JSON: 1000, Compact: 1000})
			rM.Receive("A", m, Budget{JSON: 1000, Compact: 1000})
		}
		for _, m := range rB.Advert(Budget{JSON: 1000, Compact: 1000}, []string{"A"}) {
			rA.Receive("B", m, Budget{JSON: 1000, Compact: 1000})
			rM.Receive("B", m, Budget{JSON: 1000, Compact: 1000})
		}
	}

	// Now A->N should route via B (20+10=30ms vs 80ms).
	if nh, ok := rA.NextHop("N"); !ok || nh != "B" {
		t.Fatalf("rA NextHop(N) = %q, %v; want B", nh, ok)
	}
	// 3. M->N: via A is 5+80=85ms (or 5+20+10=35ms), via B is 5+10=15ms -> M chooses B!
	if nh, ok := rM.NextHop("N"); !ok || nh != "B" {
		t.Fatalf("rM NextHop(N) = %q, %v; want B", nh, ok)
	}
	if nh, ok := rM.NextHop("A"); !ok || nh != "A" {
		t.Fatalf("rM NextHop(A) = %q, %v; want A", nh, ok)
	}
	if nh, ok := rM.NextHop("B"); !ok || nh != "B" {
		t.Fatalf("rM NextHop(B) = %q, %v; want B", nh, ok)
	}
}

// TestAdvertBurstOfAllRelays: a link going down has every relay advertise
// at once, as often as advertGap lets it, and each neighbor forwards them
// all. Forty origins for three seconds, each advertisement through both of
// A's neighbors: A takes each the first time, drops none, and ends on every
// origin's last.
func TestAdvertBurstOfAllRelays(t *testing.T) {
	relays := []string{"B", "D"}
	for i := 0; i < 40; i++ {
		relays = append(relays, fmt.Sprintf("O%02d", i))
	}
	s := newSolo(t, relays, []string{"B", "D"}, nil)
	s.up("B", 10)
	s.up("D", 10)
	for step := 0; step < 30; step++ {
		for _, o := range relays[2:] {
			s.seq[o]++
			raw := []byte(fmt.Sprintf(`{"o":%q,"s":%d,"l":[["B",%d,"u"]]}`, o, s.seq[o], 10+step))
			for _, from := range []string{"B", "D"} {
				if _, _, err := s.recv(from, raw); err != nil {
					t.Fatalf("step %d, %s through %s: %v", step, o, from, err)
				}
			}
		}
		s.now = s.now.Add(advertGap)
	}
	if d := s.r.Status().Drops; d != (AdvertDrops{}) {
		t.Fatalf("drops %+v", d)
	}
	for _, o := range relays[2:] {
		if a := s.r.lsdb[o]; a == nil || a.seq != s.seq[o] {
			t.Fatalf("%s holds %v, want seq %d", o, a, s.seq[o])
		}
	}
}
