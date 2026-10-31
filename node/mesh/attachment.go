package mesh

import (
	"crypto/ed25519"
	"encoding/binary"
	"maps"
	"net/netip"
	"os"
	"path/filepath"
	"slices"
	"sync/atomic"
	"time"

	"openbear/node/mesh/health"
	meshrouting "openbear/node/mesh/routing"
	"openbear/node/mesh/sign"
)

type healthAttachment struct {
	cfg     atomic.Pointer[Config]
	manager atomic.Pointer[health.Manager]
	router  atomic.Pointer[meshrouting.Router] // relays only
	changes chan struct{}
}

func newHealthAttachment(c *Config) *healthAttachment {
	a := &healthAttachment{changes: make(chan struct{}, 1)}
	a.cfg.Store(c)
	return a
}
func (a *healthAttachment) SetConfig(c *Config) { a.cfg.Store(c) }
func (a *healthAttachment) ActiveEntry() (string, bool) {
	m := a.manager.Load()
	if m == nil {
		return "", false
	}
	id, ok := m.ActiveEntry()
	c := a.cfg.Load()
	if c.Entry == nil || !slices.ContainsFunc(c.Entry.Candidates, func(p Candidate) bool { return p.ID == id }) {
		return "", false
	}
	return id, ok
}
func (a *healthAttachment) Owner(member string) (string, bool) {
	m := a.manager.Load()
	if m == nil {
		return "", false
	}
	r, ok := m.Owner(member)
	n := a.cfg.Load().node(member)
	if n == nil || !slices.Contains(n.Relays, r) {
		return "", false
	}
	return r, ok
}
func (a *healthAttachment) Changes() <-chan struct{} { return a.changes }

// RelayNextHop is the linked relay that traffic for the relay dest goes
// through, as the router chooses it.
func (a *healthAttachment) RelayNextHop(dest string) (string, bool, bool) {
	r := a.router.Load()
	if r == nil {
		return "", false, false
	}
	hop, ok := r.NextHop(dest)
	return hop, ok, true
}

// routerConfig is what the relay router needs of c.
func routerConfig(c *Config) meshrouting.Config {
	rc := meshrouting.Config{Self: c.Self.ID, NextHops: maps.Clone(c.NextHops), Numbers: map[string]uint32{}}
	// Compact advertisements name relays by their control address's host
	// number, the same on every relay of a config.
	base := c.Network.ControlPrefix.Addr().As4()
	number := func(a netip.Addr) uint32 {
		v := a.As4()
		return binary.BigEndian.Uint32(v[:]) - binary.BigEndian.Uint32(base[:])
	}
	rc.Numbers[c.Self.ID] = number(c.Self.ControlAddress)
	for _, n := range c.Nodes {
		if n.Role == roleRelay && n.ID != c.Self.ID {
			rc.Relays = append(rc.Relays, n.ID)
			rc.Numbers[n.ID] = number(n.ControlAddress)
		}
	}
	for _, l := range c.Links {
		if c.roleOf(l.Peer) == roleRelay {
			rc.Links = append(rc.Links, l.Peer)
		}
	}
	if r := c.Routing; r != nil {
		rc.Mode = r.Mode
		rc.Bias = maps.Clone(r.Bias)
	}
	rc.Network, rc.RelayLinks = c.Network.ID, slices.Clone(c.RelayLinks)
	rc.Keys = map[string]ed25519.PublicKey{}
	for _, n := range c.Nodes {
		if key, err := sign.ParsePublic(n.SignKey); err == nil && n.Role == roleRelay {
			rc.Keys[n.ID] = key
		}
	}
	if c.keys != nil {
		rc.Key = c.keys.sign
		rc.Keys[c.Self.ID] = c.keys.sign.Public().(ed25519.PublicKey)
	}
	return rc
}

func (e *engine) router() *meshrouting.Router {
	if a, ok := e.attach.(*healthAttachment); ok {
		return a.router.Load()
	}
	return nil
}

// setRouter makes r, nil for none, the router of this node's attachment
// and of the running health manager, and returns it.
func (e *engine) setRouter(r *meshrouting.Router) *meshrouting.Router {
	a, ok := e.attach.(*healthAttachment)
	if !ok {
		return nil
	}
	a.router.Store(r)
	if e.health != nil {
		e.health.SetRouter(r)
	}
	return r
}
func (a *healthAttachment) Direct(peer string) bool {
	m := a.manager.Load()
	return m != nil && m.Direct(peer)
}

