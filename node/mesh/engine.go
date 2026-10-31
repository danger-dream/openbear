package mesh

import (
	"encoding/hex"
	"errors"
	"fmt"
	"log"
	"net"
	"net/netip"
	"slices"
	"strings"
	"sync"
	"time"

	"golang.zx2c4.com/wireguard/device"
	"golang.zx2c4.com/wireguard/tun"
	"openbear/node/mesh/health"
	meshrouting "openbear/node/mesh/routing"
)

// hostNet is the OS side of the interface: address, routes, MTU, firewall
// and gateway. apply is called with the full wanted state each time.
type hostNet interface {
	apply(h hostConfig) error
	heal() []string // what it had to repair
	ready() bool
	close()
}

type hostConfig struct {
	Address netip.Addr
	Prefix  netip.Prefix // the mesh, for routes and address translation
	MTU     int
	Routes  []netip.Prefix
	UDP     int // actual WireGuard port
	TCP     int
	TLS     int
	Gateway *Gateway
}

func (h hostConfig) equal(o hostConfig) bool {
	gw := func(g *Gateway) string {
		if g == nil {
			return ""
		}
		return fmt.Sprint(g.Subnets, g.Masquerade)
	}
	return h.Address == o.Address && h.Prefix == o.Prefix && h.MTU == o.MTU && slices.Equal(h.Routes, o.Routes) &&
		h.UDP == o.UDP && h.TCP == o.TCP && h.TLS == o.TLS && gw(h.Gateway) == gw(o.Gateway)
}

// engine is one running data plane: the WireGuard device, meshTUN and
// host settings for the applied config.
type engine struct {
	dir           string
	name          string
	cfg           *Config
	attach        Attachment
	peers         []peerSpec
	wire          map[string]string
	hostCfg       hostConfig
	hostDirty     bool
	resourceStart string
	listen        Listen // as the bind and device have it
	skipped       []netip.Prefix

	tun     *meshTUN
	dev     *device.Device
	bind    meshBind
	host    hostNet
	filter  *filter
	sched   *scheduler
	control *health.Control
	health  *health.Manager
	e2e     *e2eSessions
	keys    *nodeKeys // keys.go; every config applied carries them

	mu        sync.Mutex
	configMu  sync.Mutex
	closeOnce sync.Once
	done      chan struct{}
	events    *eventLog
	hs        map[string]*handshakes
	entry     entryTrack
	conns     connTrack
}

type handshakes struct {
	last  int64
	count int
}

type entryTrack struct {
	candidate string
	since     time.Time
	switches  int
}

func routingFor(c *Config, peers []peerSpec) *routing {
	r := &routing{relay: c.isRelay(), local: localPrefixes(c), control: c.Network.ControlPrefix, transit: map[[32]byte]bool{}}
	for _, p := range peers {
		r.routes = append(r.routes, p.Allowed...)
		if k, err := hex.DecodeString(p.Key); (p.Kind == kindRelay || p.Kind == kindEntry) && err == nil && len(k) == 32 {
			r.transit[[32]byte(k)] = true
		}
	}
	r.hops, r.peerOf = hopsOf(peers)
	var err error
	if r.nodes, err = e2eTableFor(c); err != nil {
		// check refuses such a config; none of its packets pass.
		r.nodes = &e2eTable{self: c.Self.Address}
	}
	// What relays forward is end to end, from nodes' addresses. A member
	// takes it from its entry also while it still routes the sender over a
	// punched link that just went down; meshTUN delivers a plain packet
	// only from its own node.
	for i := range c.Nodes {
		r.sources = append(r.sources, host(c.Nodes[i].Address))
	}
	return r
}

func schedulerFor(c *Config) *scheduler {
	if resourceProfile(c) == roleRelay {
		s := newScheduler(4096, 16<<20)
		// A relay shares its uplink between the nodes it carries, not
		// between their flows: one node's thousand flows get one share.
		s.bySource = true
		return s
	}
	return newScheduler(1024, 8<<20)
}

