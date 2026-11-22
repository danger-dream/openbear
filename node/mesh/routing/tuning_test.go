package routing

import (
	"encoding/json"
	"math"
	"math/rand"
	"os"
	"strings"
	"testing"
	"time"
)

func routeTo(r *Router, d string) RouteStatus {
	for _, x := range r.Status().Routes {
		if x.Dest == d {
			return x
		}
	}
	return RouteStatus{}
}

// TestDefaultSwitchThreshold: with the route in use working, a path cheaper by
// at least max(SwitchPct% * cur, SwitchMinMs) in both real and smoothed cost is
// taken; one cheaper by less stays with HeldMargin, and one cheaper enough in
// real cost while still converging stays with HeldSettling.
func TestDefaultSwitchThreshold(t *testing.T) {
	// A-B and A-C cost 12 (rtt 10 + hopCost 2); A reaches D through B for 12+bd,
	// through C for 12+cd. Default margin is max(5%, 2ms).
	for _, c := range []struct {
		cd, bd float64
		want   string
	}{
		{47.5, 50, "B"}, // 59.5 vs 62: 2.5 ms < max(5%=3.1, 2ms)
		{46, 50, "C"},   // 58 vs 62: 4 ms >= 3.1 ms
		{8.5, 10, "B"},  // 20.5 vs 22: 1.5 ms < max(5%=1.1, 2ms)
		{7.5, 10, "C"},  // 19.5 vs 22: 2.5 ms >= 2ms
		{170, 180, "C"}, // 182 vs 192: 10 ms >= max(5%=9.6, 5ms)
		{173, 180, "B"}, // 185 vs 192: 7 ms < 9.6 ms
	} {
		s := newSoloTuned(t, []string{"B", "C", "D"}, []string{"B", "C"}, map[string]string{"D": "B"}, &DefaultTuning)
		s.up("B", 10)
		s.up("C", 10)
		s.advert("B", "B", map[string]float64{"A": 12, "D": c.bd}, map[string]string{"C": "A", "D": "D"})
		s.advert("B", "D", map[string]float64{"B": c.bd, "C": c.cd}, map[string]string{"B": "B", "C": "C"})
		// Let B settle before C appears so C starts with StartPenaltyMs against settled B.
		for range 60 {
			s.advance(10 * time.Second)
			s.advert("B", "B", map[string]float64{"A": 12, "D": c.bd}, map[string]string{"C": "A", "D": "D"})
			s.advert("B", "D", map[string]float64{"B": c.bd, "C": c.cd}, map[string]string{"B": "B", "C": "C"})
		}
		s.advert("C", "C", map[string]float64{"A": 12, "D": c.cd}, map[string]string{"B": "A", "D": "D"})
		if c.want == "C" {
			// Initially C has start penalty, so it is held as settling.
			st := routeTo(s.r, "D")
			if st.Held != HeldSettling || st.Best != "C" || math.IsInf(st.BestSmoothed, 0) || math.IsInf(st.Smoothed, 0) {
				t.Errorf("%+v: initial status %+v, want C settling with smoothed costs", c, st)
			}
		}
		for range 80 {
			s.advance(10 * time.Second)
			s.advert("B", "B", map[string]float64{"A": 12, "D": c.bd}, map[string]string{"C": "A", "D": "D"})
			s.advert("B", "D", map[string]float64{"B": c.bd, "C": c.cd}, map[string]string{"B": "B", "C": "C"})
			s.advert("C", "C", map[string]float64{"A": 12, "D": c.cd}, map[string]string{"B": "A", "D": "D"})
		}
		if s.hop("D") != c.want {
			t.Errorf("%+v: A to D through %s, want %s", c, s.hop("D"), c.want)
		}
		st := routeTo(s.r, "D")
		if c.want == "B" && (st.Best != "C" || st.Held != HeldMargin || math.Abs(st.BestCost-(12+c.cd)) > 0.15 || math.Abs(st.Cost-(12+c.bd)) > 0.15) {
			t.Errorf("%+v: status %+v, want C held by margin", c, st)
		}
		if c.want == "C" && (st.Best != "" || st.Held != "") {
			t.Errorf("%+v: status %+v after move", c, st)
		}
	}
}

