package routing

import (
	"crypto/ed25519"
	"errors"
	"fmt"
	"maps"
	"math"
	"slices"
	"sort"
	"sync"
	"time"
)

// legacyRouter preserves the pre-smoothing routing algorithm (commit 5479e980)
// strictly for tests: 48h real-data replay comparison against the new smoothed
// router, and mixed-version rolling-upgrade loop-freedom tests.

const (
	legacyLossWindow    = 30
	legacyProbationBase = 30 * time.Second
	legacyProbationMax  = 8 * time.Minute
	legacyFlapWindow    = 10 * time.Minute
	legacyFlapSettle    = advertExpiry
)

type legacyTuningParams struct {
	SwitchMs, SwitchPct   float64
	ShorterMs, ShorterPct float64
	AdvertMs, AdvertPct   float64
	Hold                  time.Duration
	JitterWeight          float64
	LossWeight            float64
	FlapHold, FlapHoldMax time.Duration
}

var legacyDefaultParams = legacyTuningParams{
	SwitchMs:     5,
	SwitchPct:    0,
	ShorterMs:    3,
	ShorterPct:   0,
	AdvertMs:     2,
	AdvertPct:    0,
	Hold:         5 * time.Second,
	JitterWeight: 2,
	LossWeight:   1000,
	FlapHold:     time.Minute,
	FlapHoldMax:  8 * time.Minute,
}

func legacyCheaper(saved, cur, ms, pct float64) bool {
	return saved >= ms && (pct == 0 || saved > cur*pct/100)
}

func (t *legacyTuningParams) differs(measured, advertised float64) bool {
	d := math.Abs(measured - advertised)
	return d > t.AdvertMs && (t.AdvertPct == 0 || d > max(measured, advertised)*t.AdvertPct/100)
}

type legacyMetric struct {
	samples      int
	recent, devs []float64
	rtt, jitter  float64
	window       []bool
	consec       int
	down         bool
	penalty      float64
	penaltyAt    time.Time
	suppressed   bool
	kept         bool
	bias         float64
	narrow       float64
	tune         *legacyTuningParams

	probation bool
	downs     []time.Time
	heldUntil time.Time
	held      bool
	heldFor   time.Duration

	advState string
	advCost  float64
	pending  time.Time
}

func newLegacyMetric(bias float64, t *legacyTuningParams) *legacyMetric {
	return &legacyMetric{bias: bias, tune: t, advState: StateDown}
}

func (m *legacyMetric) decay(now time.Time) {
	if m.penalty > 0 && !m.penaltyAt.IsZero() && now.After(m.penaltyAt) {
		m.penalty *= math.Pow(0.5, now.Sub(m.penaltyAt).Seconds()/halfLife.Seconds())
		if m.penalty < 1e-3 {
			m.penalty = 0
		}
	}
	m.penaltyAt = now
	m.held = now.Before(m.heldUntil)
	m.suppress()
}

func (m *legacyMetric) suppress() {
	if m.penalty >= suppressAt {
		m.suppressed = true
	} else if m.penalty < reuseBelow {
		m.suppressed = false
	}
}

func (m *legacyMetric) sample(rtt time.Duration, lost bool, now time.Time) {
	m.decay(now)
	m.window = append(m.window, lost)
	if len(m.window) > legacyLossWindow {
		m.window = m.window[len(m.window)-legacyLossWindow:]
	}
	if lost {
		m.consec++
	} else {
		ms := float64(rtt) / float64(time.Millisecond)
		m.consec = 0
		m.recent = last(append(m.recent, ms))
		switch {
		case m.samples == 0:
			m.rtt = ms
		case m.samples < warmSamples:
			m.rtt = min(m.rtt, ms)
		default:
			m.rtt = median(m.recent)
			m.devs = last(append(m.devs, math.Abs(ms-m.rtt)))
			m.jitter = median(m.devs)
		}
		m.samples++
	}
	m.enterDown(m.consec >= downAfter, now)
}

func (m *legacyMetric) gone(now time.Time) {
	m.decay(now)
	m.consec = max(m.consec, downAfter)
	m.enterDown(true, now)
}