// startEngine brings up the device for c on dev, which it owns from then
// on, closing it on failure.
func startEngine(dir string, c *Config, privateKey string, dev tun.Device, host hostNet, attach Attachment, events *eventLog) (*engine, error) {
	name, err := dev.Name()
	if err != nil {
		dev.Close()
		return nil, err
	}
	e := &engine{dir: dir, name: name, attach: attach, host: host, events: events, wire: map[string]string{}, hs: map[string]*handshakes{}, done: make(chan struct{}),
		filter: newFilter(c.Rules), sched: schedulerFor(c), cfg: &Config{}, resourceStart: resourceProfile(c)}
	if e.keys, err = loadKeys(dir); err != nil {
		dev.Close()
		return nil, fmt.Errorf("mesh keys (made by mesh.upgrade): %w", err)
	}
	c.keys = e.keys
	e.sched.setRate(c.Self.UploadLimitMbps)
	e.filter.setSites(sitesOf(c))
	e.tun = newMeshTUN(dev, routingFor(c, nil), e.filter, e.sched, c.Network.MTU)
	if e.e2e, err = newE2E(c, e.keys.e2e, e.tun.sendOuter, e.tun.deliverInner, e.events.add); err != nil {
		e.tun.Close()
		return nil, err
	}
	e.tun.e2e.Store(e.e2e)
	e.bind, err = newBind(dir, c)
	if err != nil {
		e.e2e.close()
		e.tun.Close()
		return nil, err
	}
	e.dev = newDevice(e.tun, e.bind)
	fail := func(err error) (*engine, error) {
		e.e2e.close()
		e.host.close()
		if e.control != nil {
			e.control.Close()
		}
		e.dev.Close()
		return nil, err
	}
	key, err := keyHex(privateKey)
	if err != nil {
		return fail(errors.New("keys/wg.key is not a base64 32-byte key"))
	}
	if err = e.dev.IpcSet(fmt.Sprintf("private_key=%s\nlisten_port=%d\n", key, c.Self.Listen.UDP)); err != nil {
		return fail(err)
	}
	e.listen = c.Self.Listen
	if err = e.initHealth(c); err != nil {
		return fail(err)
	}
	if err = e.applyConfig(c); err != nil {
		return fail(err)
	}
	restored := map[string]bool{}
	if e.control == nil {
		restored = e.restoreEndpoints(loadState(dir).Endpoints)
	}
	if err = e.dev.Up(); err != nil {
		return fail(err)
	}
	for _, p := range e.peers {
		if e.control == nil && (p.Endpoint != "" || restored[p.Key]) {
			kick(e.dev, p.Key)
		}
	}
	if e.control != nil {
		if err = e.startHealth(c); err != nil {
			return fail(err)
		}
	} else {
		go e.watchStreams()
	}
	go e.watchConns()
	if e.health != nil && !c.isRelay() {
		go e.probeEntry()
	}
	e.events.add("start", fmt.Sprintf("%s up, revision %d, %d peers, %d learned addresses restored", name, c.Revision, len(e.peers), len(restored)))
	return e, nil
}

// watchStreams starts a handshake when a dialed TCP or TLS path has a new
// connection: the side that accepted it closes it unless WireGuard answers
// through it within 10 s, and keepalives alone get no answer. A handshake
// sent less than 5 s before is not repeated by WireGuard, so it keeps asking
// until one completes after the connection came up.
func (e *engine) watchStreams() {
	tick := time.NewTicker(250 * time.Millisecond)
	defer tick.Stop()
	seen := map[string]string{}
	since := map[string]time.Time{} // new connections waiting for a handshake
	for {
		select {
		case <-e.done:
			return
		case <-tick.C:
		}
		conns := e.bind.dialedStreams()
		now := time.Now()
		for w, local := range conns {
			if local != seen[w] {
				since[w] = now
			}
		}
		seen = conns
		if len(since) == 0 {
			continue
		}
		_, state := deviceState(e.dev)
		var keys []string
		e.mu.Lock()
		for _, p := range e.peers {
			w := e.wire[p.Key+"@"+p.Endpoint]
			t, ok := since[w]
			if !ok {
				continue
			}
			if st := state[p.Key]; st != nil && st.handshakeNano > t.UnixNano() || now.Sub(t) > 10*time.Second {
				delete(since, w)
				continue
			}
			keys = append(keys, p.Key)
		}
		e.mu.Unlock()
		for w := range since {
			if _, ok := conns[w]; !ok {
				delete(since, w)
			}
		}
		for _, k := range keys {
			kick(e.dev, k)
		}
	}
}

