// Package routing chooses each relay's next hop toward every other relay
// from measured link costs that all relays share (README.md).
package routing

import (
	"crypto/ed25519"
	crand "crypto/rand"
	"encoding/binary"
	"errors"
	"fmt"
	"hash/fnv"
	"maps"
	"math"
	"math/rand/v2"
	"slices"
	"sort"
	"sync"
	"time"

	"openbear/node/mesh/sign"
)

const (
	ModeDynamic = "dynamic"
	ModeStatic  = "static"
)

// Route change reasons.
const (
	ReasonDown   = "down"
	ReasonBetter = "better"
	ReasonLoop   = "loop"
)

// Why a route stays on its next hop while another path is cheaper
// (RouteStatus.Held).
const (
	HeldMargin   = "margin"   // cheaper by less than the switch margin
	HeldSettling = "settling" // real cost beats the margin, smoothed cost has not caught up yet; or a move off no-transit relays waits transitWait
	HeldLoop     = "loop"     // its advertised path is not loop-free yet; advertisements are catching up
	HeldTransit  = "transit"  // it goes on through more no-transit relays
)

// Tuning are the parameters of link costs and smoothed route selection
// (README, "Parameters"); DefaultTuning without one.
type Tuning struct {
	// SmoothHalfLifeS is the half-life in seconds of each neighbor's smoothed
	// path cost.
	SmoothHalfLifeS float64
	// A working route moves to another neighbor once both real and smoothed
	// costs beat the current route by at least margin(x) = max(SwitchPct% * x, SwitchMinMs).
	SwitchPct   float64
	SwitchMinMs float64
	// HopCostMs is added to every up link's cost so fewer hops are preferred
	// naturally without a separate shorter-path rule.
	HopCostMs float64
	// StartPenaltyMs is added to a neighbor's initial smoothed cost whenever
	// that neighbor becomes feasible (first seen, link recovered, or loop-free again).
	StartPenaltyMs float64
	// A link costs its round trip, JitterWeight times its jitter,
	// LossWeight times its loss rate (0-1), its bias, narrow and HopCostMs.
	JitterWeight, LossWeight float64
}

var DefaultTuning = Tuning{
	SmoothHalfLifeS: 60,
	SwitchPct:       5,
	SwitchMinMs:     2,
	HopCostMs:       2,
	StartPenaltyMs:  150,
	JitterWeight:    2,
	LossWeight:      1000,
}

func (t *Tuning) margin(x float64) float64 {
	return max(x*t.SwitchPct/100, t.SwitchMinMs)
}

const flipWindow = 5 * time.Minute

// transitWait is how long a move off no-transit relays waits while the route
// in use still works, checked again against the latest advertisements when
// it is up. A path through one comes back to life on a link coming back, and
// the relays along it move off it too: a neighbor that heard of one move and
// not yet of another would take this relay's old path, which it is leaving,
// and the two would point at each other until their advertisements crossed.
const transitWait = 500 * time.Millisecond

// Config is what the router needs from the applied config.
type Config struct {
	Self     string
	Leaf     bool               // this node is a member: it receives advertisements and never forwards or advertises
	Relays   []string           // every other relay in the mesh
	Leaves   []string           // every other member in the mesh that participates in routing
	Links    []string           // the nodes this one has a link with
	NextHops map[string]string  // configured next hops: the start and static mode
	Mode     string             // dynamic (the default) or static
	Bias     map[string]float64 // ms added to a link's cost, by linked peer
	// NoTransit are the relays, this one included, that forward between two
	// other relays only when no other path is left: going on through one
	// from a relay to a relay costs lastResort.
	NoTransit []string
	Tuning    *Tuning
	// Numbers are the host numbers of the nodes' control addresses, this
	// one's included, that name them in compact advertisements; without a
	// distinct one for every relay, FormatJSON is used.
	Numbers map[string]uint32
	// Network is the mesh's id in signatures. Key signs this relay's
	// advertisements; Keys verify every relay's, and an advertisement not
	// signed by its origin is refused (package sign).
	Network string
	Key     ed25519.PrivateKey
	Keys    map[string]ed25519.PublicKey
	// RelayLinks are the links between relays the mesh has, each pair once;
	// a link an advertisement lists that is not among them is ignored.
	RelayLinks [][2]string
	// MemberLinks are the links between relays and members (or members and
	// members for this node), each pair once; an advertised member link not
	// among them is ignored when set.
	MemberLinks [][2]string
}

type LinkStatus struct {
	Peer                  string
	State                 string
	RTTMs, JitterMs, Loss float64
	Cost                  float64 // measured; +Inf unless up
	Penalty               float64
	// Advertised is the cost this relay gives the link in its
	// advertisements; routes use the larger of the two ends'. +Inf unless up.
	Advertised float64
}

type RouteStatus struct {
	Dest, NextHop string
	Cost          float64 // +Inf without a route
	Smoothed      float64 // smoothed cost of NextHop; +Inf without one
	Path          []string
	Since         time.Time
	Switches      int
	Flips         int
	// Best is the cheaper next hop when the route stays on another,
	// BestCost its real cost (+Inf when none yet), BestSmoothed its smoothed
	// cost (+Inf when unavailable), Held why (Held*).
	Best         string
	BestCost     float64
	BestSmoothed float64
	Held         string
}

type Status struct {
	Mode   string
	Links  []LinkStatus
	Routes []RouteStatus
	Drops  AdvertDrops
}

// AdvertDrops counts the advertisements Receive turned away, by why, since
// the router started.
type AdvertDrops struct {
	OriginRate uint64 // an origin's new ones past originRate
	Throttled  uint64 // a neighbor's past its senderBurst of invalid ones, not read
	Signature  uint64 // not signed by their origin
	Malformed  uint64 // not decoded
	Unlinked   uint64 // from a relay without a link
}

type route struct {
	nextHop  string
	since    time.Time
	switches int
	flips    int
	// dynamic is set once the advertisements gave this destination a route;
	// until then the configured next hop is used.
	dynamic bool
	// left is the next hop the last move left, at leftAt, broken when the
	// route in use broke.
	left   string
	leftAt time.Time
	broken bool
	// Per-neighbor smoothed costs indexed by Router.linkNames, each one's
	// rate (smoothRate index), drawn when it starts, and how far above the
	// real cost it started.
	smooth    []float64
	smoothSet []bool
	rate      []uint8
	lead      []float64
	// What the last choice found: RouteStatus.Best, BestCost, BestSmoothed, Held.
	best         string
	bestCost     key
	bestSmoothed float64
	held         string
	// transitSince is when a move off no-transit relays came up while the
	// route in use still worked; zero while none is waiting (transitWait).
	transitSince time.Time
}

// moved records a move from old to next for reason.
func (rt *route) moved(old, next, reason string, now time.Time) {
	if old == "" {
		return
	}
	voluntary := reason == ReasonBetter
	if voluntary && !rt.broken && next == rt.left && now.Sub(rt.leftAt) < flipWindow {
		rt.flips++
	}
	rt.left, rt.leftAt, rt.broken = old, now, !voluntary
}

