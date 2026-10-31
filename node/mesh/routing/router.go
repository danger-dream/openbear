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

// A path through fewer relays is taken while the route in use works when
// it costs below shorterRatio of it and at least shorterMargin less, for
// switchHold (README, "Choosing a next hop").
const (
	shorterRatio  = 0.95
	shorterMargin = 3.0 // ms
)

// Route flap damping (README, "Choosing a next hop"): a route that went
// back, by better or shorter, to the next hop it had left by better or
// shorter less than flapWindow before has flapped. While it has flapped
// within flapWindow, a move by better or shorter waits until its rule has
// held for flapHold, doubled for each further flap, up to flapHoldMax.
// Moves because the route in use broke never wait, nor does the move back
// to the next hop they left; moves within flapSettle of the route's first
// advertisements, while it converges, are no flaps.
const (
	flapWindow  = 10 * time.Minute
	flapHold    = time.Minute
	flapHoldMax = 8 * time.Minute
	flapSettle  = advertExpiry
)

// Config is what the router needs from the applied config.
type Config struct {
	Self     string
	Relays   []string           // every other relay in the mesh
	Links    []string           // the relays this one has a link with
	NextHops map[string]string  // configured next hops: the start and static mode
	Mode     string             // dynamic (the default) or static
	Bias     map[string]float64 // ms added to a link's cost, by linked relay
	// Numbers are the host numbers of the relays' control addresses, this
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
	Path          []string
	Since         time.Time
	Switches      int
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
}

