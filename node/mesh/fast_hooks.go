package mesh

import (
	"net/netip"
	"slices"
	"strings"

	meshfast "openbear/node/mesh/fast"
)

func (e *engine) fastHooks() meshfast.Hooks {
	return meshfast.Hooks{
		Dir:           e.dir,
		Paths:         e.fastPaths,
		Route:         e.fastRoute,
		LinkUp:        e.fastLinkUp,
		SourceAllowed: e.fastSourceAllowed,
		LocalSubnets:  e.fastLocalSubnets,
		Member:        e.fastMember,
	}
}

// attachFast gives the fast relay e's routing and e's data plane the fast
// relay's look at what this node sends into the mesh.
func (r *runner) attachFast(e *engine) {
	h := e.fastHooks()
	h.ToOS = e.tun.toOS
	h.ToMesh = e.tun.fastToMesh
	h.MeshMTU = func() uint32 { return uint32(max(576, int(e.tun.mtu.Load())-e2eOverhead)) }
	h.LocalTCP = r.forward.ServeFast
	r.fast.SetHooks(h)
	tap := r.fast.Outbound
	e.tun.fast.Store(&tap)
	reply := r.fast.Inbound
	e.tun.fastReply.Store(&reply)
}

func (e *engine) currentConfig() *Config {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.cfg
}

// fastPaths are up to k ways to dest the way the mesh goes there: the fixed
// paths from this node by rank, then the routes. A relay has the routes
// itself, the route in use first and then the next best; a member goes
// through its entry relays, the one in use first, each planning on over its
// first, second, ... path.
func (e *engine) fastPaths(dest string, k int, allow func(string) bool) []meshfast.Plan {
	c := e.currentConfig()
	if c == nil || dest == "" || dest == c.Self.ID || !allow(dest) {
		return nil
	}
	var out []meshfast.Plan
	seen := map[string]bool{}
	add := func(p meshfast.Plan) {
		key := strings.Join(p.Hops, ">")
		if p.Planned {
			key += "|planned"
		} else {
			key += "|" + string(rune('0'+p.Alt))
		}
		if len(out) < k && len(p.Hops) > 0 && !seen[key] && !slices.ContainsFunc(p.Hops, func(n string) bool { return !allow(n) }) {
			seen[key] = true
			out = append(out, p)
		}
	}
	for _, f := range e.pins.sourcePaths(dest) {
		add(meshfast.Plan{Hops: f.hops, Planned: true, Fixed: f.id})
	}
	self := map[string]bool{c.Self.ID: true}
	if c.isRelay() {
		for _, p := range e.relayFastPaths(c, dest, k, allow, self) {
			add(meshfast.Plan{Hops: p, Planned: true})
		}
	} else {
		relays := e.entryRelays(c)
		for alt := 0; len(out) < k && alt < k; alt++ {
			for _, rel := range relays {
				if rel == dest {
					add(meshfast.Plan{Hops: []string{dest}, Planned: true})
				} else {
					add(meshfast.Plan{Hops: []string{rel}, Alt: byte(alt)})
				}
			}
		}
	}
	if len(out) == 0 {
		if path, planned, ok := e.fastRoute(dest, 0, allow, self); ok {
			add(meshfast.Plan{Hops: path, Planned: planned})
		}
	}
	return out
}

// relayFastPaths are a relay's k paths to dest from its routes; a member not
// in them (attached to a relay rather than linked) through the relay it is
// attached to.
func (e *engine) relayFastPaths(c *Config, dest string, k int, allow func(string) bool, avoid map[string]bool) [][]string {
	r := e.router()
	if r == nil {
		return nil
	}
	if paths := r.FastPaths(dest, k, allow, avoid); len(paths) > 0 {
		return paths
	}
	if nd := c.node(dest); nd != nil && nd.Role == roleMember && !nd.Leaf {
		owner, ok := e.attach.Owner(dest)
		switch {
		case !ok || avoid[owner] || !allow(owner):
		case owner == c.Self.ID:
			return [][]string{{dest}}
		default:
			var out [][]string
			for _, p := range r.FastPaths(owner, k, allow, avoid) {
				out = append(out, append(p, dest))
			}
			return out
		}
	}
	return nil
}

