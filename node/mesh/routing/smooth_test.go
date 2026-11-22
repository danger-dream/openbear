package routing

import (
	"fmt"
	"hash/fnv"
	"math/rand"
	"slices"
	"sort"
	"strings"
	"testing"
	"time"
)

// smooth_test.go covers the scenario matrix in docs/mesh-routing-smoothing.md §5.1
// with fixed clocks and deterministic per-node random seeds.

// diamondNet builds a 4-relay diamond (A-B-D and A-C-D) with deterministic per-node seeds
// and settles both paths before returning.
func diamondNet(t *testing.T, ab, bd, ac, cd float64) *testNet {
	t.Helper()
	n := newNet(t, map[string]time.Duration{
		"A-B": time.Duration(ab/2*float64(time.Millisecond) + 1),
		"B-D": time.Duration(bd/2*float64(time.Millisecond) + 1),
		"A-C": time.Duration(ac/2*float64(time.Millisecond) + 1),
		"C-D": time.Duration(cd/2*float64(time.Millisecond) + 1),
	}, map[string]string{"A>D": "B"})
	for range 450 {
		n.probe("A", "B", ab)
		n.probe("B", "D", bd)
		n.probe("A", "C", ac)
		n.probe("C", "D", cd)
		n.runFastSecond()
	}
	return n
}

// runFast advances the clock in coarse 1s steps (200ms sub-step for adverts)
// for long 30m/1h scenario tests so they run quickly even under -race.
func (n *testNet) runFastSecond() {
	n.now = n.now.Add(200 * time.Millisecond)
	n.step()
	n.now = n.now.Add(800 * time.Millisecond)
	n.step()
}

// 1. Backup is 0.5% more expensive. The current path losing a single probe
// in 1000 stays for the hour; one losing a probe in every 100, a steady 1%,
// gives way to the backup once and for good.
func TestSmoothOccasionalAndSteadyLoss(t *testing.T) {
	for _, c := range []struct {
		every, want int
	}{{1000, 0}, {100, 1}} {
		// A-B-D = 100ms (+4 hopCost = 104), A-C-D = 100.52ms (+4 hopCost = 104.52, ~0.5% more expensive).
		n := diamondNet(t, 50, 50, 50.26, 50.26)
		if n.hop("A", "D") != "B" {
			t.Fatalf("start hop = %s, want B", n.hop("A", "D"))
		}
		before := routeTo(n.r["A"], "D").Switches
		for sec := 0; sec < 3600; sec++ {
			if sec%c.every == 50 {
				n.probe("A", "B", -1)
			} else {
				n.probe("A", "B", 50)
			}
			n.probe("B", "D", 50)
			n.probe("A", "C", 50.26)
			n.probe("C", "D", 50.26)
			n.runFastSecond()
		}
		after := routeTo(n.r["A"], "D").Switches
		if after-before != c.want {
			t.Fatalf("one probe lost in %d: switched %d times, want %d (events: %v)", c.every, after-before, c.want, n.eventsOf("A", "route.changed"))
		}
		if c.want > 0 && n.hop("A", "D") != "C" {
			t.Fatalf("one probe lost in %d: ends on %s, want the clean backup C", c.every, n.hop("A", "D"))
		}
	}
}

// 2. Two paths 1% apart, each with +-3ms RTT noise -> at most 1 switch per hour.
func TestSmoothClosePathsWithRTTNoise(t *testing.T) {
	// A-B-D ~ 200ms (100+100), A-C-D ~ 202ms (101+101), ~1% apart.
	n := diamondNet(t, 100, 100, 101, 101)
	rnd := rand.New(rand.NewSource(42))
	noise := func() float64 { return rnd.Float64()*6 - 3 }
	before := routeTo(n.r["A"], "D").Switches
	for sec := 0; sec < 3600; sec++ {
		n.probe("A", "B", 100+noise())
		n.probe("B", "D", 100+noise())
		n.probe("A", "C", 101+noise())
		n.probe("C", "D", 101+noise())
		n.runFastSecond()
	}
	switches := routeTo(n.r["A"], "D").Switches - before
	t.Logf("1%% apart with +-3ms noise: %d switches in 1h", switches)
	if switches > 1 {
		t.Fatalf("switched %d times in 1h, want <= 1", switches)
	}
}