type Router struct {
	mu    sync.Mutex
	cfg   Config
	now   func() time.Time
	event func(kind, detail string)
	rng   *rand.Rand

	known  map[string]bool // every relay, this one included if a relay
	leaves map[string]bool // every member in routing, this one included if a leaf
	// noTransit is cfg.NoTransit of the relays known.
	noTransit map[string]bool
	// tuning is cfg.Tuning, or DefaultTuning; every metric points to it.
	tuning    Tuning
	order     []string // the other nodes (relays and leaves), sorted; the order of an advertisement's routes
	links     map[string]*metric
	linkNames []string       // sorted keys of links, matching route.smooth indices
	linkIdx   map[string]int // peer -> index in linkNames
	lsdb      map[string]*advert
	routes    map[string]*route
	routeList []*route // parallel to order, avoiding per-tick map lookup
	graph     *graph
	// stale says that what build reads changed since graph was built: links
	// as this relay advertises them, the links and members of the adverts
	// held, or the config.
	stale    bool
	smoothAt time.Time

	seq       uint64
	sent      time.Time // this relay's last advertisement
	dueUrgent bool      // a state/route/config change waits to be advertised (advertGap)
	dueCost   bool      // a pure cost change waits to be advertised (costAdvertGap)
	tooLarge  bool
	sync      map[string]bool // neighbors that get the whole database next
	origins   map[string]*bucket
	senders   map[string]*bucket
	drops     AdvertDrops

	num map[string]uint32 // compact names, every relay's, or nil
	ids map[uint32]string
	// real is cfg.RelayLinks as pairs in order, realMembers cfg.MemberLinks;
	// unknownAt when each origin last had an event for listing another link,
	// clockAt for a sequence far off this relay's clock.
	real        map[[2]string]bool
	realMembers map[[2]string]bool
	unknownAt   map[string]time.Time
	clockAt     map[string]time.Time
	// verified counts the advertisements whose signatures held.
	verified uint64
	// Static topology cache rebuilt only in configure() and reused by build().
	topoNodes     []string
	topoIndex     map[string]int32
	topoLeaf      []bool
	topoNoTransit []bool
	topoSelfAt    int32
	topoOrderID   []int32
	topoLinkID    []int32
	topoSelfLinks []*metric // indexed by topoIndex for O(1) build lookup
	topoTie       []int64   // n*n precomputed link tie-breakers
}

func newNodeRNG(self string) *rand.Rand {
	var buf [16]byte
	if _, err := crand.Read(buf[:]); err == nil {
		return rand.New(rand.NewPCG(binary.LittleEndian.Uint64(buf[:8]), binary.LittleEndian.Uint64(buf[8:])))
	}
	h := fnv.New64a()
	h.Write([]byte(self))
	s := h.Sum64()
	return rand.New(rand.NewPCG(s, s^0x9e3779b97f4a7c15))
}

// New makes a router; now and event may be nil for the wall clock and no events.
func New(c Config, now func() time.Time, event func(kind, detail string)) *Router {
	if now == nil {
		now = time.Now
	}
	if event == nil {
		event = func(string, string) {}
	}
	r := &Router{now: now, event: event, rng: newNodeRNG(c.Self), links: map[string]*metric{}, linkIdx: map[string]int{},
		lsdb: map[string]*advert{}, routes: map[string]*route{},
		sync: map[string]bool{}, origins: map[string]*bucket{}, senders: map[string]*bucket{}, unknownAt: map[string]time.Time{}, clockAt: map[string]time.Time{}}
	r.configure(c)
	return r
}

func (r *Router) Configure(c Config) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.configure(c)
}

func (r *Router) configure(c Config) bool {
	if c.Mode == "" {
		c.Mode = ModeDynamic
	}
	tuning := DefaultTuning
	if c.Tuning != nil {
		tuning = *c.Tuning
	}
	now := r.now()
	retune := r.tuning != tuning
	r.cfg, r.tuning = c, tuning
	r.known = map[string]bool{}
	r.leaves = map[string]bool{}
	if c.Leaf {
		r.leaves[c.Self] = true
	} else {
		r.known[c.Self] = true
	}
	r.order = nil
	for _, id := range c.Relays {
		if id != c.Self && !r.known[id] && !r.leaves[id] {
			r.known[id] = true
			r.order = append(r.order, id)
		}
	}
	for _, id := range c.Leaves {
		if id != c.Self && !r.known[id] && !r.leaves[id] {
			r.leaves[id] = true
			r.order = append(r.order, id)
		}
	}
	sort.Strings(r.order)
	r.noTransit = map[string]bool{}
	for _, id := range c.NoTransit {
		if r.known[id] {
			r.noTransit[id] = true
		}
	}
	r.num, r.ids = map[string]uint32{}, map[uint32]string{}
	for id := range r.known {
		n, ok := c.Numbers[id]
		if _, taken := r.ids[n]; !ok || n == 0 || taken {
			r.num, r.ids = nil, nil
			break
		}
		r.num[id], r.ids[n] = n, id
	}
	if r.num != nil {
		for id := range r.leaves {
			if n, ok := c.Numbers[id]; ok && n != 0 {
				if _, taken := r.ids[n]; !taken {
					r.num[id], r.ids[n] = n, id
				}
			}
		}
	}
	r.real = map[[2]string]bool{}
	for _, p := range c.RelayLinks {
		r.real[pair(p[0], p[1])] = true
	}
	r.realMembers = map[[2]string]bool{}
	for _, p := range c.MemberLinks {
		r.realMembers[pair(p[0], p[1])] = true
	}
	for o, a := range r.lsdb {
		// What a stored advertisement lists is held to the new links too.
		r.keepReal(o, a, false)
	}
	linked := map[string]bool{}
	for _, p := range c.Links {
		if (!r.known[p] && !r.leaves[p]) || p == c.Self {
			continue
		}
		linked[p] = true
		m := r.links[p]
		if m == nil {
			m = newMetric(c.Bias[p], &r.tuning)
			r.links[p] = m
		}
		if b := c.Bias[p]; b != m.bias || retune {
			m.bias = b
			if m.state() == StateUp {
				m.advCost = round1(m.cost())
				r.dueUrgent = true
			}
		}
	}
	for p := range r.links {
		if !linked[p] {
			delete(r.links, p)
			delete(r.sync, p)
			r.dueUrgent = true
		}
	}
	oldLinkIdx := r.linkIdx
	newLinkNames := make([]string, 0, len(r.links))
	for p := range r.links {
		newLinkNames = append(newLinkNames, p)
	}
	sort.Strings(newLinkNames)
	linksChanged := !slices.Equal(newLinkNames, r.linkNames)
	if linksChanged {
		r.linkNames = newLinkNames
		r.linkIdx = make(map[string]int, len(newLinkNames))
		for i, p := range newLinkNames {
			r.linkIdx[p] = i
		}
	}
	for o := range r.lsdb {
		if !r.known[o] || o == c.Self {
			delete(r.lsdb, o)
		}
	}
	for d := range r.routes {
		if (!r.known[d] && !r.leaves[d]) || d == c.Self {
			delete(r.routes, d)
		}
	}
	r.routeList = make([]*route, len(r.order))
	for i, d := range r.order {
		rt := r.routes[d]
		if rt == nil {
			rt = &route{since: now, smooth: make([]float64, len(r.linkNames)), smoothSet: make([]bool, len(r.linkNames)),
				rate: make([]uint8, len(r.linkNames)), lead: make([]float64, len(r.linkNames)), bestSmoothed: math.Inf(1)}
			r.routes[d] = rt
		} else if linksChanged {
			ns := make([]float64, len(r.linkNames))
			nset := make([]bool, len(r.linkNames))
			nrate := make([]uint8, len(r.linkNames))
			nlead := make([]float64, len(r.linkNames))
			for i, p := range r.linkNames {
				if oi, ok := oldLinkIdx[p]; ok && oi < len(rt.smoothSet) && rt.smoothSet[oi] {
					ns[i], nset[i], nrate[i], nlead[i] = rt.smooth[oi], true, rt.rate[oi], rt.lead[oi]
				}
			}
			rt.smooth, rt.smoothSet, rt.rate, rt.lead = ns, nset, nrate, nlead
		}
		r.routeList[i] = rt
	}
	r.stale = true
	r.rebuildTopoCache()
	for _, a := range r.lsdb {
		r.indexAdvertRoutes(a)
	}
	r.graph = nil
	return r.recompute(now)
}