// applyConfig moves the running data plane to next, or leaves it as it was.
func (e *engine) applyConfig(next *Config) error {
	e.configMu.Lock()
	defer e.configMu.Unlock()
	next.keys = e.keys
	if e.health != nil {
		if err := e.health.ValidateConfig(healthConfig(next)); err != nil {
			return err
		}
	}
	e.mu.Lock()
	prev := e.cfg
	e.mu.Unlock()
	setProfile := func(c *Config) {
		if b, ok := e.bind.(*streamBind); ok {
			b.SetResourceProfile(resourceProfile(c))
			e.dev.SetPeerLimit(b.DeviceLimits().MaxPeers)
		}
	}
	setProfile(next)
	if e.control != nil {
		if err := e.control.Configure(healthConfig(next)); err != nil {
			if prev.Schema != 0 {
				setProfile(prev)
				if restoreErr := e.control.Configure(healthConfig(prev)); restoreErr != nil {
					return fmt.Errorf("%w; control rollback: %v", err, restoreErr)
				}
			}
			return err
		}
	}
	// A member that becomes a relay gets a router; a relay that becomes a
	// member drops its own once the change took.
	r := e.router()
	created := false
	if next.isRelay() {
		if r == nil {
			r, created = e.setRouter(meshrouting.New(routerConfig(next), nil, e.events.add)), true
		} else {
			r.Configure(routerConfig(next))
		}
	}
	err := e.applyDataConfig(next)
	if err == nil && e.health != nil {
		err = e.health.Configure(healthConfig(next))
	}
	switch {
	case err == nil && !next.isRelay() && r != nil:
		e.setRouter(nil)
	case err != nil && created:
		e.setRouter(nil)
	case err != nil && r != nil && next.isRelay() && prev.Schema != 0:
		r.Configure(routerConfig(prev))
	}
	if err != nil && prev.Schema != 0 {
		setProfile(prev)
	}
	if err != nil && prev.Schema != 0 && e.control != nil {
		if restoreErr := e.control.Configure(healthConfig(prev)); restoreErr != nil {
			return fmt.Errorf("%w; control rollback: %v", err, restoreErr)
		}
		if restoreErr := e.applyDataConfig(prev); restoreErr != nil {
			return fmt.Errorf("%w; route rollback: %v", err, restoreErr)
		}
	}
	return err
}

func (e *engine) applyDataConfig(next *Config) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	prev := e.cfg
	if a, ok := e.attach.(configured); ok {
		a.SetConfig(next)
	}
	err := e.applyLocked(next, true)
	if err == nil {
		e.cfg = next
		return nil
	}
	if prev.Schema != 0 {
		if a, ok := e.attach.(configured); ok {
			a.SetConfig(prev)
		}
		if rerr := e.applyLocked(prev, false); rerr != nil {
			log.Printf("mesh: restoring revision %d: %v", prev.Revision, rerr)
			return fmt.Errorf("%w; restoring revision %d: %v", err, prev.Revision, rerr)
		}
	}
	return err
}

// reroute applies what the attachment now says, for the same config.
func (e *engine) reroute() error {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.applyLocked(e.cfg, false)
}

// recheckLocal applies the host routes again when this machine's own
// networks changed which gateway subnets overlap them: a laptop that leaves
// an office LAN with the same addresses as a gateway's gets that subnet
// routed into the mesh, and loses it again on its return, without waiting
// for a new config.
func (e *engine) recheckLocal() error {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.cfg == nil || e.cfg.Schema == 0 {
		return nil
	}
	if _, skipped := withoutLocal(hostRoutes(e.cfg, e.peers), e.cfg.Network.Prefix, e.name); slices.Equal(skipped, e.skipped) {
		return nil
	}
	return e.applyLocked(e.cfg, false)
}