// TestRouteFlipsCounter: voluntary better switch back to a next hop left within
// 5 minutes increments Flips; after 5 minutes it does not.
func TestRouteFlipsCounter(t *testing.T) {
	s := newSoloTuned(t, []string{"B", "C", "D"}, []string{"B", "C"}, map[string]string{"D": "B"}, &Tuning{
		SmoothHalfLifeS: 2, SwitchPct: 1, SwitchMinMs: 1, HopCostMs: 2, StartPenaltyMs: 10, JitterWeight: 2, LossWeight: 1000,
	})
	s.up("B", 10)
	s.up("C", 10)
	feed := func(bd, cd float64, secs int) {
		for range secs {
			s.advert("B", "B", map[string]float64{"A": 12, "D": bd}, map[string]string{"C": "A", "D": "D"})
			s.advert("B", "D", map[string]float64{"B": bd, "C": cd}, map[string]string{"B": "B", "C": "C"})
			s.advert("C", "C", map[string]float64{"A": 12, "D": cd}, map[string]string{"B": "A", "D": "D"})
			s.advance(time.Second)
		}
	}
	feed(20, 40, 20)
	if s.hop("D") != "B" {
		t.Fatalf("start hop %s, want B", s.hop("D"))
	}
	feed(50, 20, 20) // B -> C (better)
	if s.hop("D") != "C" || routeTo(s.r, "D").Flips != 0 {
		t.Fatalf("after B->C: hop=%s flips=%d", s.hop("D"), routeTo(s.r, "D").Flips)
	}
	feed(15, 50, 20) // C -> B (better) within 5 minutes -> flip!
	if s.hop("D") != "B" || routeTo(s.r, "D").Flips != 1 {
		t.Fatalf("after C->B within 5m: hop=%s flips=%d", s.hop("D"), routeTo(s.r, "D").Flips)
	}
	// Wait > 5 minutes before switching back to C -> not a flip.
	feed(15, 50, 310)
	feed(60, 15, 20)
	if s.hop("D") != "C" || routeTo(s.r, "D").Flips != 1 {
		t.Fatalf("after >5m B->C: hop=%s flips=%d", s.hop("D"), routeTo(s.r, "D").Flips)
	}
}

// TestLossNoiseWithDefaults: random loss on the direct link (3 %) next to a
// detour 75 ms longer. With 100-probe loss smoothing + Babel metric smoothing,
// the route stays completely steady.
func TestLossNoiseWithDefaults(t *testing.T) {
	for seed := int64(1); seed <= 5; seed++ {
		n := newNet(t, map[string]time.Duration{"L-H": 90 * time.Millisecond, "L-S": 89 * time.Millisecond, "S-H": 38 * time.Millisecond},
			map[string]string{"L>H": "H", "L>S": "S", "H>L": "L", "H>S": "S", "S>L": "L", "S>H": "H"})
		n.retune(&DefaultTuning)
		rnd := rand.New(rand.NewSource(seed))
		jitter := func(ms float64) float64 { return ms + rnd.Float64()*2 - 1 }
		last, switches := "", 0
		const warm = 30
		for sec := 0; sec < warm+600; sec++ {
			if p, _ := n.path("L", "H"); sec >= warm {
				if s := strings.Join(p, ">"); last != "" && s != last {
					switches++
				}
				last = strings.Join(p, ">")
			}
			if rnd.Float64() < 0.03 {
				n.probe("L", "H", -1)
			} else {
				n.probe("L", "H", jitter(180))
			}
			n.probe("L", "S", jitter(179))
			n.probe("S", "H", jitter(76))
			n.run(time.Second, nil)
		}
		t.Logf("seed %d: %d route changes in ten minutes", seed, switches)
		if switches > 2 {
			t.Fatalf("seed %d: routes changed %d times in ten minutes", seed, switches)
		}
	}
}