func (m *legacyMetric) enterDown(down bool, now time.Time) {
	if down && !m.down {
		m.penalty = min(penaltyMax, m.penalty+penaltyStep)
		m.downs = append(slices.DeleteFunc(m.downs, func(t time.Time) bool { return now.Sub(t) >= legacyFlapWindow }), now)
	}
	if !down && m.down && m.probation {
		m.heldFor = min(legacyProbationMax, legacyProbationBase<<min(max(len(m.downs)-1, 0), 8))
		m.heldUntil = now.Add(m.heldFor)
		m.held = true
	}
	m.down = down
	m.suppress()
}

func legacyLostShare(w []bool) float64 {
	if len(w) == 0 {
		return 0
	}
	n := 0
	for _, l := range w {
		if l {
			n++
		}
	}
	return float64(n) / float64(len(w))
}

func (m *legacyMetric) loss() float64 {
	return min(legacyLostShare(m.window[max(0, len(m.window)-lossShort):]), legacyLostShare(m.window))
}

func (m *legacyMetric) state() string {
	switch {
	case m.suppressed && !m.kept:
		return StateSuppressed
	case m.down || m.held || m.samples < warmSamples:
		return StateDown
	}
	return StateUp
}

func (m *legacyMetric) measured() float64 {
	return max(minCost, m.rtt+m.tune.JitterWeight*m.jitter+m.tune.LossWeight*m.loss()+m.bias+m.narrow)
}

func (m *legacyMetric) cost() float64 {
	if m.state() != StateUp {
		return math.Inf(1)
	}
	return m.measured()
}

func (m *legacyMetric) working() bool {
	return !m.down && m.samples >= warmSamples
}

func (m *legacyMetric) settle(now time.Time) bool {
	st := m.state()
	if st != m.advState {
		m.advState, m.advCost, m.pending = st, 0, time.Time{}
		if st == StateUp {
			m.advCost = round1(m.cost())
		}
		return true
	}
	if st != StateUp {
		return false
	}
	c := m.cost()
	if !m.tune.differs(c, m.advCost) {
		m.pending = time.Time{}
		return false
	}
	if m.pending.IsZero() {
		m.pending = now
	}
	if now.Sub(m.pending) < m.tune.Hold {
		return false
	}
	m.advCost, m.pending = round1(c), time.Time{}
	return true
}

type legacyRoute struct {
	nextHop   string
	since     time.Time
	switches  int
	dynamic   bool
	dynamicAt time.Time
	pending   string
	pendingAt time.Time
	left      string
	leftAt    time.Time
	broken    bool
	flaps     []time.Time
}

func (rt *legacyRoute) damping(next string, now time.Time, t *legacyTuningParams) time.Duration {
	rt.flaps = slices.DeleteFunc(rt.flaps, func(t time.Time) bool { return now.Sub(t) >= legacyFlapWindow })
	if len(rt.flaps) == 0 || (rt.broken && next == rt.left) {
		return 0
	}
	return min(t.FlapHoldMax, t.FlapHold<<min(len(rt.flaps)-1, 8))
}

func (rt *legacyRoute) moved(old, next, reason string, now time.Time) {
	voluntary := reason == ReasonBetter || reason == "shorter"
	if voluntary && !rt.broken && next == rt.left && now.Sub(rt.leftAt) < legacyFlapWindow && now.Sub(rt.dynamicAt) >= legacyFlapSettle {
		rt.flaps = append(rt.flaps, now)
	}
	rt.left, rt.leftAt, rt.broken = old, now, !voluntary
}

type legacyRouter struct {
	mu    sync.Mutex
	cfg   Config
	now   func() time.Time
	event func(kind, detail string)

	known     map[string]bool
	leaves    map[string]bool
	noTransit map[string]bool
	tuning    legacyTuningParams
	order     []string
	all       []string
	links     map[string]*legacyMetric
	lsdb      map[string]*advert
	routes    map[string]*legacyRoute
	graph     *graph
	stale     bool

	seq     uint64
	sent    time.Time
	due     bool
	sync    map[string]bool
	origins map[string]*bucket
	senders map[string]*bucket

	num         map[string]uint32
	ids         map[uint32]string
	real        map[[2]string]bool
	realMembers map[[2]string]bool
	skipSign    bool
}

func newLegacyRouter(c Config, params *legacyTuningParams, now func() time.Time, event func(kind, detail string)) *legacyRouter {
	if now == nil {
		now = time.Now
	}
	if event == nil {
		event = func(string, string) {}
	}
	t := legacyDefaultParams
	if params != nil {
		t = *params
	}
	r := &legacyRouter{
		now: now, event: event, tuning: t,
		links: map[string]*legacyMetric{}, lsdb: map[string]*advert{}, routes: map[string]*legacyRoute{},
		sync: map[string]bool{}, origins: map[string]*bucket{}, senders: map[string]*bucket{},
	}
	r.configure(c)
	return r
}

