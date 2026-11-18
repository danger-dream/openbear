package mesh

import (
	"crypto/rand"
	"fmt"
	"maps"
	"slices"
	"time"

	"openbear/node/mesh/transport"
)

// A UDP link may carry less than the network MTU. Links send without DF
// (transport/socket_linux.go), so a line on the way smaller than the hosts'
// fragments the longest datagrams, and a network that drops fragments past
// it then drops every one of them while shorter ones pass: the link answers
// its probes and routes keep going over it, but nothing of the network MTU
// gets through. Each node therefore probes every link whose active path is
// UDP, straight to the peer whatever the routes say, with a packet of the
// network MTU beside a short one every linkProbeEvery, linkCopies of each
// once a long one went unanswered. A round with a long one answered clears
// the link whatever else was lost; one with nothing answered says nothing
// and is not counted. When linkProbeMisses rounds, linkProbeRetry apart,
// had a short one answered and no long one, with none between that had a
// long one answered, the link fails the network MTU: lost packets, or a
// queue that holds the long ones back, do not make that in so many rounds
// of copies. Routing then adds narrowCost to the
// link's cost, and the link is held to the longest packet of a ladder of
// sizes below the network MTU (linkLadder) the last round had answered: a
// relay answers what it would forward over it past that with "fragmentation
// needed" to its sender (large.go), whose OS then sends less. With no size
// of the ladder answered it is held to nothing, as no limit was seen to
// work, and routes keep off it. Such a link is probed with the whole ladder
// every round, so its limit follows what it carries, and one that carries
// the network MTU again is no longer held. What probing found belongs to
// the link's active path: another path starts afresh. A probe is a
// probeLink datagram padded to the size probed (entryprobe.go); the peer
// answers with a short probeLinkReply over the link it came by. A path that
// has answered nothing yet, just up and maybe not carrying anything yet, is
// probed again after linkProbeRetry for linkProbeMisses rounds; past that a
// peer that answers no short probe, an older release among others, is
// probed less and less often, up to every ten rounds.

const (
	linkProbeEvery  = time.Minute
	linkProbeRetry  = 10 * time.Second
	linkProbeWait   = 3 * time.Second
	linkProbeMisses = 3
	// linkCopies of each size go in a round after a miss, one before.
	linkCopies = 3
	// linkBase is the short probe's size: past smallPacket, so that the
	// scheduler serves it as it does the long ones, and below the ladder.
	linkBase = 512
	// shortHold: what a relay said of a path end to end is kept this long,
	// as the OS keeps what an ICMP says of a path.
	shortHold = 10 * time.Minute
)

// linkSizes is what probing found of one UDP link's path.
type linkSizes struct {
	path   transport.PathRef
	limit  int // the longest packet the link carries, 0 for the network MTU or none seen to work
	misses int // rounds with a short probe answered and no long one, since one with a long one answered
	shrink int // rounds the longest answered probe of a link that fails was below its limit, since one that was not
	silent int // rounds in a row nothing was answered
	heard  bool
	sent   time.Time
	next   time.Time
	ids    map[[8]byte]int // the round's probes not answered yet, and their sizes
	got    map[int]bool    // the sizes the round had answered
}

// failing: the link was seen not to carry the network MTU.
func (s *linkSizes) failing() bool { return s.misses >= linkProbeMisses }

// shortPath is what a relay said the path to a node takes, until when.
type shortPath struct {
	mtu   int
	until time.Time
}

// linkProbe is a probe of size bytes to send over the link to peer.
type linkProbe struct {
	peer string
	id   [8]byte
	size int
}

// linkLadder are the sizes below small a link that fails is probed with:
// multiples of 16, which WireGuard pads nothing more to, every 16 bytes for
// the first 96, as what shrinks a path takes a few dozen bytes, then in
// growing steps down to minOwnMTU.
func linkLadder(small int) []int {
	top := small &^ 15
	if top == small {
		top -= 16
	}
	var out []int
	for _, d := range []int{0, 16, 32, 48, 64, 80, 96, 128, 192, 256, 384, 512, 768} {
		if top-d >= minOwnMTU {
			out = append(out, top-d)
		}
	}
	if len(out) == 0 || out[len(out)-1] > minOwnMTU {
		out = append(out, minOwnMTU)
	}
	return out
}

// probeLinks takes the peers whose links are UDP now, with their paths, and
// returns the probes due, what changed of the links for events, and whether
// a link or a path end to end changed.
func (p *paths) probeLinks(udp map[string]transport.PathRef) (probes []linkProbe, events []string, changed bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	now := p.now()
	for id, s := range p.udp {
		if path, ok := udp[id]; !ok || path != s.path {
			changed = changed || s.failing()
			delete(p.udp, id)
		}
	}
	for id, s := range p.short {
		if !now.Before(s.until) {
			delete(p.short, id)
			changed = true
		}
	}
	for _, id := range slices.Sorted(maps.Keys(udp)) {
		s := p.udp[id]
		if s == nil {
			s = &linkSizes{path: udp[id]}
			p.udp[id] = s
		}
		if !s.sent.IsZero() && (now.Sub(s.sent) >= linkProbeWait || len(s.ids) == 0) {
			if ev, moved := p.judge(id, s, now); moved {
				events, changed = append(events, ev), true
			}
		}
		if s.sent.IsZero() && !now.Before(s.next) {
			probes = append(probes, s.start(id, p.small, now)...)
		}
	}
	if changed {
		p.publish()
		p.due()
	}
	return probes, events, changed
}