// TestReplayWithDefaults runs sim.py's scenarios with the smoothed defaults.
func TestReplayWithDefaults(t *testing.T) {
	raw, err := os.ReadFile("testdata/sim-links.json")
	if err != nil {
		t.Fatal(err)
	}
	var d simData
	if err := json.Unmarshal(raw, &d); err != nil {
		t.Fatal(err)
	}
	for _, c := range []struct {
		name     string
		ht       []*float64
		switches int
		lost     int
		ends     string
	}{
		{"none", d.Links["H-T"], 0, 0, "H>T"},
		{"outage", d.Scenarios["outage"], 2, 3, ""},
		{"flap", d.Scenarios["flap"], 1, 3, "H>S>T"},
		{"degrade", d.Scenarios["degrade"], 2, 12, ""},
	} {
		r := replay(t, d, c.ht, &DefaultTuning)
		t.Logf("%s: switches %d, lost %d s, path: %s", c.name, r.switches, r.lost, strings.Join(r.timeline, "; "))
		if r.switches > c.switches || r.lost > c.lost {
			t.Errorf("%s: switches %d (max %d), lost %d s (max %d)", c.name, r.switches, c.switches, r.lost, c.lost)
		}
	}
}

// TestHeldStaysSettlingUntilSwitch: a candidate cheaper than the switch margin
// in real cost stays HeldSettling throughout its smoothed convergence, even in
// the final stretch where its remaining lag is smaller than margin(best).
func TestHeldStaysSettlingUntilSwitch(t *testing.T) {
	// A-B = 12, B-D = 88 -> current = 100 (margin 5 -> realLimit = 95).
	// A-C = 12, C-D = 82 -> candidate = 94 (beats realLimit by 1ms, margin(94) = 4.7).
	s := newSoloTuned(t, []string{"B", "C", "D"}, []string{"B", "C"}, map[string]string{"D": "B"}, &DefaultTuning)
	s.up("B", 10)
	s.up("C", 10)
	s.advert("B", "B", map[string]float64{"A": 12, "D": 88}, map[string]string{"C": "A", "D": "D"})
	s.advert("B", "D", map[string]float64{"B": 88, "C": 82}, map[string]string{"B": "B", "C": "C"})
	for range 60 {
		s.advance(10 * time.Second)
		s.advert("B", "B", map[string]float64{"A": 12, "D": 88}, map[string]string{"C": "A", "D": "D"})
		s.advert("B", "D", map[string]float64{"B": 88, "C": 82}, map[string]string{"B": "B", "C": "C"})
	}
	s.advert("C", "C", map[string]float64{"A": 12, "D": 82}, map[string]string{"B": "A", "D": "D"})
	sawCloseSettling := false
	switched := false
	for range 600 {
		st := routeTo(s.r, "D")
		if st.NextHop == "C" {
			switched = true
			break
		}
		if st.Held != HeldSettling {
			t.Fatalf("while converging to C (cost %.1f, smoothed %.1f vs cur %.1f/%.1f): held = %q, want %q",
				st.BestCost, st.BestSmoothed, st.Cost, st.Smoothed, st.Held, HeldSettling)
		}
		if st.BestSmoothed-st.BestCost < DefaultTuning.margin(st.BestCost) {
			sawCloseSettling = true
		}
		s.advance(time.Second)
		s.advert("B", "B", map[string]float64{"A": 12, "D": 88}, map[string]string{"C": "A", "D": "D"})
		s.advert("B", "D", map[string]float64{"B": 88, "C": 82}, map[string]string{"B": "B", "C": "C"})
		s.advert("C", "C", map[string]float64{"A": 12, "D": 82}, map[string]string{"B": "A", "D": "D"})
	}
	if !switched || !sawCloseSettling {
		t.Fatalf("switched=%v sawCloseSettling=%v, want both true", switched, sawCloseSettling)
	}
}
