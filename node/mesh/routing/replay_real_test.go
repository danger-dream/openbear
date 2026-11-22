package routing

import (
	"compress/gzip"
	"container/heap"
	"encoding/json"
	"fmt"
	"hash/fnv"
	"math"
	"math/rand"
	"os"
	"path/filepath"
	"slices"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"
)

// replay_real_test.go replays 48h of real mesh_line_minute data (testdata/replay-real.json.gz)
// across all 25 relays (rev 776) comparing legacyRouter (pre-smoothing defaults) against the
// new smoothed Router with half-lives {8, 15, 30, 60}s (docs/mesh-routing-smoothing.md §5.2 & §6).

type replayFixture struct {
	Note      string            `json:"note"`
	StartTs   int64             `json:"startTs"`
	Minutes   int               `json:"minutes"`
	Relays    []string          `json:"relays"`
	NoTransit []string          `json:"noTransit"`
	Names     map[string]string `json:"names"`
	Lines     [][2]string       `json:"lines"`
	Series    [][][7]int        `json:"series"` // [deltaMin, rtt_x10, min_x10, max_x10, loss_x10000, up_s, known_s]
}

type minuteSample struct {
	present bool
	rtt     float64 // ms; < 0 means unknown
	rmin    float64
	rmax    float64
	loss    float64
	downS   int
}

type probePoint struct {
	lost bool
	rtt  float64 // ms when !lost
}

type synthReplay struct {
	noise     string
	relays    []string
	noTransit map[string]bool
	names     map[string]string
	lines     [][2]string
	seconds   int
	// probes[lineIdx][sec]
	probes [][]probePoint
	// trueCost[lineIdx][sec]: synthetic true link cost (ms, +Inf if down/missing)
	// computed from that minute's RTT + loss*1000 + hopCost(0 for fair comparison; we use RTT + loss*1000).
	trueCost [][]float64
	// outages: list of (lineIdx, startSec) where a working line starts a >=3s continuous outage
	outages []outageEvent
}

type outageEvent struct {
	lineIdx  int
	startSec int
}

func loadReplayFixture(t testing.TB) *replayFixture {
	t.Helper()
	path := "testdata/replay-real.json.gz"
	if v := os.Getenv("OBLAB_REPLAY_FIXTURE"); v != "" {
		path = v
	}
	f, err := os.Open(path)
	if err != nil {
		t.Fatalf("open replay fixture: %v", err)
	}
	defer f.Close()
	gz, err := gzip.NewReader(f)
	if err != nil {
		t.Fatalf("gzip reader: %v", err)
	}
	defer gz.Close()
	var fix replayFixture
	if err := json.NewDecoder(gz).Decode(&fix); err != nil {
		t.Fatalf("decode replay fixture: %v", err)
	}
	return &fix
}

// synthesizeMinuteRTTs produces 60 per-second RTT samples whose distribution
// matches the minute's (rmin, rtt, rmax) without smearing a single TCP spike
// across the whole minute: when rmax is far above rtt (right-skewed outlier),
// place a small number of outlier spikes while keeping the remaining samples
// centered around the minute mean using a triangular distribution.
func synthesizeMinuteRTTs(rng *rand.Rand, rmin, rtt, rmax float64, out *[60]float64) {
	if rmax <= rmin {
		for i := range out {
			out[i] = max(0.1, rtt)
		}
		return
	}
	// If the minute is heavily right-skewed (rmax - rtt > 3*(rtt - rmin) and spread > 3ms),
	// model it as 1..2 spike probes near rmax plus a tight triangular body so the
	// 60-second mean stays close to rtt rather than inflating to (rmin+rtt+rmax)/3.
	left := max(0.1, rtt-rmin)
	right := rmax - rtt
	if right > 3*left && rmax-rmin > 3.0 {
		spikeIdx := rng.Intn(60)
		// Adjust body mean so (59*bodyMean + rmax)/60 ~= rtt.
		bodyMean := max(rmin, (60*rtt-rmax)/59.0)
		bodyHi := min(rmax, bodyMean+max(0.2, bodyMean-rmin)*2)
		for i := range out {
			if i == spikeIdx {
				out[i] = max(0.1, rmax)
			} else {
				out[i] = max(0.1, sampleTriangular(rng, rmin, bodyMean, bodyHi))
			}
		}
		return
	}
	for i := range out {
		out[i] = max(0.1, sampleTriangular(rng, rmin, rtt, rmax))
	}
}

const (
	noiseTriangular = "tri"
	noiseExcursion  = "exc"
)

// synthesizeExcursion is the other noise model: the minute's recorded
// extremes are those of the round trip the node reported (a median of five
// probes), so it holds a baseline and visits rmax and rmin for three seconds
// each (the least a median shows), the baseline set so the minute keeps its
// mean. Triangular samples on every second are the noisiest reading of the
// same record, these the quietest; parameters are chosen on both.
func synthesizeExcursion(rng *rand.Rand, rmin, rtt, rmax float64, out *[60]float64) {
	if rmax <= rmin {
		for i := range out {
			out[i] = max(0.1, rtt)
		}
		return
	}
	const k = 3
	base := min(rmax, max(rmin, (60*rtt-k*rmax-k*rmin)/(60-2*k)))
	for i := range out {
		out[i] = max(0.1, base+(rng.Float64()-0.5)*min(0.4, (rmax-rmin)/4))
	}
	hi := rng.Intn(60 - k + 1)
	lo := (hi + 30) % (60 - k + 1)
	for i := 0; i < k; i++ {
		out[hi+i], out[lo+i] = max(0.1, rmax), max(0.1, rmin)
	}
}

// sampleTriangular samples from triangular distribution on [lo, hi] with mode mid.
func sampleTriangular(rng *rand.Rand, lo, mid, hi float64) float64 {
	if hi <= lo {
		return mid
	}
	if mid < lo {
		mid = lo
	}
	if mid > hi {
		mid = hi
	}
	u := rng.Float64()
	fc := (mid - lo) / (hi - lo)
	if u < fc {
		return lo + math.Sqrt(u*(hi-lo)*(mid-lo))
	}
	return hi - math.Sqrt((1-u)*(hi-lo)*(hi-mid))
}

