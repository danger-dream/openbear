package routing

import (
	"bytes"
	"fmt"
	"slices"
	"testing"
	"time"
)

// TestLeavesNeverTransit checks that a member (leaf) is never used as a
// transit hop between relays or other members, even when its links would
// form a cheaper path, while relays and members still route to it along the
// shortest relay path.
func TestLeavesNeverTransit(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	relays := []string{"A", "B"}
	leaves := []string{"M1", "M2"}
	nums := map[string]uint32{"A": 1, "B": 2, "M1": 11, "M2": 12}
	relayLinks := [][2]string{{"A", "B"}}
	memberLinks := [][2]string{{"A", "M1"}, {"B", "M1"}, {"B", "M2"}}

	cfgFor := func(self string, leaf bool, links []string) Config {
		var r, l []string
		for _, x := range relays {
			if x != self {
				r = append(r, x)
			}
		}
		for _, x := range leaves {
			if x != self {
				l = append(l, x)
			}
		}
		return signed(Config{
			Self:        self,
			Leaf:        leaf,
			Relays:      r,
			Leaves:      l,
			Links:       links,
			Numbers:     nums,
			RelayLinks:  relayLinks,
			MemberLinks: memberLinks,
		})
	}

	rA := New(cfgFor("A", false, []string{"B", "M1"}), func() time.Time { return now }, nil)
	rB := New(cfgFor("B", false, []string{"A", "M1", "M2"}), func() time.Time { return now }, nil)
	rM1 := New(cfgFor("M1", true, []string{"A", "B"}), func() time.Time { return now }, nil)

	// A-B is 200ms, while A-M1 is 5ms and B-M1 is 5ms.
	// If M1 were a relay, A->B would go A->M1->B (10ms). As a leaf, A->B must stay A->B.
	for range warmSamples {
		rA.Probe("B", 200*time.Millisecond)
		rB.Probe("A", 200*time.Millisecond)
		rA.Probe("M1", 5*time.Millisecond)
		rM1.Probe("A", 5*time.Millisecond)
		rB.Probe("M1", 50*time.Millisecond)
		rM1.Probe("B", 50*time.Millisecond)
		rB.Probe("M2", 10*time.Millisecond)
	}

	budget := Budget{JSON: 1000, Compact: 1000}
	// Leaf M1 never emits advertisements or syncs.
	if msgs := rM1.Advert(budget, []string{"A", "B"}); len(msgs) != 0 {
		t.Fatalf("leaf M1 sent advertisements: %+v", msgs)
	}
	if msgs := rM1.Sync(budget); len(msgs) != 0 {
		t.Fatalf("leaf M1 synced advertisements: %+v", msgs)
	}

	// Relays A and B exchange advertisements; M1 receives them too.
	advA := rA.Advert(budget, []string{"B", "M1"})
	advB := rB.Advert(budget, []string{"A", "M1", "M2"})
	for _, m := range advA {
		if m.Peer == "B" {
			if _, _, err := rB.Receive("A", m, budget); err != nil {
				t.Fatal(err)
			}
		}
	}
	// M1 receives both A's and B's advertisements from A/B and never forwards them.
	if fwd, _, err := rM1.Receive("A", advA[0], budget); err != nil || len(fwd) != 0 {
		t.Fatalf("M1 receive A: fwd=%v err=%v", fwd, err)
	}
	if fwd, _, err := rM1.Receive("B", advB[0], budget); err != nil || len(fwd) != 0 {
		t.Fatalf("M1 receive B: fwd=%v err=%v", fwd, err)
	}
	for _, m := range advB {
		if m.Peer == "A" {
			if _, _, err := rA.Receive("B", m, budget); err != nil {
				t.Fatal(err)
			}
		}
	}

	// A routes to B directly (200ms), never through M1 (5+50=55ms).
	if nh, ok := rA.NextHop("B"); !ok || nh != "B" {
		t.Fatalf("A -> B nextHop = %q, want B", nh)
	}
	// A routes to M1 directly (5ms vs 200+50=250ms).
	if nh, ok := rA.NextHop("M1"); !ok || nh != "M1" {
		t.Fatalf("A -> M1 nextHop = %q, want M1", nh)
	}
	// B routes to M1 directly (50ms vs 200+5=205ms).
	if nh, ok := rB.NextHop("M1"); !ok || nh != "M1" {
		t.Fatalf("B -> M1 nextHop = %q, want M1", nh)
	}
	// A routes to M2 via B.
	if nh, ok := rA.NextHop("M2"); !ok || nh != "B" {
		t.Fatalf("A -> M2 nextHop = %q, want B", nh)
	}
	// M1 routes to M2 via B (50+10=60ms vs via A: 5+200+10=215ms).
	if nh, ok := rM1.NextHop("M2"); !ok || nh != "B" {
		t.Fatalf("M1 -> M2 nextHop = %q, want B", nh)
	}
	// M1 routes to A via A (5ms) and to B via B (50ms).
	if nh, ok := rM1.NextHop("A"); !ok || nh != "A" {
		t.Fatalf("M1 -> A nextHop = %q, want A", nh)
	}
	if nh, ok := rM1.NextHop("B"); !ok || nh != "B" {
		t.Fatalf("M1 -> B nextHop = %q, want B", nh)
	}

	// Now B-M1 becomes very slow or A-B becomes very fast (10ms): B->M1 should go via A (10+5=15ms < 50ms).
	now = now.Add(10 * time.Second)
	for range 6 {
		rA.Probe("B", 10*time.Millisecond)
		rB.Probe("A", 10*time.Millisecond)
	}
	now = now.Add(switchHold + time.Second)
	rA.Tick()
	rB.Tick()
	for _, m := range rA.Advert(budget, []string{"B"}) {
		rB.Receive("A", m, budget)
	}
	for _, m := range rB.Advert(budget, []string{"A"}) {
		rA.Receive("B", m, budget)
	}
	if nh, ok := rB.NextHop("M1"); !ok || nh != "A" {
		t.Fatalf("B -> M1 after A-B fast: nextHop = %q, want A", nh)
	}
	if nh, ok := rM1.NextHop("B"); ok && nh != "B" {
		// M1 has not received the new adverts yet.
	}
	for _, m := range rA.Advert(budget, []string{"B"}) {
		rM1.Receive("A", m, budget)
	}
	now = now.Add(switchHold + time.Second)
	for _, m := range rA.Advert(budget, []string{"B"}) {
		rM1.Receive("A", m, budget)
	}
	for _, m := range rB.Advert(budget, []string{"A"}) {
		rM1.Receive("B", m, budget)
	}
	if nh, ok := rM1.NextHop("B"); !ok || nh != "A" {
		t.Fatalf("M1 -> B after A-B fast: nextHop = %q, want A", nh)
	}
}

