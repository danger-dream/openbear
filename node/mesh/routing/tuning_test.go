package routing

import (
	"encoding/json"
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

// TestDefaultSwitchThreshold: with the route in use working, a path at
// least SwitchMs (5 ms) cheaper is taken whatever its share; one less
// cheaper is not, and the status says which and why.
func TestDefaultSwitchThreshold(t *testing.T) {
	// A-B and A-C cost 10; A reaches D through B for 10 + bd, through C
	// for 10 + cd.
	for _, c := range []struct {
		cd, bd float64
		want   string
	}{
		{46, 50, "B"}, // 56 against 60: 4 ms
		{45, 50, "C"}, // 55 against 60: 5 ms, 8 %
		{1, 10, "C"},  // 11 against 20
		{170, 180, "C"},
	} {
		s := newSoloTuned(t, []string{"B", "C", "D"}, []string{"B", "C"}, map[string]string{"D": "B"}, &DefaultTuning)
		s.up("B", 10)
		s.up("C", 10)
		s.advert("B", "B", map[string]float64{"A": 10, "D": c.bd}, map[string]string{"C": "A", "D": "D"})
		s.advert("B", "D", map[string]float64{"B": c.bd, "C": c.cd}, map[string]string{"B": "B", "C": "C"})
		s.advert("C", "C", map[string]float64{"A": 10, "D": c.cd}, map[string]string{"B": "A", "D": "D"})
		if s.hop("D") != c.want {
			t.Errorf("%+v: A to D through %s, want %s", c, s.hop("D"), c.want)
		}
		st := routeTo(s.r, "D")
		if c.want == "B" && (st.Best != "C" || st.Held != HeldMargin || st.BestCost != 10+c.cd || st.Cost != 10+c.bd) {
			t.Errorf("%+v: status %+v, want C held by the margin", c, st)
		}
		if c.want == "C" && (st.Best != "" || st.Held != "") {
			t.Errorf("%+v: status %+v after the move", c, st)
		}
	}
}

// TestStatusSaysTheRouteIsDamped: after going back and forth, the next
// cheaper path waits FlapHold, and the status says until when.
func TestStatusSaysTheRouteIsDamped(t *testing.T) {
	n := triangle(t)
	n.retune(&DefaultTuning)
	second := func(ab float64) {
		n.probe("A", "B", ab)
		n.probe("A", "C", 10)
		n.probe("B", "C", 10)
		n.run(time.Second, nil)
	}
	for range 60 {
		second(10)
	}
	// A-B spikes for 15 s twice: the first is a detour and back, the second waits.
	for range 2 {
		for range 15 {
			second(300)
		}
		for range 30 {
			second(10)
		}
	}
	if e := n.eventsOf("A", "route.changed"); len(e) != 4 || !strings.HasSuffix(e[2], "B -> C (better)") || !strings.HasSuffix(e[3], "C -> B (better)") {
		t.Fatalf("events %v, want one detour and back", e)
	}
	for range 10 {
		second(300)
	}
	st := routeTo(n.r["A"], "B")
	if n.hop("A", "B") != "B" || st.Best != "C" || st.Held != HeldDamped || st.Until.Sub(n.now) <= 0 || st.Until.Sub(n.now) > DefaultTuning.FlapHold {
		t.Fatalf("A to B through %s, status %+v, now %v", n.hop("A", "B"), st, n.now)
	}
}

// TestLossNoiseWithDefaults: random loss on the direct link (3 %) next to
// a detour 75 ms longer. With the default thresholds a few losses close
// together are worth moving for; flap damping keeps it to a detour or two
// in ten minutes.
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

// TestReplayWithDefaults runs sim.py's scenarios with the default
// thresholds: as with the old ones, no switch without a fault, an outage
// and a degradation away and back once; a link that keeps flapping is held
// down past the end of the data (probationBase), and the path stays on its
// one detour.
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
		{"none", d.Links["H-T"], 0, 0, "H>T"}, {"outage", d.Scenarios["outage"], 2, 3, "H>T"}, {"flap", d.Scenarios["flap"], 1, 3, "H>S>T"}, {"degrade", d.Scenarios["degrade"], 2, 4, "H>T"},
	} {
		r := replay(t, d, c.ht, &DefaultTuning)
		t.Logf("%s: switches %d, lost %d s, path: %s", c.name, r.switches, r.lost, strings.Join(r.timeline, "; "))
		if r.longest > 100*time.Millisecond || r.switches > c.switches || r.lost > c.lost || !strings.HasSuffix(r.timeline[len(r.timeline)-1], " "+c.ends) {
			t.Errorf("%s: switches %d (at most %d), lost %d s (at most %d), longest asymmetry %v, ends %s", c.name, r.switches, c.switches, r.lost, c.lost,
				r.longest, r.timeline[len(r.timeline)-1])
		}
	}
}