func (r *Router) indexAdvertRoutes(a *advert) {
	if a == nil || len(r.topoNodes) == 0 {
		if a != nil {
			a.routesByID = nil
			a.linksByID = nil
		}
		return
	}
	n := len(r.topoNodes)
	larr := make([]advLink, n)
	for p, l := range a.links {
		if pi, ok := r.topoIndex[p]; ok {
			larr[pi] = l
		}
	}
	for p, l := range a.members {
		if pi, ok := r.topoIndex[p]; ok {
			larr[pi] = l
		}
	}
	a.linksByID = larr
	if a.routes == nil {
		a.routesByID = nil
		return
	}
	arr := make([]int32, n)
	for i := range arr {
		arr[i] = -1
	}
	for dest, nh := range a.routes {
		if di, ok1 := r.topoIndex[dest]; ok1 {
			if nhi, ok2 := r.topoIndex[nh]; ok2 {
				arr[di] = nhi
			}
		}
	}
	a.routesByID = arr
}

func (r *Router) rebuildTopoCache() {
	nodes := append([]string{r.cfg.Self}, r.order...)
	slices.Sort(nodes)
	n := len(nodes)
	idx := make(map[string]int32, n)
	leaf := make([]bool, n)
	nt := make([]bool, n)
	for i, id := range nodes {
		idx[id] = int32(i)
		leaf[i], nt[i] = r.leaves[id], r.noTransit[id]
	}
	orderID := make([]int32, len(r.order))
	for i, d := range r.order {
		orderID[i] = idx[d]
	}
	linkID := make([]int32, len(r.linkNames))
	for i, p := range r.linkNames {
		if id, ok := idx[p]; ok {
			linkID[i] = id
		} else {
			linkID[i] = -1
		}
	}
	tie := make([]int64, n*n)
	for i := 0; i < n; i++ {
		for j := i + 1; j < n; j++ {
			t := linkKey(nodes[i], nodes[j], 1).tie
			tie[i*n+j] = t
			tie[j*n+i] = t
		}
	}
	selfLinks := make([]*metric, n)
	for p, m := range r.links {
		if pi, ok := idx[p]; ok {
			selfLinks[pi] = m
		}
	}
	r.topoNodes = nodes
	r.topoIndex = idx
	r.topoLeaf = leaf
	r.topoNoTransit = nt
	r.topoSelfAt = idx[r.cfg.Self]
	r.topoOrderID = orderID
	r.topoLinkID = linkID
	r.topoSelfLinks = selfLinks
	r.topoTie = tie
}

// probeOnly / lostOnly update a link metric without immediately running
// recompute; used by tick-driven replay simulations that batch all link probes
// of the same second before calling Tick().
func (r *Router) probeOnly(peer string, rtt time.Duration) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if m := r.links[peer]; m != nil {
		m.sample(rtt, false, r.now())
	}
}

func (r *Router) lostOnly(peer string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if m := r.links[peer]; m != nil {
		m.sample(0, true, r.now())
	}
}

// Probe records a probe of a linked relay that came back after rtt.
func (r *Router) Probe(peer string, rtt time.Duration) bool {
	return r.measure(peer, func(m *metric, now time.Time) { m.sample(rtt, false, now) })
}

// Lost records a probe of a linked relay that did not come back.
func (r *Router) Lost(peer string) bool {
	return r.measure(peer, func(m *metric, now time.Time) { m.sample(0, true, now) })
}

// Gone records that the link's path went away: the link is down at once.
func (r *Router) Gone(peer string) bool {
	return r.measure(peer, func(m *metric, now time.Time) { m.gone(now) })
}

// Down says whether the link to peer lost its last probes in a row: a
// suppressed link that answers is not down.
func (r *Router) Down(peer string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	m := r.links[peer]
	return m != nil && m.down
}

// narrowCost is what a link that carries less than the network MTU from
// this end adds to its cost, in ms: packets over it are held to less, so
// routes take it only where nothing else comes within that.
const narrowCost = 50.0

// SetNarrow says which linked peers' links carry less than the network MTU
// from this end. The cost a link advertises follows at once; next hops move
// at the next Tick.
func (r *Router) SetNarrow(peers map[string]bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for p, m := range r.links {
		n := 0.0
		if peers[p] {
			n = narrowCost
		}
		if n == m.narrow {
			continue
		}
		m.narrow = n
		if m.state() == StateUp {
			m.advCost = round1(m.cost())
			r.dueUrgent, r.stale = true, true
		}
	}
}

func (r *Router) measure(peer string, f func(*metric, time.Time)) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	m := r.links[peer]
	if m == nil {
		return false
	}
	now := r.now()
	f(m, now)
	for _, p := range r.keep() {
		if p != peer {
			r.settle(p, r.links[p])
		}
	}
	r.settle(peer, m)
	return r.recompute(now)
}

// keep leaves a member a link while every one it has that answers is
// suppressed: damping is to steer around a flapping link, not to cut the
// member off the mesh. A member keeps its best; for the way back, one relay
// keeps its link to it (lastToLeaf). Links between relays are left to
// damping: a relay has other relays to route through. It says whose kept
// changed.
func (r *Router) keep() []string {
	want := map[string]bool{}
	if r.cfg.Leaf {
		pick, best := "", math.Inf(1)
		bestRank := 2
		for p, m := range r.links {
			if !m.working() {
				continue
			}
			// Leaves do not advertise. Follow the relay's usable return edge
			// before local cost, otherwise the two ends can keep different links.
			rank := 1 // unknown peer advertisement: retain N-1/static fallback
			if a := r.lsdb[p]; a != nil {
				if l, ok := a.members[r.cfg.Self]; ok {
					if l.state != StateUp {
						continue
					}
					rank = 0
				}
			}
			if !m.suppressed {
				pick = ""
				break
			}
			if c := m.measured(); rank < bestRank || rank == bestRank && (c < best || c == best && p < pick) {
				pick, best, bestRank = p, c, rank
			}
		}
		if pick != "" {
			want[pick] = true
		}
	} else {
		for p, m := range r.links {
			want[p] = r.leaves[p] && m.suppressed && m.working() && r.lastToLeaf(p)
		}
	}
	var changed []string
	for p, m := range r.links {
		if want[p] != m.kept {
			m.kept = want[p]
			changed = append(changed, p)
		}
	}
	return changed
}

// lastToLeaf says whether this relay is the one to keep its suppressed link
// to leaf: no other relay advertises one that is up, and none named before
// it one that is suppressed. Kept, the link is advertised up, so the others
// leave theirs suppressed.
func (r *Router) lastToLeaf(leaf string) bool {
	for o, a := range r.lsdb {
		if o == r.cfg.Self {
			continue
		}
		if l, ok := a.members[leaf]; ok && (l.state == StateUp || l.state == StateSuppressed && o < r.cfg.Self) {
			return false
		}
	}
	return true
}

func (r *Router) settle(peer string, m *metric) {
	before := m.advState
	changed, stateChanged := m.settle()
	if !changed {
		return
	}
	r.stale = true
	if stateChanged {
		r.dueUrgent = true
	} else {
		r.dueCost = true
	}
	switch {
	case m.advState == StateSuppressed:
		r.event("link.suppressed", fmt.Sprintf("%s: penalty %.0f", peer, m.penalty))
	case m.kept && before == StateSuppressed:
		r.event("link.kept", fmt.Sprintf("%s: penalty %.0f", peer, m.penalty))
	case before == StateSuppressed:
		r.event("link.reused", fmt.Sprintf("%s: penalty %.0f", peer, m.penalty))
	}
	if !r.cfg.Leaf && m.advState == StateUp && before != StateUp && (r.known[peer] || r.leaves[peer]) {
		// The neighbor may have restarted: it gets the whole database.
		r.sync[peer] = true
	}
}

// Tick lets penalties decay, advertised costs settle, smoothed costs advance
// and advertisements expire. It says whether a next hop changed.
func (r *Router) Tick() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	now := r.now()
	for _, m := range r.links {
		m.decay(now)
	}
	r.keep()
	for p, m := range r.links {
		r.settle(p, m)
	}
	for o, a := range r.lsdb {
		if now.Sub(a.at) > advertExpiry {
			delete(r.lsdb, o)
			r.stale = true
			if r.graph != nil {
				r.graph.forget()
			}
		}
	}
	return r.recompute(now)
}

