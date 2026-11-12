package mesh

import (
	"fmt"
	"net/netip"
	"slices"
)

// peerSpec is one WireGuard peer as the config and attachment ask for it.
type peerSpec struct {
	ID        string
	Kind      string // entry, relay, direct or member
	Key       string // hex public key
	Endpoint  string // configured endpoint; empty for a peer that dials in
	Pins      []string
	Allowed   []netip.Prefix
	Candidate string // entry only
	Keepalive int    // seconds; 0 is off
}

func (p peerSpec) keepalive() int { return p.Keepalive }

// Keepalive intervals. The dialing side of UDP keeps NAT state open. A
// stream accepted by the transport is closed unless WireGuard sends a
// handshake response or data through it within 10 s, so both ends of a
// stream send at least every 5 s: the dialing side always, the accepting
// side whenever it listens for streams, as it cannot tell which peers use
// them.
const (
	keepaliveUDP    = 25
	keepaliveStream = 5
)

func keepaliveFor(endpoint string, c *Config) int {
	switch {
	case endpoint == "":
		if c.Self.Listen.TCP != 0 || c.Self.Listen.TLS != 0 {
			return keepaliveStream
		}
		return 0
	case transportOf(endpoint) == "udp":
		return keepaliveUDP
	default:
		return keepaliveStream
	}
}

func (p peerSpec) equal(o peerSpec) bool {
	return p.ID == o.ID && p.Kind == o.Kind && p.Key == o.Key && p.Endpoint == o.Endpoint && p.Candidate == o.Candidate && p.Keepalive == o.Keepalive &&
		slices.Equal(p.Pins, o.Pins) && slices.Equal(p.Allowed, o.Allowed)
}

// relayRoutes is an attachment whose relay router chooses next hops;
// active is false while it has no router.
type relayRoutes interface {
	RelayNextHop(dest string) (hop string, ok, active bool)
}

// localPrefixes are the destinations this node delivers to its own OS.
func localPrefixes(c *Config) []netip.Prefix {
	out := []netip.Prefix{host(c.Self.Address)}
	if c.Self.Gateway != nil {
		out = append(out, c.Self.Gateway.Subnets...)
	}
	return out
}

// nodePrefixes is what a relay routes to a node: its address and the
// subnets it carries now.
func nodePrefixes(c *Config, a Attachment, n *Node) []netip.Prefix {
	return append([]netip.Prefix{host(n.Address)}, subnetsOf(c, a, n)...)
}

// subnetsOf is the gateway subnets a node carries now: those it announces,
// and those it stands by for, each only while it is the node gatewayOf
// names for it. Every node, the end-to-end table included, sees one node
// carrying a subnet a backup stands by for.
func subnetsOf(c *Config, a Attachment, n *Node) []netip.Prefix {
	if len(n.StandbySubnets) == 0 && !c.hasStandby() {
		return n.Subnets
	}
	var out []netip.Prefix
	for _, s := range n.Subnets {
		if !c.standingBy(s) || gatewayOf(c, a, s) == n.ID {
			out = append(out, s)
		}
	}
	for _, s := range n.StandbySubnets {
		if gatewayOf(c, a, s) == n.ID {
			out = append(out, s)
		}
	}
	return out
}

// hasStandby: a backup gateway, this node included, stands by for a subnet.
func (c *Config) hasStandby() bool {
	if c.Self.Gateway != nil && c.Self.Gateway.Backup {
		return true
	}
	return slices.ContainsFunc(c.Nodes, func(n Node) bool { return len(n.StandbySubnets) > 0 })
}

// standingBy: a backup gateway, this node included, stands by for s.
func (c *Config) standingBy(s netip.Prefix) bool {
	if c.Self.Gateway != nil && c.Self.Gateway.Backup && slices.Contains(c.Self.Gateway.Subnets, s) {
		return true
	}
	return slices.ContainsFunc(c.Nodes, func(n Node) bool { return slices.Contains(n.StandbySubnets, s) })
}

// gatewayCandidates are the other nodes that announce s and those that
// stand by for it, in config order.
func gatewayCandidates(c *Config, s netip.Prefix) (primaries, backups []string) {
	for i := range c.Nodes {
		if slices.Contains(c.Nodes[i].Subnets, s) {
			primaries = append(primaries, c.Nodes[i].ID)
		}
		if slices.Contains(c.Nodes[i].StandbySubnets, s) {
			backups = append(backups, c.Nodes[i].ID)
		}
	}
	return primaries, backups
}