// TestOldNodeIgnoresMemberFields verifies that JSON and compact advertisements
// containing member links are accepted by a decoder that knows only relays
// (or an old decoder without member knowledge), ignoring the member links.
func TestOldNodeIgnoresMemberFields(t *testing.T) {
	a := &advert{
		origin:  "A",
		seq:     42,
		links:   map[string]advLink{"B": {12.5, StateUp}},
		members: map[string]advLink{"M1": {5.0, StateUp}, "M2": {0, StateDown}},
		routes:  map[string]string{"B": "B"},
	}
	order := []string{"A", "B"}
	rawJSON := encodeJSON(a, order, true)
	if !bytes.Contains(rawJSON, []byte(`"m":[["M1",5,"u"],["M2",0,"d"]]`)) {
		t.Fatalf("JSON lacks member field: %s", rawJSON)
	}
	// A router that only knows relays A and B decodes JSON and ignores M1, M2.
	decJSON, err := decodeJSON(rawJSON, map[string]bool{"A": true, "B": true}, nil)
	if err != nil {
		t.Fatalf("decodeJSON without leaves failed: %v", err)
	}
	if len(decJSON.links) != 1 || len(decJSON.members) != 0 {
		t.Fatalf("decoded JSON: links=%v members=%v", decJSON.links, decJSON.members)
	}
	// A router that knows M1 and M2 decodes them.
	decJSON2, err := decodeJSON(rawJSON, map[string]bool{"A": true, "B": true}, map[string]bool{"M1": true, "M2": true})
	if err != nil || len(decJSON2.members) != 2 || decJSON2.members["M1"].cost != 5.0 {
		t.Fatalf("decodeJSON with leaves: %+v %v", decJSON2, err)
	}

	num := map[string]uint32{"A": 1, "B": 2, "M1": 10, "M2": 11}
	ids := map[uint32]string{1: "A", 2: "B", 10: "M1", 11: "M2"}
	rawCompact, ok := encodeCompact(a, num, order, true)
	if !ok {
		t.Fatal("encodeCompact failed")
	}
	// Decoder that only knows relays A and B (ids without M1/M2, leaves nil):
	decCompactOld, err := decodeCompact(rawCompact, map[uint32]string{1: "A", 2: "B"}, map[string]bool{"A": true, "B": true}, nil)
	if err != nil || len(decCompactOld.links) != 1 || len(decCompactOld.members) != 0 {
		t.Fatalf("decodeCompact without leaves: %+v %v", decCompactOld, err)
	}
	// Decoder that knows M1 and M2:
	decCompactNew, err := decodeCompact(rawCompact, ids, map[string]bool{"A": true, "B": true}, map[string]bool{"M1": true, "M2": true})
	if err != nil || len(decCompactNew.members) != 2 || decCompactNew.members["M1"].cost != 5.0 || decCompactNew.members["M2"].state != StateDown {
		t.Fatalf("decodeCompact with leaves: %+v %v", decCompactNew, err)
	}
}