// Advert is this relay's advertisement for peers when one is due, nil
// otherwise: compact when every relay has a number, JSON otherwise, and
// signed. Routes are left out of an advertisement that would not fit its
// budget. Relays forward it as it is, so the budget is the one of the
// longest envelope any relay puts it in.
func (r *Router) Advert(b Budget, peers []string) []Message {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.cfg.Leaf {
		return nil
	}
	now := r.now()
	since := now.Sub(r.sent)
	urgentReady := r.dueUrgent && since >= advertGap
	costReady := r.dueCost && since >= costAdvertGap
	if !r.sent.IsZero() && !urgentReady && !costReady && since < advertEvery {
		return nil
	}
	a := &advert{origin: r.cfg.Self, seq: max(r.seq+1, uint64(now.UnixMicro())), links: map[string]advLink{}, members: map[string]advLink{}, routes: map[string]string{}}
	for p, m := range r.links {
		if r.leaves[p] {
			a.members[p] = advLink{m.advCost, m.advState}
		} else {
			a.links[p] = advLink{m.advCost, m.advState}
		}
	}
	for d, rt := range r.routes {
		if rt.nextHop == "" {
			continue
		}
		// Next hops to members too: the others follow them as they do those
		// to relays, where a held route may be no one's shortest. A member
		// without a host number would leave every route out of a compact
		// advertisement.
		if r.known[d] || r.leaves[d] && (r.num == nil || r.num[d] != 0) {
			a.routes[d] = rt.nextHop
		}
	}
	ok := r.encode(a, b)
	if !ok {
		if !r.tooLarge {
			r.event("routing.error", fmt.Sprintf("advertisement exceeds %d bytes (compact %d)", b.JSON, b.Compact))
		}
		r.tooLarge = true
	} else {
		r.tooLarge = false
	}
	r.sent, r.dueUrgent, r.dueCost = now, false, false
	var destPeers []string
	for _, p := range peers {
		if r.known[p] || r.leaves[p] {
			destPeers = append(destPeers, p)
		}
	}
	if !ok || len(destPeers) == 0 {
		return nil
	}
	r.seq = a.seq
	return a.messages(destPeers)
}

// encode writes this relay's a in its format within b and signs it; false
// when nothing fits.
func (r *Router) encode(a *advert, b Budget) bool {
	budget := b.JSON
	a.format = FormatJSON
	write := func(order []string, routes bool) []byte { return encodeJSON(a, order, routes) }
	if r.num != nil {
		a.format, budget = FormatCompact, b.Compact
		write = func(order []string, routes bool) []byte {
			raw, _ := encodeCompact(a, r.num, order, routes)
			return raw
		}
	}
	// Over budget, the routes to members go first, then every route.
	a.raw = nil
	for i := range 3 {
		order := r.order
		if i == 1 {
			order = slices.DeleteFunc(slices.Clone(r.order), func(d string) bool { return r.leaves[d] })
		}
		if raw := write(order, i < 2); raw != nil && len(raw) <= budget {
			a.raw = raw
			break
		}
	}
	if a.raw == nil {
		return false
	}
	a.sig = nil
	if len(r.cfg.Key) == ed25519.PrivateKeySize {
		a.sig = advertInput(r.cfg.Network, a.format, a.raw).Sign(r.cfg.Key)
	}
	return true
}

// advertInput is what an advertisement's signature covers: its format and
// bytes, as the origin wrote them.
func advertInput(network string, f Format, raw []byte) *sign.Input {
	return sign.New(sign.Advert, network).Uint(uint64(f)).Bytes(raw)
}

// messages is a, as its origin wrote and signed it, for each of peers.
func (a *advert) messages(peers []string) []Message {
	out := make([]Message, 0, len(peers))
	for _, p := range peers {
		out = append(out, Message{Peer: p, Format: a.format, Raw: a.raw, Sig: a.sig})
	}
	return out
}

// Sync is every advertisement this relay holds for each neighbor whose
// link just came up, so they need not wait for refreshes.
func (r *Router) Sync(b Budget) []Message {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.cfg.Leaf || len(r.sync) == 0 {
		r.sync = map[string]bool{}
		return nil
	}
	var out []Message
	for _, p := range slices.Sorted(func(yield func(string) bool) {
		for p := range r.sync {
			if !yield(p) {
				return
			}
		}
	}) {
		if !r.known[p] && !r.leaves[p] {
			continue
		}
		for _, o := range r.order {
			if a := r.lsdb[o]; a != nil {
				out = append(out, a.messages([]string{p})...)
			}
		}
	}
	r.sync = map[string]bool{}
	return out
}

var errUnlinked = errors.New("advert from a relay without a link")
var errRate = errors.New("advert rate exceeded")
var errThrottled = errors.New("throttled after invalid advertisements")
var errNoNumbers = errors.New("compact advert without control addresses for every relay")

// ErrSignature: an advertisement not signed by the relay it is from.
var ErrSignature = errors.New("advert not signed by its origin")

// Receive takes an advertisement a linked relay sent. A new one is stored
// and returned, as its origin signed it, for the neighbors to forward it
// to; changed says whether a next hop changed. The same seq again is new
// only when it brings the routes the one held left out.
func (r *Router) Receive(from string, msg Message, b Budget) (forward []Message, changed bool, err error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	now := r.now()
	if r.links[from] == nil || !r.known[from] {
		r.drops.Unlinked++
		return nil, false, errUnlinked
	}
	s := r.senders[from]
	if s == nil {
		s = &bucket{}
		r.senders[from] = s
	}
	if !s.ready(now, senderRate, senderBurst) {
		r.drops.Throttled++
		return nil, false, errThrottled
	}
	var a *advert
	if msg.Format == FormatCompact {
		if r.num == nil {
			return nil, false, errNoNumbers
		}
		if origin, seq, ok := peekCompactHeader(msg.Raw, r.ids, r.known); ok {
			if origin == r.cfg.Self {
				return nil, false, nil
			}
			if old := r.lsdb[origin]; old != nil && (seq < old.seq || (seq == old.seq && old.routes != nil)) {
				return nil, false, nil
			}
		}
		if a, err = decodeCompact(msg.Raw, r.ids, r.known, r.leaves); errors.Is(err, errNewerFormat) {
			return nil, false, nil
		}
	} else {
		a, err = decodeJSON(msg.Raw, r.known, r.leaves)
	}
	if err != nil {
		s.take(now, senderRate, senderBurst)
		r.drops.Malformed++
		return nil, false, err
	}
	if a.origin == r.cfg.Self {
		return nil, false, nil
	}
	if old := r.lsdb[a.origin]; old != nil && (a.seq < old.seq || (a.seq == old.seq && (old.routes != nil || a.routes == nil))) {
		return nil, false, nil
	}
	// Verified after the cheap checks: an old advertisement costs nothing.
	if !advertInput(r.cfg.Network, msg.Format, msg.Raw).Verify(r.cfg.Keys[a.origin], msg.Sig) {
		s.take(now, senderRate, senderBurst)
		r.drops.Signature++
		return nil, false, ErrSignature
	}
	r.verified++
	o := r.origins[a.origin]
	if o == nil {
		o = &bucket{}
		r.origins[a.origin] = o
	}
	if !o.take(now, originRate, originBurst) {
		r.drops.OriginRate++
		return nil, false, fmt.Errorf("%w by %s", errRate, a.origin)
	}
	a.at, a.format, a.raw, a.sig = now, msg.Format, slices.Clone(msg.Raw), slices.Clone(msg.Sig)
	r.keepReal(a.origin, a, true)
	r.checkClock(a, now)
	old := r.lsdb[a.origin]
	linksChanged := old == nil || !maps.Equal(old.links, a.links) || !maps.Equal(old.members, a.members)
	routesChanged := old == nil || (old.routes == nil) != (a.routes == nil) || !maps.Equal(old.routes, a.routes)
	if !linksChanged && !routesChanged {
		// Pure periodic refresh: update seq/at/raw/sig for expiry & forwarding
		// without invalidating cached paths or recomputing routes.
		old.seq, old.at, old.format, old.raw, old.sig = a.seq, a.at, a.format, a.raw, a.sig
		var peers []string
		if !r.cfg.Leaf {
			for _, p := range r.cfg.Links {
				if p != from && p != a.origin && r.links[p] != nil && (r.known[p] || r.leaves[p]) {
					peers = append(peers, p)
				}
			}
		}
		return old.messages(peers), false, nil
	}
	if linksChanged {
		r.stale = true
	}
	r.indexAdvertRoutes(a)
	r.lsdb[a.origin] = a
	if r.graph != nil {
		if oi, ok := r.topoIndex[a.origin]; ok && int(oi) < len(r.graph.lsdbByID) {
			r.graph.lsdbByID[oi] = a
		}
		r.graph.forget()
	}
	var peers []string
	if !r.cfg.Leaf {
		for _, p := range r.cfg.Links {
			if p != from && p != a.origin && r.links[p] != nil && (r.known[p] || r.leaves[p]) {
				peers = append(peers, p)
			}
		}
	}
	return a.messages(peers), r.recompute(now), nil
}

