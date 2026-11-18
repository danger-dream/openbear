// Package routing chooses each relay's next hop toward every other relay
// from measured link costs that all relays share (README.md).
package routing

import (
	"crypto/ed25519"
	"errors"
	"fmt"
	"hash/fnv"
	"math"
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
	ReasonDown    = "down"
	ReasonBetter  = "better"
	ReasonLoop    = "loop"
	ReasonShorter = "shorter"
)

// Why a route stays on its next hop while the shortest path's is another
// (RouteStatus.Held).
const (
	HeldMargin  = "margin"  // cheaper by less than the switch (or shorter) threshold
	HeldHold    = "hold"    // the rule is met and has to hold until Until
	HeldDamped  = "damped"  // flap damping: the route went back and forth lately; until Until
	HeldLoop    = "loop"    // its advertised path is not loop-free yet; advertisements are catching up
	HeldPenalty = "penalty" // a path through fewer relays, over a first link that went down lately
	HeldTransit = "transit" // it goes on through more no-transit relays
)

// Tuning are the parameters of link costs and of moving routes (README,
// "Link metrics" and "Choosing a next hop"); DefaultTuning without one.
type Tuning struct {
	// A working route moves to one costing at least SwitchMs less and, with
	// SwitchPct, more than that share of its cost less.
	SwitchMs, SwitchPct float64
	// One through fewer relays: at least ShorterMs less and, with
	// ShorterPct, more than that share less, for Hold on end.
	ShorterMs, ShorterPct float64
	// A link's advertised cost follows the measured one once they differ by
	// more than AdvertMs and, with AdvertPct, more than that share of the
	// larger, for Hold.
	AdvertMs, AdvertPct float64
	Hold                time.Duration
	// A link costs its round trip, JitterWeight times its jitter,
	// LossWeight times its loss rate (0-1) and its bias, in ms.
	JitterWeight, LossWeight float64
	// Flap damping: a route that went back, by better or shorter, to the
	// next hop it had left by better or shorter less than flapWindow before
	// has flapped. While it has flapped within flapWindow, a move by better
	// or shorter waits until its rule has held for FlapHold, doubled for
	// each further flap, up to FlapHoldMax. Moves because the route in use
	// broke never wait, nor does the move back to the next hop they left;
	// moves within flapSettle of the route's first advertisements, while it
	// converges, are no flaps.
	FlapHold, FlapHoldMax time.Duration
}

var DefaultTuning = Tuning{SwitchMs: 5, ShorterMs: 3, AdvertMs: 2, Hold: 5 * time.Second, JitterWeight: 2, LossWeight: 1000,
	FlapHold: time.Minute, FlapHoldMax: 8 * time.Minute}

// cheaper says whether saving saved ms on a route costing cur ms meets ms
// and pct.
func cheaper(saved, cur, ms, pct float64) bool {
	return saved >= ms && (pct == 0 || saved > cur*pct/100)
}

const (
	flapWindow = 10 * time.Minute
	flapSettle = advertExpiry
)

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
	// Held is how much longer a link back from down is held down
	// (probationBase), 0 unless it is.
	Held time.Duration
}

type RouteStatus struct {
	Dest, NextHop string
	Cost          float64 // +Inf without a route
	Path          []string
	Since         time.Time
	Switches      int
	// Best is the shortest path's next hop when the route stays on another,
	// BestCost its cost along the next hops advertised (+Inf when it has
	// none yet), Held why (Held*), until Until for HeldHold and HeldDamped.
	Best     string
	BestCost float64
	Held     string
	Until    time.Time
}

type Status struct {
	Mode   string
	Links  []LinkStatus
	Routes []RouteStatus
}