// buildSynthetic synthesizes deterministic per-second probes for [startMin, startMin+numMins).
func buildSynthetic(fix *replayFixture, startMin, numMins int, noise string) *synthReplay {
	if startMin+numMins > fix.Minutes {
		numMins = fix.Minutes - startMin
	}
	nLines := len(fix.Lines)
	// Expand delta-encoded minute records into dense grid for [0, startMin+numMins).
	endMin := startMin + numMins
	grid := make([][]minuteSample, nLines)
	for li, entries := range fix.Series {
		row := make([]minuteSample, endMin)
		m := 0
		for _, e := range entries {
			m += e[0]
			if m >= endMin {
				break
			}
			downS := max(0, e[6]-e[5])
			if downS > 60 {
				downS = 60
			}
			rtt := -1.0
			if e[1] > 0 {
				rtt = float64(e[1]) / 10.0
			}
			rmin := rtt
			if e[2] > 0 {
				rmin = float64(e[2]) / 10.0
			}
			rmax := rtt
			if e[3] > 0 {
				rmax = float64(e[3]) / 10.0
			}
			row[m] = minuteSample{
				present: true,
				rtt:     rtt,
				rmin:    rmin,
				rmax:    rmax,
				loss:    float64(e[4]) / 10000.0,
				downS:   downS,
			}
		}
		// Forward-fill short gaps (up to 5 minutes) within a line's active life,
		// without carrying over transient outage seconds.
		var last minuteSample
		gap := 0
		for mi := 0; mi < endMin; mi++ {
			if row[mi].present {
				if row[mi].rtt < 0 && last.present && last.rtt > 0 {
					row[mi].rtt, row[mi].rmin, row[mi].rmax = last.rtt, last.rmin, last.rmax
				}
				last = row[mi]
				gap = 0
			} else if last.present && gap < 5 {
				filled := last
				filled.downS = 0
				row[mi] = filled
				gap++
			}
		}
		grid[li] = row[startMin:endMin]
	}

	totalSec := numMins * 60
	probes := make([][]probePoint, nLines)
	trueCost := make([][]float64, nLines)
	rng := rand.New(rand.NewSource(20261120))
	var outages []outageEvent

	for li := 0; li < nLines; li++ {
		ps := make([]probePoint, totalSec)
		tc := make([]float64, totalSec)
		prevUp := false
		for mi := 0; mi < numMins; mi++ {
			ms := grid[li][mi]
			baseSec := mi * 60
			if !ms.present || ms.rtt <= 0 {
				for s := 0; s < 60; s++ {
					ps[baseSec+s] = probePoint{lost: true}
					tc[baseSec+s] = math.Inf(1)
				}
				prevUp = false
				continue
			}
			// Synthesize known_s - up_s continuous outage seconds within the minute.
			downStart := 60
			downEnd := 60
			if ms.downS > 0 {
				if ms.downS >= 60 {
					downStart, downEnd = 0, 60
				} else {
					downStart = rng.Intn(60 - ms.downS + 1)
					downEnd = downStart + ms.downS
				}
			}
			var minRTTs [60]float64
			if noise == noiseExcursion {
				synthesizeExcursion(rng, ms.rmin, ms.rtt, ms.rmax, &minRTTs)
			} else {
				synthesizeMinuteRTTs(rng, ms.rmin, ms.rtt, ms.rmax, &minRTTs)
			}
			// The minute's loss counts the probes its outage lost, synthesized above.
			upLoss := ms.loss
			if ms.downS > 0 && ms.downS < 60 {
				upLoss = max(0, ms.loss*60-float64(ms.downS)) / float64(60-ms.downS)
			}
			for s := 0; s < 60; s++ {
				sec := baseSec + s
				inDown := s >= downStart && s < downEnd
				if inDown {
					if prevUp && ms.downS >= 3 {
						// Record onset of a real >=3s continuous disconnect.
						outages = append(outages, outageEvent{lineIdx: li, startSec: sec})
					}
					ps[sec] = probePoint{lost: true}
					tc[sec] = math.Inf(1)
					prevUp = false
					continue
				}
				lost := upLoss > 0 && rng.Float64() < upLoss
				if lost {
					ps[sec] = probePoint{lost: true}
				} else {
					ps[sec] = probePoint{lost: false, rtt: minRTTs[s]}
				}
				// True synthetic cost of the link for path-quality evaluation:
				// minute's true RTT + loss*1000 (same ground truth for both legacy and new).
				tc[sec] = ms.rtt + ms.loss*1000.0
				prevUp = true
			}
		}
		probes[li] = ps
		trueCost[li] = tc
	}

	nt := map[string]bool{}
	for _, id := range fix.NoTransit {
		nt[id] = true
	}
	return &synthReplay{
		noise:     noise,
		relays:    fix.Relays,
		noTransit: nt,
		names:     fix.Names,
		lines:     fix.Lines,
		seconds:   totalSec,
		probes:    probes,
		trueCost:  trueCost,
		outages:   outages,
	}
}

// snapDelivery is a scheduled in-process advert delivery for fast multi-day replay.
type snapDelivery struct {
	at       time.Time
	from, to int
	adv      *advert
}

type snapHeap []snapDelivery

func (h snapHeap) Len() int           { return len(h) }
func (h snapHeap) Less(i, j int) bool { return h[i].at.Before(h[j].at) }
func (h snapHeap) Swap(i, j int)      { h[i], h[j] = h[j], h[i] }
func (h *snapHeap) Push(x any)        { *h = append(*h, x.(snapDelivery)) }
func (h *snapHeap) Pop() any {
	old := *h
	n := len(old)
	x := old[n-1]
	*h = old[:n-1]
	return x
}

type nodeSummary struct {
	ID             string  `json:"id"`
	Name           string  `json:"name"`
	BetterSwitches int     `json:"betterSwitches"`
	Flips5m        int     `json:"flips5m"`
	DownSwitches   int     `json:"downSwitches"`
	LoopSwitches   int     `json:"loopSwitches"`
	AdvertsSent    int     `json:"advertsSent"`
	AvgPathCostMs  float64 `json:"avgPathCostMs"`
	Within3PctRate float64 `json:"within3PctRate"`
}

type replayMetrics struct {
	Label            string  `json:"label"`
	Noise            string  `json:"noise"`
	HalfLifeS        float64 `json:"halfLifeS"`
	SwitchMinMs      float64 `json:"switchMinMs"`
	StartPenaltyMs   float64 `json:"startPenaltyMs"`
	ElapsedSec       float64 `json:"elapsedSec"`
	BetterSwitches   int     `json:"betterSwitches"`
	Flips5m          int     `json:"flips5m"`
	DownSwitches     int     `json:"downSwitches"`
	LoopSwitches     int     `json:"loopSwitches"`
	AdvertsSent      int     `json:"advertsSent"`
	AvgPathCostMs    float64 `json:"avgPathCostMs"`
	Within3PctRate   float64 `json:"within3PctRate"`
	WithinMarginRate float64 `json:"withinMarginRate"`
	Over10PctRate    float64 `json:"over10PctRate"`
	OutageRoutes     int     `json:"outageRoutes"`
	OutageMeanSec    float64 `json:"outageMeanSec"`
	OutageP95Sec     float64 `json:"outageP95Sec"`
	OutageMaxSec     float64 `json:"outageMaxSec"`
	LoopEvents       int     `json:"loopEvents"`
	LoopTotalSec     int     `json:"loopTotalSec"`
	LoopMaxSec       int     `json:"loopMaxSec"`
	// Route-seconds whose path lost a probe that second outside an outage,
	// and those over a line that lost probes in at least a fifth of its minutes.
	LossHitRate   float64 `json:"lossHitRate"`
	LossyLineRate float64 `json:"lossyLineRate"`
	// Forwarding loops as the routers' next hops formed them, whatever their
	// length: how many, their mean and longest life, and how many formed
	// within two seconds of a member's move off no-transit relays with none
	// of them moving off a broken route (those are failover loops).
	RealLoops      int           `json:"realLoops"`
	RealLoopMeanMs float64       `json:"realLoopMeanMs"`
	RealLoopMaxMs  float64       `json:"realLoopMaxMs"`
	TransitLoops   int           `json:"transitLoops"`
	PerRelay       []nodeSummary `json:"perRelay"`
}