func (r *legacyRouter) configure(c Config) bool {
	if c.Mode == "" {
		c.Mode = ModeDynamic
	}
	now := r.now()
	r.cfg = c
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
	for _, p := range c.Links {
		if (!r.known[p] && !r.leaves[p]) || p == c.Self {
			continue
		}
		m := r.links[p]
		if m == nil {
			m = newLegacyMetric(c.Bias[p], &r.tuning)
			r.links[p] = m
		}
		m.probation = !c.Leaf && r.known[p]
	}
	for _, d := range r.order {
		if r.routes[d] == nil {
			r.routes[d] = &legacyRoute{since: now}
		}
	}
	r.stale = true
	return r.recompute(now)
}

func (r *legacyRouter) Probe(peer string, rtt time.Duration) bool {
	return r.measure(peer, func(m *legacyMetric, now time.Time) { m.sample(rtt, false, now) })
}

func (r *legacyRouter) probeOnly(peer string, rtt time.Duration) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if m := r.links[peer]; m != nil {
		m.sample(rtt, false, r.now())
	}
}

func (r *legacyRouter) lostOnly(peer string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if m := r.links[peer]; m != nil {
		m.sample(0, true, r.now())
	}
}

func (r *legacyRouter) Lost(peer string) bool {
	return r.measure(peer, func(m *legacyMetric, now time.Time) { m.sample(0, true, now) })
}

func (r *legacyRouter) Gone(peer string) bool {
	return r.measure(peer, func(m *legacyMetric, now time.Time) { m.gone(now) })
}

func (r *legacyRouter) measure(peer string, f func(*legacyMetric, time.Time)) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	m := r.links[peer]
	if m == nil {
		return false
	}
	now := r.now()
	f(m, now)
	if m.heldFor > 0 {
		r.event("link.held", fmt.Sprintf("%s: back after %d downs in %v, held for %v", peer, len(m.downs), legacyFlapWindow, m.heldFor))
		m.heldFor = 0
	}
	for _, p := range r.release() {
		if p != peer {
			r.settle(p, r.links[p], now)
		}
	}
	r.settle(peer, m, now)
	return r.recompute(now)
}

func (r *legacyRouter) release() (released []string) {
	for _, p := range r.order {
		m := r.links[p]
		if m == nil || !m.held || r.graph != nil && !r.graph.distance(r.cfg.Self, p).unreachable() {
			continue
		}
		m.held, m.heldUntil = false, time.Time{}
		r.event("link.released", p+": not reachable another way")
		released = append(released, p)
	}
	return released
}

func (r *legacyRouter) settle(peer string, m *legacyMetric, now time.Time) {
	before := m.advState
	if !m.settle(now) {
		return
	}
	r.due, r.stale = true, true
	if !r.cfg.Leaf && m.advState == StateUp && before != StateUp && (r.known[peer] || r.leaves[peer]) {
		r.sync[peer] = true
	}
}

