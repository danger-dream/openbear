package routing

import (
	"fmt"
	"hash/fnv"
	"math/rand/v2"
	"slices"
	"testing"
	"time"
)

// memberNet is a testNet whose relays also route to member M, linked to the
// relays in homes. rtt gives every link's round trip, relays' and members'.
func memberNet(t *testing.T, rtt map[string]float64, homes []string) *testNet {
	links := map[string]time.Duration{}
	for l, v := range rtt {
		if a, b := l[:1], l[2:]; a != "M" && b != "M" {
			links[l] = time.Duration(v / 2 * float64(time.Millisecond))
		}
	}
	n := newNet(t, links, nil)
	for _, name := range n.names {
		n.r[name].Configure(memberConfig(n, name, homes))
	}
	return n
}

func memberConfig(n *testNet, name string, homes []string) Config {
	return n.config(name, func(c *Config) {
		c.Leaves = []string{"M"}
		c.Numbers["M"] = 100
		for _, h := range homes {
			c.MemberLinks = append(c.MemberLinks, [2]string{h, "M"})
			if h == name {
				c.Links = append(c.Links, "M")
			}
		}
	})
}

// memberProbes gives every link a probe, its rtt with up to 3% of noise; the
// links of a relay in down are lost.
func memberProbes(n *testNet, rtt map[string]float64, down map[string]bool, rng *rand.Rand) {
	for l, v := range rtt {
		a, b := l[:1], l[2:]
		lost := down[a] || down[b]
		v *= 1 + 0.03*rng.Float64()
		for _, x := range [][2]string{{a, b}, {b, a}} {
			if x[0] == "M" {
				continue
			}
			if lost {
				n.r[x[0]].Lost(x[1])
			} else {
				n.r[x[0]].Probe(x[1], time.Duration(v*float64(time.Millisecond)))
			}
		}
	}
}

// memberLoop is a cycle of next hops to M, nil when there is none.
func memberLoop(n *testNet) []string {
	for _, src := range n.names {
		seen := map[string]int{}
		var path []string
		for x := src; x != "M"; {
			if i, ok := seen[x]; ok {
				return path[i:]
			}
			seen[x] = len(path)
			path = append(path, x)
			nh, ok := n.r[x].NextHop("M")
			if !ok || nh == "" {
				break
			}
			x = nh
		}
	}
	return nil
}

// TestMemberRoutesRollingRestart is the loop of 2026-11-21 on node
// 1.4.1-dev.5: GreenCloud-SG, Po0-HK, HyperPods-HK and GCP-SG (G, P, H, S)
// around JCloud-ECO (M) behind Berlin (B), its other upstream Seattle (K).
// Relays restarted one after the other came back with their direct links to
// B settling for minutes, and held routes to M that were not their shortest;
// the others, taking every relay to use its shortest path to a member, went
// through them in a circle. With the next hops to members advertised there
// is no loop at any time; taken as shortest paths, six of these seven seeds
// looped, the first as G P H S.
func TestMemberRoutesRollingRestart(t *testing.T) {
	rtt := map[string]float64{"G-P": 38.9, "P-H": 1.8, "H-S": 37.3, "S-G": 2.4, "G-B": 199.7, "H-B": 187.7, "P-B": 205, "S-B": 212, "B-M": 6,
		"G-K": 170, "H-K": 158, "S-K": 175, "B-K": 150, "K-M": 151}
	homes := []string{"B", "K"}
	run := func(seed uint64) (loops int, first []string) {
		n := memberNet(t, rtt, homes)
		h := fnv.New64a()
		fmt.Fprint(h, seed)
		rng := rand.New(rand.NewPCG(seed, h.Sum64()))
		down := map[string]bool{}
		probeFor := func(d time.Duration, check bool) {
			for end := n.now.Add(d); n.now.Before(end); {
				memberProbes(n, rtt, down, rng)
				for range 10 {
					n.run(100*time.Millisecond, nil)
					if !check {
						continue
					}
					if c := memberLoop(n); c != nil {
						if loops == 0 {
							first = c
						}
						loops++
					}
				}
			}
		}
		probeFor(3*time.Minute, false)
		if c := memberLoop(n); c != nil {
			t.Fatalf("seed %d: settled with a loop %v", seed, c)
		}
		order := slices.Clone(n.names)
		rng.Shuffle(len(order), func(i, j int) { order[i], order[j] = order[j], order[i] })
		for _, name := range order {
			// A restart: a new router, its links down for a few seconds.
			down[name] = true
			n.r[name] = New(memberConfig(n, name, homes), func() time.Time { return n.now }, nil)
			n.r[name].setRandSeed(rng.Uint64(), rng.Uint64())
			probeFor(time.Duration(3+rng.IntN(5))*time.Second, true)
			down[name] = false
			probeFor(time.Duration(10+rng.IntN(20))*time.Second, true)
		}
		probeFor(4*time.Minute, true)
		return loops, first
	}
	seeds := uint64(7)
	if raceEnabled {
		seeds = 1
	}
	for seed := range seeds {
		if loops, c := run(seed); loops > 0 {
			t.Errorf("seed %d: %d checks in a loop, first %v", seed, loops, c)
		}
	}
}

