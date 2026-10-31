package routing

import (
	"math"
	"math/rand"
	"strings"
	"testing"
	"time"
)

// TestLossNoiseDoesNotFlap: L reaches H directly over a 180 ms link that
// loses 3 % of its probes at random, or through S for 255 ms (179 + 76).
// Ten minutes of probes once a second, as the relays send them, move L's
// and H's routes to each other at most once. Before the loss rate took the
// lower of the last 10 and 30 probes, two losses within ten probes made the
// direct link cost 380 ms and sent the routes through S and back, about
// four times in ten minutes on L's route alone.
func TestLossNoiseDoesNotFlap(t *testing.T) {
	for seed := int64(1); seed <= 5; seed++ {
		n := newNet(t, map[string]time.Duration{"L-H": 90 * time.Millisecond, "L-S": 89 * time.Millisecond, "S-H": 38 * time.Millisecond},
			map[string]string{"L>H": "H", "L>S": "S", "H>L": "L", "H>S": "S", "S>L": "L", "S>H": "H"})
		rnd := rand.New(rand.NewSource(seed))
		jitter := func(ms float64) float64 { return ms + rnd.Float64()*2 - 1 }
		var lost []int
		paths := map[string]string{}
		switches := 0
		const warm = 30
		for sec := 0; sec < warm+600; sec++ {
			if sec >= warm {
				for _, f := range [][2]string{{"L", "H"}, {"H", "L"}} {
					p, _ := n.path(f[0], f[1])
					s := strings.Join(p, ">")
					if last, ok := paths[f[0]]; ok && last != s {
						switches++
						t.Logf("seed %d, %d s: %s", seed, sec-warm, s)
					}
					paths[f[0]] = s
				}
			}
			if rnd.Float64() < 0.03 {
				n.probe("L", "H", -1)
				lost = append(lost, sec)
			} else {
				n.probe("L", "H", jitter(180))
			}
			n.probe("L", "S", jitter(179))
			n.probe("S", "H", jitter(76))
			n.run(time.Second, nil)
		}
		close := 0 // losses within ten probes of the one before
		for i := 1; i < len(lost); i++ {
			if lost[i]-lost[i-1] < 10 {
				close++
			}
		}
		t.Logf("seed %d: %d probes lost, %d within ten of the last, %d route changes", seed, len(lost), close, switches)
		if switches > 1 {
			t.Fatalf("seed %d: routes changed %d times in ten minutes", seed, switches)
		}
		if close == 0 {
			t.Fatalf("seed %d: no losses close together; the case is not tested", seed)
		}
	}
}

// TestLossRate: the lower of the shares lost in the last 10 and 30 probes.
func TestLossRate(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	m := newMetric(0)
	exact := func(what string, want float64) {
		t.Helper()
		if got := m.loss(); math.Abs(got-want) > 1e-9 {
			t.Fatalf("%s: loss %v, want %v", what, got, want)
		}
	}
	feed := func(lost bool, n int) {
		for i := 0; i < n; i++ {
			now = now.Add(time.Second)
			m.sample(180*time.Millisecond, lost, now)
		}
	}
	feed(false, 3)
	feed(true, 1)
	exact("one of four lost", 0.25) // both windows hold the same four
	feed(false, 27)                 // the first loss leaves the last 30
	feed(true, 1)
	feed(false, 1)
	feed(true, 1)
	// Two of the last 10, two of the last 30: 30 probes weigh them.
	exact("two close together", 2.0/30)
	near(t, "their cost", m.cost(), 180+1000*2.0/30)
	// Lost on and on: the last 30 catch up with the last 10.
	feed(true, 2) // 4 of 10, 4 of 30
	exact("four of the last 30", 4.0/30)
	feed(false, 1)
	feed(true, 1)
	feed(false, 1)
	feed(true, 1)
	feed(false, 1)
	feed(true, 1)
	feed(false, 1)
	feed(true, 1)
	exact("eight of 30, six of 10", min(0.6, 8.0/30))
	// Ten good probes clear it, whatever the last 30 say.
	feed(false, 10)
	exact("cleared", 0)
}