type route struct {
	nextHop  string
	since    time.Time
	switches int
	// dynamic is set once the advertisements gave this destination a route,
	// at dynamicAt; until then the configured next hop is used.
	dynamic   bool
	dynamicAt time.Time
	// pending is a next hop that has met the shorter rule, or while
	// damped the better one, continuously since pendingAt.
	pending   string
	pendingAt time.Time
	// left is the next hop the last move left, at leftAt, broken when the
	// route in use broke; flaps are when the route went back to one left by
	// better or shorter.
	left   string
	leftAt time.Time
	broken bool
	flaps  []time.Time
	// What the last choice found: RouteStatus.Best, BestCost, Held, Until.
	best     string
	bestCost key
	held     string
	until    time.Time
}

// damping is how long a move by better or shorter to next must wait, 0
// without flaps within flapWindow.
func (rt *route) damping(next string, now time.Time, t *Tuning) time.Duration {
	rt.flaps = slices.DeleteFunc(rt.flaps, func(t time.Time) bool { return now.Sub(t) >= flapWindow })
	if len(rt.flaps) == 0 || (rt.broken && next == rt.left) {
		return 0
	}
	return min(t.FlapHoldMax, t.FlapHold<<min(len(rt.flaps)-1, 8))
}

// moved records a move from old to next for reason.
func (rt *route) moved(old, next, reason string, now time.Time) {
	voluntary := reason == ReasonBetter || reason == ReasonShorter
	if voluntary && !rt.broken && next == rt.left && now.Sub(rt.leftAt) < flapWindow && now.Sub(rt.dynamicAt) >= flapSettle {
		rt.flaps = append(rt.flaps, now)
	}
	rt.left, rt.leftAt, rt.broken = old, now, !voluntary
}

type Router struct {
	mu    sync.Mutex
	cfg   Config
	now   func() time.Time
	event func(kind, detail string)

	known  map[string]bool // every relay, this one included if a relay
	leaves map[string]bool // every member in routing, this one included if a leaf
	// noTransit is cfg.NoTransit of the relays known.
	noTransit map[string]bool
	// tuning is cfg.Tuning, or DefaultTuning; every metric points to it.
	tuning Tuning
	order  []string // the other nodes (relays and leaves), sorted
	// all is every relay, this one included when not a leaf, sorted: the
	// routes of an advertisement this one sends include those to relays.
	all    []string
	links  map[string]*metric
	lsdb   map[string]*advert
	routes map[string]*route
	graph  *graph

	seq      uint64
	sent     time.Time // this relay's last advertisement
	due      bool      // a change waits to be advertised
	tooLarge bool
	sync     map[string]bool // neighbors that get the whole database next
	origins  map[string]*bucket
	senders  map[string]*bucket

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
}

