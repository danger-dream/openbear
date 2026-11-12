package mesh

import (
	"crypto/rand"
	"fmt"
	mrand "math/rand/v2"
	"net/netip"
	"slices"
	"sync"
	"sync/atomic"
	"time"
)

// A member sees no attachments, so unlike a relay (gatewayOf) it cannot
// tell whether the gateway announcing a subnet a backup stands by for can be
// reached. While it sends into such a subnet it asks the gateways: every
// gatewayProbeEvery it probes the one it sends there end to end, and the
// first candidate when that is another (entryprobe.go's probes: every node's
// data plane answers them, older ones too, and only that node's end-to-end
// session can carry the answer). After gatewayFailures unanswered in a row it
// moves the subnet, in its routes and end-to-end table alike, to the first
// other candidate that answered; back to the first candidate once that
// answered gatewayReturns in a row. Nothing moves while no other candidate
// answers, so a member never does worse than keeping to the first. A subnet
// nothing is sent into is not probed and keeps its last choice.

const (
	gatewayProbeEvery = 5 * time.Second
	gatewayFailures   = 3
	gatewayReturns    = 3
	// gatewayInUse: how long after the last packet into a subnet its
	// gateways are still probed.
	gatewayInUse = e2eIdle
)

type standbyGateways struct {
	mu   sync.Mutex
	lans map[netip.Prefix]*standbyLAN
	// uses are the subnets pump marks as sent into, read without mu.
	uses atomic.Pointer[[]standbyUse]
}

type standbyLAN struct {
	candidates []string // the nodes announcing it, then its backups, in config order
	chosen     string
	used       atomic.Bool
	lastUse    time.Time
	probes     map[string]*gatewayProbe // by candidate
}

type gatewayProbe struct {
	id              [8]byte
	waiting         bool
	misses, answers int // in a row
}

type standbyUse struct {
	p    netip.Prefix
	used *atomic.Bool
}

type gatewaySend struct {
	node string
	id   [8]byte
}

// configure takes the subnets backups stand by for from c, a member's, with
// more than one candidate and not this node's own. One whose candidates are
// unchanged keeps its choice.
func (g *standbyGateways) configure(c *Config) {
	g.mu.Lock()
	defer g.mu.Unlock()
	lans := map[netip.Prefix]*standbyLAN{}
	uses := []standbyUse{}
	for i := range c.Nodes {
		if c.isRelay() {
			break
		}
		for _, s := range c.Nodes[i].StandbySubnets {
			if lans[s] != nil || c.Self.Gateway != nil && slices.Contains(c.Self.Gateway.Subnets, s) {
				continue
			}
			primaries, backups := gatewayCandidates(c, s)
			candidates := append(primaries, backups...)
			if len(candidates) < 2 {
				continue
			}
			l := g.lans[s]
			if l == nil || !slices.Equal(l.candidates, candidates) {
				l = &standbyLAN{candidates: candidates, chosen: candidates[0], probes: map[string]*gatewayProbe{}}
			}
			lans[s] = l
			uses = append(uses, standbyUse{s, &l.used})
		}
	}
	g.lans = lans
	g.uses.Store(&uses)
}

// any: some subnet has gateways to choose between.
func (g *standbyGateways) any() bool {
	if g == nil {
		return false
	}
	u := g.uses.Load()
	return u != nil && len(*u) > 0
}

// chosen is the node s goes to, "" for a subnet with nothing to choose.
func (g *standbyGateways) chosen(s netip.Prefix) string {
	if g == nil {
		return ""
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	if l := g.lans[s]; l != nil {
		return l.chosen
	}
	return ""
}

// use marks the subnet dst is in as sent into; pump calls it for each packet.
func (g *standbyGateways) use(dst netip.Addr) {
	u := g.uses.Load()
	if u == nil {
		return
	}
	for _, x := range *u {
		if x.p.Contains(dst) {
			if !x.used.Load() {
				x.used.Store(true)
			}
			return
		}
	}
}

// answered takes node from's answer to probe id.
func (g *standbyGateways) answered(from string, id [8]byte) {
	g.mu.Lock()
	defer g.mu.Unlock()
	for _, l := range g.lans {
		if p := l.probes[from]; p != nil && p.waiting && p.id == id {
			p.waiting, p.misses = false, 0
			p.answers++
		}
	}
}

// tick counts the probes still unanswered as missed, moves the subnets
// whose choice that changes, and gives the probes to send now and what
// moved, as "<subnet>: <from> -> <to> (unanswered|returned)".
func (g *standbyGateways) tick(now time.Time) (sends []gatewaySend, moved []string) {
	g.mu.Lock()
	defer g.mu.Unlock()
	for s, l := range g.lans {
		if l.used.Swap(false) {
			l.lastUse = now
		}
		if now.Sub(l.lastUse) >= gatewayInUse {
			clear(l.probes)
			continue
		}
		probe := func(id string) *gatewayProbe {
			p := l.probes[id]
			if p == nil {
				p = &gatewayProbe{}
				l.probes[id] = p
			}
			return p
		}
		for _, p := range l.probes {
			if p.waiting {
				p.waiting, p.answers = false, 0
				p.misses++
			}
		}
		first, cur := l.candidates[0], probe(l.chosen)
		switch {
		case l.chosen != first && probe(first).answers >= gatewayReturns:
			moved = append(moved, fmt.Sprintf("%s: %s -> %s (returned)", s, l.chosen, first))
			l.chosen = first
		case cur.misses >= gatewayFailures:
			for _, id := range l.candidates {
				if id != l.chosen && probe(id).answers > 0 {
					moved = append(moved, fmt.Sprintf("%s: %s -> %s (unanswered)", s, l.chosen, id))
					l.chosen = id
					break
				}
			}
		}
		// The others are asked too once the chosen one misses, so that one
		// that answers is known by the time it is given up.
		asking := probe(l.chosen).misses > 0
		for _, id := range l.candidates {
			if id == l.chosen || id == first || asking {
				p := probe(id)
				rand.Read(p.id[:])
				p.waiting = true
				sends = append(sends, gatewaySend{id, p.id})
			}
		}
	}
	return sends, moved
}

// watchGateways runs a member's probes of its gateways until the engine
// closes.
func (e *engine) watchGateways() {
	for {
		select {
		case <-e.done:
			return
		case <-time.After(gatewayProbeEvery + time.Duration(mrand.Int64N(int64(gatewayProbeEvery/5)))):
		}
		e.checkGateways(time.Now())
	}
}

func (e *engine) checkGateways(now time.Time) {
	sends, moved := e.gateways.tick(now)
	r := e.tun.route.Load()
	for _, s := range sends {
		if z := r.nodes.byID[s.node]; z != nil {
			e.e2e.sendControl(probePacket(r.nodes.self, z.addr, probeRequest, s.id, probeMin), z)
		}
	}
	if len(moved) == 0 {
		return
	}
	for _, m := range moved {
		e.events.add("gateway.standby", m)
	}
	if err := e.reroute(); err != nil {
		e.events.add("route.error", err.Error())
	}
}