// TestAdvertMemberRoutesGoFirstOverBudget: an advertisement carries the next
// hops to members; over budget it leaves those out first, then every route.
func TestAdvertMemberRoutesGoFirstOverBudget(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	nums := map[string]uint32{"A": 1, "B": 2, "C": 3, "M": 10}
	relayLinks := [][2]string{{"A", "B"}, {"B", "C"}}
	cfg := func(self string, links []string) Config {
		var relays []string
		for _, x := range []string{"A", "B", "C"} {
			if x != self {
				relays = append(relays, x)
			}
		}
		return signed(Config{Self: self, Relays: relays, Leaves: []string{"M"}, Links: links, Numbers: nums, RelayLinks: relayLinks,
			MemberLinks: [][2]string{{"A", "M"}}})
	}
	rA := New(cfg("A", []string{"B", "M"}), func() time.Time { return now }, nil)
	rB := New(cfg("B", []string{"A", "C"}), func() time.Time { return now }, nil)
	rC := New(cfg("C", []string{"B"}), func() time.Time { return now }, nil)
	for range warmSamples {
		rA.Probe("B", 10*time.Millisecond)
		rA.Probe("M", 5*time.Millisecond)
		rB.Probe("A", 10*time.Millisecond)
		rB.Probe("C", 10*time.Millisecond)
		rC.Probe("B", 10*time.Millisecond)
	}
	big := Budget{JSON: 1000, Compact: 1000}
	for _, m := range rC.Advert(big, []string{"B"}) {
		rB.Receive("C", m, big)
	}
	for _, m := range rB.Advert(big, []string{"A"}) {
		if _, _, err := rA.Receive("B", m, big); err != nil {
			t.Fatal(err)
		}
	}
	advert := func(b Budget) *advert {
		t.Helper()
		rA.sent = time.Time{}
		msgs := rA.Advert(b, []string{"B"})
		if len(msgs) == 0 {
			t.Fatalf("budget %+v: no advertisement", b)
		}
		a, err := decodeCompact(msgs[0].Raw, rB.ids, rB.known, rB.leaves)
		if err != nil {
			t.Fatal(err)
		}
		return a
	}
	full := advert(big)
	if full.routes["M"] != "M" || full.routes["C"] != "B" {
		t.Fatalf("routes %v, want M direct and C through B", full.routes)
	}
	rA.sent = time.Time{}
	size := len(rA.Advert(big, []string{"B"})[0].Raw)
	if a := advert(Budget{JSON: 1000, Compact: size - 1}); a.routes == nil || a.routes["C"] != "B" || a.routes["M"] != "" {
		t.Fatalf("one byte short: routes %v, want C's alone", a.routes)
	}
	if a := advert(Budget{JSON: 1000, Compact: size - 8}); a.routes != nil {
		t.Fatalf("eight bytes short: routes %v, want none", a.routes)
	}
}

// TestMemberPathFollowsAdvertisedNextHops: the path a relay reports to a
// member, the one its status, the controller's "now through" and fast paths
// show, goes on as the relays on the way advertise their next hops, as
// traffic does: X holds M on C while through B is shorter.
func TestMemberPathFollowsAdvertisedNextHops(t *testing.T) {
	rtt := map[string]float64{"A-X": 10, "X-B": 10, "X-C": 30, "B-M": 5, "C-M": 5}
	n := memberNet(t, rtt, []string{"B", "C"})
	rng := rand.New(rand.NewPCG(1, 2))
	for range 30 {
		memberProbes(n, rtt, nil, rng)
		n.run(time.Second, nil)
	}
	if nh, _ := n.r["X"].NextHop("M"); nh != "B" {
		t.Fatalf("X's next hop to M %q, want B", nh)
	}
	// Held on C, as a smoothed route may be after its shortest moved.
	x := n.r["X"]
	x.mu.Lock()
	x.routes["M"].nextHop, x.sent = "C", time.Time{}
	x.mu.Unlock()
	for _, m := range x.Advert(n.budget, []string{"A"}) {
		if _, _, err := n.r["A"].Receive("X", m, n.budget); err != nil {
			t.Fatal(err)
		}
	}
	for _, st := range n.r["A"].Status().Routes {
		if st.Dest == "M" {
			if want := []string{"A", "X", "C", "M"}; !slices.Equal(st.Path, want) {
				t.Fatalf("A's path to M %v, want %v", st.Path, want)
			}
			return
		}
	}
	t.Fatal("A has no route to M")
}
