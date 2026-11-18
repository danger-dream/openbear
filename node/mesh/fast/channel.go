package fast

import (
	"errors"
	"io"
	"math"
	"net/netip"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"
)

// A channel is this node's way to one destination's fast ports: the paths the
// mesh would take there (Hooks.Paths), each probed end to end and so kept
// warm, hop by hop, while the channel is in use. A new connection takes the
// first path that answers, in the mesh's order; one that stops answering is
// passed over until it answers again, and the streams on it are cut, so that
// their clients reconnect over one that answers. A stream ending in an error
// has its path probed at once. A channel nothing used for channelIdle is
// dropped; the next connection to the destination goes over the mesh and
// opens it again.
var channelIdle = 10 * time.Minute

const (
	refreshEvery  = 5 * time.Second
	busyFor       = time.Minute
	probeRetryMax = 30 * time.Second
	probeTimeout  = 5 * time.Second
	probePort     = 1
)

var (
	// probeEvery is how often an answering path is probed; probeBusy while
	// the channel carries streams or took one within busyFor.
	probeEvery    = 15 * time.Second
	probeBusy     = 5 * time.Second
	probeRetryMin = 2 * time.Second
	// A probe waits three of its path's round trips, within probeTimeoutMin
	// and probeTimeout.
	probeTimeoutMin = time.Second
)

const (
	pathUntested = "untested"
	pathReady    = "ready"
	pathDown     = "down"
)

// Plan is one way to a destination: Hops after this node, ending at the
// destination when Planned, else at a relay that plans the rest over its
// Alt-th path. Fixed names the fixed path it is, "" for the routes.
type Plan struct {
	Hops    []string
	Planned bool
	Alt     byte
	Fixed   string
}

func (p Plan) key() string {
	return strings.Join(p.Hops, ">") + "|" + strconv.FormatBool(p.Planned) + "|" + strconv.Itoa(int(p.Alt)) + "|" + p.Fixed
}

type candidate struct {
	plan    Plan
	state   string
	since   time.Time
	rtt     time.Duration
	path    []string // the nodes the last answer came through, after this one
	err     string
	fails   int
	next    time.Time
	probing bool
}

type channel struct {
	dest      string
	cands     []*candidate
	opened    time.Time
	lastUse   time.Time
	refreshed time.Time
	streams   int64
	fallbacks int64
}

func (c *channel) ready() bool {
	return slices.ContainsFunc(c.cands, func(x *candidate) bool { return x.state == pathReady })
}

type channels struct {
	svc *Service
	// The probe timings in force when the channels were made.
	every, busy, retryMin, timeoutMin time.Duration
	mu                                sync.Mutex
	byDest                            map[string]*channel
	wake                              chan struct{}
	stop                              chan struct{}
	done                              chan struct{}
}

func newChannels(svc *Service) *channels {
	c := &channels{svc: svc, every: probeEvery, busy: probeBusy, retryMin: probeRetryMin, timeoutMin: probeTimeoutMin,
		byDest: map[string]*channel{}, wake: make(chan struct{}, 1), stop: make(chan struct{}), done: make(chan struct{})}
	go c.loop()
	return c
}

func (c *channels) close() {
	select {
	case <-c.stop:
	default:
		close(c.stop)
	}
	<-c.done
}

func (c *channels) poke() {
	select {
	case c.wake <- struct{}{}:
	default:
	}
}

// warm opens the channel to dest, or keeps it, for a connection that went
// over the mesh because it was not ready.
func (c *channels) warm(dest string) {
	c.mu.Lock()
	ch := c.byDest[dest]
	now := c.svc.clock()
	if ch == nil {
		ch = &channel{dest: dest, opened: now}
		c.byDest[dest] = ch
	}
	ch.lastUse = now
	c.mu.Unlock()
	c.poke()
}

// pick is the paths a new connection to dest may take, the first first; nil
// while none answers.
func (c *channels) pick(dest string) []Plan {
	c.mu.Lock()
	defer c.mu.Unlock()
	ch := c.byDest[dest]
	now := c.svc.clock()
	if ch == nil {
		ch = &channel{dest: dest, opened: now}
		c.byDest[dest] = ch
		c.poke()
	}
	ch.lastUse = now
	var out []Plan
	for _, x := range ch.cands {
		if x.state == pathReady {
			out = append(out, x.plan)
		}
	}
	if len(out) > 0 {
		ch.streams++
	} else {
		ch.fallbacks++
	}
	return out
}

// rtt is the round trip of the first path to dest that answers.
func (c *channels) rtt(dest string) (time.Duration, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if ch := c.byDest[dest]; ch != nil {
		for _, x := range ch.cands {
			if x.state == pathReady {
				return x.rtt, true
			}
		}
	}
	return 0, false
}