// keepReal drops the links a lists that the mesh does not have; an
// origin that lists one gets an event a minute at most.
func (r *Router) keepReal(origin string, a *advert, report bool) {
	var unknown []string
	for p := range a.links {
		if !r.real[pair(origin, p)] {
			delete(a.links, p)
			unknown = append(unknown, p)
		}
	}
	if len(r.realMembers) > 0 {
		for p := range a.members {
			if !r.realMembers[pair(origin, p)] {
				delete(a.members, p)
				unknown = append(unknown, p)
			}
		}
	}
	if len(unknown) == 0 || !report {
		return
	}
	now := r.now()
	if now.Sub(r.unknownAt[origin]) >= time.Minute {
		r.unknownAt[origin] = now
		slices.Sort(unknown)
		r.event("routing.unknown_link", fmt.Sprintf("%s advertises links the mesh does not have, ignored: %v", origin, unknown))
	}
}

// checkClock tells, every ten minutes at most per origin, of a sequence
// (its origin's clock in microseconds) a minute or more off this relay's
// clock. Nothing is refused for it: sequences only need to grow.
func (r *Router) checkClock(a *advert, now time.Time) {
	off := time.Duration(int64(a.seq)-now.UnixMicro()) * time.Microsecond
	if off < time.Minute && off > -time.Minute || now.Sub(r.clockAt[a.origin]) < 10*time.Minute {
		return
	}
	r.clockAt[a.origin] = now
	r.event("routing.clock", fmt.Sprintf("%s's clock is %v off this relay's", a.origin, off.Round(time.Second)))
}

func pair(a, b string) [2]string {
	if b < a {
		a, b = b, a
	}
	return [2]string{a, b}
}

// NextHop is the linked relay that traffic for the relay dest goes to.
func (r *Router) NextHop(dest string) (string, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	rt := r.routes[dest]
	if rt == nil || rt.nextHop == "" {
		return "", false
	}
	return rt.nextHop, true
}

// Verified is how many advertisements' signatures held.
func (r *Router) Verified() uint64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.verified
}

func (r *Router) Status() Status {
	r.mu.Lock()
	defer r.mu.Unlock()
	s := Status{Mode: r.cfg.Mode, Drops: r.drops}
	for _, p := range r.linkNames {
		m := r.links[p]
		adv := math.Inf(1)
		if m.advState == StateUp {
			adv = m.advCost
		}
		s.Links = append(s.Links, LinkStatus{Peer: p, State: m.state(), RTTMs: round1(m.rtt), JitterMs: round1(m.jitter), Loss: m.loss(),
			Cost: round1(m.cost()), Penalty: math.Round(m.penalty), Advertised: adv})
	}
	g := r.graph
	for _, d := range r.order {
		rt := r.routes[d]
		st := RouteStatus{Dest: d, NextHop: rt.nextHop, Cost: math.Inf(1), Smoothed: math.Inf(1), Path: []string{}, Since: rt.since,
			Switches: rt.switches, Flips: rt.flips, Best: rt.best, BestCost: math.Inf(1), BestSmoothed: math.Inf(1), Held: rt.held}
		if rt.nextHop != "" && g != nil {
			if path, cost, ok := g.path(rt.nextHop, d); ok {
				st.Path, st.Cost = path, cost
			}
			if idx, ok := r.linkIdx[rt.nextHop]; ok && idx < len(rt.smoothSet) && rt.smoothSet[idx] {
				st.Smoothed = round1(rt.smooth[idx])
			}
		}
		if rt.best != "" {
			if !rt.bestCost.unreachable() {
				st.BestCost = rt.bestCost.ms()
			}
			if !math.IsInf(rt.bestSmoothed, 0) && !math.IsNaN(rt.bestSmoothed) {
				st.BestSmoothed = round1(rt.bestSmoothed)
			}
		}
		s.Routes = append(s.Routes, st)
	}
	return s
}

// recompute updates smoothed costs and chooses every next hop again, saying
// whether one changed. The graph is built again only when stale.
func (r *Router) recompute(now time.Time) bool {
	if r.graph == nil || r.stale {
		r.graph, r.stale = r.build(), false
	}
	g := r.graph
	// The share of the way to its real cost each smoothed cost moves in this
	// step, by its rate.
	var alphas [smoothRates]float64
	if r.smoothAt.IsZero() {
		r.smoothAt = now
	} else if now.After(r.smoothAt) {
		dt := now.Sub(r.smoothAt).Seconds()
		h := r.tuning.SmoothHalfLifeS
		if h <= 0 {
			h = DefaultTuning.SmoothHalfLifeS
		}
		for k := range alphas {
			alphas[k] = 1 - math.Pow(2, -dt*smoothRate(k)/h)
		}
		r.smoothAt = now
	}
	foreign := len(r.lsdb) > 0
	changed := false
	for oi, d := range r.order {
		rt := r.routeList[oi]
		df := foreign || (r.leaves[d] && r.links[d] != nil && r.links[d].advState == StateUp)
		if !df {
			rt.dynamic = false
		}
		di := g.orderID[oi]
		next, reason := r.choose(d, di, rt, df, &alphas, now)
		if next == rt.best {
			rt.best, rt.bestCost, rt.bestSmoothed, rt.held = "", unreachable, math.Inf(1), ""
		}
		if next == rt.nextHop {
			continue
		}
		old := rt.nextHop
		rt.nextHop, rt.since = next, now
		if old != "" {
			rt.switches++
		}
		rt.moved(old, next, reason, now)
		changed = true
		r.dueUrgent = true
		if reason != "" {
			r.event("route.changed", fmt.Sprintf("%s: %s -> %s (%s)", d, orNone(old), orNone(next), reason))
		}
	}
	return changed
}

func orNone(s string) string {
	if s == "" {
		return "none"
	}
	return s
}

func clearSmooth(rt *route) {
	for i := range rt.smoothSet {
		rt.smoothSet[i] = false
		rt.smooth[i] = 0
	}
}

// smoothRates is how many rates a smoothed cost may move at: each one draws
// its own when it starts, so that relays and routes seeing the same change do
// not move together (babeld randomises its smoothing for the same reason).
const smoothRates = 8

// smoothRate is rate k as a multiple of the half-life's: 0.75 to 1.25.
func smoothRate(k int) float64 { return 0.75 + 0.5*float64(k)/(smoothRates-1) }