// damping is how long a move by better or shorter to next must wait, 0
// without flaps within flapWindow.
func (rt *route) damping(next string, now time.Time) time.Duration {
	rt.flaps = slices.DeleteFunc(rt.flaps, func(t time.Time) bool { return now.Sub(t) >= flapWindow })
	if len(rt.flaps) == 0 || (rt.broken && next == rt.left) {
		return 0
	}
	return min(flapHoldMax, flapHold<<min(len(rt.flaps)-1, 8))
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

	known map[string]bool // every relay, this one included
	order []string        // the other relays, sorted
	// all is every relay, this one included, sorted: the routes of an
	// advertisement this one forwards include those to itself.
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
	// real is cfg.RelayLinks as pairs in order; unknownAt when each origin
	// last had an event for listing another link, clockAt for a sequence
	// far off this relay's clock.
	real      map[[2]string]bool
	unknownAt map[string]time.Time
	clockAt   map[string]time.Time
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
	now := r.now()
	r.cfg = c
	r.known = map[string]bool{c.Self: true}
	r.order = nil
	for _, id := range c.Relays {
		if id != c.Self && !r.known[id] {
			r.known[id] = true
			r.order = append(r.order, id)
		}
	}
	sort.Strings(r.order)
	r.all = append([]string{c.Self}, r.order...)
	sort.Strings(r.all)
	r.num, r.ids = map[string]uint32{}, map[uint32]string{}
	for id := range r.known {
		n, ok := c.Numbers[id]
		if _, taken := r.ids[n]; !ok || n == 0 || taken {
			r.num, r.ids = nil, nil
			break
		}
		r.num[id], r.ids[n] = n, id
	}
	r.real = map[[2]string]bool{}
	for _, p := range c.RelayLinks {
		r.real[pair(p[0], p[1])] = true
	}
	for o, a := range r.lsdb {
		// What a stored advertisement lists is held to the new links too.
		r.keepReal(o, a, false)
	}
	linked := map[string]bool{}
	for _, p := range c.Links {
		if !r.known[p] || p == c.Self {
			continue
		}
		linked[p] = true
		m := r.links[p]
		if m == nil {
			m = newMetric(c.Bias[p])
			r.links[p] = m
		}
		if b := c.Bias[p]; b != m.bias {
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
		if !r.known[d] || d == c.Self {
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

func (r *Router) measure(peer string, f func(*metric, time.Time)) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	m := r.links[peer]
	if m == nil {
		return false
	}
	now := r.now()
	f(m, now)
	r.settle(peer, m, now)
	return r.recompute(now)
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
	case before == StateSuppressed:
		r.event("link.reused", fmt.Sprintf("%s: penalty %.0f", peer, m.penalty))
	}
	if m.advState == StateUp && before != StateUp {
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
	for p, m := range r.links {
		m.decay(now)
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
	now := r.now()
	since := now.Sub(r.sent)
	if !r.sent.IsZero() && !(r.due && since >= advertGap) && since < advertEvery {
		return nil
	}
	a := &advert{origin: r.cfg.Self, seq: max(r.seq+1, uint64(now.UnixMicro())), links: map[string]advLink{}, routes: map[string]string{}}
	for p, m := range r.links {
		a.links[p] = advLink{m.advCost, m.advState}
	}
	for d, rt := range r.routes {
		if rt.nextHop != "" {
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
	if !ok || len(peers) == 0 {
		return nil
	}
	r.seq = a.seq
	return a.messages(peers)
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
	if len(r.sync) == 0 {
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
	if r.links[from] == nil {
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
		if a, err = decodeCompact(msg.Raw, r.ids, r.known); errors.Is(err, errNewerFormat) {
			return nil, false, nil
		}
	} else {
		a, err = decodeJSON(msg.Raw, r.known)
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
	for _, p := range r.cfg.Links {
		if p != from && p != a.origin && r.links[p] != nil {
			peers = append(peers, p)
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
		s.Links = append(s.Links, LinkStatus{Peer: p, State: m.state(), RTTMs: round1(m.rtt), JitterMs: round1(m.jitter), Loss: m.loss(),
			Cost: round1(m.cost()), Penalty: math.Round(m.penalty), Advertised: adv})
	}
	g := r.graph
	for _, d := range r.order {
		rt := r.routes[d]
		st := RouteStatus{Dest: d, NextHop: rt.nextHop, Cost: math.Inf(1), Path: []string{}, Since: rt.since, Switches: rt.switches}
		if rt.nextHop != "" && g != nil {
			if path, cost, ok := g.path(rt.nextHop, d); ok {
				st.Path, st.Cost = path, cost
			}
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
		if !foreign {
			rt.dynamic = false
		}
		next, reason := r.choose(d, rt, foreign, now)
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
	static := r.cfg.NextHops[d]
	if r.links[static] == nil {
		static = ""
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
	// only when it is below switchRatio of its cost and more than
	// switchMargin cheaper. The costs compared are advertised ones, which
	// follow a link only after a change lasted switchHold.
	if pref := g.first[d]; pref != "" && g.feasible(pref, d, curCost) {
		if pref == cur {
			return cur, ""
		}
		if !curUsable {
			return pref, reason
		}
		// While the route in use works, a move waits for its rule to hold
		// as long as flap damping says, and the shorter rule for switchHold
		// at least.
		damping := rt.damping(pref, now)
		wait := func(hold time.Duration, why string) (string, string) {
			if pending != pref {
				rt.pendingAt = now
			}
			rt.pending = pref
			if now.Sub(rt.pendingAt) >= hold {
				rt.pending = ""
				return pref, why
			}
			return cur, ""
		}
		prefCost, _, _ := g.via(pref, d)
		if prefCost.ms() < switchRatio*curCost.ms() && curCost.ms()-prefCost.ms() > switchMargin {
			if damping == 0 {
				return pref, ReasonBetter
			}
			return wait(damping, ReasonBetter)
		}
		// A path through fewer relays needs less: below shorterRatio and
		// at least shorterMargin cheaper, but for switchHold on end, as
		// nothing else delays it, and not over a first link that went down
		// lately (penalty not yet below reuseBelow): a flapping link comes
		// back up cheaper than the detour every time.
		if m := r.links[pref]; m != nil && m.penalty < reuseBelow && g.relays(pref, d) < g.relays(cur, d) &&
			prefCost.ms() < shorterRatio*curCost.ms() && curCost.ms()-prefCost.ms() >= shorterMargin {
			return wait(max(switchHold, damping), ReasonShorter)
		}
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

// graph is every relay and link as this relay knows them, with the
// shortest distances between all relays.
type graph struct {
	self  string
	nodes []string
	adj   map[string]map[string]key // symmetric
	dist  map[string]map[string]key
	first map[string]string // this relay's shortest-path next hop by destination
	lsdb  map[string]*advert
}

// key orders paths: by cost in tenths of a millisecond, then by a
// tie-breaker every relay derives the same from the link's two ends, so
// all relays agree on one shortest path between any two.
type key struct{ w, tie int64 }

var unreachable = key{math.MaxInt64 / 4, 0}

func (k key) less(o key) bool   { return k.w < o.w || (k.w == o.w && k.tie < o.tie) }
func (k key) add(o key) key     { return key{k.w + o.w, k.tie + o.tie} }
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
	return key{max(1, int64(math.Round(c*10))), int64(h.Sum32()&0xffff) + 1}
}

func (r *Router) build() *graph {
	g := &graph{self: r.cfg.Self, nodes: append([]string{r.cfg.Self}, r.order...), adj: map[string]map[string]key{}, dist: map[string]map[string]key{}, first: map[string]string{}, lsdb: r.lsdb}
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
		dist, first := g.dijkstra(src)
		g.dist[src] = dist
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
		for v, k := range g.adj[u] {
			if done[v] {
				continue
			}
			nd := dist[u].add(k)
			hop := first[u]
			if u == src {
				hop = v
			}
			if nd.less(dist[v]) || (nd == dist[v] && hop < first[v]) {
				dist[v], first[v] = nd, hop
			}
		}
	}
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
		seen[x] = true
		a := g.lsdb[x]
		if a == nil {
			return unreachable, false, false
		}
		if a.routes == nil {
			rest := g.dist[x][d]
			if rest.unreachable() {
				return unreachable, false, false
			}
			return cost.add(rest), true, false
		}
		nh, has := a.routes[d]
		if !has {
			return unreachable, false, false
		}
		k, linked := g.adj[x][nh]
		if !linked {
			return unreachable, false, false
		}
		cost, x = cost.add(k), nh
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
		if a == nil || a.routes == nil {
			// Follow the shortest path from x.
			for x != d {
				best := ""
				for y, k := range g.adj[x] {
					if g.dist[x][d] == k.add(g.dist[y][d]) && (best == "" || y < best) {
						best = y
					}
				}
				if best == "" || slices.Contains(out, best) {
					return out, cost.ms(), true
				}
				out = append(out, best)
				x = best
			}
			return out, cost.ms(), true
		}
		x = a.routes[d]
	}
	return out, cost.ms(), true
}