// precomputeOptimal computes, for each second (sampled every minute since trueCost
// is constant per minute except down seconds; we compute per second only when link
// up-set changes or minute changes!), the shortest true cost between every (src, dst)
// pair respecting noTransit tolls.
type optGraph struct {
	n         int
	noTransit []bool
	adj       [][]optEdge
}

type optEdge struct {
	to   int
	cost float64
}

type optKey struct {
	tolls int
	cost  float64
}

func optLess(a, b optKey) bool {
	if a.tolls != b.tolls {
		return a.tolls < b.tolls
	}
	return a.cost < b.cost
}

func computeAllPairsOpt(n int, noTransit []bool, lines [][2]int, costs []float64, distBuf []float64, tollBuf []int) {
	adj := make([][]optEdge, n)
	for li, ep := range lines {
		c := costs[li]
		if math.IsInf(c, 0) {
			continue
		}
		a, b := ep[0], ep[1]
		adj[a] = append(adj[a], optEdge{b, c})
		adj[b] = append(adj[b], optEdge{a, c})
	}
	done := make([]bool, n)
	for src := 0; src < n; src++ {
		off := src * n
		for i := 0; i < n; i++ {
			distBuf[off+i] = math.Inf(1)
			tollBuf[off+i] = 1 << 30
			done[i] = false
		}
		distBuf[off+src] = 0
		tollBuf[off+src] = 0
		for {
			u := -1
			for i := 0; i < n; i++ {
				if done[i] || math.IsInf(distBuf[off+i], 0) {
					continue
				}
				if u < 0 || optLess(optKey{tollBuf[off+i], distBuf[off+i]}, optKey{tollBuf[off+u], distBuf[off+u]}) {
					u = i
				}
			}
			if u < 0 {
				break
			}
			done[u] = true
			ut := tollBuf[off+u]
			uc := distBuf[off+u]
			for _, e := range adj[u] {
				if done[e.to] {
					continue
				}
				nt := ut
				if u != src && noTransit[u] {
					nt++
				}
				nc := uc + e.cost
				if optLess(optKey{nt, nc}, optKey{tollBuf[off+e.to], distBuf[off+e.to]}) {
					tollBuf[off+e.to] = nt
					distBuf[off+e.to] = nc
				}
			}
		}
	}
}

type replayRunOpts struct {
	label     string
	useLegacy bool
	tune      Tuning
}