// track moves neighbor i's smoothed cost toward its real cost m: it starts at
// m + StartPenaltyMs, more by the flap penalty of the link to the neighbor
// (one step for each down lately, half-life 30 s), then moves by its rate's
// share of the step. A link back from one outage is taken again within a
// minute or two when it is much the better; one going down again and again
// starts each time too high to draw routes before it goes. It never gets
// further above m than it started: one started on a passing spike (a link's
// first probes, a round trip that just fell) held a path 60% cheaper off for
// six minutes.
func (r *Router) track(rt *route, i int, m float64, alphas *[smoothRates]float64) float64 {
	if !rt.smoothSet[i] {
		start := r.tuning.StartPenaltyMs
		if l := r.links[r.linkNames[i]]; l != nil {
			start *= 1 + l.penalty/penaltyStep
		}
		rt.smoothSet[i], rt.smooth[i], rt.rate[i], rt.lead[i] = true, m+start, uint8(r.rng.IntN(smoothRates)), start
		return rt.smooth[i]
	}
	if a := alphas[rt.rate[i]]; a > 0 && rt.smooth[i] != m {
		rt.smooth[i] += (m - rt.smooth[i]) * a
		if math.Abs(m-rt.smooth[i]) < 0.1 {
			rt.smooth[i] = m
		}
	}
	rt.smooth[i] = min(rt.smooth[i], m+rt.lead[i])
	return rt.smooth[i]
}

// smoothBetter orders two candidates by (tolls, smoothed cost, tie-breaker, name).
func smoothBetter(kA key, sA float64, nA string, kB key, sB float64, nB string) bool {
	if kA.tolls != kB.tolls {
		return kA.tolls < kB.tolls
	}
	if sA != sB {
		return sA < sB
	}
	if kA.tie != kB.tie {
		return kA.tie < kB.tie
	}
	return nA < nB
}

// choose is the next hop for d and, when it differs from the current one,
// why. See README, "Choosing a next hop".
func (r *Router) choose(d string, di int32, rt *route, foreign bool, alphas *[smoothRates]float64, now time.Time) (string, string) {
	rt.best, rt.bestCost, rt.bestSmoothed, rt.held = "", unreachable, math.Inf(1), ""
	transitSince := rt.transitSince
	rt.transitSince = time.Time{}
	static := r.cfg.NextHops[d]
	if r.links[static] == nil {
		static = ""
	}
	if static == "" && r.cfg.Leaf && r.cfg.Mode != ModeStatic {
		if r.links[d] != nil && r.links[d].advState == StateUp {
			static = d
		} else {
			for _, l := range r.cfg.Links {
				if r.known[l] && r.links[l] != nil && r.links[l].advState == StateUp {
					static = l
					break
				}
			}
			if static == "" {
				for _, l := range r.cfg.Links {
					if r.known[l] && r.links[l] != nil {
						static = l
						break
					}
				}
			}
		}
	}
	if r.cfg.Mode == ModeStatic || !foreign {
		clearSmooth(rt)
		return static, ""
	}
	g := r.graph
	if di < 0 || g.dist[g.selfAt][di].unreachable() {
		clearSmooth(rt)
		if !rt.dynamic {
			return static, ""
		}
		return "", ReasonDown
	}
	rt.dynamic = true
	cur := rt.nextHop
	curIdx := -1
	if cur != "" {
		if idx, ok := r.linkIdx[cur]; ok {
			curIdx = idx
		}
	}
	var curCost key
	var curUsable, curLoops bool
	if curIdx >= 0 && g.linkUp[curIdx] {
		rest, ok, loops := g.chainID(g.linkID[curIdx], di)
		if ok {
			curCost, curUsable = g.linkCost[curIdx].add(rest), true
		} else {
			curCost, curLoops = unreachable, loops
		}
	} else {
		curCost = unreachable
	}
	reason := ReasonBetter
	switch {
	case cur == "":
	case curLoops:
		reason = ReasonLoop
	case !curUsable:
		reason = ReasonDown
	}
	t := &r.tuning

	// First update cur's smoothed cost so smoothLimit is ready for the single
	// pass over all other neighbors.
	var curMs, curSmooth, realLimit, smoothLimit float64
	if curUsable {
		curMs = curCost.ms()
		curSmooth = r.track(rt, curIdx, curMs, alphas)
		realLimit = curMs - t.margin(curMs) + 1e-9
		smoothLimit = curSmooth - t.margin(curSmooth) + 1e-9
	}

	// Single pass over all direct neighbors:
	// - Maintain smoothed cost for every neighbor with a working, loopless path.
	// - When !curUsable, track best immediate failover candidate (tolls, m_N, tie, name).
	// - When curUsable, track best feasible switch candidate and cheapest real candidate.
	failBest, failKey := "", unreachable
	var switchCand string
	var switchKey key
	var switchSmooth float64
	var realBest string
	realBestKey := curCost
	var realBestSmooth float64

	for i, n := range r.linkNames {
		if i == curIdx && curUsable {
			continue
		}
		if !g.linkUp[i] {
			rt.smoothSet[i] = false
			rt.smooth[i] = 0
			continue
		}
		ni := g.linkID[i]
		var rest key
		var usable bool
		if ni == di {
			usable = true
		} else {
			rest, usable, _ = g.chainID(ni, di)
		}
		if !usable {
			rt.smoothSet[i] = false
			rt.smooth[i] = 0
			continue
		}
		nCost := g.linkCost[i].add(rest)
		m := nCost.ms()
		s := r.track(rt, i, m, alphas)
		if !curUsable {
			// For !curUsable, curCost == unreachable, so ni == di || rest.less(unreachable) holds whenever usable.
			if nCost.less(failKey) || (nCost == failKey && n < failBest) {
				failBest, failKey = n, nCost
			}
			continue
		}
		// Loop-free feasibility check: ni == di || rest.less(curCost).
		if ni != di && !rest.less(curCost) {
			continue
		}
		fewer := nCost.tolls < curCost.tolls
		same := nCost.tolls == curCost.tolls
		if fewer || (same && m <= realLimit && s <= smoothLimit) {
			better := switchCand == ""
			switch {
			case better:
			case fewer && switchKey.tolls < curCost.tolls:
				// A move off no-transit relays is forced, like one off a
				// broken route: by real cost, whatever smoothing says.
				better = nCost.less(switchKey) || (nCost == switchKey && n < switchCand)
			default:
				better = smoothBetter(nCost, s, n, switchKey, switchSmooth, switchCand)
			}
			if better {
				switchCand, switchKey, switchSmooth = n, nCost, s
			}
		}
		if same && nCost.w < realBestKey.w && (realBest == "" || nCost.less(realBestKey) || (nCost == realBestKey && n < realBest)) {
			realBest, realBestKey, realBestSmooth = n, nCost, s
		}
	}

	if !curUsable {
		return failBest, reason
	}
	if switchCand != "" {
		if switchKey.tolls < curCost.tolls {
			if transitSince.IsZero() {
				transitSince = now
			}
			if now.Sub(transitSince) < transitWait {
				rt.transitSince = transitSince
				rt.best, rt.bestCost, rt.bestSmoothed, rt.held = switchCand, switchKey, switchSmooth, HeldSettling
				return cur, ""
			}
		}
		return switchCand, ReasonBetter
	}
	if realBest != "" {
		rt.best, rt.bestCost, rt.bestSmoothed = realBest, realBestKey, realBestSmooth
		// Real cost already beats the switch margin; only the smoothed costs
		// catching up hold the move back.
		if realBestKey.ms() <= realLimit {
			rt.held = HeldSettling
		} else {
			rt.held = HeldMargin
		}
		return cur, ""
	}

	// Report shortest path held back by loop-freedom or no-transit toll.
	if prefIdx := g.first[g.selfAt][di]; prefIdx >= 0 {
		if pref := g.nodes[prefIdx]; pref != cur {
			if !g.feasible(pref, d, curCost) {
				rt.best, rt.held = pref, HeldLoop
				rt.bestCost, _, _ = g.via(pref, d)
			} else if prefCost, ok, _ := g.via(pref, d); ok && prefCost.tolls > curCost.tolls {
				rt.best, rt.bestCost, rt.held = pref, prefCost, HeldTransit
				if idx, has := r.linkIdx[pref]; has && idx < len(rt.smoothSet) && rt.smoothSet[idx] {
					rt.bestSmoothed = rt.smooth[idx]
				}
			}
		}
	}
	return cur, ""
}