func (e *engine) applyLocked(next *Config, reconfigure bool) error {
	peers, err := planPeers(next, e.attach)
	if err != nil {
		return err
	}
	if e.control != nil {
		peers, err = e.managedPeers(next, peers)
		if err != nil {
			return err
		}
	}
	for _, p := range peers {
		if p.Endpoint == "" {
			continue
		}
		k := p.Key + "@" + p.Endpoint
		if _, ok := e.wire[k]; !ok {
			w, err := e.bind.endpoint(p.Endpoint)
			if err != nil {
				return fmt.Errorf("peer %s: %w", p.ID, err)
			}
			e.wire[k] = w
		}
	}
	if reconfigure || next.Self.Listen != e.listen || resourceProfile(next) != resourceProfile(e.cfg) || next.Network.MTU != e.cfg.Network.MTU {
		if err := e.bind.reconfigure(next); err != nil {
			return err
		}
	}
	if next.Self.Listen.UDP != e.listen.UDP {
		// Paths learned on the old socket go with it; the peers that dial
		// in are pointed at the same addresses through the new one.
		learned := learnedOf(e.peers, e.dev)
		if err := e.dev.IpcSet(fmt.Sprintf("listen_port=%d\n", next.Self.Listen.UDP)); err != nil {
			return err
		}
		// That closed and reopened the shared bind; the control plane's
		// device has to open its half on the new one, or it never receives
		// again.
		if e.control != nil {
			if err := e.control.Rebind(); err != nil {
				return err
			}
		}
		e.restoreEndpoints(learned)
	}
	e.listen = next.Self.Listen
	for _, p := range peers {
		if len(p.Pins) > 0 {
			e.bind.setPins(e.wire[p.Key+"@"+p.Endpoint], p.Pins)
		}
	}
	diff, released := peerDiff(e.peers, peers, e.wire)
	released = append(released, e.learnedGone(peers)...)
	if diff != "" {
		if err := e.dev.IpcSet(diff); err != nil {
			e.resync()
			return err
		}
	}
	e.peers = peers
	inUse := map[string]bool{}
	for _, p := range peers {
		inUse[e.wire[p.Key+"@"+p.Endpoint]] = true
	}
	for _, w := range released {
		if w != "" && !inUse[w] {
			e.bind.release(w)
		}
	}
	e.forgetWire()
	e.filter.setRules(next.Rules)
	e.filter.setSites(sitesOf(next))
	e.sched.setRate(next.Self.UploadLimitMbps)
	if err := e.e2e.configure(next); err != nil {
		return fmt.Errorf("end-to-end sessions: %w", err)
	}
	e.tun.mtu.Store(int32(next.Network.MTU))
	r := routingFor(next, peers)
	old := e.tun.route.Swap(r)
	e.e2e.rerouted(old, r)

	listen, _ := deviceState(e.dev)
	h := hostConfig{Address: next.Self.Address, Prefix: next.Network.Prefix, MTU: next.Network.MTU - e2eOverhead, Routes: hostRoutes(next, peers),
		UDP: listen, TCP: next.Self.Listen.TCP, TLS: next.Self.Listen.TLS, Gateway: next.Self.Gateway}
	var skipped []netip.Prefix
	h.Routes, skipped = withoutLocal(h.Routes, next.Network.Prefix, e.name)
	if !slices.Equal(skipped, e.skipped) {
		if len(skipped) > 0 {
			e.events.add("route.skipped", fmt.Sprintf("gateway subnets %v overlap this machine's networks; not routed into the mesh", skipped))
		} else {
			e.events.add("route.skipped", fmt.Sprintf("gateway subnets %v no longer overlap this machine's networks; routed into the mesh", e.skipped))
		}
		e.skipped = skipped
	}
	if e.hostDirty || !h.equal(e.hostCfg) {
		e.hostDirty = true
		if err := e.host.apply(h); err != nil {
			return fmt.Errorf("host network: %w", err)
		}
		e.hostCfg = h
		e.hostDirty = false
	}
	e.trackEntry(peers)
	return nil
}

// resync rewrites every peer after a failed set left the device somewhere
// between the old and new peer lists.
func (e *engine) resync() {
	_, actual := deviceState(e.dev)
	var b strings.Builder
	want := map[string]bool{}
	for _, p := range e.peers {
		want[p.Key] = true
		fmt.Fprintf(&b, "public_key=%s\nreplace_allowed_ips=true\n", p.Key)
		for _, a := range p.Allowed {
			fmt.Fprintf(&b, "allowed_ip=%s\n", a)
		}
		if p.Endpoint != "" {
			fmt.Fprintf(&b, "endpoint=%s\n", e.wire[p.Key+"@"+p.Endpoint])
		}
		fmt.Fprintf(&b, "persistent_keepalive_interval=%d\n", p.keepalive())
	}
	for k := range actual {
		if !want[k] {
			fmt.Fprintf(&b, "public_key=%s\nremove=true\n", k)
		}
	}
	if err := e.dev.IpcSet(b.String()); err != nil {
		log.Printf("mesh: resync peers: %v", err)
	}
}

func (e *engine) forgetWire() {
	used := map[string]bool{}
	for _, p := range e.peers {
		used[p.Key+"@"+p.Endpoint] = true
	}
	for k := range e.wire {
		if !used[k] {
			delete(e.wire, k)
		}
	}
}