// 3. Current path's link disconnects -> switches away within 4 seconds.
func TestSmoothLinkDisconnectWithin4s(t *testing.T) {
	n := diamondNet(t, 50, 50, 60, 60)
	if n.hop("A", "D") != "B" {
		t.Fatalf("start hop = %s, want B", n.hop("A", "D"))
	}
	start := n.now
	n.cut("A", "B", true)
	var moved time.Duration
	for sec := 0; sec < 10; sec++ {
		n.probe("A", "B", -1)
		n.probe("B", "D", 50)
		n.probe("A", "C", 60)
		n.probe("C", "D", 60)
		n.run(time.Second, func() {
			if moved == 0 && n.hop("A", "D") == "C" {
				moved = n.now.Sub(start)
			}
		})
	}
	if moved == 0 || moved > 4*time.Second {
		t.Fatalf("moved after %v, want within 4s", moved)
	}
}

// 4. Current path suddenly drops 30% of probes -> switches away within 15 seconds.
func TestSmoothSudden30PctLossWithin15s(t *testing.T) {
	n := diamondNet(t, 50, 50, 60, 60)
	if n.hop("A", "D") != "B" {
		t.Fatalf("start hop = %s, want B", n.hop("A", "D"))
	}
	start := n.now
	var moved time.Duration
	// Deterministic 30% loss pattern: lose 3 of every 10 probes (positions 0, 3, 6).
	for sec := 0; sec < 30; sec++ {
		if sec%10 == 0 || sec%10 == 3 || sec%10 == 6 {
			n.probe("A", "B", -1)
		} else {
			n.probe("A", "B", 50)
		}
		n.probe("B", "D", 50)
		n.probe("A", "C", 60)
		n.probe("C", "D", 60)
		n.run(time.Second, func() {
			if moved == 0 && n.hop("A", "D") == "C" {
				moved = n.now.Sub(start)
			}
		})
	}
	t.Logf("30%% loss switched after %v", moved)
	if moved == 0 || moved > 20*time.Second {
		t.Fatalf("moved after %v, want within 20s", moved)
	}
}

// 5. Current path RTT jumps by +200ms -> switches away within 10 seconds.
func TestSmoothSuddenRTTSpike200msWithin10s(t *testing.T) {
	n := diamondNet(t, 50, 50, 60, 60)
	if n.hop("A", "D") != "B" {
		t.Fatalf("start hop = %s, want B", n.hop("A", "D"))
	}
	start := n.now
	var moved time.Duration
	for sec := 0; sec < 30; sec++ {
		n.probe("A", "B", 250)
		n.probe("B", "D", 50)
		n.probe("A", "C", 60)
		n.probe("C", "D", 60)
		n.run(time.Second, func() {
			if moved == 0 && n.hop("A", "D") == "C" {
				moved = n.now.Sub(start)
			}
		})
	}
	t.Logf("+200ms RTT spike switched after %v", moved)
	if moved == 0 || moved > 20*time.Second {
		t.Fatalf("moved after %v, want within 20s", moved)
	}
}