// TestScale200Nodes1000Links checks advertisement sizes and convergence at
// the target scale of 200 nodes (e.g. 30 relays + 170 members) and 1000 links.
func TestScale200Nodes1000Links(t *testing.T) {
	const numRelays = 30
	const numMembers = 170
	relays := make([]string, numRelays)
	members := make([]string, numMembers)
	numbers := map[string]uint32{}
	for i := range relays {
		relays[i] = fmt.Sprintf("r-%02d", i)
		numbers[relays[i]] = uint32(i + 1)
	}
	for i := range members {
		members[i] = fmt.Sprintf("m-%03d", i)
		numbers[members[i]] = uint32(100 + i)
	}
	// 30 relays: each connected to ~20 relays (300 relay links) + ~24 members (700 member links) = 1000 links.
	var myLinks []string
	for i := 1; i <= 20; i++ {
		myLinks = append(myLinks, relays[i])
	}
	for i := 0; i < 24; i++ {
		myLinks = append(myLinks, members[i])
	}
	now := time.Unix(1_800_000_000, 0)
	r := New(signed(Config{
		Self:    relays[0],
		Relays:  relays[1:],
		Leaves:  members,
		Links:   myLinks,
		Numbers: numbers,
	}), func() time.Time { return now }, nil)
	for i, p := range myLinks {
		for range warmSamples {
			r.Probe(p, time.Duration(10+i)*time.Millisecond)
		}
	}
	msgs := r.Advert(Budget{JSON: compactBudget, Compact: compactBudget}, relays[1:2])
	if len(msgs) != 1 || msgs[0].Format != FormatCompact {
		t.Fatalf("expected 1 compact message, got %+v", msgs)
	}
	if len(msgs[0].Raw) > compactBudget {
		t.Fatalf("compact advert %d bytes exceeds budget %d", len(msgs[0].Raw), compactBudget)
	}
	dec, err := decodeCompact(msgs[0].Raw, r.ids, r.known, r.leaves)
	if err != nil {
		t.Fatal(err)
	}
	if len(dec.links) != 20 || len(dec.members) != 24 {
		t.Fatalf("decoded %d relay links, %d member links", len(dec.links), len(dec.members))
	}
	// Members never appear in routes ("r").
	for d := range dec.routes {
		if slices.Contains(members, d) {
			t.Fatalf("member %s appeared in advertised routes", d)
		}
	}
	t.Logf("200 nodes / 1000 links per-relay compact advert (20 relay links + 24 member links + 29 relay routes): %d / %d bytes", len(msgs[0].Raw), compactBudget)
}