func (r *legacyRouter) Tick() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	now := r.now()
	for _, m := range r.links {
		m.decay(now)
	}
	r.release()
	for p, m := range r.links {
		r.settle(p, m, now)
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

func (r *legacyRouter) advertSnap(peers []string) (*advert, []string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.cfg.Leaf {
		return nil, nil
	}
	now := r.now()
	since := now.Sub(r.sent)
	if !r.sent.IsZero() && !(r.due && since >= advertGap) && since < advertEvery {
		return nil, nil
	}
	a := &advert{
		origin: r.cfg.Self,
		seq:    max(r.seq+1, uint64(now.UnixMicro())),
		links:  make(map[string]advLink, len(r.links)),
		routes: make(map[string]string, len(r.routes)),
	}
	for p, m := range r.links {
		c := m.advCost
		if m.advState != StateUp {
			c = 0
		}
		if r.leaves[p] {
			if a.members == nil {
				a.members = map[string]advLink{}
			}
			a.members[p] = advLink{c, m.advState}
		} else {
			a.links[p] = advLink{c, m.advState}
		}
	}
	for d, rt := range r.routes {
		if rt.nextHop != "" && r.known[d] {
			a.routes[d] = rt.nextHop
		}
	}
	r.sent, r.due = now, false
	var destPeers []string
	for _, p := range peers {
		if r.known[p] || r.leaves[p] {
			destPeers = append(destPeers, p)
		}
	}
	if len(destPeers) == 0 {
		return nil, nil
	}
	r.seq = a.seq
	return a, destPeers
}

func (r *legacyRouter) syncSnap() ([]*advert, []string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.cfg.Leaf || len(r.sync) == 0 {
		r.sync = map[string]bool{}
		return nil, nil
	}
	var advs []*advert
	var targets []string
	for _, p := range slices.Sorted(maps.Keys(r.sync)) {
		if !r.known[p] && !r.leaves[p] {
			continue
		}
		for _, o := range r.order {
			if a := r.lsdb[o]; a != nil {
				advs = append(advs, a)
				targets = append(targets, p)
			}
		}
	}
	r.sync = map[string]bool{}
	return advs, targets
}

func (r *legacyRouter) receiveSnap(from string, src *advert, deferCostRecompute bool) ([]string, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	now := r.now()
	if r.links[from] == nil || !r.known[from] || src.origin == r.cfg.Self {
		return nil, false
	}
	s := r.senders[from]
	if s == nil {
		s = &bucket{}
		r.senders[from] = s
	}
	if !s.take(now, senderRate, senderBurst) {
		return nil, false
	}
	old := r.lsdb[src.origin]
	if old != nil && (src.seq < old.seq || (src.seq == old.seq && (old.routes != nil || src.routes == nil))) {
		return nil, false
	}
	o := r.origins[src.origin]
	if o == nil {
		o = &bucket{}
		r.origins[src.origin] = o
	}
	if !o.take(now, originRate, originBurst) {
		return nil, false
	}
	var peers []string
	if !r.cfg.Leaf {
		for _, p := range r.cfg.Links {
			if p != from && p != src.origin && r.links[p] != nil && (r.known[p] || r.leaves[p]) {
				peers = append(peers, p)
			}
		}
	}
	linksChanged := old == nil || !maps.Equal(old.links, src.links) || !maps.Equal(old.members, src.members)
	routesChanged := old == nil || (old.routes == nil) != (src.routes == nil) || !maps.Equal(old.routes, src.routes)
	if !linksChanged && !routesChanged {
		old.seq, old.at = src.seq, now
		return peers, false
	}
	stateChanged := old == nil || len(old.links) != len(src.links)
	if !stateChanged {
		for p, l := range src.links {
			if ol, ok := old.links[p]; !ok || ol.state != l.state {
				stateChanged = true
				break
			}
		}
	}
	cp := *src
	cp.at = now
	if linksChanged {
		r.stale = true
	}
	r.lsdb[cp.origin] = &cp
	if r.graph != nil {
		r.graph.forget()
	}
	if deferCostRecompute && !stateChanged && !routesChanged {
		return peers, false
	}
	return peers, r.recompute(now)
}

func (r *legacyRouter) recomputeNow() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.recompute(r.now())
}

func (r *legacyRouter) Advert(b Budget, peers []string) []Message {
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
	if a.raw != nil && !r.skipSign && len(r.cfg.Key) == ed25519.PrivateKeySize {
		a.sig = advertInput(r.cfg.Network, a.format, a.raw).Sign(r.cfg.Key)
	}
	r.sent, r.due = now, false
	var destPeers []string
	for _, p := range peers {
		if r.known[p] || r.leaves[p] {
			destPeers = append(destPeers, p)
		}
	}
	if a.raw == nil || len(destPeers) == 0 {
		return nil
	}
	r.seq = a.seq
	return a.messages(destPeers)
}