// failed passes over plan to dest until it answers a probe again.
func (c *channels) failed(dest string, plan Plan, err error) {
	c.mu.Lock()
	ch := c.byDest[dest]
	changed := false
	key := plan.key()
	if ch != nil {
		for _, x := range ch.cands {
			if x.plan.key() == key && x.state != pathDown {
				x.state, x.since, x.err, x.next = pathDown, c.svc.clock(), errText(err), c.svc.clock()
				changed = true
			}
		}
	}
	c.mu.Unlock()
	if changed {
		c.svc.cutPath(dest, key)
		c.svc.readyChanged()
		c.poke()
	}
}

// recheck probes plan to dest now: a stream on it ended in an error.
func (c *channels) recheck(dest string, plan Plan) {
	c.mu.Lock()
	changed := false
	if ch := c.byDest[dest]; ch != nil {
		key := plan.key()
		for _, x := range ch.cands {
			if x.plan.key() == key && x.state == pathReady && !x.probing {
				x.next, changed = c.svc.clock(), true
			}
		}
	}
	c.mu.Unlock()
	if changed {
		c.poke()
	}
}

// fellBack counts a connection the paths did not take after pick gave some.
func (c *channels) fellBack(dest string) {
	c.mu.Lock()
	if ch := c.byDest[dest]; ch != nil {
		ch.streams--
		ch.fallbacks++
	}
	c.mu.Unlock()
}

// ready are the destinations a new connection finds a path to.
func (c *channels) ready() map[string]bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := map[string]bool{}
	for d, ch := range c.byDest {
		if ch.ready() {
			out[d] = true
		}
	}
	return out
}

func (c *channels) loop() {
	defer close(c.done)
	t := time.NewTicker(time.Second)
	defer t.Stop()
	for {
		select {
		case <-c.stop:
			return
		case <-t.C:
		case <-c.wake:
		}
		c.tick()
	}
}

func (c *channels) tick() {
	st := c.svc.state.Load()
	now := c.svc.clock()
	type job struct {
		dest string
		cand *candidate
		plan Plan
	}
	var jobs []job
	var refresh []string
	changed := false
	c.mu.Lock()
	for d, ch := range c.byDest {
		if st == nil || !st.knows(d) || now.Sub(ch.lastUse) > channelIdle && !c.svc.streamsTo(d) {
			changed = changed || ch.ready()
			delete(c.byDest, d)
			continue
		}
		if ch.refreshed.IsZero() || now.Sub(ch.refreshed) >= refreshEvery {
			refresh = append(refresh, d)
		}
	}
	c.mu.Unlock()
	for _, d := range refresh {
		plans := c.svc.paths(st, d)
		c.mu.Lock()
		if ch := c.byDest[d]; ch != nil {
			changed = ch.adopt(plans, now) || changed
		}
		c.mu.Unlock()
	}
	c.mu.Lock()
	for d, ch := range c.byDest {
		for _, x := range ch.cands {
			if !x.probing && !now.Before(x.next) {
				x.probing = true
				jobs = append(jobs, job{d, x, x.plan})
			}
		}
	}
	c.mu.Unlock()
	if changed {
		c.svc.readyChanged()
	}
	for _, j := range jobs {
		go c.probe(j.dest, j.cand, j.plan)
	}
}

// adopt takes plans as the channel's candidates, keeping what is known of
// the ones it had; whether a path is ready may have changed.
func (ch *channel) adopt(plans []Plan, now time.Time) bool {
	before := ch.ready()
	old := map[string]*candidate{}
	for _, x := range ch.cands {
		old[x.plan.key()] = x
	}
	next := make([]*candidate, 0, len(plans))
	for _, p := range plans {
		if x := old[p.key()]; x != nil {
			next = append(next, x)
			continue
		}
		next = append(next, &candidate{plan: p, state: pathUntested, since: now, next: now})
	}
	ch.cands, ch.refreshed = next, now
	return ch.ready() != before
}