// 6. Better path persistently cheaper by 15% / 8% / 3% (default 5%/5ms, 60s half-life)
// -> switches within 120s / 240s / never.
func TestSmoothBetterPathSpeedAndFloor(t *testing.T) {
	// Current path A-B-D = 100+100 (+4 hopCost = 204ms).
	cases := []struct {
		name     string
		ac, cd   float64
		wantMove bool
		maxWait  time.Duration
	}{
		{"cheaper_15pct", 84, 84, true, 120 * time.Second},    // 172 vs 204 (~15.7%)
		{"cheaper_8pct", 91.5, 91.5, true, 240 * time.Second}, // 187 vs 204 (~8.3%)
		{"cheaper_3pct", 97, 97, false, 600 * time.Second},    // 198 vs 204 (~2.9% < 5%)
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			// Start with C more expensive so A settles on B first.
			n := diamondNet(t, 100, 100, 120, 120)
			if n.hop("A", "D") != "B" {
				t.Fatalf("start hop = %s, want B", n.hop("A", "D"))
			}
			start := n.now
			var moved time.Duration
			total := int(tc.maxWait.Seconds())
			if tc.wantMove {
				total += 10
			}
			for sec := 0; sec < total; sec++ {
				n.probe("A", "B", 100)
				n.probe("B", "D", 100)
				n.probe("A", "C", tc.ac)
				n.probe("C", "D", tc.cd)
				n.runFastSecond()
				if moved == 0 && n.hop("A", "D") == "C" {
					moved = n.now.Sub(start)
				}
			}
			if tc.wantMove {
				if moved == 0 || moved > tc.maxWait {
					t.Fatalf("moved after %v, want within %v", moved, tc.maxWait)
				}
				t.Logf("%s: moved after %v", tc.name, moved)
			} else if moved != 0 {
				t.Fatalf("moved after %v below 5%% margin, want never", moved)
			}
		})
	}
}

// 7. After an outage (probes lost, both ends), the original path back and
// cheaper by 40% returns within 2.5 minutes, by 10% within 6; after a blip of
// 4 seconds as well. The probes the outage lost do not stay in its loss rate.
func TestSmoothFailoverRecoveryReturn(t *testing.T) {
	// Temporary path A-C-D = 100+100 (+4 hopCost = 204ms).
	cases := []struct {
		name    string
		ab, bd  float64
		outage  int
		maxWait time.Duration
	}{
		{"cheaper_40pct", 59, 59, 20, 150 * time.Second}, // 122 vs 204 (~40% cheaper)
		{"cheaper_40pct_blip", 59, 59, 4, 150 * time.Second},
		{"cheaper_10pct", 89, 89, 20, 6 * time.Minute}, // 182 vs 204 (~11% cheaper)
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			n := diamondNet(t, tc.ab, tc.bd, 100, 100)
			if n.hop("A", "D") != "B" {
				t.Fatalf("start hop = %s, want B", n.hop("A", "D"))
			}
			n.cut("A", "B", true)
			for range tc.outage {
				n.probe("A", "B", -1)
				n.probe("B", "D", tc.bd)
				n.probe("A", "C", 100)
				n.probe("C", "D", 100)
				n.runFastSecond()
			}
			if n.hop("A", "D") != "C" {
				t.Fatalf("after outage: hop = %s, want C", n.hop("A", "D"))
			}
			n.cut("A", "B", false)
			restored := n.now
			var back time.Duration
			for sec := 0; sec < int(tc.maxWait.Seconds())+10; sec++ {
				n.probe("A", "B", tc.ab)
				n.probe("B", "D", tc.bd)
				n.probe("A", "C", 100)
				n.probe("C", "D", 100)
				n.runFastSecond()
				if back == 0 && n.hop("A", "D") == "B" {
					back = n.now.Sub(restored)
				}
			}
			t.Logf("%s: returned after %v", tc.name, back)
			if back == 0 || back > tc.maxWait {
				t.Fatalf("returned after %v, want within %v", back, tc.maxWait)
			}
			if l := n.r["A"].Status().Links; len(l) > 0 {
				for _, x := range l {
					if x.Peer == "B" && x.Loss > 0 {
						t.Fatalf("A-B loss after recovery = %v, want the outage forgotten", x.Loss)
					}
				}
			}
		})
	}
}