func (r *legacyRouter) Sync(b Budget) []Message {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.cfg.Leaf || len(r.sync) == 0 {
		r.sync = map[string]bool{}
		return nil
	}
	var out []Message
	for _, p := range slices.Sorted(maps.Keys(r.sync)) {
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

func (r *legacyRouter) Receive(from string, msg Message, b Budget) ([]Message, bool, error) {
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
	var err error
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
		return nil, false, err
	}
	if a.origin == r.cfg.Self {
		return nil, false, nil
	}
	if old := r.lsdb[a.origin]; old != nil && (a.seq < old.seq || (a.seq == old.seq && (old.routes != nil || a.routes == nil))) {
		return nil, false, nil
	}
	if !r.skipSign && !advertInput(r.cfg.Network, msg.Format, msg.Raw).Verify(r.cfg.Keys[a.origin], msg.Sig) {
		return nil, false, ErrSignature
	}
	o := r.origins[a.origin]
	if o == nil {
		o = &bucket{}
		r.origins[a.origin] = o
	}
	if !o.take(now, originRate, originBurst) {
		return nil, false, errRate
	}
	a.at, a.format, a.raw, a.sig = now, msg.Format, slices.Clone(msg.Raw), slices.Clone(msg.Sig)
	for p := range a.links {
		if !r.real[pair(a.origin, p)] {
			delete(a.links, p)
		}
	}
	old := r.lsdb[a.origin]
	linksChanged := old == nil || !maps.Equal(old.links, a.links) || !maps.Equal(old.members, a.members)
	routesChanged := old == nil || (old.routes == nil) != (a.routes == nil) || !maps.Equal(old.routes, a.routes)
	if !linksChanged && !routesChanged {
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
	r.lsdb[a.origin] = a
	if r.graph != nil {
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

func (r *legacyRouter) NextHop(dest string) (string, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	rt := r.routes[dest]
	if rt == nil || rt.nextHop == "" {
		return "", false
	}
	return rt.nextHop, true
}

func (r *legacyRouter) build() *graph {
	nodes := append([]string{r.cfg.Self}, r.order...)
	slices.Sort(nodes)
	n := len(nodes)
	g := &graph{self: r.cfg.Self, nodes: nodes, index: make(map[string]int32, n), leaves: r.leaves, leaf: make([]bool, n), noTransit: make([]bool, n),
		adj: make([][]edge, n), dist: make([][]key, n), first: make([][]int32, n), lsdb: r.lsdb, epoch: 1}
	for i, id := range nodes {
		g.index[id] = int32(i)
		g.leaf[i], g.noTransit[i] = r.leaves[id], r.noTransit[id]
	}
	g.selfAt = g.index[r.cfg.Self]
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
	for i, a := range nodes {
		for j := i + 1; j < n; j++ {
			b := nodes[j]
			la, okA := side(a, b)
			lb, okB := side(b, a)
			if (a == r.cfg.Self && !okA) || (b == r.cfg.Self && !okB) || (!okA && !okB) {
				continue
			}
			if (okA && la.state != StateUp) || (okB && lb.state != StateUp) {
				continue
			}
			k := linkKey(a, b, max(la.cost, lb.cost))
			g.adj[i] = append(g.adj[i], edge{int32(j), k})
			g.adj[j] = append(g.adj[j], edge{int32(i), k})
		}
	}
	g.solve()
	return g
}

func (r *legacyRouter) recompute(now time.Time) bool {
	if r.graph == nil || r.stale {
		r.graph, r.stale = r.build(), false
	}
	foreign := len(r.lsdb) > 0
	changed := false
	for _, d := range r.order {
		rt := r.routes[d]
		df := foreign || (r.leaves[d] && r.links[d] != nil && r.links[d].advState == StateUp)
		if !df {
			rt.dynamic = false
		}
		next, reason := r.choose(d, rt, df, now)
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

func (r *legacyRouter) choose(d string, rt *legacyRoute, foreign bool, now time.Time) (string, string) {
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
	if g.distance(self, d).unreachable() {
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
	t := &r.tuning
	pref := g.hop(self, d)
	if pref != "" && g.feasible(pref, d, curCost) {
		if pref == cur {
			return cur, ""
		}
		if !curUsable {
			return pref, reason
		}
		damping := rt.damping(pref, now, t)
		prefCost, _, _ := g.via(pref, d)
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
		same, fewer := prefCost.tolls == curCost.tolls, prefCost.tolls < curCost.tolls
		saved := curCost.ms() - prefCost.ms()
		if fewer || same && legacyCheaper(saved, curCost.ms(), t.SwitchMs, t.SwitchPct) {
			if damping == 0 || fewer {
				return pref, ReasonBetter
			}
			return wait(damping, ReasonBetter)
		}
		if m := r.links[pref]; same && m != nil && g.relays(pref, d) < g.relays(cur, d) && legacyCheaper(saved, curCost.ms(), t.ShorterMs, t.ShorterPct) {
			if m.penalty < reuseBelow {
				return wait(max(t.Hold, damping), "shorter")
			}
		}
	}
	if curUsable {
		return cur, ""
	}
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