func runRealReplayOpts(sr *synthReplay, opts replayRunOpts) replayMetrics {
	label, useLegacy, customTune := opts.label, opts.useLegacy, opts.tune
	wallStart := time.Now()
	nRelays := len(sr.relays)
	relayIdx := make(map[string]int, nRelays)
	nums := make(map[string]uint32, nRelays)
	noTransitBool := make([]bool, nRelays)
	var noTransitList []string
	for i, id := range sr.relays {
		relayIdx[id] = i
		nums[id] = uint32(i + 1)
		if sr.noTransit[id] {
			noTransitBool[i] = true
			noTransitList = append(noTransitList, id)
		}
	}

	// Build neighbor lists and line index lookups.
	neighbors := make([][]string, nRelays)
	linePairIdx := make([][2]int, len(sr.lines))
	pairLine := make([]int, nRelays*nRelays)
	for i := range pairLine {
		pairLine[i] = -1
	}
	for li, pairIDs := range sr.lines {
		a, b := relayIdx[pairIDs[0]], relayIdx[pairIDs[1]]
		linePairIdx[li] = [2]int{a, b}
		pairLine[a*nRelays+b] = li
		pairLine[b*nRelays+a] = li
		neighbors[a] = append(neighbors[a], pairIDs[1])
		neighbors[b] = append(neighbors[b], pairIDs[0])
	}
	for i := range neighbors {
		sort.Strings(neighbors[i])
	}

	// Per-relay event tracking.
	type hopHistory struct {
		left   string
		leftAt time.Time
		broken bool
	}
	hist := make([][]hopHistory, nRelays)
	for i := range hist {
		hist[i] = make([]hopHistory, nRelays)
	}
	betterSw := make([]int, nRelays)
	flips5m := make([]int, nRelays)
	downSw := make([]int, nRelays)
	loopSw := make([]int, nRelays)
	advertsSent := make([]int, nRelays)
	var swBuckets [4]int // <2ms, 2-5ms, 5-20ms, >=20ms
	swTo64ab := 0

	now := time.Unix(1_800_000_000, 0)
	warmSec := 300 // ignore initial 5m cold-start convergence in metrics
	if sr.seconds <= 300 {
		warmSec = sr.seconds / 5
	}

	nodes := make([]mixedNode, nRelays)
	nodeByID := make(map[string]int, nRelays)
	// Forwarding loops: checked on every route change against every router's
	// next hop as it is then. A loop stands until a change breaks it.
	hopOf := func(x int, dest string) string {
		if lr, ok := nodes[x].(*legacyRouter); ok {
			if rt := lr.routes[dest]; rt != nil {
				return rt.nextHop
			}
			return ""
		}
		if rt := nodes[x].(*Router).routes[dest]; rt != nil {
			return rt.nextHop
		}
		return ""
	}
	// cycleFrom is the members of the loop x's next hops lead into, sorted, or nil.
	cycleFrom := func(x int, dest string) []int {
		di := relayIdx[dest]
		seenAt := map[int]int{}
		var walk []int
		for x >= 0 && x != di {
			if at, ok := seenAt[x]; ok {
				c := slices.Clone(walk[at:])
				slices.Sort(c)
				return c
			}
			seenAt[x] = len(walk)
			walk = append(walk, x)
			nh := hopOf(x, dest)
			if nh == "" {
				return nil
			}
			x = nodeByID[nh]
		}
		return nil
	}
	type openLoop struct {
		members []int
		since   time.Time
	}
	openLoops := map[string][]openLoop{} // by dest
	transitAt := make([]time.Time, nRelays*nRelays)
	downAt := make([]time.Time, nRelays*nRelays)
	var loopLives []float64
	realLoops, transitLoops := 0, 0
	trackLoops := func(x int, dest string) {
		di := relayIdx[dest]
		kept := openLoops[dest][:0]
		for _, l := range openLoops[dest] {
			if c := cycleFrom(l.members[0], dest); slices.Equal(c, l.members) {
				kept = append(kept, l)
			} else if int(l.since.Sub(time.Unix(1_800_000_000, 0)).Seconds()) >= warmSec {
				loopLives = append(loopLives, float64(now.Sub(l.since).Milliseconds()))
			}
		}
		openLoops[dest] = kept
		c := cycleFrom(x, dest)
		if c == nil || slices.ContainsFunc(kept, func(l openLoop) bool { return slices.Equal(l.members, c) }) {
			return
		}
		openLoops[dest] = append(openLoops[dest], openLoop{c, now})
		if int(now.Sub(time.Unix(1_800_000_000, 0)).Seconds()) < warmSec {
			return
		}
		realLoops++
		recent := func(at []time.Time) bool {
			return slices.ContainsFunc(c, func(m int) bool { t := at[m*nRelays+di]; return !t.IsZero() && now.Sub(t) <= 2*time.Second })
		}
		if recent(transitAt) && !recent(downAt) {
			transitLoops++
		}
	}
	for i, id := range sr.relays {
		nodeByID[id] = i
		var others []string
		for _, o := range sr.relays {
			if o != id {
				others = append(others, o)
			}
		}
		iCopy := i
		onEvent := func(kind, detail string) {
			if kind != "route.changed" {
				return
			}
			// Format: "<dest>: <old> -> <next> (<reason>)"
			colon := strings.Index(detail, ": ")
			if colon < 0 {
				return
			}
			dest := detail[:colon]
			di, ok := relayIdx[dest]
			if !ok {
				return
			}
			rest := detail[colon+2:]
			lp := strings.LastIndex(rest, " (")
			if lp < 0 || !strings.HasSuffix(rest, ")") {
				return
			}
			reason := rest[lp+2 : len(rest)-1]
			arrow := strings.Index(rest[:lp], " -> ")
			if arrow < 0 {
				return
			}
			oldHop := rest[:arrow]
			newHop := rest[arrow+4 : lp]
			if rr, ok := nodes[iCopy].(*Router); ok && reason == ReasonBetter && oldHop != "none" && rr.graph != nil {
				cOld, ok1, _ := rr.graph.via(oldHop, dest)
				cNew, ok2, _ := rr.graph.via(newHop, dest)
				if ok1 && ok2 && cNew.tolls < cOld.tolls {
					transitAt[iCopy*nRelays+di] = now
				}
			}
			if reason == ReasonDown {
				downAt[iCopy*nRelays+di] = now
			}
			trackLoops(iCopy, dest)
			if oldHop == "none" {
				return
			}
			secElapsed := int(now.Sub(time.Unix(1_800_000_000, 0)).Seconds())
			h := &hist[iCopy][di]
			voluntary := reason == ReasonBetter || reason == "shorter"
			if secElapsed >= warmSec {
				switch reason {
				case ReasonBetter, "shorter":
					betterSw[iCopy]++
					if !h.broken && newHop == h.left && now.Sub(h.leftAt) < 5*time.Minute {
						flips5m[iCopy]++
					}
					if strings.HasPrefix(dest, "64ab6eb4") || strings.HasPrefix(id, "64ab6eb4") {
						swTo64ab++
					}
					// Measure real path cost difference on current synthetic graph (approx by router graph).
					if !useLegacy {
						if rr, ok := nodes[iCopy].(*Router); ok && rr.graph != nil {
							cOld, ok1, _ := rr.graph.via(oldHop, dest)
							cNew, ok2, _ := rr.graph.via(newHop, dest)
							if ok1 && ok2 {
								saved := cOld.ms() - cNew.ms()
								switch {
								case saved < 2:
									swBuckets[0]++
								case saved < 5:
									swBuckets[1]++
								case saved < 20:
									swBuckets[2]++
								default:
									swBuckets[3]++
								}
							}
						}
					}
				case ReasonDown:
					downSw[iCopy]++
				case ReasonLoop:
					loopSw[iCopy]++
				}
			}
			h.left, h.leftAt, h.broken = oldHop, now, !voluntary
		}

		cfg := Config{
			Self:       id,
			Relays:     others,
			Links:      neighbors[i],
			NoTransit:  noTransitList,
			Numbers:    nums,
			RelayLinks: sr.lines,
		}
		if !useLegacy {
			tune := customTune
			cfg.Tuning = &tune
		}
		cfg = signed(cfg)
		if useLegacy {
			lr := newLegacyRouter(cfg, &legacyDefaultParams, func() time.Time { return now }, onEvent)
			lr.skipSign = true
			nodes[i] = lr
		} else {
			r := New(cfg, func() time.Time { return now }, onEvent)
			h := fnv.New64a()
			h.Write([]byte(id))
			r.setRandSeed(h.Sum64(), uint64(i+1)*0x9e3779b97f4a7c15)
			nodes[i] = r
		}
	}

	var q snapHeap
	heap.Init(&q)

	// Per-line current one-way delay (half of latest non-lost RTT).
	lineDelay := make([]time.Duration, len(sr.lines))
	lineUp := make([]bool, len(sr.lines))
	for li := range lineDelay {
		lineDelay[li] = 20 * time.Millisecond
	}

	// Deduplicate flooded adverts already received by a node (avoids redundant
	// heap pushes/pops while keeping identical earliest arrival).
	recvSeq := make([]uint64, nRelays*nRelays)
	anyRouteChanged := true

	sendSnap := func(fromIdx int, toID string, adv *advert) {
		toIdx := nodeByID[toID]
		li := pairLine[fromIdx*nRelays+toIdx]
		if li < 0 || !lineUp[li] {
			return
		}
		oi := nodeByID[adv.origin]
		if oi == toIdx || recvSeq[toIdx*nRelays+oi] >= adv.seq {
			return
		}
		heap.Push(&q, snapDelivery{at: now.Add(lineDelay[li]), from: fromIdx, to: toIdx, adv: adv})
	}

	dirtyNodes := make([]bool, nRelays)
	deliverUntil := func(until time.Time) {
		clear(dirtyNodes)
		for q.Len() > 0 && !q[0].at.After(until) {
			m := heap.Pop(&q).(snapDelivery)
			now = m.at
			oi := nodeByID[m.adv.origin]
			if oi == m.to || recvSeq[m.to*nRelays+oi] >= m.adv.seq {
				continue
			}
			recvSeq[m.to*nRelays+oi] = m.adv.seq
			var fwd []string
			var ch bool
			if lr, ok := nodes[m.to].(*legacyRouter); ok {
				fwd, ch = lr.receiveSnap(sr.relays[m.from], m.adv, true)
			} else {
				fwd, ch = nodes[m.to].(*Router).receiveSnap(sr.relays[m.from], m.adv, true)
			}
			dirtyNodes[m.to] = true
			if ch {
				anyRouteChanged = true
			}
			for _, peer := range fwd {
				sendSnap(m.to, peer, m.adv)
			}
		}
		now = until
		for i := 0; i < nRelays; i++ {
			if !dirtyNodes[i] {
				continue
			}
			var ch bool
			if lr, ok := nodes[i].(*legacyRouter); ok {
				ch = lr.recomputeNow()
			} else {
				ch = nodes[i].(*Router).recomputeNow()
			}
			if ch {
				anyRouteChanged = true
			}
		}
	}

	// Map outages by startSec for O(1) lookup.
	outageAt := make(map[int][]int)
	for _, ev := range sr.outages {
		if ev.startSec >= warmSec && ev.startSec+15 < sr.seconds {
			outageAt[ev.startSec] = append(outageAt[ev.startSec], ev.lineIdx)
		}
	}
	type trackedOutage struct {
		src, dst int
		lineIdx  int
		startSec int
	}
	var activeOutages []trackedOutage
	var outageTimes []float64

	// Path quality & loop tracking.
	costSum := make([]float64, nRelays)
	costSamples := make([]int, nRelays)
	within3 := make([]int, nRelays)
	// Route-seconds within the switching margin of the best path (max(5%, 2 ms)),
	// and more than 10% above it.
	withinMargin, over10, cachedWithinMargin, cachedOver10 := 0, 0, 0, 0
	optDist := make([]float64, nRelays*nRelays)
	optToll := make([]int, nRelays*nRelays)
	secCosts := make([]float64, len(sr.lines))
	prevSecCosts := make([]float64, len(sr.lines))
	for i := range prevSecCosts {
		prevSecCosts[i] = -1
	}

	loopActive := make([]int, nRelays*nRelays) // consecutive loop seconds for (src, dst)
	loopEvents := 0
	loopTotalSec := 0
	loopMaxSec := 0
	anyLoopActive := false

	// Route-seconds by line, of the paths last walked; lines that lose probes
	// in at least a fifth of their minutes.
	usage := make([]int, len(sr.lines))
	lossyLine := make([]bool, len(sr.lines))
	for li := range sr.lines {
		lossyMin, upMin := 0, 0
		for m := 0; m+60 <= sr.seconds; m += 60 {
			c := sr.trueCost[li][m+59]
			if math.IsInf(c, 0) {
				continue
			}
			upMin++
			for s := m; s < m+60; s++ {
				if sr.probes[li][s].lost && !math.IsInf(sr.trueCost[li][s], 0) {
					lossyMin++
					break
				}
			}
		}
		lossyLine[li] = upMin > 0 && lossyMin*5 >= upMin
	}
	lossHits, lossyRouteSec := 0, 0
	tallyLoss := func(sec int) {
		for li, n := range usage {
			if n == 0 {
				continue
			}
			if sr.probes[li][sec].lost && !math.IsInf(sr.trueCost[li][sec], 0) {
				lossHits += n
			}
			if lossyLine[li] {
				lossyRouteSec += n
			}
		}
	}

	// Cached per-second path-cost increments reused when neither routes nor true costs changed.
	cachedCost := make([]float64, nRelays)
	cachedSamples := make([]int, nRelays)
	cachedWithin3 := make([]int, nRelays)
	cacheValid := false

	// Next-hop table snapshot per second for fast path walks.
	nhTable := make([]int, nRelays*nRelays)
	seen := make([]int, nRelays)
	seenGen := 0

	baseTime := time.Unix(1_800_000_000, 0)
	for sec := 0; sec < sr.seconds; sec++ {
		secTime := baseTime.Add(time.Duration(sec) * time.Second)
		deliverUntil(secTime)

		// Feed per-second probes for all lines.
		costsChanged := false
		for li, ep := range linePairIdx {
			p := sr.probes[li][sec]
			c := sr.trueCost[li][sec]
			secCosts[li] = c
			if c != prevSecCosts[li] && !(math.IsInf(c, 0) && math.IsInf(prevSecCosts[li], 0)) {
				costsChanged = true
				prevSecCosts[li] = c
			}
			lineUp[li] = !p.lost
			if !p.lost {
				lineDelay[li] = time.Duration(p.rtt / 2.0 * float64(time.Millisecond))
				if lineDelay[li] < time.Millisecond {
					lineDelay[li] = time.Millisecond
				}
				d := time.Duration(p.rtt * float64(time.Millisecond))
				if lr, ok := nodes[ep[0]].(*legacyRouter); ok {
					lr.probeOnly(sr.relays[ep[1]], d)
					nodes[ep[1]].(*legacyRouter).probeOnly(sr.relays[ep[0]], d)
				} else {
					nodes[ep[0]].(*Router).probeOnly(sr.relays[ep[1]], d)
					nodes[ep[1]].(*Router).probeOnly(sr.relays[ep[0]], d)
				}
			} else {
				if lr, ok := nodes[ep[0]].(*legacyRouter); ok {
					lr.lostOnly(sr.relays[ep[1]])
					nodes[ep[1]].(*legacyRouter).lostOnly(sr.relays[ep[0]])
				} else {
					nodes[ep[0]].(*Router).lostOnly(sr.relays[ep[1]])
					nodes[ep[1]].(*Router).lostOnly(sr.relays[ep[0]])
				}
			}
		}

		// Tick and flush Sync/Advert at t + 100ms (after advertGap) and deliver adverts within the second.
		flushAdverts := func(withTick bool) {
			for i := 0; i < nRelays; i++ {
				if withTick && nodes[i].Tick() {
					anyRouteChanged = true
				}
				if withTick {
					var sAdvs []*advert
					var sTargets []string
					if lr, ok := nodes[i].(*legacyRouter); ok {
						sAdvs, sTargets = lr.syncSnap()
					} else {
						sAdvs, sTargets = nodes[i].(*Router).syncSnap()
					}
					for k := range sAdvs {
						sendSnap(i, sTargets[k], sAdvs[k])
					}
				}
				var adv *advert
				var dests []string
				if lr, ok := nodes[i].(*legacyRouter); ok {
					adv, dests = lr.advertSnap(neighbors[i])
				} else {
					adv, dests = nodes[i].(*Router).advertSnap(neighbors[i])
				}
				if adv != nil {
					if sec >= warmSec {
						advertsSent[i]++
					}
					for _, p := range dests {
						sendSnap(i, p, adv)
					}
				}
			}
		}
		// Advertisements go out every 100 ms, as the relays send what is due
		// (advertGap); routers tick once a second.
		flushAdverts(true)
		for step := 1; step < 10; step++ {
			deliverUntil(secTime.Add(time.Duration(step) * 100 * time.Millisecond))
			flushAdverts(false)
		}
		deliverUntil(secTime.Add(990 * time.Millisecond))

		if sec < warmSec {
			anyRouteChanged = true
			continue
		}

		// Recompute ground-truth optimal costs only when synthetic link costs changed.
		if costsChanged {
			computeAllPairsOpt(nRelays, noTransitBool, linePairIdx, secCosts, optDist, optToll)
		}

		// Snapshot next hops only when a route changed.
		snapChanged := anyRouteChanged
		if anyRouteChanged {
			cacheValid = false
			for s := 0; s < nRelays; s++ {
				off := s * nRelays
				if lr, ok := nodes[s].(*legacyRouter); ok {
					for d := 0; d < nRelays; d++ {
						if s == d {
							nhTable[off+d] = d
							continue
						}
						if rt := lr.routes[sr.relays[d]]; rt != nil && rt.nextHop != "" {
							nhTable[off+d] = nodeByID[rt.nextHop]
						} else {
							nhTable[off+d] = -1
						}
					}
				} else {
					rr := nodes[s].(*Router)
					for d := 0; d < nRelays; d++ {
						if s == d {
							nhTable[off+d] = d
							continue
						}
						if rt := rr.routes[sr.relays[d]]; rt != nil && rt.nextHop != "" {
							nhTable[off+d] = nodeByID[rt.nextHop]
						} else {
							nhTable[off+d] = -1
						}
					}
				}
			}
			anyRouteChanged = false
		}

		// Walk every (src, dst) forwarding path for cost, optimality, loops, and outage tracking.
		pathUsesLine := func(s, d, targetLine int) bool {
			seenGen++
			x := s
			for x != d {
				if seen[x] == seenGen {
					return false
				}
				seen[x] = seenGen
				nh := nhTable[x*nRelays+d]
				if nh < 0 {
					return false
				}
				if li := pairLine[x*nRelays+nh]; li >= 0 && li == targetLine {
					return true
				}
				x = nh
			}
			return false
		}

		// Check ongoing tracked outages.
		if len(activeOutages) > 0 {
			keep := activeOutages[:0]
			for _, tr := range activeOutages {
				dt := sec - tr.startSec
				if !pathUsesLine(tr.src, tr.dst, tr.lineIdx) {
					outageTimes = append(outageTimes, float64(dt))
				} else if dt >= 30 {
					// Cap unresolved outage at 30s (e.g. destination partitioned).
					outageTimes = append(outageTimes, 30)
				} else {
					keep = append(keep, tr)
				}
			}
			activeOutages = keep
		}

		// Register newly starting outages (check if any routes currently traverse the failing line).
		if newLines := outageAt[sec]; len(newLines) > 0 {
			for _, li := range newLines {
				for s := 0; s < nRelays; s++ {
					for d := 0; d < nRelays; d++ {
						if s == d || math.IsInf(optDist[s*nRelays+d], 0) {
							continue
						}
						if pathUsesLine(s, d, li) {
							activeOutages = append(activeOutages, trackedOutage{src: s, dst: d, lineIdx: li, startSec: sec})
						}
					}
				}
			}
		}

		// Evaluate all 600 pairs (reuse cached per-second tallies when nothing changed and no loops exist).
		if cacheValid && !costsChanged && !snapChanged && !anyLoopActive {
			for s := 0; s < nRelays; s++ {
				costSum[s] += cachedCost[s]
				costSamples[s] += cachedSamples[s]
				within3[s] += cachedWithin3[s]
			}
			withinMargin += cachedWithinMargin
			over10 += cachedOver10
			tallyLoss(sec)
			continue
		}
		clear(usage)
		clear(cachedCost)
		clear(cachedSamples)
		clear(cachedWithin3)
		cachedWithinMargin, cachedOver10 = 0, 0
		anyLoopActive = false
		for s := 0; s < nRelays; s++ {
			for d := 0; d < nRelays; d++ {
				if s == d {
					continue
				}
				seenGen++
				x := s
				totalC := 0.0
				looped := false
				broken := false
				for x != d {
					if seen[x] == seenGen {
						looped = true
						break
					}
					seen[x] = seenGen
					nh := nhTable[x*nRelays+d]
					if nh < 0 {
						broken = true
						break
					}
					li := pairLine[x*nRelays+nh]
					if li < 0 || math.IsInf(secCosts[li], 0) {
						broken = true
						break
					}
					totalC += secCosts[li]
					x = nh
				}
				pairKey := s*nRelays + d
				if looped {
					anyLoopActive = true
					if loopActive[pairKey] == 0 {
						loopEvents++
					}
					loopActive[pairKey]++
					loopTotalSec++
					if loopActive[pairKey] > loopMaxSec {
						loopMaxSec = loopActive[pairKey]
					}
				} else {
					loopActive[pairKey] = 0
				}
				if !looped && !broken {
					optC := optDist[pairKey]
					if !math.IsInf(optC, 0) && optC > 0 {
						for y := s; y != d; y = nhTable[y*nRelays+d] {
							usage[pairLine[y*nRelays+nhTable[y*nRelays+d]]]++
						}
						cachedCost[s] += totalC
						cachedSamples[s]++
						costSum[s] += totalC
						costSamples[s]++
						if totalC <= optC*1.03+1e-6 {
							cachedWithin3[s]++
							within3[s]++
						}
						if totalC <= optC+max(optC*0.05, 2)+1e-6 {
							cachedWithinMargin++
							withinMargin++
						}
						if totalC > optC*1.10+1e-6 {
							cachedOver10++
							over10++
						}
					}
				}
			}
		}
		tallyLoss(sec)
		cacheValid = !anyLoopActive
	}

	res := replayMetrics{
		Label:          label,
		Noise:          sr.noise,
		HalfLifeS:      customTune.SmoothHalfLifeS,
		SwitchMinMs:    customTune.SwitchMinMs,
		StartPenaltyMs: customTune.StartPenaltyMs,
		ElapsedSec:     math.Round(time.Since(wallStart).Seconds()*100) / 100,
		LoopEvents:     loopEvents,
		LoopTotalSec:   loopTotalSec,
		LoopMaxSec:     loopMaxSec,
		RealLoops:      realLoops,
		TransitLoops:   transitLoops,
		PerRelay:       make([]nodeSummary, nRelays),
	}
	for _, ls := range openLoops {
		for _, l := range ls {
			if int(l.since.Sub(time.Unix(1_800_000_000, 0)).Seconds()) >= warmSec {
				loopLives = append(loopLives, float64(now.Sub(l.since).Milliseconds()))
			}
		}
	}
	if len(loopLives) > 0 {
		sum := 0.0
		for _, v := range loopLives {
			sum += v
			res.RealLoopMaxMs = max(res.RealLoopMaxMs, v)
		}
		res.RealLoopMeanMs = math.Round(sum / float64(len(loopLives)))
	}
	totalCostSum, totalSamples, totalWithin3 := 0.0, 0, 0
	for i, id := range sr.relays {
		avgC := 0.0
		w3 := 0.0
		if costSamples[i] > 0 {
			avgC = math.Round(costSum[i]/float64(costSamples[i])*100) / 100
			w3 = math.Round(float64(within3[i])/float64(costSamples[i])*10000) / 10000
		}
		res.PerRelay[i] = nodeSummary{
			ID:             id,
			Name:           sr.names[id],
			BetterSwitches: betterSw[i],
			Flips5m:        flips5m[i],
			DownSwitches:   downSw[i],
			LoopSwitches:   loopSw[i],
			AdvertsSent:    advertsSent[i],
			AvgPathCostMs:  avgC,
			Within3PctRate: w3,
		}
		res.BetterSwitches += betterSw[i]
		res.Flips5m += flips5m[i]
		res.DownSwitches += downSw[i]
		res.LoopSwitches += loopSw[i]
		res.AdvertsSent += advertsSent[i]
		totalCostSum += costSum[i]
		totalSamples += costSamples[i]
		totalWithin3 += within3[i]
	}
	if totalSamples > 0 {
		res.AvgPathCostMs = math.Round(totalCostSum/float64(totalSamples)*100) / 100
		res.Within3PctRate = math.Round(float64(totalWithin3)/float64(totalSamples)*10000) / 10000
		res.WithinMarginRate = math.Round(float64(withinMargin)/float64(totalSamples)*10000) / 10000
		res.Over10PctRate = math.Round(float64(over10)/float64(totalSamples)*10000) / 10000
		res.LossHitRate = float64(lossHits) / float64(totalSamples)
		res.LossyLineRate = float64(lossyRouteSec) / float64(totalSamples)
	}
	if len(outageTimes) > 0 {
		slices.Sort(outageTimes)
		sum := 0.0
		for _, v := range outageTimes {
			sum += v
		}
		res.OutageRoutes = len(outageTimes)
		res.OutageMeanSec = math.Round(sum/float64(len(outageTimes))*100) / 100
		p95Idx := int(math.Ceil(float64(len(outageTimes))*0.95)) - 1
		if p95Idx < 0 {
			p95Idx = 0
		}
		res.OutageP95Sec = outageTimes[p95Idx]
		res.OutageMaxSec = outageTimes[len(outageTimes)-1]
	}
	if os.Getenv("OBLAB_REPLAY_DEBUG") == "1" {
		fmt.Printf("DEBUG [%s] totalBetter=%d to64ab=%d savedBuckets(<2,2-5,5-20,>=20)=%v\n", label, res.BetterSwitches, swTo64ab, swBuckets)
		cp := slices.Clone(res.PerRelay)
		slices.SortFunc(cp, func(a, b nodeSummary) int { return b.BetterSwitches - a.BetterSwitches })
		for _, ns := range cp[:min(8, len(cp))] {
			fmt.Printf("  node %s: better=%d flips=%d down=%d adverts=%d avgCost=%.1f w3=%.1f%%\n",
				ns.ID[:min(8, len(ns.ID))], ns.BetterSwitches, ns.Flips5m, ns.DownSwitches, ns.AdvertsSent, ns.AvgPathCostMs, ns.Within3PctRate*100)
		}
	}
	return res
}

