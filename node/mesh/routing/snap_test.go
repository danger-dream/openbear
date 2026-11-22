package routing

import (
	"maps"
	"math/rand/v2"
	"slices"
)

// setRandSeed replaces the router's random source with a deterministic seed (tests).
func (r *Router) setRandSeed(s1, s2 uint64) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.rng = rand.New(rand.NewPCG(s1, s2))
}

// advertSnap / receiveSnap are the replay's fast paths: the timing, sequence,
// rate limits, graph updates and route selection of Advert and Receive,
// without encoding, signing and decoding every advertisement across 25 nodes.
func (r *Router) advertSnap(peers []string) (*advert, []string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.cfg.Leaf {
		return nil, nil
	}
	now := r.now()
	since := now.Sub(r.sent)
	urgentReady := r.dueUrgent && since >= advertGap
	costReady := r.dueCost && since >= costAdvertGap
	if !r.sent.IsZero() && !urgentReady && !costReady && since < advertEvery {
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
	r.indexAdvertRoutes(a)
	r.sent, r.dueUrgent, r.dueCost = now, false, false
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

func (r *Router) syncSnap() ([]*advert, []string) {
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

func (r *Router) receiveSnap(from string, src *advert, deferCostRecompute bool) ([]string, bool) {
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
	if len(r.topoNodes) > 0 && (len(cp.linksByID) != len(r.topoNodes) || (cp.routes != nil && len(cp.routesByID) != len(r.topoNodes))) {
		r.indexAdvertRoutes(&cp)
	}
	if linksChanged {
		r.stale = true
	}
	r.lsdb[cp.origin] = &cp
	if r.graph != nil {
		if oi, ok := r.topoIndex[cp.origin]; ok && int(oi) < len(r.graph.lsdbByID) {
			r.graph.lsdbByID[oi] = &cp
		}
		r.graph.forget()
	}
	if deferCostRecompute && !stateChanged && !routesChanged {
		return peers, false
	}
	return peers, r.recompute(now)
}

func (r *Router) recomputeNow() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.recompute(r.now())
}