// TestLateAnswersDoNotFlap is the flapping seen on the relays' long TCP
// links (TOY, home, Berlin): T reaches B over a 159 ms link, or through H
// for 177.5 ms (2.5 + 175), B's links both over TCP. TCP turns a lost
// segment into a late answer: 3 % of the probes on each of them come a
// retransmission timeout (360 ms) late, and one in 200 so late that it
// counts as lost. In ten minutes neither T's route to B nor B's to T goes
// through H more than once. With round trip and jitter averaged over
// every answer, one late answer made the direct link cost about 270 ms
// for 5 s and more, and with seed 1 each route went through H and back 7
// times in ten minutes.
func TestLateAnswersDoNotFlap(t *testing.T) {
	for seed := int64(1); seed <= 5; seed++ {
		n := newNet(t, map[string]time.Duration{"T-B": 79500 * time.Microsecond, "T-H": 1250 * time.Microsecond, "H-B": 87500 * time.Microsecond},
			map[string]string{"T>B": "B", "T>H": "H", "B>T": "T", "B>H": "H", "H>T": "T", "H>B": "B"})
		rnd := rand.New(rand.NewSource(seed))
		late := 0
		tcp := func(a, b string, ms float64) {
			ms += rnd.Float64()*2 - 1
			switch v := rnd.Float64(); {
			case v < 0.005:
				n.probe(a, b, -1)
				late++
				return
			case v < 0.03:
				ms += 360
				late++
			}
			n.probe(a, b, ms)
		}
		paths := map[string]string{}
		detours := map[string]int{}
		const warm = 30
		for sec := 0; sec < warm+600; sec++ {
			if sec >= warm {
				for _, f := range [][2]string{{"T", "B"}, {"B", "T"}} {
					p, _ := n.path(f[0], f[1])
					s := strings.Join(p, ">")
					if last, ok := paths[f[0]]; ok && last != s {
						t.Logf("seed %d, %d s: %s", seed, sec-warm, s)
						if len(p) > 2 {
							detours[f[0]]++
						}
					}
					paths[f[0]] = s
				}
			}
			tcp("T", "B", 159)
			tcp("H", "B", 175)
			n.probe("T", "H", 2.5+rnd.Float64()*0.2)
			n.run(time.Second, nil)
		}
		t.Logf("seed %d: %d late or lost answers, detours %v", seed, late, detours)
		for f, k := range detours {
			if k > 1 {
				t.Fatalf("seed %d: %s's route went through H %d times in ten minutes", seed, f, k)
			}
		}
		if p, _ := n.path("T", "B"); len(p) != 2 {
			t.Fatalf("seed %d: T ends on %v", seed, p)
		}
	}
}

// TestFlapDamping: A-B, 10 ms against 20 ms through C, costs 300 ms for
// 15 s every 75 s. The first time A↔B goes through C and back; then it
// has flapped, and 15 s are short of the minute a move waits: it stays.
// Ten minutes after the flap the next one goes through C again. A cut of
// A-B moves the route at once all the same, and back once A-B returns.
func TestFlapDamping(t *testing.T) {
	n := triangle(t)
	second := func(ab float64) {
		n.probe("A", "B", ab)
		n.probe("A", "C", 10)
		n.probe("B", "C", 10)
		n.run(time.Second, nil)
	}
	for i := 0; i < 60; i++ {
		second(10)
	}
	var detours []int
	via := false
	for sec := 0; sec < 750; sec++ {
		ab := 10.0
		if sec%75 >= 60 {
			ab = 300
		}
		second(ab)
		if c := n.hop("A", "B") == "C"; c != via {
			via = c
			if c {
				detours = append(detours, sec)
			}
		}
	}
	// The first at 60 s + 2 + 5, back by 82; the flap is at about 82 s,
	// so the spike from 660 s on is the first one after ten minutes.
	if len(detours) != 2 || detours[0] > 70 || detours[1] < 660 {
		t.Fatalf("A went through C at %v s; events %v", detours, n.eventsOf("A", "route.changed"))
	}
	// Flapped again just now. A cut moves at once, and the return waits not.
	for n.hop("A", "B") != "B" {
		second(10)
	}
	n.probe("A", "B", -1)
	n.probe("A", "B", -1)
	cut := n.now
	for n.hop("A", "B") != "C" {
		n.probe("A", "B", -1)
		n.probe("A", "C", 10)
		n.probe("B", "C", 10)
		n.run(time.Second, nil)
		if n.now.Sub(cut) > 3*time.Second {
			t.Fatal("A did not move off a cut A-B")
		}
	}
	back := n.now
	for n.hop("A", "B") != "B" {
		second(10)
		if n.now.Sub(back) > 90*time.Second {
			t.Fatal("A did not come back to A-B")
		}
	}
	t.Logf("detours at %v s; cut moved off within %v, back %v after A-B returned; events %v", detours, back.Sub(cut), n.now.Sub(back), n.eventsOf("A", "route.changed"))
}

// TestLateAnswerBarelyMoves: one answer a retransmission timeout late, or
// two in a row, hardly change a link's cost; three in a row are a change
// and show.
func TestLateAnswerBarelyMoves(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	m := newMetric(0)
	feed := func(ms float64, k int) {
		for i := 0; i < k; i++ {
			now = now.Add(time.Second)
			m.sample(time.Duration(ms*float64(time.Millisecond)), false, now)
		}
	}
	feed(159, 20)
	base := m.cost()
	feed(519, 1)
	feed(159, 10)
	if c := m.cost(); c > base+1 {
		t.Fatalf("one late answer: cost %.1f from %.1f", c, base)
	}
	feed(519, 2)
	if c := m.cost(); c > base+1 {
		t.Fatalf("two late answers: cost %.1f from %.1f", c, base)
	}
	feed(159, 10)
	feed(519, 3)
	if c := m.cost(); c < base+100 {
		t.Fatalf("three late answers: cost %.1f from %.1f", c, base)
	}
}