// TestLeafNewDirectLinkBreaksNeighborLoop reproduces a multi-region scenario
// (Relay-SG1, Relay-SG2, Berlin, leaf Leaf-EU): when Relay-SG1 initially
// routes to Leaf-EU via Relay-SG2->Berlin (214ms), and a direct link
// Relay-SG1<->Leaf-EU (163ms) comes up, Relay-SG2's shortest path to Leaf-EU
// now traverses Relay-SG1 (2+163=165ms). Relay-SG1 must detect that Relay-SG2's path to
// the leaf loops back through Relay-SG1 and switch to direct Leaf-EU at once.
func TestLeafNewDirectLinkBreaksNeighborLoop(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	relays := []string{"Relay-SG1", "Relay-SG2", "Berlin"}
	leaves := []string{"Leaf-EU"}
	nums := map[string]uint32{"Relay-SG1": 1, "Relay-SG2": 2, "Berlin": 3, "Leaf-EU": 10}
	relayLinks := [][2]string{{"Relay-SG1", "Relay-SG2"}, {"Berlin", "Relay-SG2"}}
	memberLinks := [][2]string{{"Relay-SG1", "Leaf-EU"}, {"Berlin", "Leaf-EU"}}

	cfgFor := func(self string, links []string) Config {
		var r []string
		for _, x := range relays {
			if x != self {
				r = append(r, x)
			}
		}
		return signed(Config{
			Self:        self,
			Relays:      r,
			Leaves:      leaves,
			Links:       links,
			Numbers:     nums,
			RelayLinks:  relayLinks,
			MemberLinks: memberLinks,
		})
	}
	rAws := New(cfgFor("Relay-SG1", []string{"Relay-SG2", "Leaf-EU"}), func() time.Time { return now }, nil)
	rGC := New(cfgFor("Relay-SG2", []string{"Relay-SG1", "Berlin"}), func() time.Time { return now }, nil)
	rBer := New(cfgFor("Berlin", []string{"Relay-SG2", "Leaf-EU"}), func() time.Time { return now }, nil)

	// Phase 1: Relay-SG1<->Relay-SG2 (2ms), Relay-SG2<->Berlin (164ms), Berlin<->Leaf-EU (48ms). Relay-SG1<->Leaf-EU not up yet.
	for range warmSamples {
		rAws.Probe("Relay-SG2", 2*time.Millisecond)
		rGC.Probe("Relay-SG1", 2*time.Millisecond)
		rGC.Probe("Berlin", 164*time.Millisecond)
		rBer.Probe("Relay-SG2", 164*time.Millisecond)
		rBer.Probe("Leaf-EU", 48*time.Millisecond)
	}
	budget := Budget{JSON: 1000, Compact: 1000}
	for _, msg := range rBer.Advert(budget, []string{"Relay-SG2"}) {
		fwd, _, _ := rGC.Receive("Berlin", msg, budget)
		for _, fm := range fwd {
			if fm.Peer == "Relay-SG1" {
				rAws.Receive("Relay-SG2", fm, budget)
			}
		}
	}
	for _, msg := range rGC.Advert(budget, []string{"Relay-SG1", "Berlin"}) {
		if msg.Peer == "Relay-SG1" {
			rAws.Receive("Relay-SG2", msg, budget)
		}
	}
	if got, _ := rAws.NextHop("Leaf-EU"); got != "Relay-SG2" {
		t.Fatalf("phase 1: Relay-SG1 nextHop to Leaf-EU = %q, want Relay-SG2", got)
	}

	// Phase 2: Relay-SG1<->Leaf-EU (163ms) comes up!
	now = now.Add(time.Second)
	for range warmSamples {
		rAws.Probe("Leaf-EU", 163*time.Millisecond)
	}
	// Relay-SG1 must switch to direct Leaf-EU immediately because Relay-SG2's shortest path to Leaf-EU now loops via Relay-SG1.
	if got, _ := rAws.NextHop("Leaf-EU"); got != "Leaf-EU" {
		t.Fatalf("phase 2: Relay-SG1 nextHop to Leaf-EU = %q, want Leaf-EU (no loop with Relay-SG2)", got)
	}
	st := rAws.Status()
	for _, rt := range st.Routes {
		if rt.Dest == "Leaf-EU" && !slices.Equal(rt.Path, []string{"Relay-SG1", "Leaf-EU"}) {
			t.Fatalf("Relay-SG1 path to Leaf-EU = %v, want [Relay-SG1 Leaf-EU]", rt.Path)
		}
	}
}
