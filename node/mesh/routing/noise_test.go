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
		// Each direction at most one detour-and-back (4 total across L->H and H->L).
		if switches > 4 {
			t.Fatalf("seed %d: routes changed %d times in ten minutes", seed, switches)
		}
		if close == 0 {
			t.Fatalf("seed %d: no losses close together; the case is not tested", seed)
		}
	}
}

// TestLossRate checks the dual-window loss formula:
// L = max(max(0, lost100 - 1) / 100, lost10 >= 3 ? lost10 / 10 : 0),
// with denominator always 100 even when fewer than 100 probes were seen.
func TestLossRate(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	m := newMetric(0, &DefaultTuning)
	exact := func(what string, want float64) {
		t.Helper()
		if got := m.windowLoss(); math.Abs(got-want) > 1e-9 {
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
	exact("single loss ignored", 0)
	feed(false, 1)
	feed(true, 1)
	// 2 lost in 100, 2 in 10 (<3): (2-1)/100 = 0.01.
	exact("two losses in 100", 0.01)
	near(t, "their cost", m.cost(), 180+1000*m.loss()+DefaultTuning.HopCostMs)
	// A third loss within 10 probes triggers short window: 3/10 = 0.30.
	feed(false, 1)
	feed(true, 1)
	exact("three in 10 triggers short window", 0.3)
	// 10 good probes clear the short window; 3 losses remain in 100 -> (3-1)/100 = 0.02.
	feed(false, 10)
	exact("short window cleared, long window remains", 0.02)
	// Push all 3 losses out of the 100-probe window.
	feed(false, 95)
	exact("long window cleared", 0)
}

// TestSlowLoss: every lost probe counts toward a rate that halves every
// fifteen minutes. A single loss on a clean link costs under a millisecond;
// a link losing about one probe in 125, in runs the windows forget, keeps
// about 0.8 % (8 ms) through its clean spells; an outage's probes and its
// time count for nothing.
func TestSlowLoss(t *testing.T) {
	start := time.Unix(1_800_000_000, 0)
	feed := func(m *metric, now *time.Time, lost bool, n int) {
		for i := 0; i < n; i++ {
			*now = now.Add(time.Second)
			m.sample(180*time.Millisecond, lost, *now)
		}
	}

	m, now := newMetric(0, &DefaultTuning), start
	feed(m, &now, false, 10)
	feed(m, &now, true, 1)
	feed(m, &now, false, 1)
	if got := 1000 * m.loss(); got <= 0 || got >= 1 {
		t.Fatalf("one lost probe costs %.3f ms, want a little under 1", got)
	}
	one := m.slowLoss
	feed(m, &now, false, int(slowLossHalfLife/time.Second))
	near(t, "one loss after a half-life", m.slowLoss, one/2)

	// One probe in 125 lost, as runs of two every 250 s: the 100-probe window
	// is clear most of the time, the slow rate is not.
	m, now = newMetric(0, &DefaultTuning), start
	feed(m, &now, false, 10)
	clean := 0
	for cycle := 0; cycle < 16; cycle++ {
		feed(m, &now, true, 2)
		for s := 0; s < 248; s++ {
			feed(m, &now, false, 1)
			if cycle >= 8 && m.windowLoss() == 0 {
				clean++
				if got := m.loss(); got < 0.005 || got > 0.012 {
					t.Fatalf("cycle %d: loss %.4f in a clean spell, want 0.5%%-1.2%%", cycle, got)
				}
			}
		}
	}
	if clean < 1000 {
		t.Fatalf("%d clean-window seconds; the case is not tested", clean)
	}

	// An outage: its run of losses and the time it lasts leave the slow rate
	// where the last answer left it.
	m, now = newMetric(0, &DefaultTuning), start
	feed(m, &now, false, 10)
	feed(m, &now, true, 1)
	feed(m, &now, false, 20)
	before := m.slowLoss
	feed(m, &now, true, 600)
	if m.state() != StateDown {
		t.Fatalf("state %s after ten minutes lost, want down", m.state())
	}
	near(t, "slow loss through an outage", m.slowLoss, before)
	feed(m, &now, false, 1)
	near(t, "slow loss on the first answer after it", m.slowLoss, before)
	if got := m.loss(); got != m.slowLoss {
		t.Fatalf("loss %.5f after the outage, want the slow rate %.5f alone", got, m.slowLoss)
	}
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

// TestLateAnswerBarelyMoves: one answer a retransmission timeout late, or
// two in a row, hardly change a link's cost; three in a row are a change
// and show.
func TestLateAnswerBarelyMoves(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	m := newMetric(0, &DefaultTuning)
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