func healthConfig(c *Config) health.Config {
	h := health.Config{Network: c.Network.ID, Self: c.Self.ID, Role: c.Self.Role, Revision: c.Revision, Address: c.Self.ControlAddress, MTU: c.Network.MTU, Parameters: health.Defaults(),
		Prefix: c.Network.Prefix, Port: c.Self.Listen.UDP, Reflectors: slices.Clone(c.Reflectors), NextHops: maps.Clone(c.NextHops), Protocol: protocol}
	if c.Reachability != nil {
		h.STUN = slices.Clone(c.Reachability.STUN)
	}
	for _, n := range c.Nodes {
		key, _ := sign.ParsePublic(n.SignKey)
		h.Nodes = append(h.Nodes, health.Node{ID: n.ID, Role: n.Role, PublicKey: n.PublicKey, ControlKey: n.ControlKey, Address: n.ControlAddress, Relays: slices.Clone(n.Relays), SignKey: key})
	}
	if c.keys != nil {
		h.SignKey = c.keys.sign
	}
	for _, l := range c.Links {
		h.Links = append(h.Links, health.Link{Peer: l.Peer, Endpoint: l.Endpoint, Dial: l.Dial, Pins: slices.Clone(l.TLSPins), Punch: l.Punch})
	}
	if c.Entry != nil {
		h.Mode = c.Entry.Mode
		h.AutoReturn = c.Entry.AutoReturn
		for _, p := range c.Entry.Candidates {
			h.Candidates = append(h.Candidates, health.Candidate{ID: p.ID, Peer: p.Relay, Endpoint: p.Endpoint, Pins: slices.Clone(p.TLSPins), Punch: p.Punch})
		}
	}
	if p := c.Health; p != nil {
		durations := []struct {
			value  int
			target *time.Duration
		}{{p.ActiveIntervalMs, &h.Parameters.ActiveInterval}, {p.StandbyIntervalMs, &h.Parameters.StandbyInterval}, {p.ColdRetryMs, &h.Parameters.ColdRetry}, {p.InitialTimeoutMs, &h.Parameters.InitialTimeout}, {p.MinTimeoutMs, &h.Parameters.MinTimeout}, {p.MaxTimeoutMs, &h.Parameters.MaxTimeout}, {p.HandshakeGraceMs, &h.Parameters.HandshakeGrace}, {p.ReturnHealthyMs, &h.Parameters.ReturnHealthy}, {p.MinResidenceMs, &h.Parameters.MinResidence}, {p.LeaseMs, &h.Parameters.Lease}, {p.LeaseRefreshMs, &h.Parameters.LeaseRefresh}}
		for _, d := range durations {
			if d.value > 0 {
				*d.target = time.Duration(d.value) * time.Millisecond
			}
		}
		if p.ConsecutiveFailures > 0 {
			h.Parameters.ConsecutiveFailures = p.ConsecutiveFailures
		}
		if p.ReadySuccesses > 0 {
			h.Parameters.ReadySuccesses = p.ReadySuccesses
		}
	}
	return h
}

func (e *engine) initHealth(c *Config) error {
	if _, ok := e.attach.(*healthAttachment); !ok {
		return nil
	}
	key, err := os.ReadFile(filepath.Join(e.dir, "keys", "control.key"))
	if err != nil {
		return err
	}
	var r *meshrouting.Router
	if c.isRelay() {
		r = meshrouting.New(routerConfig(c), nil, e.events.add)
	}
	e.attach.(*healthAttachment).router.Store(r)
	b := e.bind.(*streamBind)
	e.control, err = health.NewControl(healthConfig(c), string(key), b.Bind, e.dev, newWGLog(func(msg string) { e.events.add("control.error", msg) }).errorf)
	return err
}
func (e *engine) startHealth(c *Config) error {
	a, ok := e.attach.(*healthAttachment)
	if !ok {
		return nil
	}
	m, err := health.New(e.dir, healthConfig(c), e.control, health.Hooks{Event: e.events.add, Routes: e.reroute})
	if err != nil {
		return err
	}
	if r := a.router.Load(); r != nil {
		m.SetRouter(r)
	}
	a.manager.Store(m)
	e.health = m
	if err = e.control.Start(m); err != nil {
		m.Close()
		a.manager.Store(nil)
		e.health = nil
		return err
	}
	return nil
}

func (e *engine) managedPeers(c *Config, peers []peerSpec) ([]peerSpec, error) {
	b := e.bind.(*streamBind)
	if !c.isRelay() && c.Entry != nil {
		for i, p := range c.Entry.Candidates {
			if c.Entry.Mode == "fixed" && i > 0 {
				break
			}
			if slices.ContainsFunc(peers, func(s peerSpec) bool { return s.ID == p.Relay }) {
				continue
			}
			n := c.node(p.Relay)
			key, err := keyHex(n.PublicKey)
			if err != nil {
				return nil, err
			}
			peers = append(peers, peerSpec{ID: n.ID, Kind: kindEntry, Key: key})
		}
	}
	for i := range peers {
		endpoint, err := b.PeerEndpoint(peers[i].ID)
		if err != nil {
			return nil, err
		}
		peers[i].Endpoint = endpoint
		peers[i].Pins = nil
		peers[i].Keepalive = 0
	}
	return peers, nil
}