// PathDown is the first link between consecutive nodes that is not up as this
// node knows them, its own as it measures them and the others as their ends
// advertise them, or nil when every one is up (fixed paths).
func (r *Router) PathDown(nodes []string) []string {
	if len(nodes) < 2 {
		return []string{}
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	g := r.graph
	if g == nil {
		return []string{nodes[0], nodes[1]}
	}
	for i := 0; i+1 < len(nodes); i++ {
		if _, ok := g.link(nodes[i], nodes[i+1]); !ok {
			return []string{nodes[i], nodes[i+1]}
		}
	}
	return nil
}

// graph is every relay and link as this relay knows them, with the
// shortest distances between all relays. Nodes are numbered in name order,
// so that comparing numbers compares names.
type graph struct {
	self      string
	selfAt    int32
	nodes     []string
	index     map[string]int32
	leaves    map[string]bool
	leaf      []bool
	noTransit []bool
	adj       [][]edge // symmetric
	// dist and first are by source, nil for a leaf other than this node,
	// then by destination: the shortest path's cost and next hop (-1 for
	// none).
	dist     [][]key
	first    [][]int32
	lsdb     map[string]*advert
	lsdbByID []*advert
	// chains holds chain's and relays' answers by n and d numbered, those
	// of epoch current: they hold while the graph and the advertisements
	// do.
	chains []chainMemo
	epoch  uint32
	// Pre-resolved indices and direct link costs matching Router.order and
	// Router.linkNames, avoiding map lookups in the per-tick choose hot loop.
	orderID  []int32
	linkID   []int32
	linkCost []key
	linkUp   []bool
}

type chainMemo struct {
	epoch     uint32 // of cost, ok and loops
	cost      key
	ok, loops bool
	relaysAt  uint32 // epoch of relays
	relays    int
}

type edge struct {
	to int32
	k  key
}

func (g *graph) id(n string) int32 {
	if i, ok := g.index[n]; ok {
		return i
	}
	return -1
}

// link is the cost of the link between a and b while it is up.
func (g *graph) link(a, b string) (key, bool) {
	i, j := g.id(a), g.id(b)
	if i < 0 || j < 0 {
		return key{}, false
	}
	for _, e := range g.adj[i] {
		if e.to == j {
			return e.k, true
		}
	}
	return key{}, false
}

// ensureSrc computes Dijkstra shortest paths from src on demand.
func (g *graph) ensureSrc(src int32) bool {
	if src < 0 || int(src) >= len(g.nodes) {
		return false
	}
	if src != g.selfAt && g.leaf[src] {
		return false
	}
	if g.dist[src] == nil {
		n := len(g.nodes)
		g.dist[src] = make([]key, n)
		g.first[src] = make([]int32, n)
		g.dijkstra(src, make([]bool, n))
	}
	return true
}

// distance is the cost of the shortest path from src to d.
func (g *graph) distance(src, d string) key {
	i, j := g.id(src), g.id(d)
	if i < 0 || j < 0 || !g.ensureSrc(i) {
		return unreachable
	}
	return g.dist[i][j]
}

// hop is the shortest path's next hop from src to d, or "".
func (g *graph) hop(src, d string) string {
	i, j := g.id(src), g.id(d)
	if i < 0 || j < 0 || !g.ensureSrc(i) || g.first[i][j] < 0 {
		return ""
	}
	return g.nodes[g.first[i][j]]
}

// key orders paths: by how many times they go on through a no-transit
// relay from a relay to a relay (tolls), then by cost in tenths of a
// millisecond, then by a tie-breaker every relay derives the same from the
// link's two ends, so all relays agree on one shortest path between any two.
type key struct{ tolls, w, tie int64 }

var unreachable = key{tolls: math.MaxInt32, w: math.MaxInt64 / 4}

// lastResort is what going on through a no-transit relay adds: such a path
// is taken only when no other is up, and left by better once one is.
var lastResort = key{tolls: 1}

func (k key) less(o key) bool {
	if k.tolls != o.tolls {
		return k.tolls < o.tolls
	}
	return k.w < o.w || (k.w == o.w && k.tie < o.tie)
}
func (k key) add(o key) key     { return key{k.tolls + o.tolls, k.w + o.w, k.tie + o.tie} }
func (k key) unreachable() bool { return k.w >= unreachable.w }
func (k key) ms() float64       { return float64(k.w) / 10 }
func linkKey(a, b string, c float64) key {
	if b < a {
		a, b = b, a
	}
	const offset32, prime32 = 2166136261, 16777619
	var h uint32 = offset32
	for i := 0; i < len(a); i++ {
		h ^= uint32(a[i])
		h *= prime32
	}
	h *= prime32 // separator 0x00 (h ^= 0)
	for i := 0; i < len(b); i++ {
		h ^= uint32(b[i])
		h *= prime32
	}
	return key{w: max(1, int64(math.Round(c*10))), tie: int64(h&0xffff) + 1}
}

func (r *Router) build() *graph {
	nodes := r.topoNodes
	n := len(nodes)
	g := &graph{
		self:      r.cfg.Self,
		selfAt:    r.topoSelfAt,
		nodes:     nodes,
		index:     r.topoIndex,
		leaves:    r.leaves,
		leaf:      r.topoLeaf,
		noTransit: r.topoNoTransit,
		adj:       make([][]edge, n),
		dist:      make([][]key, n),
		first:     make([][]int32, n),
		lsdb:      r.lsdb,
		lsdbByID:  make([]*advert, n),
		chains:    make([]chainMemo, n*n),
		epoch:     1,
		orderID:   r.topoOrderID,
		linkID:    r.topoLinkID,
		linkCost:  make([]key, len(r.linkNames)),
		linkUp:    make([]bool, len(r.linkNames)),
	}
	for i, id := range nodes {
		g.lsdbByID[i] = r.lsdb[id]
	}
	selfAt := int(g.selfAt)
	side := func(xi, yi int) (advLink, bool) {
		if xi == selfAt {
			m := r.topoSelfLinks[yi]
			if m == nil {
				return advLink{}, false
			}
			return advLink{m.advCost, m.advState}, true
		}
		a := g.lsdbByID[xi]
		if a == nil {
			return advLink{}, false
		}
		if len(a.linksByID) == n {
			l := a.linksByID[yi]
			return l, l.state != ""
		}
		y := nodes[yi]
		if r.leaves[y] {
			l, ok := a.members[y]
			return l, ok
		}
		l, ok := a.links[y]
		return l, ok
	}
	for i := 0; i < n; i++ {
		for j := i + 1; j < n; j++ {
			la, okA := side(i, j)
			lb, okB := side(j, i)
			if (i == selfAt && !okA) || (j == selfAt && !okB) || (!okA && !okB) {
				continue
			}
			if (okA && la.state != StateUp) || (okB && lb.state != StateUp) {
				continue
			}
			k := key{w: max(1, int64(math.Round(max(la.cost, lb.cost)*10))), tie: r.topoTie[i*n+j]}
			g.adj[i] = append(g.adj[i], edge{int32(j), k})
			g.adj[j] = append(g.adj[j], edge{int32(i), k})
		}
	}
	g.solve()
	for i, pi := range g.linkID {
		if pi >= 0 {
			for _, e := range g.adj[g.selfAt] {
				if e.to == pi {
					g.linkCost[i] = e.k
					g.linkUp[i] = true
					break
				}
			}
		}
	}
	return g
}

// solve computes the shortest paths from this node; other relays are solved
// lazily on demand in ensureSrc (only needed when an advert omits routes or
// when routing to leaves).
func (g *graph) solve() {
	g.ensureSrc(g.selfAt)
}

// dijkstra fills g.dist[src] and g.first[src]; done is scratch, one per node.
func (g *graph) dijkstra(src int32, done []bool) {
	dist, first := g.dist[src], g.first[src]
	for i := range dist {
		dist[i], first[i], done[i] = unreachable, -1, false
	}
	dist[src] = key{}
	for {
		u := int32(-1)
		for n := range dist {
			if !done[n] && !dist[n].unreachable() && (u < 0 || dist[n].less(dist[u])) {
				u = int32(n)
			}
		}
		if u < 0 {
			return
		}
		done[u] = true
		if u != src && g.leaf[u] {
			continue
		}
		for _, e := range g.adj[u] {
			v := e.to
			if done[v] {
				continue
			}
			nd := dist[u].add(e.k)
			hop := first[u]
			if u == src {
				hop = v
			} else {
				nd = nd.add(g.toll(u, v, g.leaf[src] && first[u] == u))
			}
			if nd.less(dist[v]) || (nd == dist[v] && hop < first[v]) {
				dist[v], first[v] = nd, hop
			}
		}
	}
}

// toll is what going on from relay u to v adds to a path: lastResort when
// u does not forward between two other relays, it was not entered from the
// member that uses it as an entry (fromMember), and v is a relay. A path
// to a member ends at the relays it enters by, which carry it as their own.
func (g *graph) toll(u, v int32, fromMember bool) key {
	if g.noTransit[u] && !fromMember && !g.leaf[v] {
		return lastResort
	}
	return key{}
}

// via is the cost from this relay through neighbor n to d along the next
// hops the relays on the way advertise. ok is false when that path is
// broken or comes back here (loops).
func (g *graph) via(n, d string) (cost key, ok, loops bool) {
	if n == "" {
		return unreachable, false, false
	}
	k, linked := g.link(g.self, n)
	if !linked {
		return unreachable, false, false
	}
	rest, ok, loops := g.chain(n, d)
	if !ok {
		return unreachable, false, loops
	}
	return k.add(rest), true, false
}

func (g *graph) chainID(ni, di int32) (cost key, ok, loops bool) {
	if ni < 0 || di < 0 {
		return unreachable, false, false
	}
	if g.chains == nil {
		g.chains = make([]chainMemo, len(g.nodes)*len(g.nodes))
	}
	c := &g.chains[int(ni)*len(g.nodes)+int(di)]
	if c.epoch != g.epoch {
		cost, ok, loops := g.walkID(ni, di)
		c.epoch, c.cost, c.ok, c.loops = g.epoch, cost, ok, loops
	}
	return c.cost, c.ok, c.loops
}

// chain is the cost from n to d along advertised next hops. A relay that
// left its routes out of its advertisement is assumed to use its shortest
// path.
func (g *graph) chain(n, d string) (cost key, ok, loops bool) {
	i, j := g.id(n), g.id(d)
	if i < 0 || j < 0 {
		return g.walk(n, d)
	}
	return g.chainID(i, j)
}

// memo is where chain and relays keep their answers for n and d, nil for a
// node not in the graph.
func (g *graph) memo(n, d string) *chainMemo {
	i, j := g.id(n), g.id(d)
	if i < 0 || j < 0 {
		return nil
	}
	if g.chains == nil {
		g.chains = make([]chainMemo, len(g.nodes)*len(g.nodes))
	}
	return &g.chains[int(i)*len(g.nodes)+int(j)]
}

// forget drops what chain and relays learned: an advertisement held changed.
func (g *graph) forget() {
	if g.epoch++; g.epoch == 0 {
		clear(g.chains)
		g.epoch = 1
	}
}

func (g *graph) linkByID(i, j int32) (key, bool) {
	for _, e := range g.adj[i] {
		if e.to == j {
			return e.k, true
		}
	}
	return key{}, false
}

func (g *graph) walk(n, d string) (cost key, ok, loops bool) {
	ni, di := g.id(n), g.id(d)
	if ni < 0 || di < 0 {
		return unreachable, false, false
	}
	return g.walkID(ni, di)
}

func (g *graph) walkID(ni, di int32) (cost key, ok, loops bool) {
	nNodes := len(g.nodes)
	var seenArr [64]bool
	var seen []bool
	if nNodes <= len(seenArr) {
		seen = seenArr[:nNodes]
	} else {
		seen = make([]bool, nNodes)
	}
	seen[g.selfAt] = true
	xi := ni
	dName := g.nodes[di]
	for range nNodes {
		if xi == di {
			return cost, true, false
		}
		if xi < 0 {
			return unreachable, false, false
		}
		if seen[xi] {
			return unreachable, false, true
		}
		if g.leaf[xi] {
			return unreachable, false, false
		}
		seen[xi] = true
		var a *advert
		if len(g.lsdbByID) > int(xi) {
			a = g.lsdbByID[xi]
		} else {
			a = g.lsdb[g.nodes[xi]]
		}
		if a == nil {
			return unreachable, false, false
		}
		var nhi int32 = -1
		if a.routes != nil {
			if len(a.routesByID) == nNodes {
				nhi = a.routesByID[di]
			} else if nh, has := a.routes[dName]; has {
				nhi = g.id(nh)
			}
		}
		// One that gives no next hop to a member (left out to fit, or a
		// version before 1.4.1) is taken to use its shortest path to it.
		if nhi < 0 && (a.routes == nil || g.leaf[di]) && g.ensureSrc(xi) {
			nhi = g.first[xi][di]
		}
		if nhi < 0 {
			return unreachable, false, false
		}
		k, linked := g.linkByID(xi, nhi)
		if !linked {
			return unreachable, false, false
		}
		cost = cost.add(k).add(g.toll(xi, nhi, xi == ni && g.leaf[g.selfAt]))
		xi = nhi
	}
	return unreachable, false, true
}

// feasible is the loop-free condition for taking n as the next hop to d:
// the path n advertises leads to d without coming back here, over links
// that are up, at a cost strictly below current, the cost of the route in
// use (unreachable when it broke). Following next hops then always gets
// closer to d.
func (g *graph) feasible(n, d string, current key) bool {
	if _, linked := g.link(g.self, n); n == "" || !linked {
		return false
	}
	if n == d {
		return true
	}
	rest, ok, _ := g.chain(n, d)
	return ok && rest.less(current)
}

// relays is how many relays the path through next hop n to d passes,
// d and this relay not counted.
func (g *graph) relays(n, d string) int {
	c := g.memo(n, d)
	if c == nil {
		return g.count(n, d)
	}
	if c.relaysAt != g.epoch {
		c.relays, c.relaysAt = g.count(n, d), g.epoch
	}
	return c.relays
}

func (g *graph) count(n, d string) int {
	path, _, ok := g.path(n, d)
	if !ok {
		return math.MaxInt
	}
	return len(path) - 2
}

// path is the route through next hop n as the relays on the way advertise
// it, or the shortest one from n where they do not say: the next hops walkID
// follows.
func (g *graph) path(n, d string) ([]string, float64, bool) {
	cost, ok, _ := g.via(n, d)
	if !ok {
		return nil, 0, false
	}
	out := []string{g.self}
	x := n
	for range g.nodes {
		out = append(out, x)
		if x == d {
			return out, cost.ms(), true
		}
		var nh string
		if a := g.lsdb[x]; a != nil && a.routes != nil {
			nh = a.routes[d]
		}
		if nh == "" {
			nh = g.hop(x, d)
		}
		if nh == "" || slices.Contains(out, nh) {
			return out, cost.ms(), true
		}
		x = nh
	}
	return out, cost.ms(), true
}
