package mesh

import (
	"encoding/json"
	"testing"
	"time"

	meshclock "openbear/node/mesh/clock"
)

// windowsClock is time.Now as Windows has it, moving in 15.625 ms steps,
// and its performance counter.
func windowsClock() (coarse func() time.Time, counter func() time.Duration) {
	base := time.Now()
	return func() time.Time { return base.Add(time.Since(base).Truncate(15625 * time.Microsecond)) },
		func() time.Duration { return time.Since(base) }
}

// Every node on a clock as Windows': probes answered well within a step,
// as on a LAN, still measure their round trip, in entry.candidates[] and
// peers[] alike.
func TestCoarseClockStillMeasures(t *testing.T) {
	defer meshclock.Simulate(windowsClock())()
	l := newHealthLab(t, spec{relays: []string{"A", "B"}, members: map[string][]string{"M": {"A", "B"}}, rules: map[string][]Rule{"A": allowICMP, "B": allowICMP, "M": allowICMP}})
	nodes := map[string]*running{}
	for _, name := range []string{"A", "B", "M"} {
		nodes[name] = l.startHealthNode(name)
	}
	healthWait(t, 30*time.Second, func() bool {
		e := nodes["M"].status().Entry
		if e == nil || e.Relay != "A" {
			return false
		}
		for _, c := range e.Candidates {
			if c.Probes < 8 {
				return false
			}
		}
		return true
	})
	const least = .005 // ms: a loopback round trip through two tunnels is more
	ok := func(ms float64) bool { return ms >= least && ms < 15 }
	st := nodes["M"].status()
	raw, _ := json.Marshal(st.Entry.Candidates)
	t.Logf("M entry.candidates %s", raw)
	for _, c := range st.Entry.Candidates {
		if c.RTTMs == nil || !ok(*c.RTTMs) || c.Timeouts != 0 {
			t.Errorf("M %s: rttMs %v, %d of %d probes timed out", c.ID, c.RTTMs, c.Timeouts, c.Probes)
		}
	}
	// Both ends of a relay link measure it for routing (rounded to 0.1 ms).
	for _, id := range []string{"A", "B"} {
		r := nodes[id].status().Routing
		raw, _ := json.Marshal(r)
		t.Logf("%s routing %s", id, raw)
		if r == nil || len(r.Links) != 1 || r.Links[0].RTTMs <= 0 {
			t.Errorf("%s: routing link without a round trip", id)
		}
	}
	// peers[]: what a node probes, the entry in use and a relay link from
	// the side that dials it. (A relay does not probe its members.)
	for _, x := range [][2]string{{"M", "A"}, {"A", "B"}} {
		for _, p := range nodes[x[0]].status().Peers {
			if p.ID == x[1] {
				t.Logf("%s -> %s %s rttMs=%v", x[0], p.ID, p.Kind, p.RTTMs)
				if !ok(p.RTTMs) {
					t.Errorf("%s -> %s: rttMs %v", x[0], p.ID, p.RTTMs)
				}
			}
		}
	}
}