// entryRelays are a member's entry relays, the one in use first.
func (e *engine) entryRelays(c *Config) []string {
	if c.Entry == nil {
		return nil
	}
	var out []string
	if cid, ok := e.attach.ActiveEntry(); ok {
		for _, cand := range c.Entry.Candidates {
			if cand.ID == cid {
				out = append(out, cand.Relay)
			}
		}
	}
	for _, cand := range c.Entry.Candidates {
		if !slices.Contains(out, cand.Relay) {
			out = append(out, cand.Relay)
		}
	}
	return out
}

// fastRoute is the path after this node to dest for a stream planned no
// further: a relay's alt-th path (the last it has when it has fewer), a
// member's entry relay in use, which plans on.
func (e *engine) fastRoute(dest string, alt int, allow func(string) bool, avoid map[string]bool) ([]string, bool, bool) {
	c := e.currentConfig()
	if c == nil || dest == "" || dest == c.Self.ID || avoid[dest] || !allow(dest) {
		return nil, false, false
	}
	if c.isRelay() {
		if paths := e.relayFastPaths(c, dest, alt+1, allow, avoid); len(paths) > 0 {
			return paths[min(alt, len(paths)-1)], true, true
		}
		if hop := c.NextHops[dest]; hop != "" && allow(hop) && !avoid[hop] {
			return []string{hop}, false, true
		}
		return []string{dest}, true, true
	}
	for _, rel := range e.entryRelays(c) {
		if rel == dest {
			return []string{dest}, true, true
		}
		if allow(rel) && !avoid[rel] {
			return []string{rel}, false, true
		}
	}
	return []string{dest}, true, true
}

func (e *engine) fastLinkUp(peer string) bool {
	if r := e.router(); r != nil && r.Linked(peer) {
		return true
	}
	c := e.currentConfig()
	if c == nil {
		return false
	}
	if c.isRelay() {
		if owner, ok := e.attach.Owner(peer); ok && owner == c.Self.ID {
			return true
		}
	}
	for _, l := range c.Links {
		if l.Peer == peer {
			if l.Punch {
				if d, ok := e.attach.(directRoutes); ok {
					return d.Direct(peer)
				}
				return false
			}
			return true
		}
	}
	if c.Entry != nil {
		for _, cand := range c.Entry.Candidates {
			if cand.Relay == peer {
				return true
			}
		}
	}
	return false
}

func (e *engine) fastSourceAllowed(originID string, source netip.Addr) bool {
	c := e.currentConfig()
	if c == nil {
		return false
	}
	nd := c.node(originID)
	if nd == nil {
		return false
	}
	for _, p := range nd.Subnets {
		if p.Contains(source) {
			return true
		}
	}
	for _, p := range nd.StandbySubnets {
		if p.Contains(source) {
			return true
		}
	}
	return false
}

func (e *engine) fastMember(a netip.Addr) string {
	c := e.currentConfig()
	if c == nil {
		return ""
	}
	if c.Self.Address == a {
		return c.Self.ID
	}
	for _, n := range c.Nodes {
		if n.Address == a {
			return n.ID
		}
	}
	return ""
}

func (e *engine) fastLocalSubnets() []netip.Prefix {
	c := e.currentConfig()
	if c == nil || c.Self.Gateway == nil || c.Self.Gateway.SNAT {
		return nil
	}
	return slices.Clone(c.Self.Gateway.Subnets)
}

func fastRules(rules []Rule) []meshfast.Rule {
	if len(rules) == 0 {
		return nil
	}
	out := make([]meshfast.Rule, len(rules))
	for i, r := range rules {
		out[i] = meshfast.Rule{
			Src:   slices.Clone(r.Src),
			Dst:   slices.Clone(r.Dst),
			Proto: r.Proto,
			Ports: slices.Clone(r.Ports),
		}
	}
	return out
}