func (c *channels) probe(dest string, cand *candidate, plan Plan) {
	c.mu.Lock()
	timeout := probeTimeout
	if cand.rtt > 0 {
		timeout = min(probeTimeout, max(c.timeoutMin, 3*cand.rtt))
	}
	c.mu.Unlock()
	lifetime := c.svc.state.Load()
	start := c.svc.clock()
	path, err := c.svc.probe(dest, plan, timeout)
	took := c.svc.clock().Sub(start)
	busy := c.svc.streamsTo(dest)
	c.mu.Lock()
	cand.probing = false
	current := c.svc.state.Load()
	ch := c.byDest[dest]
	if lifetime == nil || current == nil || lifetime.pool != current.pool || ch == nil || !slices.Contains(ch.cands, cand) {
		c.mu.Unlock()
		return
	}
	was := ch != nil && ch.ready()
	now := c.svc.clock()
	cut := false
	if err == nil {
		if cand.state != pathReady {
			cand.since = now
		}
		cand.state, cand.err, cand.fails, cand.path = pathReady, "", 0, path
		if cand.rtt == 0 {
			cand.rtt = took
		} else {
			cand.rtt = (cand.rtt*7 + took*3) / 10
		}
		every := c.every
		if busy || ch != nil && now.Sub(ch.lastUse) < busyFor {
			every = c.busy
		}
		cand.next = now.Add(every)
	} else {
		if cand.state != pathDown {
			cand.since = now
			cut = cand.state == pathReady
		}
		cand.state, cand.err = pathDown, errText(err)
		cand.fails++
		cand.next = now.Add(min(probeRetryMax, c.retryMin<<min(cand.fails-1, 4)))
	}
	is := ch != nil && ch.ready()
	c.mu.Unlock()
	if cut {
		c.svc.cutPath(dest, plan.key())
	}
	if was != is {
		c.svc.readyChanged()
	}
}

func errText(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

// ChannelStatus is one destination's channel for diagnostics.
type ChannelStatus struct {
	Dest      string       `json:"dest"`
	Ready     bool         `json:"ready"`
	Using     int          `json:"using"`
	Opened    int64        `json:"opened"`
	LastUse   int64        `json:"lastUse"`
	Streams   int64        `json:"streams"`
	Fallbacks int64        `json:"fallbacks"`
	Paths     []PathStatus `json:"paths"`
}

// PathStatus is one candidate path of a channel.
type PathStatus struct {
	Hops    []string `json:"hops"`
	Fixed   string   `json:"fixed,omitempty"`
	Planned bool     `json:"planned,omitempty"`
	Alt     int      `json:"alt,omitempty"`
	State   string   `json:"state"`
	Since   int64    `json:"since"`
	RTTMs   float64  `json:"rttMs,omitempty"`
	Error   string   `json:"error,omitempty"`
}

func (c *channels) status() []ChannelStatus {
	c.mu.Lock()
	defer c.mu.Unlock()
	dests := make([]string, 0, len(c.byDest))
	for d := range c.byDest {
		dests = append(dests, d)
	}
	slices.Sort(dests)
	out := make([]ChannelStatus, 0, len(dests))
	for _, d := range dests {
		ch := c.byDest[d]
		cs := ChannelStatus{Dest: d, Ready: ch.ready(), Using: -1, Opened: ch.opened.UnixMilli(), LastUse: ch.lastUse.UnixMilli(), Streams: ch.streams, Fallbacks: ch.fallbacks}
		for i, x := range ch.cands {
			if cs.Using < 0 && x.state == pathReady {
				cs.Using = i
			}
			hops := x.path
			if len(hops) == 0 {
				hops = x.plan.Hops
			}
			ps := PathStatus{Hops: slices.Clone(hops), Fixed: x.plan.Fixed, Planned: x.plan.Planned || len(x.path) > 0, Alt: int(x.plan.Alt),
				State: x.state, Since: x.since.UnixMilli(), Error: x.err}
			if x.rtt > 0 {
				ps.RTTMs = math.Round(float64(x.rtt)/float64(time.Microsecond)/100) / 10
			}
			cs.Paths = append(cs.Paths, ps)
		}
		out = append(out, cs)
	}
	return out
}

// probeAnswer is what the destination writes back for a probe: 'P', how many
// nodes it came through (the origin and itself included), and their mesh
// addresses.
func probeAnswer(visited []netip.Addr) []byte {
	out := []byte{'P', byte(len(visited))}
	for _, a := range visited {
		b := a.As4()
		out = append(out, b[:]...)
	}
	return out
}

var errProbeAnswer = errors.New("probe answer is malformed")

func readProbeAnswer(r io.Reader) ([]netip.Addr, error) {
	var head [2]byte
	if _, err := io.ReadFull(r, head[:]); err != nil {
		return nil, err
	}
	if head[0] != 'P' || int(head[1]) > maxVisited+1 {
		return nil, errProbeAnswer
	}
	raw := make([]byte, 4*int(head[1]))
	if _, err := io.ReadFull(r, raw); err != nil {
		return nil, err
	}
	out := make([]netip.Addr, 0, head[1])
	for i := 0; i < len(raw); i += 4 {
		out = append(out, netip.AddrFrom4([4]byte(raw[i:i+4])))
	}
	return out, nil
}