// gatewayOf is the node that carries s now, this one's ID for itself. A
// relay sees attachments: the first node announcing s it can reach, else
// itself when it stands by for s, else the first backup it can reach. A
// member cannot: it goes by what its probes of the gateways found
// (standby.go). A leaf member goes by its probes of s too while the node
// they chose is one it routes to, else as a relay does, with only those
// members its probes of s chose. Either way it is the first candidate while
// nothing better is known.
func gatewayOf(c *Config, a Attachment, s netip.Prefix) string {
	if g := c.Self.Gateway; g != nil && !g.Backup && slices.Contains(g.Subnets, s) {
		return c.Self.ID
	}
	primaries, backups := gatewayCandidates(c, s)
	if c.isRouted() {
		if !c.isRelay() {
			if id := c.standby.chosen(s); id != "" && reachable(c, a, s, c.node(id)) {
				return id
			}
		}
		for _, id := range primaries {
			if reachable(c, a, s, c.node(id)) {
				return id
			}
		}
		if g := c.Self.Gateway; g != nil && slices.Contains(g.Subnets, s) {
			return c.Self.ID
		}
		for _, id := range backups {
			if reachable(c, a, s, c.node(id)) {
				return id
			}
		}
	} else if id := c.standby.chosen(s); id != "" && (slices.Contains(primaries, id) || slices.Contains(backups, id)) {
		return id
	}
	if len(primaries) > 0 {
		return primaries[0]
	}
	if len(backups) > 0 {
		return backups[0]
	}
	return ""
}

// reachable, for a relay or routed member, as a gateway of s: n is a node it
// has a next hop to, or an unrouted member attached to some reachable relay;
// for a leaf member, which sees no attachments, one its probes of s chose, or
// any while it has none.
func reachable(c *Config, a Attachment, s netip.Prefix, n *Node) bool {
	if n == nil {
		return false
	}
	if n.Role == roleRelay || n.Leaf {
		if routes, dynamic := a.(relayRoutes); dynamic {
			if _, ok, active := routes.RelayNextHop(n.ID); active {
				return ok
			}
		}
		if c.isRelay() {
			_, ok := c.NextHops[n.ID]
			return ok
		}
		return false
	}
	if c.isRelay() {
		_, ok := a.Owner(n.ID)
		return ok
	}
	id := c.standby.chosen(s)
	return id == "" || id == n.ID
}

// intersect is every overlap of a prefix in a with one in b, the narrower
// of the two.
func intersect(a, b []netip.Prefix) []netip.Prefix {
	var out []netip.Prefix
	for _, x := range a {
		for _, y := range b {
			if x.Overlaps(y) {
				if x.Bits() >= y.Bits() {
					out = append(out, x)
				} else {
					out = append(out, y)
				}
			}
		}
	}
	return out
}

// gatewaySubnets is what of subnets this node routes: all of them without
// self.gatewayRoutes, else their overlap with it.
func gatewaySubnets(c *Config, subnets []netip.Prefix) []netip.Prefix {
	if c.Self.GatewayRoutes == nil {
		return subnets
	}
	return intersect(subnets, *c.Self.GatewayRoutes)
}

// memberPrefixes is a member's view of another node, and the end-to-end
// one: its address and the part of the subnets it carries now that this
// node routes.
func memberPrefixes(c *Config, a Attachment, n *Node) []netip.Prefix {
	return append([]netip.Prefix{host(n.Address)}, gatewaySubnets(c, subnetsOf(c, a, n))...)
}