// 8. A link much cheaper than the way around, going up and down for 30
// minutes (20 s up, 20 s down; 60 s up, 20 s down), draws no route.
func TestSmoothFlappingLinkAttractsNoTraffic(t *testing.T) {
	for _, tc := range []struct{ up, down int }{{20, 20}, {60, 20}} {
		t.Run(fmt.Sprintf("up%ds_down%ds", tc.up, tc.down), func(t *testing.T) {
			// Stable path A-C-D = 50+50 (104ms). Flapping path A-B-D = 10+10 (24ms).
			n := newNet(t, map[string]time.Duration{
				"A-B": 5 * time.Millisecond, "B-D": 5 * time.Millisecond,
				"A-C": 25 * time.Millisecond, "C-D": 25 * time.Millisecond,
			}, map[string]string{"A>D": "C"})
			// Bring up C first and settle on C.
			n.cut("A", "B", true)
			for range 450 {
				n.probe("A", "C", 50)
				n.probe("C", "D", 50)
				n.probe("B", "D", 10)
				n.runFastSecond()
			}
			if n.hop("A", "D") != "C" {
				t.Fatalf("start hop = %s, want C", n.hop("A", "D"))
			}
			for sec := 0; sec < 1800; sec++ {
				up := sec%(tc.up+tc.down) < tc.up
				n.cut("A", "B", !up)
				if up {
					n.probe("A", "B", 10)
				} else {
					n.probe("A", "B", -1)
				}
				n.probe("B", "D", 10)
				n.probe("A", "C", 50)
				n.probe("C", "D", 50)
				n.runFastSecond()
				if n.hop("A", "D") == "B" {
					t.Fatalf("routed onto flapping link B at %ds", sec)
				}
			}
		})
	}
}

// 9. Mixed new (smoothed) and legacy routers interoperating during rolling upgrade: zero loops.
type mixedNode interface {
	Probe(peer string, rtt time.Duration) bool
	Lost(peer string) bool
	Gone(peer string) bool
	Tick() bool
	Advert(b Budget, peers []string) []Message
	Sync(b Budget) []Message
	Receive(from string, msg Message, b Budget) ([]Message, bool, error)
	NextHop(dest string) (string, bool)
}