func (e *engine) trackEntry(peers []peerSpec) {
	cand := ""
	for _, p := range peers {
		if p.Kind == kindEntry && p.Candidate != "" {
			cand = p.Candidate
		}
	}
	if cand == e.entry.candidate {
		return
	}
	if e.entry.candidate != "" && cand != "" && e.health == nil {
		e.entry.switches++
		e.events.add("entry.switch", e.entry.candidate+" -> "+cand)
	}
	e.entry.candidate, e.entry.since = cand, time.Now()
}

// learnedGone lists the addresses learned for peers that dial in and are
// now removed or recreated, so the transport drops their paths too.
func (e *engine) learnedGone(next []peerSpec) []string {
	keep := map[string]bool{}
	for _, p := range next {
		keep[p.Key] = true
		for _, o := range e.peers {
			if o.Key == p.Key && (o.Endpoint == "") != (p.Endpoint == "") {
				keep[p.Key] = false
			}
		}
	}
	_, state := deviceState(e.dev)
	var out []string
	for _, o := range e.peers {
		if s := state[o.Key]; s != nil && o.Endpoint == "" && !keep[o.Key] && s.endpoint != "" {
			out = append(out, s.endpoint)
		}
	}
	return out
}

// restoreEndpoints gives peers that wait to be dialed the UDP address they
// were last seen at, so a restarted relay reaches members behind NAT
// first. Done after the peers exist, so they keep learning new addresses.
func (e *engine) restoreEndpoints(seen map[string]string) map[string]bool {
	out := map[string]bool{}
	var b strings.Builder
	for _, p := range e.peers {
		addr, ok := seen[p.ID]
		if p.Endpoint != "" || !ok {
			continue
		}
		if _, err := netip.ParseAddrPort(addr); err != nil {
			continue
		}
		w, err := e.bind.endpoint("udp://" + addr)
		if err != nil {
			continue
		}
		fmt.Fprintf(&b, "public_key=%s\nupdate_only=true\nendpoint=%s\n", p.Key, w)
		out[p.Key] = true
	}
	if b.Len() > 0 {
		if err := e.dev.IpcSet(b.String()); err != nil {
			log.Printf("mesh: restoring learned addresses: %v", err)
			return nil
		}
	}
	return out
}

// learnedEndpoints are the current UDP addresses of peers this node does
// not dial; a stream that came in cannot be dialed back.
func (e *engine) learnedEndpoints() map[string]string {
	e.mu.Lock()
	peers := slices.Clone(e.peers)
	e.mu.Unlock()
	return learnedOf(peers, e.dev)
}

func learnedOf(peers []peerSpec, dev *device.Device) map[string]string {
	_, state := deviceState(dev)
	out := map[string]string{}
	for _, p := range peers {
		if s := state[p.Key]; s != nil && p.Endpoint == "" && s.endpoint != "" && transportOf(s.endpoint) == "udp" {
			out[p.ID] = stripScheme(s.endpoint)
		}
	}
	return out
}

// reloadCertificate is for a config file rewritten unchanged, which is how
// the node says the relay certificate changed.
func (e *engine) reloadCertificate() error {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.bind.reconfigure(e.cfg)
}

func (e *engine) close() {
	e.closeOnce.Do(func() {
		close(e.done)
		if e.health != nil {
			e.health.Close()
		}
		if e.control != nil {
			e.control.Close()
		}
		e.host.close()
		e.dev.Close()
		e.e2e.close()
	})
}

// withoutLocal drops routes that overlap a network this machine is already
// on, so a gateway subnet cannot capture the local LAN. The mesh prefix
// itself is checked when the node joins.
func withoutLocal(routes []netip.Prefix, mesh netip.Prefix, iface string) (kept, skipped []netip.Prefix) {
	var local []netip.Prefix
	ifaces, _ := net.Interfaces()
	for _, ifc := range ifaces {
		if ifc.Name == iface {
			continue
		}
		addrs, _ := ifc.Addrs()
		for _, a := range addrs {
			n, ok := a.(*net.IPNet)
			if !ok || n.IP.To4() == nil || n.IP.IsLoopback() {
				continue
			}
			bits, _ := n.Mask.Size()
			addr, _ := netip.AddrFromSlice(n.IP.To4())
			local = append(local, netip.PrefixFrom(addr, bits).Masked())
		}
	}
	for _, r := range routes {
		if r != mesh && slices.ContainsFunc(local, r.Overlaps) {
			skipped = append(skipped, r)
		} else {
			kept = append(kept, r)
		}
	}
	return kept, skipped
}
