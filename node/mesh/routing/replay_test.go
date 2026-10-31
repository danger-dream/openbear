package routing

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"
)

type simData struct {
	Links     map[string][]*float64 `json:"links"`
	Scenarios map[string][]*float64 `json:"scenarios"`
}

type replayResult struct {
	switches int
	lost     int
	timeline []string
	changes  []int // seconds H's path to T changed, after warm-up
	// asymmetric is the time T's path to H was not H's reversed, in all and
	// at most in one stretch: traffic one way is then dropped by
	// WireGuard's source check at the far end.
	asymmetric, longest time.Duration
}

// replay feeds the per-second probes behind sim.py to four relays: H home,
// T TOY, S Singapore, L Los Angeles, starting from the configured next hops
// of today (home to TOY through Singapore). The flow measured is H to T,
// as in sim.py, along the next hops the relays actually use.
func replay(t *testing.T, d simData, ht []*float64) replayResult {
	const warm = 15 // sim.py's WARM
	n := newNet(t, map[string]time.Duration{
		"H-T": 1250 * time.Microsecond, "H-S": 40 * time.Millisecond, "H-L": 101 * time.Millisecond,
		"L-S": 82 * time.Millisecond, "S-T": 37 * time.Millisecond,
	}, map[string]string{
		"H>T": "S", "H>S": "S", "H>L": "L",
		"T>H": "S", "T>S": "S", "T>L": "S",
		"S>H": "H", "S>T": "T", "S>L": "L",
		"L>H": "H", "L>S": "S", "L>T": "S",
	})
	series := map[string][]*float64{"H-T": ht, "H-S": d.Links["H-S"], "H-L": d.Links["H-L"], "L-S": d.Links["S-L"], "S-T": d.Links["T-S"]}
	steps := len(ht)
	sample := func(link string, at int) *float64 { return series[link][at] }
	var res replayResult
	var stretch time.Duration
	last := ""
	for sec := 0; sec < steps; sec++ {
		path, ok := n.path("H", "T")
		p := strings.Join(path, ">")
		if p != last {
			res.timeline = append(res.timeline, fmt.Sprintf("%ds %s", sec, p))
			if sec >= warm && last != "" {
				res.switches++
				res.changes = append(res.changes, sec)
			}
			last = p
		}
		if sec >= warm {
			broken := !ok
			for i := 0; i+1 < len(path) && !broken; i++ {
				if sample(strings.Join(sortedPair(path[i], path[i+1]), "-"), sec) == nil {
					broken = true
				}
			}
			if broken || d.Links["T-M"][sec] == nil {
				res.lost++
			}
		}
		for link := range series {
			a, b, _ := strings.Cut(link, "-")
			v := sample(link, sec)
			n.cut(a, b, v == nil)
			if v == nil {
				n.probe(a, b, -1)
			} else {
				n.probe(a, b, *v)
			}
		}
		n.run(time.Second, func() {
			if sec < warm {
				return
			}
			there, _ := n.path("H", "T")
			back, _ := n.path("T", "H")
			for i := range back {
				if len(back) != len(there) || back[i] != there[len(there)-1-i] {
					res.asymmetric += 10 * time.Millisecond
					stretch += 10 * time.Millisecond
					res.longest = max(res.longest, stretch)
					return
				}
			}
			stretch = 0
		})
	}
	return res
}

func sortedPair(a, b string) []string {
	p := pair(a, b)
	return p[:]
}

// TestReplayMatchesSimulation runs sim.py's scenarios through the routers.
// Its results (result.txt) for the proposed algorithm: no switches without
// a fault; outage 2 switches, 3 s lost, back to the direct path at 103 s;
// flapping 2 switches, 3 s lost, back at 213 s; degradation 2 switches,
// 4 s lost, back at 168 s.
func TestReplayMatchesSimulation(t *testing.T) {
	raw, err := os.ReadFile("testdata/sim-links.json")
	if err != nil {
		t.Fatal(err)
	}
	var d simData
	if err := json.Unmarshal(raw, &d); err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		name     string
		ht       []*float64
		switches int
		lost     int    // at most
		away     [2]int // the move off the direct path falls in this range of seconds
		back     [2]int // and the move back in this one
	}{
		{"none", d.Links["H-T"], 0, 0, [2]int{}, [2]int{}},
		{"outage", d.Scenarios["outage"], 2, 3, [2]int{62, 64}, [2]int{100, 106}},
		{"flap", d.Scenarios["flap"], 2, 3, [2]int{122, 124}, [2]int{205, 216}},
		{"degrade", d.Scenarios["degrade"], 2, 4, [2]int{128, 134}, [2]int{165, 171}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			r := replay(t, d, c.ht)
			t.Logf("switches %d, lost %d s, asymmetric %v (longest %v), path: %s", r.switches, r.lost, r.asymmetric, r.longest, strings.Join(r.timeline, "; "))
			if r.longest > 100*time.Millisecond {
				t.Fatalf("the two directions disagreed for %v at a stretch", r.longest)
			}
			if r.switches != c.switches || r.lost > c.lost {
				t.Fatalf("switches %d (want %d), lost %d s (want at most %d)", r.switches, c.switches, r.lost, c.lost)
			}
			if c.switches == 2 {
				if r.changes[0] < c.away[0] || r.changes[0] > c.away[1] || r.changes[1] < c.back[0] || r.changes[1] > c.back[1] {
					t.Fatalf("moved away at %d s and back at %d s, want %v and %v", r.changes[0], r.changes[1], c.away, c.back)
				}
			}
			if last := r.timeline[len(r.timeline)-1]; !strings.HasSuffix(last, " H>T") {
				t.Fatalf("ends on %s", last)
			}
		})
	}
}
