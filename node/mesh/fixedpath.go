package mesh

import (
	"encoding/binary"
	"encoding/hex"
	"fmt"
	mrand "math/rand/v2"
	"net/netip"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// Fixed paths (design 2.4), the own routes of the controller. A path names
// sources, destinations and the relays between them; traffic from a source to a
// destination then goes through those relays in that order while every link on
// the way is up, whatever the routes say. A pair on several paths takes them in
// the config's order: the first one in use, the next while it is down, the
// routes while none is. The source picks the first relay for its own packets,
// each relay picks the next one by the packet's source and destination, and only
// for a packet that came from the node before it on a path in use: anything else
// is routed as usual. No byte is added: what relays forward is end to end, from
// the sending node's address to the receiving node's.
//
// Every node on a path judges it from the same link states (its own links and
// what the others advertise): one link not up and it is down; all of them up
// again for pinHold and it is in use again. A path just configured is taken as
// soon as it is up.
//
// Links up is not packets through. The source probes each of its paths whose
// links are up, end to end every pathProbeEvery, and the destination answers
// over its way back of the same rank: a path that leaves a probe unanswered for
// pathSilent is down at the source as well, until it has been answered again for
// pinHold; one not answered yet is still coming up, as one whose links are not
// all up yet. The source marks its end-to-end packets of a pair with the rank of
// the path they take, 0 for the routes, in the outer header's identification
// field, which they do not need as they are never fragmented. A relay takes a
// marked packet over the path of that rank while its links are up, whatever it
// judges in use, so that the pair leaves a path the source finds silent; an
// unmarked one, from a node before marks, over the first path in use.

// pinHold is how long a path that went down has to stay up before it is used again.
var pinHold = 2 * time.Minute

// pinStart is how long a path just configured may take to come up before its
// being down counts as an outage.
var pinStart = time.Minute

// pathProbeEvery is how often the source probes each path whose links are up;
// pathSilent how long a probe may go unanswered before the path is down.
var (
	pathProbeEvery = time.Second
	pathSilent     = 3 * time.Second
)

// pathMark flags the identification field of an outer packet marked with the
// rank of its path; pathFlow the flow id meshTUN.sendOuter takes a probe of
// one path by, with its rank.
const (
	pathMark = 0x8000
	pathFlow = 1 << 63
)

// FixedPath is one entry of config.json's paths: every source to every
// destination, but the pairs in skip, through via.
type FixedPath struct {
	ID   string      `json:"id"`
	From []string    `json:"from"`
	To   []string    `json:"to"`
	Via  []string    `json:"via"`
	Skip [][2]string `json:"skip,omitempty"`
	// Rank is the path's place among its pair's, the same on each of its
	// nodes and for its way back; 0: its place among them here.
	Rank int `json:"rank,omitempty"`
	// NoProbe: the destination does not answer path probes (an older node),
	// so the source goes by the links alone.
	NoProbe bool `json:"noProbe,omitempty"`
}

const maxFixedPaths = 512
const maxPathHops = 8
const maxPathRank = 8

func (c *Config) checkPaths() error {
	if len(c.Paths) > maxFixedPaths {
		return fmt.Errorf("at most %d paths", maxFixedPaths)
	}
	ids := map[string]bool{}
	for i, p := range c.Paths {
		at := fmt.Sprintf("paths[%d]", i)
		if p.ID == "" || ids[p.ID] {
			return fmt.Errorf("%s.id is empty or repeated", at)
		}
		ids[p.ID] = true
		if p.Rank < 0 || p.Rank > maxPathRank {
			return fmt.Errorf("%s.rank is not 0 to %d", at, maxPathRank)
		}
		if len(p.Via) == 0 || len(p.Via) > maxPathHops {
			return fmt.Errorf("%s.via has 1 to %d relays", at, maxPathHops)
		}
		on := map[string]bool{}
		for _, v := range p.Via {
			if c.roleOf(v) != roleRelay {
				return fmt.Errorf("%s.via: %q is not a relay", at, v)
			}
			if on[v] {
				return fmt.Errorf("%s.via: %q is repeated", at, v)
			}
			on[v] = true
		}
		if len(p.From) == 0 || len(p.To) == 0 {
			return fmt.Errorf("%s has no source or no destination", at)
		}
		for _, list := range [][]string{p.From, p.To} {
			for _, n := range list {
				if c.roleOf(n) == "" {
					return fmt.Errorf("%s: %q is not a node of the mesh", at, n)
				}
				if on[n] {
					return fmt.Errorf("%s: %q is both an end and a relay of the path", at, n)
				}
			}
		}
	}
	return nil
}

// pinHop is what this node does with a packet of one pair on a path.
type pinHop struct {
	prev   string // the node the packet comes from on the path; "" when this node is its source
	nextID string
	next   *[32]byte // the next node's WireGuard key
	rank   int
	run    *pinRun
}

// pinTable is a config's pairs that pass this node, by source and destination
// address: each pair's hops here in the order of its paths.
type pinTable map[[2]netip.Addr][]pinHop

// local is the peer for a packet of this node's own to dst: the first path of
// the pair in use.
func (t pinTable) local(self, dst netip.Addr) (pinHop, bool) {
	for _, h := range t[[2]netip.Addr{self, dst}] {
		if h.prev == "" && h.run.active.Load() {
			return h, true
		}
	}
	return pinHop{}, false
}

// forward is the peer for an unmarked packet from src to dst that came from
// node from: the first path in use that brings it from there.
func (t pinTable) forward(src, dst netip.Addr, from string) (pinHop, bool) {
	for _, h := range t[[2]netip.Addr{src, dst}] {
		if h.prev != "" && h.prev == from && h.run.active.Load() {
			return h, true
		}
	}
	return pinHop{}, false
}

// marked is the peer for a packet from src to dst marked with rank that came
// from node from: the path of that rank, while its links are up.
func (t pinTable) marked(src, dst netip.Addr, from string, rank int) (pinHop, bool) {
	for _, h := range t[[2]netip.Addr{src, dst}] {
		if h.prev != "" && h.prev == from && h.rank == rank && h.run.up.Load() {
			return h, true
		}
	}
	return pinHop{}, false
}

// ranked is the peer for a probe of this node's own to dst over the path of
// rank, while its links are up.
func (t pinTable) ranked(self, dst netip.Addr, rank int) (pinHop, bool) {
	for _, h := range t[[2]netip.Addr{self, dst}] {
		if h.prev == "" && h.rank == rank && h.run.up.Load() {
			return h, true
		}
	}
	return pinHop{}, false
}

// source: this node is the source of the pair on a path.
func (t pinTable) source(self, dst netip.Addr) bool {
	return slices.ContainsFunc(t[[2]netip.Addr{self, dst}], func(h pinHop) bool { return h.prev == "" })
}

// answer takes the answer to a probe of this node's own to dst over the path
// of rank.
func (t pinTable) answer(self, dst netip.Addr, rank int, token uint32) {
	for _, h := range t[[2]netip.Addr{self, dst}] {
		if h.prev == "" && h.rank == rank && h.run.token == token {
			h.run.unanswered.Store(0)
			h.run.answered.Store(true)
		}
	}
}

// markPath marks b, an outer packet, with rank.
func markPath(b []byte, rank int) {
	binary.BigEndian.PutUint16(b[4:6], pathMark|uint16(rank))
	b[10], b[11] = 0, 0
	binary.BigEndian.PutUint16(b[10:12], headerChecksum(b[:20]))
}

// pathRank is the rank p is marked with; false: it is not.
func (p ipv4) pathRank() (int, bool) {
	v := binary.BigEndian.Uint16(p.b[4:6])
	return int(v &^ pathMark), v&pathMark != 0
}

// pinRun is one pair of a path as this node follows it; it outlives the
// configs that keep the pair on the same nodes.
type pinRun struct {
	path    string
	nodes   []string // source, the via relays, destination
	source  bool     // this node is the pair's source
	quiet   bool     // not probed: FixedPath.NoProbe
	rank    int      // its place among the pair's paths, from 1
	created time.Time
	active  atomic.Bool
	up      atomic.Bool // every link up
	token   uint32      // in the ids of its probes
	// unanswered is when the oldest probe since the last answer went out, in
	// unix nanoseconds; 0 while none is. answered: one was.
	unanswered atomic.Int64
	answered   atomic.Bool
	// Read and written by the evaluation alone.
	outage  bool // it went down after it was used, or never came up in pinStart
	upSince time.Time
	down    []string // the first link not up, while one is not
	silent  bool     // its links up, a probe unanswered for pathSilent
	state   string
	since   time.Time
}

// pinRuns are this node's runs.
type pinRuns struct {
	mu   sync.Mutex
	runs map[string]*pinRun
	// use is, by source and destination, the rank of the path this node's own
	// traffic takes; 0 (or none) for the routes.
	use map[[2]string]int
	now func() time.Time
}

// pinSwitch is a pair of this node's own going over to another path, or to
// the routes (rank 0).
type pinSwitch struct {
	dst      string
	from, to int
	down     []string // the link that took the path before down
	silent   bool     // the path before did not answer its probes
}

func newPinRuns() *pinRuns {
	return &pinRuns{runs: map[string]*pinRun{}, use: map[[2]string]int{}, now: time.Now}
}

func runKey(path string, nodes []string) string { return path + "\x00" + strings.Join(nodes, "\x00") }

// tableFor is the table of c's pairs that pass this node; the runs for them
// are kept or made, the others dropped. keys are the peers' WireGuard keys by node.
func (r *pinRuns) tableFor(c *Config, keys map[string]string, nodes *e2eTable) pinTable {
	t := pinTable{}
	if r == nil {
		return t
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	keep := map[string]bool{}
	ranks := map[[2]string]int{}
	self := c.Self.ID
	for _, p := range c.Paths {
		skip := map[[2]string]bool{}
		for _, s := range p.Skip {
			skip[s] = true
		}
		for _, s := range p.From {
			for _, d := range p.To {
				if s == d || skip[[2]string{s, d}] {
					continue
				}
				path := append(append([]string{s}, p.Via...), d)
				i := slices.Index(path, self)
				if i < 0 || i == len(path)-1 {
					continue
				}
				sa, da := nodes.byID[s], nodes.byID[d]
				if s == self {
					sa = &e2eNode{addr: nodes.self}
				}
				if sa == nil || da == nil {
					continue
				}
				pair := [2]netip.Addr{sa.addr, da.addr}
				key := runKey(p.ID, path)
				run := r.runs[key]
				if run == nil {
					now := r.now()
					run = &pinRun{path: p.ID, nodes: path, source: i == 0, created: now, token: mrand.Uint32(), state: "starting", since: now}
					r.runs[key] = run
				}
				keep[key] = true
				ranks[[2]string{s, d}]++
				if run.quiet = p.NoProbe; run.quiet {
					run.unanswered.Store(0)
				}
				run.rank = p.Rank
				if run.rank == 0 {
					run.rank = ranks[[2]string{s, d}]
				}
				// A next node with no peer for now keeps its run, not its hop.
				k, err := hex.DecodeString(keys[path[i+1]])
				if err != nil || len(k) != 32 {
					continue
				}
				prev := ""
				if i > 0 {
					prev = path[i-1]
				}
				next := [32]byte(k)
				t[pair] = append(t[pair], pinHop{prev: prev, nextID: path[i+1], next: &next, rank: run.rank, run: run})
			}
		}
	}
	for k := range r.runs {
		if !keep[k] {
			delete(r.runs, k)
		}
	}
	return t
}

// evaluate moves every run by the first of its links down says is not up (nil
// when all are) and, at the source, by its probes, then has each pair of this
// node's own take its first path in use; it returns the pairs that went over to
// another one, for events.
func (r *pinRuns) evaluate(down func(nodes []string) []string) []pinSwitch {
	r.mu.Lock()
	defer r.mu.Unlock()
	now := r.now()
	set := func(run *pinRun, state string) {
		if run.state != state {
			run.state, run.since = state, now
		}
	}
	for _, run := range r.runs {
		run.down = down(run.nodes)
		run.up.Store(run.down == nil)
		if run.down != nil {
			run.unanswered.Store(0)
		}
		u := run.unanswered.Load()
		run.silent = run.source && u != 0 && now.Sub(time.Unix(0, u)) >= pathSilent
		if run.down != nil || run.silent {
			if run.active.Load() && (run.down != nil || run.answered.Load()) || now.Sub(run.created) >= pinStart {
				run.outage = true
			}
			run.active.Store(false)
			run.upSince = time.Time{}
			if run.outage {
				set(run, "fallback")
			} else {
				set(run, "starting")
			}
			continue
		}
		if run.active.Load() {
			continue
		}
		if run.outage {
			if run.upSince.IsZero() {
				run.upSince = now
			}
			if now.Sub(run.upSince) < pinHold {
				set(run, "waiting")
				continue
			}
		}
		run.active.Store(true)
		run.outage = false
		set(run, "pinned")
	}
	use := map[[2]string]int{}
	for _, run := range r.runs {
		if !run.source {
			continue
		}
		pair := [2]string{run.nodes[0], run.nodes[len(run.nodes)-1]}
		at, seen := use[pair]
		if !seen {
			use[pair] = 0
		}
		if run.active.Load() && (at == 0 || run.rank < at) {
			use[pair] = run.rank
		}
	}
	var out []pinSwitch
	for pair, to := range use {
		if from := r.use[pair]; from != to {
			sw := pinSwitch{dst: pair[1], from: from, to: to}
			if old := r.ranked(pair, from); old != nil {
				sw.down, sw.silent = slices.Clone(old.down), old.silent
			}
			out = append(out, sw)
		}
	}
	r.use = use
	slices.SortFunc(out, func(a, b pinSwitch) int { return strings.Compare(a.dst, b.dst) })
	return out
}

// pathProbe is a probe of this node's pair with dst over the path of rank.
type pathProbe struct {
	dst  string
	rank int
	id   [8]byte // the rank, the run's token, zeros
}

// probes are the probes to send now, over each path of this node's own whose
// links are up; each is unanswered from now unless one before it already is.
func (r *pinRuns) probes() []pathProbe {
	r.mu.Lock()
	defer r.mu.Unlock()
	now := r.now().UnixNano()
	var out []pathProbe
	for _, run := range r.runs {
		if !run.source || run.quiet || !run.up.Load() {
			continue
		}
		run.unanswered.CompareAndSwap(0, now)
		p := pathProbe{dst: run.nodes[len(run.nodes)-1], rank: run.rank}
		p.id[0] = byte(run.rank)
		binary.BigEndian.PutUint32(p.id[1:5], run.token)
		out = append(out, p)
	}
	return out
}

// ranked is this node's run of the pair at rank, or nil.
func (r *pinRuns) ranked(pair [2]string, rank int) *pinRun {
	for _, run := range r.runs {
		if run.source && run.rank == rank && run.nodes[0] == pair[0] && run.nodes[len(run.nodes)-1] == pair[1] {
			return run
		}
	}
	return nil
}

// PathStatus is a fixed path's pair this node is the source of.
type PathStatus struct {
	ID    string   `json:"id"`
	Nodes []string `json:"nodes"`
	// State: pinned (the pair is on it), standby (up, behind an earlier path
	// in use), fallback (a link is down), waiting (all up again, usable at
	// until), starting (just configured, not up yet).
	State string `json:"state"`
	Since int64  `json:"since"`
	Until int64  `json:"until,omitempty"`
	// Rank is its place among the pair's paths, from 1; Down the first link not
	// up, while it is fallback or starting; Silent, then, that its links are up
	// but its probes go unanswered.
	Rank   int      `json:"rank"`
	Down   []string `json:"down,omitempty"`
	Silent bool     `json:"silent,omitempty"`
}

const maxPathStatus = 256

func (r *pinRuns) status() []PathStatus {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := []PathStatus{}
	for _, run := range r.runs {
		if !run.source {
			continue
		}
		s := PathStatus{ID: run.path, Nodes: slices.Clone(run.nodes), State: run.state, Since: run.since.UnixMilli(), Rank: run.rank}
		pair := [2]string{run.nodes[0], run.nodes[len(run.nodes)-1]}
		switch {
		case run.state == "pinned" && r.use[pair] != run.rank:
			s.State = "standby"
		case run.state == "waiting":
			s.Until = run.upSince.Add(pinHold).UnixMilli()
		case run.down != nil:
			s.Down = slices.Clone(run.down)
		case run.silent:
			s.Silent = true
		}
		out = append(out, s)
	}
	slices.SortFunc(out, func(a, b PathStatus) int {
		if c := strings.Compare(a.Nodes[len(a.Nodes)-1], b.Nodes[len(b.Nodes)-1]); c != 0 {
			return c
		}
		if a.Rank != b.Rank {
			return a.Rank - b.Rank
		}
		return strings.Compare(a.ID, b.ID)
	})
	if len(out) > maxPathStatus {
		out = out[:maxPathStatus]
	}
	return out
}

// watchPins keeps every run's state current and probes this node's own until
// the engine closes.
func (e *engine) watchPins() {
	tick := time.NewTicker(500 * time.Millisecond)
	defer tick.Stop()
	var probed time.Time
	for {
		select {
		case <-e.done:
			return
		case <-tick.C:
		}
		if time.Since(probed) >= pathProbeEvery {
			probed = time.Now()
			e.probePaths()
		}
		down := func(nodes []string) []string { return nodes[:2] }
		if rt := e.router(); rt != nil {
			down = rt.PathDown
		}
		// "<destination>: <rank before> -> <rank now>[ down <a> <b>| silent]", 0 for the routes.
		for _, sw := range e.pins.evaluate(down) {
			detail := fmt.Sprintf("%s: %d -> %d", sw.dst, sw.from, sw.to)
			switch {
			case sw.down != nil:
				detail += " down " + strings.Join(sw.down, " ")
			case sw.silent:
				detail += " silent"
			}
			e.events.add("pin.switch", detail)
		}
	}
}

// probePaths sends the probes of this node's own paths.
func (e *engine) probePaths() {
	r, s := e.tun.route.Load(), e.tun.e2e.Load()
	if r == nil || s == nil {
		return
	}
	for _, p := range e.pins.probes() {
		if z := r.nodes.byID[p.dst]; z != nil {
			s.sendPath(probePacket(r.nodes.self, z.addr, probePath, p.id, 0), z, p.rank)
		}
	}
}