// replayCandidates: the routers compared on the full replay. Legacy first;
// then the parameters deployed before these defaults, the defaults, and
// around them a shorter half-life and other start penalties. Labels are
// half-life / minimum gap / start penalty.
func replayCandidates() []replayRunOpts {
	mk := func(halfLife, minMs, penalty float64) replayRunOpts {
		tt := DefaultTuning
		tt.SmoothHalfLifeS, tt.SwitchMinMs, tt.StartPenaltyMs = halfLife, minMs, penalty
		return replayRunOpts{label: fmt.Sprintf("%.0fs/%.0fms/%.0f", halfLife, minMs, penalty), tune: tt}
	}
	return []replayRunOpts{
		{label: "legacy", useLegacy: true, tune: DefaultTuning},
		mk(60, 5, 500),
		mk(60, 2, 300),
		mk(60, 2, 150),
		mk(60, 2, 100),
		mk(30, 2, 150),
	}
}

func TestRealDataReplay(t *testing.T) {
	fix := loadReplayFixture(t)
	full := os.Getenv("OBLAB_REPLAY") == "1"
	startMin, numMins := 0, 5 // quick 5-minute smoke during normal go test
	if os.Getenv("OBLAB_REAL_REPLAY") == "1" {
		numMins = 360 // 6-hour slice
	}
	if full {
		startMin, numMins = 0, fix.Minutes
	}
	if v := os.Getenv("OBLAB_REPLAY_START"); v != "" {
		var m int
		if _, err := fmt.Sscanf(v, "%d", &m); err == nil && m >= 0 {
			startMin = m
		}
	}
	if v := os.Getenv("OBLAB_REPLAY_MINS"); v != "" {
		var m int
		if _, err := fmt.Sscanf(v, "%d", &m); err == nil && m > 0 {
			numMins = m
		}
	}
	noises := []string{noiseTriangular}
	specs := []replayRunOpts{{label: "legacy", useLegacy: true, tune: DefaultTuning}, {label: "default", tune: DefaultTuning}}
	if full || os.Getenv("OBLAB_REPLAY_ALL") == "1" {
		noises, specs = []string{noiseTriangular, noiseExcursion}, replayCandidates()
	}
	if v := os.Getenv("OBLAB_REPLAY_NOISE"); v != "" {
		noises = []string{v}
	}
	type job struct {
		sr *synthReplay
		o  replayRunOpts
	}
	var jobs []job
	for _, noise := range noises {
		sr := buildSynthetic(fix, startMin, numMins, noise)
		for _, o := range specs {
			jobs = append(jobs, job{sr, o})
		}
	}
	results := make([]replayMetrics, len(jobs))
	var wg sync.WaitGroup
	for i, j := range jobs {
		wg.Add(1)
		go func() {
			defer wg.Done()
			results[i] = runRealReplayOpts(j.sr, j.o)
		}()
	}
	wg.Wait()

	for _, r := range results {
		t.Logf("%-3s %-12s better=%6d flips5m=%5d down=%5d loopSw=%3d adverts=%7d avgCost=%6.2fms within3%%=%5.2f%% margin=%5.2f%% over10%%=%5.2f%% outage(n=%d mean=%.2fs p95=%.1fs max=%.1fs) loops=%d (mean=%.0fms max=%.0fms, transit %d) lossHit=%.4f%% lossyLine=%.3f%% wall=%.1fs",
			r.Noise, r.Label, r.BetterSwitches, r.Flips5m, r.DownSwitches, r.LoopSwitches, r.AdvertsSent,
			r.AvgPathCostMs, r.Within3PctRate*100, r.WithinMarginRate*100, r.Over10PctRate*100, r.OutageRoutes, r.OutageMeanSec, r.OutageP95Sec, r.OutageMaxSec,
			r.RealLoops, r.RealLoopMeanMs, r.RealLoopMaxMs, r.TransitLoops, r.LossHitRate*100, r.LossyLineRate*100, r.ElapsedSec)
	}
	if full {
		if err := writeReplayReport(results, jobs[0].sr); err != nil {
			t.Errorf("write replay report: %v", err)
		}
	}

	// The defaults against legacy, on each noise model.
	for _, noise := range noises {
		var leg, def replayMetrics
		for i, r := range results {
			if r.Noise != noise {
				continue
			}
			if jobs[i].o.useLegacy {
				leg = r
			} else if jobs[i].o.tune == DefaultTuning {
				def = r
			}
		}
		// What the defaults were chosen at on the 48 hours (report-20261121.md):
		// tri 85% fewer flips, exc 64%, with a quarter less time far from the
		// best path; a 5 ms floor flips less and leaves more paths 10% or more
		// off it.
		if numMins >= 60 {
			if float64(def.BetterSwitches) > float64(leg.BetterSwitches)*0.55 {
				t.Errorf("%s: better switches cut by less than 45%%: new=%d legacy=%d", noise, def.BetterSwitches, leg.BetterSwitches)
			}
			if float64(def.Flips5m) > float64(leg.Flips5m)*0.40 {
				t.Errorf("%s: 5m flips cut by less than 60%%: new=%d legacy=%d", noise, def.Flips5m, leg.Flips5m)
			}
			if def.AvgPathCostMs > leg.AvgPathCostMs*1.01 {
				t.Errorf("%s: average path cost above legacy +1%%: new=%.2f legacy=%.2f", noise, def.AvgPathCostMs, leg.AvgPathCostMs)
			}
			if def.Over10PctRate > leg.Over10PctRate {
				t.Errorf("%s: more time 10%% off the best path than legacy: new=%.4f legacy=%.4f", noise, def.Over10PctRate, leg.Over10PctRate)
			}
		}
		if def.OutageMaxSec > 5.0 || (leg.OutageMaxSec > 0 && def.OutageMaxSec > leg.OutageMaxSec+1.0) {
			t.Errorf("%s: outage failover too slow: new max=%.1fs legacy max=%.1fs", noise, def.OutageMaxSec, leg.OutageMaxSec)
		}
		// Loops as the next hops formed them, however short: no more than
		// legacy's, and none from a move off no-transit relays (11-21).
		if def.RealLoops > leg.RealLoops || def.TransitLoops > 0 {
			t.Errorf("%s: loops: new=%d (%d after a move off no-transit relays, longest %.0fms), legacy=%d", noise, def.RealLoops, def.TransitLoops, def.RealLoopMaxMs, leg.RealLoops)
		}
	}
}