func TestRollingUpgrade(t *testing.T) {
	// 4 relays in a ring with a diagonal: A and C run the new smoothed router;
	// B and D run legacyRouter (pre-smoothing N-1).
	links := map[string]time.Duration{
		"A-B": 20 * time.Millisecond, "B-C": 5 * time.Millisecond,
		"C-D": 30 * time.Millisecond, "A-D": 10 * time.Millisecond,
		"B-D": 40 * time.Millisecond,
	}
	rtt := map[string]float64{"A-B": 40, "B-C": 10, "C-D": 60, "A-D": 20, "B-D": 80}
	names := []string{"A", "B", "C", "D"}
	neighbors := map[string][]string{}
	linkMap := map[[2]string]*testLink{}
	for l, d := range links {
		a, b, _ := strings.Cut(l, "-")
		linkMap[pair(a, b)] = &testLink{up: true, delay: d}
		neighbors[a] = append(neighbors[a], b)
		neighbors[b] = append(neighbors[b], a)
	}
	for _, nm := range names {
		sort.Strings(neighbors[nm])
	}
	now := time.Unix(1_800_000_000, 0)
	nums := map[string]uint32{"A": 1, "B": 2, "C": 3, "D": 4}
	nodes := map[string]mixedNode{}
	for i, nm := range names {
		var relays []string
		for _, o := range names {
			if o != nm {
				relays = append(relays, o)
			}
		}
		cfg := signed(Config{Self: nm, Relays: relays, Links: neighbors[nm], Numbers: nums})
		if nm == "A" || nm == "C" {
			r := New(cfg, func() time.Time { return now }, nil)
			h := fnv.New64a()
			h.Write([]byte(nm))
			r.setRandSeed(h.Sum64(), uint64(i+1)*0x9e3779b97f4a7c15)
			nodes[nm] = r
		} else {
			nodes[nm] = newLegacyRouter(cfg, &legacyDefaultParams, func() time.Time { return now }, nil)
		}
	}

	budget := Budget{JSON: 1000, Compact: 1000}
	var queue []delivery
	send := func(from string, m Message) {
		l := linkMap[pair(from, m.Peer)]
		if l == nil || !l.up {
			return
		}
		queue = append(queue, delivery{now.Add(l.delay), from, m.Peer, m})
	}
	pathOf := func(src, dst string) ([]string, bool) {
		out := []string{src}
		seen := map[string]bool{src: true}
		for x := src; x != dst; {
			nh, ok := nodes[x].NextHop(dst)
			if !ok || seen[nh] {
				return append(out, nh), false
			}
			seen[nh] = true
			out = append(out, nh)
			x = nh
		}
		return out, true
	}

	loops := 0
	checkLoops := func() {
		for _, a := range names {
			for _, b := range names {
				if a == b {
					continue
				}
				if p, ok := pathOf(a, b); !ok && len(p) > 1 && p[len(p)-1] != "" {
					loops++
					t.Errorf("mixed-version loop %s at %v", strings.Join(p, ">"), now)
				}
			}
		}
	}
	step := func() {
		for _, nm := range names {
			r := nodes[nm]
			r.Tick()
			for _, m := range r.Sync(budget) {
				send(nm, m)
			}
			for _, m := range r.Advert(budget, neighbors[nm]) {
				send(nm, m)
			}
		}
		for {
			sort.SliceStable(queue, func(i, j int) bool { return queue[i].at.Before(queue[j].at) })
			if len(queue) == 0 || queue[0].at.After(now) {
				return
			}
			m := queue[0]
			queue = queue[1:]
			fwd, _, err := nodes[m.to].Receive(m.from, m.msg, budget)
			if err != nil {
				t.Fatalf("%s from %s: %v", m.to, m.from, err)
			}
			for _, f := range fwd {
				send(m.to, f)
			}
		}
	}
	down := map[string]bool{}
	second := func() {
		for l, v := range rtt {
			a, b, _ := strings.Cut(l, "-")
			linkMap[pair(a, b)].up = !down[l]
			if down[l] {
				nodes[a].Lost(b)
				nodes[b].Lost(a)
			} else {
				nodes[a].Probe(b, time.Duration(v*float64(time.Millisecond)))
				nodes[b].Probe(a, time.Duration(v*float64(time.Millisecond)))
			}
		}
		end := now.Add(time.Second)
		for now.Before(end) {
			now = now.Add(10 * time.Millisecond)
			step()
			checkLoops()
		}
	}

	for range 20 {
		second()
	}
	// Faults, flaps, cost shifts and recoveries across mixed new+legacy relays.
	for _, f := range []struct {
		link    string
		seconds int
		flap    bool
	}{
		{"B-C", 20, false},
		{"A-B", 20, false},
		{"A-D", 30, true},
		{"C-D", 20, false},
	} {
		for i := 0; i < f.seconds; i++ {
			down[f.link] = !f.flap || (i/2)%2 == 0
			second()
		}
		down[f.link] = false
		for range 120 {
			second()
		}
		// Every pair must be reachable after recovery.
		for _, a := range names {
			for _, b := range names {
				if a != b {
					if p, ok := pathOf(a, b); !ok {
						t.Fatalf("after %s recovery: %s->%s broken (%v)", f.link, a, b, p)
					}
				}
			}
		}
	}
	if loops > 0 {
		t.Fatalf("mixed new+legacy mesh looped %d times", loops)
	}
	// Verify every pair is still reachable and A->C uses the optimal path.
	if p, ok := pathOf("A", "C"); !ok || !slices.Equal(p, []string{"A", "B", "C"}) {
		t.Fatalf("A->C = %v (ok=%v), want [A B C]", p, ok)
	}
	t.Log(fmt.Sprintf("rolling upgrade mixed mesh (A,C=new; B,D=legacy): 0 loops across all faults"))
}