// New makes a router; now and event may be nil for the wall clock and no events.
func New(c Config, now func() time.Time, event func(kind, detail string)) *Router {
	if now == nil {
		now = time.Now
	}
	if event == nil {
		event = func(string, string) {}
	}
	r := &Router{now: now, event: event, links: map[string]*metric{}, lsdb: map[string]*advert{}, routes: map[string]*route{},
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
	r.all = nil
	if !c.Leaf {
		r.all = append(r.all, c.Self)
	}
	for _, id := range c.Relays {
		if id != c.Self && !r.known[id] && !r.leaves[id] {
			r.known[id] = true
			r.order = append(r.order, id)
			r.all = append(r.all, id)
		}
	}
	for _, id := range c.Leaves {
		if id != c.Self && !r.known[id] && !r.leaves[id] {
			r.leaves[id] = true
			r.order = append(r.order, id)
		}
	}
	sort.Strings(r.order)
	sort.Strings(r.all)
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
		// A member has its links and the way back to it kept (keep): no
		// probation that would cut it off longer than its link was down.
		m.probation = !c.Leaf && r.known[p]
		if b := c.Bias[p]; b != m.bias || retune {
			m.bias = b
			if m.state() == StateUp {
				m.advCost = round1(m.cost())
				m.pending = time.Time{}
				r.due = true
			}
		}
	}
	for p := range r.links {
		if !linked[p] {
			delete(r.links, p)
			delete(r.sync, p)
			r.due = true
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
	for _, d := range r.order {
		if r.routes[d] == nil {
			r.routes[d] = &route{since: now}
		}
	}
	return r.recompute(now)
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
			m.pending = time.Time{}
			r.due = true
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
	if m.heldFor > 0 {
		r.event("link.held", fmt.Sprintf("%s: back after %d downs in %v, held for %v", peer, len(m.downs), flapWindow, m.heldFor))
		m.heldFor = 0
	}
	for _, p := range r.release() {
		if p != peer {
			r.settle(p, r.links[p], now)
		}
	}
	for _, p := range r.keep() {
		if p != peer {
			r.settle(p, r.links[p], now)
		}
	}
	r.settle(peer, m, now)
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

// release lets a held link up at once when its peer is not reachable another
// way: the hold steers routes around a link that keeps going down, it cuts
// off neither end (a relay that restarted comes back on every link at once,
// and each neighbor has others).
func (r *Router) release() (released []string) {
	for _, p := range r.order {
		m := r.links[p]
		if m == nil || !m.held || r.graph != nil && !r.graph.dist[r.cfg.Self][p].unreachable() {
			continue
		}
		m.held, m.heldUntil = false, time.Time{}
		r.event("link.released", p+": not reachable another way")
		released = append(released, p)
	}
	return released
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

func (r *Router) settle(peer string, m *metric, now time.Time) {
	before := m.advState
	if !m.settle(now) {
		return
	}
	r.due = true
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

// Tick lets penalties decay, advertised costs settle and advertisements
// expire. It says whether a next hop changed.
func (r *Router) Tick() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	now := r.now()
	for _, m := range r.links {
		m.decay(now)
	}
	r.release()
	r.keep()
	for p, m := range r.links {
		r.settle(p, m, now)
	}
	for o, a := range r.lsdb {
		if now.Sub(a.at) > advertExpiry {
			delete(r.lsdb, o)
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
	if !r.sent.IsZero() && !(r.due && since >= advertGap) && since < advertEvery {
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
		if rt.nextHop != "" && r.known[d] {
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
	r.sent, r.due = now, false
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
	if r.num != nil {
		a.format = FormatCompact
		a.raw = fit(b.Compact, func(routes bool) []byte {
			raw, _ := encodeCompact(a, r.num, r.all, routes)
			return raw
		})
	} else {
		a.format = FormatJSON
		a.raw = fit(b.JSON, func(routes bool) []byte { return encodeJSON(a, r.all, routes) })
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

// fit is the encoding with routes if it is within budget, else without
// them, else nil.
func fit(budget int, encode func(routes bool) []byte) []byte {
	for _, routes := range []bool{true, false} {
		if raw := encode(routes); raw != nil && len(raw) <= budget {
			return raw
		}
	}
	return nil
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
		return nil, false, errUnlinked
	}
	s := r.senders[from]
	if s == nil {
		s = &bucket{}
		r.senders[from] = s
	}
	if !s.take(now, senderRate, senderBurst) {
		return nil, false, errRate
	}
	var a *advert
	if msg.Format == FormatCompact {
		if r.num == nil {
			return nil, false, errNoNumbers
		}
		if a, err = decodeCompact(msg.Raw, r.ids, r.known, r.leaves); errors.Is(err, errNewerFormat) {
			return nil, false, nil
		}
	} else {
		a, err = decodeJSON(msg.Raw, r.known, r.leaves)
	}
	if err != nil {
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
		return nil, false, ErrSignature
	}
	r.verified++
	o := r.origins[a.origin]
	if o == nil {
		o = &bucket{}
		r.origins[a.origin] = o
	}
	if !o.take(now, originRate, originBurst) {
		return nil, false, errRate
	}
	a.at, a.format, a.raw, a.sig = now, msg.Format, slices.Clone(msg.Raw), slices.Clone(msg.Sig)
	r.keepReal(a.origin, a, true)
	r.checkClock(a, now)
	r.lsdb[a.origin] = a
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
	s := Status{Mode: r.cfg.Mode}
	for _, p := range slices.Sorted(func(yield func(string) bool) {
		for p := range r.links {
			if !yield(p) {
				return
			}
		}
	}) {
		m := r.links[p]
		adv := math.Inf(1)
		if m.advState == StateUp {
			adv = m.advCost
		}
		var held time.Duration
		if m.held {
			held = max(0, m.heldUntil.Sub(r.now()))
		}
		s.Links = append(s.Links, LinkStatus{Peer: p, State: m.state(), RTTMs: round1(m.rtt), JitterMs: round1(m.jitter), Loss: m.loss(),
			Cost: round1(m.cost()), Penalty: math.Round(m.penalty), Advertised: adv, Held: held})
	}
	g := r.graph
	for _, d := range r.order {
		rt := r.routes[d]
		st := RouteStatus{Dest: d, NextHop: rt.nextHop, Cost: math.Inf(1), Path: []string{}, Since: rt.since, Switches: rt.switches,
			Best: rt.best, BestCost: math.Inf(1), Held: rt.held, Until: rt.until}
		if rt.nextHop != "" && g != nil {
			if path, cost, ok := g.path(rt.nextHop, d); ok {
				st.Path, st.Cost = path, cost
			}
		}
		if rt.best != "" && !rt.bestCost.unreachable() {
			st.BestCost = rt.bestCost.ms()
		}
		s.Routes = append(s.Routes, st)
	}
	return s
}

// recompute chooses every next hop again and says whether one changed.
func (r *Router) recompute(now time.Time) bool {
	r.graph = r.build()
	foreign := len(r.lsdb) > 0
	changed := false
	for _, d := range r.order {
		rt := r.routes[d]
		df := foreign || (r.leaves[d] && r.links[d] != nil && r.links[d].advState == StateUp)
		if !df {
			rt.dynamic = false
		}
		next, reason := r.choose(d, rt, df, now)
		if next == rt.best {
			rt.best, rt.bestCost, rt.held, rt.until = "", unreachable, "", time.Time{}
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
		r.due = true
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

// choose is the next hop for d and, when it differs from the current one,
// why. See README, "Choosing a next hop".
func (r *Router) choose(d string, rt *route, foreign bool, now time.Time) (string, string) {
	pending := rt.pending
	rt.pending = ""
	rt.best, rt.bestCost, rt.held, rt.until = "", unreachable, "", time.Time{}
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
		return static, ""
	}
	g := r.graph
	self := r.cfg.Self
	dist := g.dist[self][d]
	if dist.unreachable() {
		if !rt.dynamic {
			return static, ""
		}
		return "", ReasonDown
	}
	if !rt.dynamic {
		rt.dynamic, rt.dynamicAt = true, now
	}
	cur := rt.nextHop
	curCost, curUsable, curLoops := g.via(cur, d)
	reason := ReasonBetter
	switch {
	case cur == "":
	case curLoops:
		reason = ReasonLoop
	case !curUsable:
		reason = ReasonDown
	}
	// The shortest path's next hop, once the path it advertises is
	// loop-free and cheaper than the route in use; while that route works,
	// only when it is cheaper by Tuning.SwitchMs (and SwitchPct). The costs
	// compared are advertised ones, which follow a link only after a change
	// lasted Tuning.Hold.
	t := &r.tuning
	pref := g.first[d]
	if pref != "" && g.feasible(pref, d, curCost) {
		if pref == cur {
			return cur, ""
		}
		if !curUsable {
			return pref, reason
		}
		// While the route in use works, a move waits for its rule to hold
		// as long as flap damping says, and the shorter rule for Hold at
		// least.
		damping := rt.damping(pref, now, t)
		prefCost, _, _ := g.via(pref, d)
		rt.best, rt.bestCost = pref, prefCost
		wait := func(hold time.Duration, why string) (string, string) {
			if pending != pref {
				rt.pendingAt = now
			}
			rt.pending = pref
			if now.Sub(rt.pendingAt) >= hold {
				rt.pending = ""
				return pref, why
			}
			rt.held, rt.until = HeldHold, rt.pendingAt.Add(hold)
			if damping >= hold {
				rt.held = HeldDamped
			}
			return cur, ""
		}
		// Through fewer no-transit relays wins whatever it costs, without
		// waiting for flap damping; the thresholds hold between paths
		// through as many.
		same, fewer := prefCost.tolls == curCost.tolls, prefCost.tolls < curCost.tolls
		saved := curCost.ms() - prefCost.ms()
		if fewer || same && cheaper(saved, curCost.ms(), t.SwitchMs, t.SwitchPct) {
			if damping == 0 || fewer {
				return pref, ReasonBetter
			}
			return wait(damping, ReasonBetter)
		}
		// A path through fewer relays needs less: ShorterMs (and
		// ShorterPct), but for Hold on end, as nothing else delays it, and
		// not over a first link that went down lately (penalty not yet below
		// reuseBelow): a flapping link comes back up cheaper than the detour
		// every time.
		if m := r.links[pref]; same && m != nil && g.relays(pref, d) < g.relays(cur, d) && cheaper(saved, curCost.ms(), t.ShorterMs, t.ShorterPct) {
			if m.penalty < reuseBelow {
				return wait(max(t.Hold, damping), ReasonShorter)
			}
			rt.held = HeldPenalty
		}
		if rt.held == "" {
			rt.held = HeldMargin
			if !same {
				rt.held = HeldTransit
			}
		}
	} else if pref != "" && pref != cur && curUsable {
		rt.best, rt.held = pref, HeldLoop
		rt.bestCost, _, _ = g.via(pref, d)
	}
	if curUsable {
		return cur, ""
	}
	// The route in use broke and the shortest path's next hop still points
	// back here or along a path that is gone: until its advertisement
	// catches up, use the cheapest neighbor whose path is loop-free, or none.
	best, bestKey := "", unreachable
	for _, n := range r.cfg.Links {
		if r.links[n] == nil || !g.feasible(n, d, unreachable) {
			continue
		}
		k, _, _ := g.via(n, d)
		if k.less(bestKey) || (k == bestKey && n < best) {
			best, bestKey = n, k
		}
	}
	return best, reason
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
		if _, ok := g.adj[nodes[i]][nodes[i+1]]; !ok {
			return []string{nodes[i], nodes[i+1]}
		}
	}
	return nil
}

// graph is every relay and link as this relay knows them, with the
// shortest distances between all relays.
type graph struct {
	self      string
	nodes     []string
	leaves    map[string]bool
	noTransit map[string]bool
	adj       map[string]map[string]key // symmetric
	dist      map[string]map[string]key
	first     map[string]string            // this node's shortest-path next hop by destination
	firstBy   map[string]map[string]string // src -> dest -> shortest-path next hop
	lsdb      map[string]*advert
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
	h := fnv.New32a()
	h.Write([]byte(a))
	h.Write([]byte{0})
	h.Write([]byte(b))
	return key{w: max(1, int64(math.Round(c*10))), tie: int64(h.Sum32()&0xffff) + 1}
}

func (r *Router) build() *graph {
	g := &graph{self: r.cfg.Self, nodes: append([]string{r.cfg.Self}, r.order...), leaves: r.leaves, noTransit: r.noTransit, adj: map[string]map[string]key{}, dist: map[string]map[string]key{}, first: map[string]string{}, firstBy: map[string]map[string]string{}, lsdb: r.lsdb}
	side := func(x, y string) (advLink, bool) {
		if x == r.cfg.Self {
			m := r.links[y]
			if m == nil {
				return advLink{}, false
			}
			return advLink{m.advCost, m.advState}, true
		}
		a := r.lsdb[x]
		if a == nil {
			return advLink{}, false
		}
		if r.leaves[y] {
			l, ok := a.members[y]
			return l, ok
		}
		l, ok := a.links[y]
		return l, ok
	}
	for i, a := range g.nodes {
		for _, b := range g.nodes[i+1:] {
			la, okA := side(a, b)
			lb, okB := side(b, a)
			if (a == r.cfg.Self && !okA) || (b == r.cfg.Self && !okB) || (!okA && !okB) {
				continue
			}
			if (okA && la.state != StateUp) || (okB && lb.state != StateUp) {
				continue
			}
			k := linkKey(a, b, max(la.cost, lb.cost))
			for _, x := range [][2]string{{a, b}, {b, a}} {
				if g.adj[x[0]] == nil {
					g.adj[x[0]] = map[string]key{}
				}
				g.adj[x[0]][x[1]] = k
			}
		}
	}
	for _, src := range g.nodes {
		if src != r.cfg.Self && g.leaves[src] {
			continue
		}
		dist, first := g.dijkstra(src)
		g.dist[src] = dist
		g.firstBy[src] = first
		if src == r.cfg.Self {
			g.first = first
		}
	}
	return g
}

func (g *graph) dijkstra(src string) (map[string]key, map[string]string) {
	dist := map[string]key{}
	first := map[string]string{}
	done := map[string]bool{}
	for _, n := range g.nodes {
		dist[n] = unreachable
	}
	dist[src] = key{}
	for {
		u := ""
		for _, n := range g.nodes {
			if !done[n] && !dist[n].unreachable() && (u == "" || dist[n].less(dist[u])) {
				u = n
			}
		}
		if u == "" {
			return dist, first
		}
		done[u] = true
		if u != src && g.leaves[u] {
			continue
		}
		for v, k := range g.adj[u] {
			if done[v] {
				continue
			}
			nd := dist[u].add(k)
			hop := first[u]
			if u == src {
				hop = v
			} else {
				nd = nd.add(g.toll(u, v, g.leaves[src] && first[u] == u))
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
func (g *graph) toll(u, v string, fromMember bool) key {
	if g.noTransit[u] && !fromMember && !g.leaves[v] {
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
	k, linked := g.adj[g.self][n]
	if !linked {
		return unreachable, false, false
	}
	rest, ok, loops := g.chain(n, d)
	if !ok {
		return unreachable, false, loops
	}
	return k.add(rest), true, false
}

// chain is the cost from n to d along advertised next hops. A relay that
// left its routes out of its advertisement is assumed to use its shortest
// path.
func (g *graph) chain(n, d string) (cost key, ok, loops bool) {
	seen := map[string]bool{g.self: true}
	x := n
	for range g.nodes {
		if x == d {
			return cost, true, false
		}
		if seen[x] {
			return unreachable, false, true
		}
		if g.leaves[x] {
			return unreachable, false, false
		}
		seen[x] = true
		a := g.lsdb[x]
		if a == nil {
			return unreachable, false, false
		}
		var nh string
		var has bool
		if a.routes == nil || g.leaves[d] {
			nh = g.firstBy[x][d]
			has = nh != ""
		} else {
			nh, has = a.routes[d]
		}
		if !has {
			return unreachable, false, false
		}
		k, linked := g.adj[x][nh]
		if !linked {
			return unreachable, false, false
		}
		cost, x = cost.add(k).add(g.toll(x, nh, x == n && g.leaves[g.self])), nh
	}
	return unreachable, false, true
}

// feasible is the loop-free condition for taking n as the next hop to d:
// the path n advertises leads to d without coming back here, over links
// that are up, at a cost strictly below current, the cost of the route in
// use (unreachable when it broke). Following next hops then always gets
// closer to d.
func (g *graph) feasible(n, d string, current key) bool {
	if _, linked := g.adj[g.self][n]; n == "" || !linked {
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
	path, _, ok := g.path(n, d)
	if !ok {
		return math.MaxInt
	}
	return len(path) - 2
}

// path is the route through next hop n as the relays on the way advertise
// it, or the shortest one from n where they do not say.
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
		a := g.lsdb[x]
		if a == nil || a.routes == nil || g.leaves[d] {
			nh := g.firstBy[x][d]
			if nh == "" || slices.Contains(out, nh) {
				return out, cost.ms(), true
			}
			x = nh
			continue
		}
		x = a.routes[d]
	}
	return out, cost.ms(), true
}