// planPeers computes every peer and its AllowedIPs (config section 3).
// Each destination has exactly one next hop, so the sets never overlap
// except where a longer prefix is meant to win.
func planPeers(c *Config, a Attachment) ([]peerSpec, error) {
	var out []peerSpec
	add := func(n *Node, kind, endpoint string, allowed []netip.Prefix) error {
		k, err := keyHex(n.PublicKey)
		if err != nil {
			return fmt.Errorf("node %s: %w", n.ID, err)
		}
		out = append(out, peerSpec{ID: n.ID, Kind: kind, Key: k, Endpoint: endpoint, Allowed: sortPrefixes(allowed), Keepalive: keepaliveFor(endpoint, c)})
		return nil
	}
	if !c.isRelay() {
		if c.isRouted() {
			via := map[string][]netip.Prefix{}
			routes, dynamic := a.(relayRoutes)
			upstreams := []string{}
			for _, l := range c.Links {
				if l.Kind == kindRelay {
					upstreams = append(upstreams, l.Peer)
				}
			}
			route := func(dest string, p ...netip.Prefix) {
				if dynamic {
					if hop, ok, active := routes.RelayNextHop(dest); active {
						if ok {
							via[hop] = append(via[hop], p...)
						}
						return
					}
				}
				if len(upstreams) > 0 {
					via[upstreams[0]] = append(via[upstreams[0]], p...)
				}
			}
			direct := map[string]bool{}
			for _, l := range c.Links {
				if l.Kind != kindDirect {
					continue
				}
				if !l.Punch {
					direct[l.Peer] = true
				} else if d, ok := a.(directRoutes); ok && d.Direct(l.Peer) {
					direct[l.Peer] = true
				}
			}
			for i := range c.Nodes {
				n := &c.Nodes[i]
				if direct[n.ID] {
					via[n.ID] = append(via[n.ID], memberPrefixes(c, a, n)...)
					continue
				}
				if n.Role == roleRelay || n.Leaf {
					route(n.ID, memberPrefixes(c, a, n)...)
					continue
				}
				// Unrouted member: route via the first reachable upstream relay.
				placed := false
				if dynamic {
					for _, u := range upstreams {
						if _, ok, active := routes.RelayNextHop(u); active && ok {
							via[u] = append(via[u], memberPrefixes(c, a, n)...)
							placed = true
							break
						}
					}
				}
				if !placed && len(upstreams) > 0 {
					via[upstreams[0]] = append(via[upstreams[0]], memberPrefixes(c, a, n)...)
				}
			}
			for _, l := range c.Links {
				n := c.node(l.Peer)
				endpoint, allowed := l.Endpoint, via[l.Peer]
				if l.Punch {
					endpoint = ""
					if d, ok := a.(directRoutes); !ok || !d.Direct(l.Peer) {
						allowed = nil
					}
				}
				if err := add(n, l.Kind, endpoint, allowed); err != nil {
					return nil, err
				}
				out[len(out)-1].Pins = slices.Clone(l.TLSPins)
			}
			return out, nil
		}
		direct := map[string]bool{}
		for _, l := range c.Links {
			n := c.node(l.Peer)
			if l.Punch {
				// Until the punched path is up on both members the peer is
				// reached through the entry, which also has to be where its
				// packets come from for WireGuard to accept them.
				var allowed []netip.Prefix
				if d, ok := a.(directRoutes); ok && d.Direct(n.ID) {
					allowed = memberPrefixes(c, a, n)
					direct[n.ID] = true
				}
				if err := add(n, kindDirect, "", allowed); err != nil {
					return nil, err
				}
				continue
			}
			direct[n.ID] = true
			if err := add(n, kindDirect, l.Endpoint, memberPrefixes(c, a, n)); err != nil {
				return nil, err
			}
			out[len(out)-1].Pins = slices.Clone(l.TLSPins)
		}
		if c.Entry == nil {
			return out, nil
		}
		id, ok := a.ActiveEntry()
		if !ok {
			return out, nil
		}
		i := slices.IndexFunc(c.Entry.Candidates, func(x Candidate) bool { return x.ID == id })
		if i < 0 {
			return nil, fmt.Errorf("active entry %q is not a candidate", id)
		}
		cand := c.Entry.Candidates[i]
		// The entry carries everything, other relays' own addresses included:
		// a narrower route to a standby relay would fail WireGuard's source
		// check for its traffic arriving through the entry.
		allowed := []netip.Prefix{c.Network.Prefix}
		if gw := c.Self.GatewayRoutes; gw != nil {
			// The gateway routes, but not those a direct peer carries all of.
			var carried []netip.Prefix
			for i := range c.Nodes {
				if direct[c.Nodes[i].ID] {
					carried = append(carried, subnetsOf(c, a, &c.Nodes[i])...)
				}
			}
			if c.Self.Gateway != nil {
				carried = append(carried, c.Self.Gateway.Subnets...)
			}
			for _, g := range *gw {
				if !slices.ContainsFunc(carried, func(s netip.Prefix) bool { return s.Overlaps(g) && s.Bits() <= g.Bits() }) {
					allowed = append(allowed, g)
				}
			}
		} else {
			for i := range c.Nodes {
				if !direct[c.Nodes[i].ID] {
					allowed = append(allowed, subnetsOf(c, a, &c.Nodes[i])...)
				}
			}
		}
		if err := add(c.node(cand.Relay), kindEntry, cand.Endpoint, allowed); err != nil {
			return nil, err
		}
		out[len(out)-1].Pins = slices.Clone(cand.TLSPins)
		out[len(out)-1].Candidate = cand.ID
		return out, nil
	}

	via := map[string][]netip.Prefix{} // linked relay -> destinations
	routes, dynamic := a.(relayRoutes)
	route := func(relay string, p ...netip.Prefix) {
		if dynamic {
			// The router starts from and falls back to nextHops itself.
			if hop, ok, active := routes.RelayNextHop(relay); active {
				if ok {
					via[hop] = append(via[hop], p...)
				}
				return
			}
		}
		if hop, ok := c.NextHops[relay]; ok {
			via[hop] = append(via[hop], p...)
		}
	}
	linkedMembers := map[string]bool{}
	for _, l := range c.Links {
		if l.Kind == kindMember {
			linkedMembers[l.Peer] = true
		}
	}
	for i := range c.Nodes {
		n := &c.Nodes[i]
		if n.Role == roleRelay {
			route(n.ID, nodePrefixes(c, a, n)...)
			continue
		}
		if n.Leaf || linkedMembers[n.ID] {
			if dynamic {
				if hop, ok, active := routes.RelayNextHop(n.ID); active && ok {
					via[hop] = append(via[hop], nodePrefixes(c, a, n)...)
					continue
				}
			}
			if owner, ok := a.Owner(n.ID); ok {
				if owner == c.Self.ID {
					if linkedMembers[n.ID] {
						via[n.ID] = append(via[n.ID], nodePrefixes(c, a, n)...)
					}
				} else {
					route(owner, nodePrefixes(c, a, n)...)
				}
				continue
			}
			if hop, ok := c.NextHops[n.ID]; ok {
				via[hop] = append(via[hop], nodePrefixes(c, a, n)...)
			}
			continue
		}
		owner, ok := a.Owner(n.ID)
		if !ok || owner == c.Self.ID {
			continue
		}
		route(owner, nodePrefixes(c, a, n)...)
	}
	for _, l := range c.Links {
		endpoint, allowed := l.Endpoint, via[l.Peer]
		if l.Punch {
			// The punched path is the endpoint; until it is up on both
			// relays nothing is routed over the link.
			endpoint = ""
			if d, ok := a.(directRoutes); !ok || !d.Direct(l.Peer) {
				allowed = nil
			}
		}
		if err := add(c.node(l.Peer), l.Kind, endpoint, allowed); err != nil {
			return nil, err
		}
		out[len(out)-1].Pins = slices.Clone(l.TLSPins)
	}
	for i := range c.Nodes {
		n := &c.Nodes[i]
		if n.Role != roleMember || linkedMembers[n.ID] || !slices.Contains(n.Relays, c.Self.ID) {
			continue
		}
		// Every member allowed to attach here keeps its peer, so its session
		// survives moving away and back; only the owner relay routes to it.
		var allowed []netip.Prefix
		if owner, ok := a.Owner(n.ID); ok && owner == c.Self.ID {
			allowed = nodePrefixes(c, a, n)
		}
		if err := add(n, kindMember, "", allowed); err != nil {
			return nil, err
		}
	}
	return out, nil
}

// hostRoutes are the OS routes into the mesh interface: the mesh prefix
// and every gateway subnet some peer carries, as far as self.gatewayRoutes
// allows when present. A relay's peers carry every subnet it forwards;
// only its own traffic is limited.
func hostRoutes(c *Config, peers []peerSpec) []netip.Prefix {
	out := []netip.Prefix{c.Network.Prefix}
	var subnets []netip.Prefix
	for _, p := range peers {
		for _, a := range p.Allowed {
			if !c.Network.Prefix.Contains(a.Addr()) {
				subnets = append(subnets, a)
			}
		}
	}
	return sortPrefixes(append(out, gatewaySubnets(c, subnets)...))
}
