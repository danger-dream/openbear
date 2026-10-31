package routing

import (
	"sort"
	"strings"
	"testing"
	"time"
)

// testNet runs routers on a fake clock and carries their advertisements
// over the links that are up, each after its one-way delay.
type testNet struct {
	t      *testing.T
	now    time.Time
	r      map[string]*Router
	names  []string
	links  map[[2]string]*testLink
	queue  []delivery
	events map[string][]string
	budget Budget
	onSend func(from string, m Message)
	hops   map[string]map[string]string
}

type testLink struct {
	up    bool
	delay time.Duration
}

type delivery struct {
	at       time.Time
	from, to string
	msg      Message
}

// newNet builds relays joined by links "A-B" with their one-way delays;
// nextHops are each relay's configured next hops, "A>C": "B".
func newNet(t *testing.T, links map[string]time.Duration, nextHops map[string]string) *testNet {
	n := &testNet{t: t, now: time.Unix(1_800_000_000, 0), r: map[string]*Router{}, links: map[[2]string]*testLink{}, events: map[string][]string{}, budget: Budget{JSON: 1000, Compact: 1000}}
	neighbors := map[string][]string{}
	for l, d := range links {
		a, b, _ := strings.Cut(l, "-")
		n.links[pair(a, b)] = &testLink{up: true, delay: d}
		neighbors[a] = append(neighbors[a], b)
		neighbors[b] = append(neighbors[b], a)
	}
	for name := range neighbors {
		n.names = append(n.names, name)
	}
	sort.Strings(n.names)
	n.hops = map[string]map[string]string{}
	for k, v := range nextHops {
		from, to, _ := strings.Cut(k, ">")
		if n.hops[from] == nil {
			n.hops[from] = map[string]string{}
		}
		n.hops[from][to] = v
	}
	for _, name := range n.names {
		name := name
		n.r[name] = New(n.config(name, nil), func() time.Time { return n.now }, func(kind, detail string) {
			n.events[name] = append(n.events[name], kind+" "+detail)
		})
	}
	return n
}

func (n *testNet) config(name string, edit func(*Config)) Config {
	c := Config{Self: name, NextHops: n.hops[name], Numbers: map[string]uint32{}}
	for i, o := range n.names {
		c.Numbers[o] = uint32(i + 1)
	}
	for _, o := range n.names {
		if o != name {
			c.Relays = append(c.Relays, o)
		}
		if _, ok := n.links[pair(name, o)]; ok && o != name {
			c.Links = append(c.Links, o)
		}
	}
	if edit != nil {
		edit(&c)
	}
	return signed(c)
}

// probe gives both ends of a link the same probe result; rtt < 0 is lost.
func (n *testNet) probe(a, b string, rttMs float64) {
	for _, x := range [][2]string{{a, b}, {b, a}} {
		if rttMs < 0 {
			n.r[x[0]].Lost(x[1])
		} else {
			n.r[x[0]].Probe(x[1], time.Duration(rttMs*float64(time.Millisecond)))
		}
	}
}

// cut stops a link carrying advertisements; probes are up to the test.
func (n *testNet) cut(a, b string, down bool) { n.links[pair(a, b)].up = !down }

func (n *testNet) send(from string, m Message) {
	if n.onSend != nil {
		n.onSend(from, m)
	}
	l := n.links[pair(from, m.Peer)]
	if l == nil || !l.up {
		return
	}
	n.queue = append(n.queue, delivery{n.now.Add(l.delay), from, m.Peer, m})
}

// run moves the clock forward in steps of 10 ms, ticking every router and
// delivering what is due. check, if set, runs after every step.
func (n *testNet) run(d time.Duration, check func()) {
	end := n.now.Add(d)
	for n.now.Before(end) {
		n.now = n.now.Add(10 * time.Millisecond)
		n.step()
		if check != nil {
			check()
		}
	}
}

func (n *testNet) step() {
	for _, name := range n.names {
		r := n.r[name]
		r.Tick()
		for _, m := range r.Sync(n.budget) {
			n.send(name, m)
		}
		for _, m := range r.Advert(n.budget, r.cfg.Links) {
			n.send(name, m)
		}
	}
	for {
		sort.SliceStable(n.queue, func(i, j int) bool { return n.queue[i].at.Before(n.queue[j].at) })
		if len(n.queue) == 0 || n.queue[0].at.After(n.now) {
			return
		}
		m := n.queue[0]
		n.queue = n.queue[1:]
		fwd, _, err := n.r[m.to].Receive(m.from, m.msg, n.budget)
		if err != nil {
			n.t.Fatalf("%s from %s: %v", m.to, m.from, err)
		}
		for _, f := range fwd {
			n.send(m.to, f)
		}
	}
}

// path follows next hops from src to dst; ok is false on a loop or a
// relay without a route.
func (n *testNet) path(src, dst string) ([]string, bool) {
	out := []string{src}
	seen := map[string]bool{src: true}
	for x := src; x != dst; {
		nh, ok := n.r[x].NextHop(dst)
		if !ok || seen[nh] {
			return append(out, nh), false
		}
		seen[nh] = true
		out = append(out, nh)
		x = nh
	}
	return out, true
}

func (n *testNet) hop(src, dst string) string {
	h, _ := n.r[src].NextHop(dst)
	return h
}

func (n *testNet) eventsOf(name, kind string) []string {
	var out []string
	for _, e := range n.events[name] {
		if strings.HasPrefix(e, kind+" ") {
			out = append(out, strings.TrimPrefix(e, kind+" "))
		}
	}
	return out
}