// start sends a round: a short probe and the network MTU, and, while the
// network MTU went unanswered, the ladder and copies of each.
func (s *linkSizes) start(peer string, small int, now time.Time) []linkProbe {
	sizes, copies := []int{linkBase, small}, 1
	if s.misses > 0 {
		sizes, copies = append(sizes, linkLadder(small)...), linkCopies
	}
	s.sent, s.ids, s.got = now, map[[8]byte]int{}, map[int]bool{}
	out := make([]linkProbe, 0, copies*len(sizes))
	for range copies {
		for _, n := range sizes {
			var id [8]byte
			rand.Read(id[:])
			s.ids[id] = n
			out = append(out, linkProbe{peer, id, n})
		}
	}
	return out
}

// linkAnswered takes the answer to a probe of the link to peer.
func (p *paths) linkAnswered(peer string, id [8]byte) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if s := p.udp[peer]; s != nil && !s.sent.IsZero() {
		if n, ok := s.ids[id]; ok {
			delete(s.ids, id)
			s.got[n] = true
		}
	}
}

// judge ends s's round. moved says whether the link started or stopped
// failing the network MTU or its limit changed, ev how, for an event.
func (p *paths) judge(peer string, s *linkSizes, now time.Time) (ev string, moved bool) {
	s.sent, s.next = time.Time{}, now.Add(linkProbeEvery)
	s.heard = s.heard || len(s.got) > 0
	was, failing := s.limit, s.failing()
	if s.got[p.small] {
		s.limit, s.misses, s.shrink, s.silent = 0, 0, 0, 0
		if failing {
			return fmt.Sprintf("%s: carries the network's %d bytes again", peer, p.small), true
		}
		return "", false
	}
	if !s.got[linkBase] {
		// Lost, or a peer that does not know the probe: nothing learned.
		s.silent++
		if !s.heard && s.silent <= linkProbeMisses {
			s.next = now.Add(linkProbeRetry)
		} else {
			s.next = now.Add(linkProbeEvery * time.Duration(min(s.silent, 10)))
		}
		return "", false
	}
	s.silent = 0
	if s.misses++; s.misses < linkProbeMisses {
		// Again soon, with the ladder.
		s.next = now.Add(linkProbeRetry)
		return "", false
	}
	best := 0
	for n := range s.got {
		if n != linkBase {
			best = max(best, n)
		}
	}
	if failing && best < was {
		// The limit went unanswered too: a lost probe, until it does again.
		if s.shrink++; s.shrink < linkProbeMisses {
			return "", false
		}
	}
	s.limit, s.shrink = best, 0
	switch {
	case failing && s.limit == was:
		return "", false
	case s.limit == 0:
		return fmt.Sprintf("%s: carries less than %d bytes of the network's %d", peer, minOwnMTU, p.small), true
	}
	return fmt.Sprintf("%s: carries %d bytes of the network's %d", peer, s.limit, p.small), true
}

// narrowed are the peers whose links fail the network MTU.
func (v *pathView) narrowed() map[string]bool { return maps.Clone(v.failing) }

// watchLinks probes the UDP links until the engine closes.
func (e *engine) watchLinks() {
	tick := time.NewTicker(time.Second)
	defer tick.Stop()
	for {
		select {
		case <-e.done:
			return
		case <-tick.C:
		}
		e.checkLinks()
	}
}

func (e *engine) checkLinks() {
	b, ok := e.bind.(*streamBind)
	if !ok {
		return
	}
	p := e.tun.paths
	probes, events, changed := p.probeLinks(b.UDPLinks())
	for _, ev := range events {
		e.events.add("link.mtu", ev)
	}
	r := e.tun.route.Load()
	if len(probes) > 0 && r.nodes != nil {
		keys := map[string][32]byte{}
		for k, id := range r.peerOf {
			keys[id] = k
		}
		for _, pr := range probes {
			z, ok := r.nodes.byID[pr.peer]
			key, known := keys[pr.peer]
			if !ok || !known {
				continue
			}
			e.tun.sched.enqueuePeer(probePacket(r.nodes.self, z.addr, probeLink, pr.id, pr.size), -1, &key)
		}
	}
	if rt := e.router(); rt != nil {
		rt.SetNarrow(p.view.Load().narrowed())
	}
	if changed {
		e.refreshHost()
	}
}