func writeReplayReport(results []replayMetrics, sr *synthReplay) error {
	dir := filepath.Join("..", "..", "..", "workspace", "artifacts", "mesh-replay")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	date := time.Now().Format("20060102")
	jsonPath := filepath.Join(dir, fmt.Sprintf("report-%s.json", date))
	mdPath := filepath.Join(dir, fmt.Sprintf("report-%s.md", date))

	payload := map[string]any{
		"generatedAt": time.Now().Format(time.RFC3339),
		"relays":      len(sr.relays),
		"lines":       len(sr.lines),
		"seconds":     sr.seconds,
		"outages":     len(sr.outages),
		"results":     results,
	}
	raw, err := json.MarshalIndent(payload, "", "  ")
	if err != nil {
		return err
	}
	if err := os.WriteFile(jsonPath, raw, 0o644); err != nil {
		return err
	}

	var b strings.Builder
	fmt.Fprintf(&b, "# 组网平滑选路 48 小时真实数据回放（%s）\n\n", date)
	fixture := "node/mesh/routing/testdata/replay-real.json.gz（修订版 776）"
	if v := os.Getenv("OBLAB_REPLAY_FIXTURE"); v != "" {
		fixture = v
	}
	fmt.Fprintf(&b, "- 数据：`%s`，%d 台中转、%d 条中转线路、%.1f 小时，合成断线 %d 次。\n",
		fixture, len(sr.relays), len(sr.lines), float64(sr.seconds)/3600.0, len(sr.outages))
	fmt.Fprintf(&b, "- 切换门槛 %.0f%%、每跳 %.0f ms；方案名是「平滑半衰期 / 最低差距 / 起步惩罚」。默认值：%.0fs / %.0fms / %.0f。\n",
		DefaultTuning.SwitchPct, DefaultTuning.HopCostMs, DefaultTuning.SmoothHalfLifeS, DefaultTuning.SwitchMinMs, DefaultTuning.StartPenaltyMs)
	fmt.Fprintf(&b, "- 两种噪声：tri 每秒在分钟的最小、最大值之间取三角分布（偏吵）；exc 保持分钟均值，只用 3 秒碰到最小、最大值（偏静）。断线分钟的丢包已扣除断线本身。\n")
	fmt.Fprintf(&b, "- 跳过前 5 分钟；路径代价按合成的真实链路代价（rtt + 丢包 × 1000）沿实际转发路径逐秒计算。「切换门槛内」是离最优路径不超过 max(5%%, 2ms) 的时间占比，「差 10%% 以上」是明显没走最优的时间占比。\n")
	fmt.Fprintf(&b, "- 通告每 100 ms 发一次（各中转按 advertGap 发出到期的），与线上一致；路由器每秒推进一次。环路在每次换路时按全网实际的下一跳检查，记录从形成到被下一次换路打破的时长，不论多短；「离开不做中转引起」是环上有节点在形成前 2 秒内离开过不做中转的节点，而且没有节点因当前路断开而切换（那是断开切换造成的环路）。\n")
	for _, noise := range []string{noiseTriangular, noiseExcursion} {
		var rows []replayMetrics
		for _, r := range results {
			if r.Noise == noise {
				rows = append(rows, r)
			}
		}
		if len(rows) == 0 {
			continue
		}
		leg := rows[0]
		fmt.Fprintf(&b, "\n## 噪声 %s\n\n", noise)
		fmt.Fprintf(&b, "| 方案 | better 换路 | 降幅 | 5 分钟切回 | 降幅 | 断线换路 | 通告 | 平均路径代价 | 代价差 | 最优 3%% 内 | 切换门槛内 | 差 10%% 以上 | 断线切走 均值/P95/最大 | 环路 次数/平均/最长 | 其中离开不做中转引起 |\n")
		fmt.Fprintf(&b, "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---:|\n")
		for _, r := range rows {
			redB, redF, diffC := "—", "—", "—"
			if r.Label != leg.Label {
				redB = fmt.Sprintf("-%.1f%%", (1-float64(r.BetterSwitches)/float64(max(1, leg.BetterSwitches)))*100)
				redF = fmt.Sprintf("-%.1f%%", (1-float64(r.Flips5m)/float64(max(1, leg.Flips5m)))*100)
				diffC = fmt.Sprintf("%+.2f%%", (r.AvgPathCostMs/leg.AvgPathCostMs-1)*100)
			}
			fmt.Fprintf(&b, "| %s | %d | %s | %d | %s | %d | %d | %.2f ms | %s | %.2f%% | %.2f%% | %.2f%% | %.2fs / %.1fs / %.1fs | %d / %.0fms / %.0fms | %d |\n",
				r.Label, r.BetterSwitches, redB, r.Flips5m, redF, r.DownSwitches, r.AdvertsSent,
				r.AvgPathCostMs, diffC, r.Within3PctRate*100, r.WithinMarginRate*100, r.Over10PctRate*100, r.OutageMeanSec, r.OutageP95Sec, r.OutageMaxSec, r.RealLoops, r.RealLoopMeanMs, r.RealLoopMaxMs, r.TransitLoops)
		}
	}
	return os.WriteFile(mdPath, []byte(b.String()), 0o644)
}
