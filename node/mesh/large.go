package mesh

import (
	"crypto/rand"
	"encoding/binary"
	"fmt"
	"maps"
	"net/netip"
	"runtime"
	"slices"
	"sync"
	"sync/atomic"
	"time"

	"openbear/node/mesh/wire"
)

// Large packets (network.tcpJumbo, Linux only). A link is large when both
// ends say they take large frames and its path is a TCP or TLS stream of
// wire.LargeFrames (transport); it carries packets of up to wire.LargeMTU
// bytes, every other link the network MTU. The OS interface takes
// largeInner bytes, but the mesh's routes keep the network's MTU less
// e2eOverhead; a node gets a host route of a larger MTU while its path is
// large: the link to it when it is the next hop, else, end to end, the
// first hop's link and every hop past it, which only a probe of that size
// answered through all of them shows. A packet longer than its path takes
// is dropped and, with DF, answered with an ICMP "fragmentation needed":
// to the OS from this node, to the sender from a relay.

const (
	// largeInner is the interface's MTU and a next hop's route's.
	largeInner = wire.LargeMTU - e2eOverhead
	// largeFar is the route's MTU of a node end to end: the inner device
	// pads a packet past the 1368 bytes of its MTU by up to 15 bytes.
	largeFar = largeInner - 16
	// farProbeEvery: a node end to end in use whose first hop is large is
	// probed this often; farMisses probes unanswered within farProbeWait in
	// a row, or an ICMP from a relay, end its large path, and after the
	// latter no probe goes for farHoldOff.
	farProbeEvery = 30 * time.Second
	farProbeWait  = 3 * time.Second
	farMisses     = 2
	farHoldOff    = time.Minute
	// tooBigRate: ICMP errors this node sends a second, at most.
	tooBigRate = 20.0
)

// largePool: the data device has a pool of large buffers and a reader for
// them, whether or not the network turns large packets on: that only
// changes with a restart.
var largePool = runtime.GOOS == "linux"

// largePackets: this node takes and sends large packets.
func (c *Config) largePackets() bool {
	return largePool && c.Network.TCPJumbo && c.Network.MTU <= 1420
}

// pathView is what packets are checked against, swapped whole.
type pathView struct {
	on    bool
	small int             // the network MTU
	own   int             // the longest packet of this node's own its links carry (underlay.go)
	links map[string]bool // large links, by peer
	far   map[string]bool // nodes end to end a probe showed large paths to
}

// linkMTU is the longest packet the link to peer carries.
func (v *pathView) linkMTU(peer string) int {
	if v.on && v.links[peer] {
		return wire.LargeMTU
	}
	return v.small
}

// innerMTU is the longest packet from the OS for node z, whose packets
// leave through hop.
func (v *pathView) innerMTU(z *e2eNode, hop string) int {
	switch {
	case !v.on || !v.links[hop]:
	case hop == z.id:
		return largeInner
	case v.far[z.id]:
		return largeFar
	}
	return v.own - e2eOverhead
}

// pathMTU is the MTU status shows for the path to node id through hop.
func (v *pathView) pathMTU(id, hop string) int {
	if v.on && v.links[hop] && (hop == id || v.far[id]) {
		return wire.LargeMTU
	}
	return v.own
}

// paths keeps the view: the engine sets the links, probes answered and
// ICMP errors from relays the end-to-end paths.
type paths struct {
	view    atomic.Pointer[pathView]
	changed chan struct{} // host routes are due
	now     func() time.Time

	mu     sync.Mutex
	on     bool
	small  int
	own    int
	links  map[string]bool
	far    map[string]*farPath
	tokens float64
	filled time.Time
}

type farPath struct {
	large   bool
	hop     string
	id      [8]byte
	sent    time.Time // the probe waiting for an answer, zero for none
	checked time.Time // the last probe
	missed  int
	hold    time.Time // no probe before
}

func newPaths(small int) *paths {
	p := &paths{changed: make(chan struct{}, 1), now: time.Now, small: small, own: small, links: map[string]bool{}, far: map[string]*farPath{}, tokens: tooBigRate}
	p.publish()
	return p
}

// publish swaps in a view of what p holds. Called with p.mu.
func (p *paths) publish() {
	v := &pathView{on: p.on, small: p.small, own: p.own, links: maps.Clone(p.links), far: map[string]bool{}}
	for id, f := range p.far {
		if f.large {
			v.far[id] = true
		}
	}
	p.view.Store(v)
}

func (p *paths) due() {
	select {
	case p.changed <- struct{}{}:
	default:
	}
}

// configure takes the applied config's settings; off forgets every path.
func (p *paths) configure(on bool, small, own int) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if on == p.on && small == p.small && own == p.own {
		return
	}
	p.on, p.small, p.own = on, small, own
	if !on {
		p.links, p.far = map[string]bool{}, map[string]*farPath{}
	}
	p.publish()
}

// farProbe is a probe to send to node id.
type farProbe struct {
	node string
	id   [8]byte
}

// update takes the large links and the nodes end to end in use with the
// hop each leaves through; it returns the probes due and whether a path
// changed.
func (p *paths) update(links map[string]bool, targets map[string]string) (probes []farProbe, changed bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if !p.on {
		links, targets = nil, nil
	}
	now := p.now()
	if !maps.Equal(links, p.links) {
		p.links, changed = maps.Clone(links), true
		if p.links == nil {
			p.links = map[string]bool{}
		}
	}
	for id, f := range p.far {
		if hop, ok := targets[id]; !ok || !links[hop] {
			changed = changed || f.large
			delete(p.far, id)
		}
	}
	for _, id := range slices.Sorted(maps.Keys(targets)) {
		hop := targets[id]
		if !links[hop] {
			continue
		}
		f := p.far[id]
		if f == nil {
			f = &farPath{hop: hop}
			p.far[id] = f
		}
		if f.hop != hop {
			changed = changed || f.large
			*f = farPath{hop: hop}
		}
		if !f.sent.IsZero() && now.Sub(f.sent) >= farProbeWait {
			f.sent = time.Time{}
			if f.missed++; f.missed >= farMisses && f.large {
				f.large, changed = false, true
			}
		}
		if f.sent.IsZero() && !now.Before(f.hold) && (f.checked.IsZero() || now.Sub(f.checked) >= farProbeEvery) {
			rand.Read(f.id[:])
			f.sent, f.checked = now, now
			probes = append(probes, farProbe{id, f.id})
		}
	}
	if changed {
		p.publish()
	}
	return probes, changed
}

// answered takes a probe's answer from node id.
func (p *paths) answered(id string, probe [8]byte) {
	p.mu.Lock()
	defer p.mu.Unlock()
	f := p.far[id]
	if f == nil || f.sent.IsZero() || f.id != probe {
		return
	}
	f.sent, f.missed = time.Time{}, 0
	if !f.large {
		f.large = true
		p.publish()
		p.due()
	}
}

// tooBig: a relay could not carry this node's packet to node id.
func (p *paths) tooBig(id string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	f := p.far[id]
	if f == nil {
		return
	}
	f.sent, f.hold = time.Time{}, p.now().Add(farHoldOff)
	if f.large {
		f.large = false
		p.publish()
		p.due()
	}
}

// allowICMP takes one of the ICMP errors this node may send.
func (p *paths) allowICMP() bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	now := p.now()
	p.tokens = min(tooBigRate, p.tokens+now.Sub(p.filled).Seconds()*tooBigRate)
	p.filled = now
	if p.tokens < 1 {
		return false
	}
	p.tokens--
	return true
}

// hostRoute is a route to one node of an MTU above the mesh's.
type hostRoute struct {
	Addr netip.Addr
	MTU  int
}

// largeRoutes are the host routes the view gives the nodes of r.
func largeRoutes(v *pathView, r *routing) []hostRoute {
	if !v.on || r == nil || r.nodes == nil {
		return nil
	}
	var out []hostRoute
	for _, z := range r.nodes.byID {
		if z.addr == r.nodes.self {
			continue
		}
		if mtu := v.innerMTU(z, r.hop(z.addr)); mtu > v.own-e2eOverhead {
			out = append(out, hostRoute{z.addr, mtu})
		}
	}
	slices.SortFunc(out, func(a, b hostRoute) int { return a.Addr.Compare(b.Addr) })
	return out
}

func (p ipv4) dontFragment() bool { return p.b[6]&0x40 != 0 }

// tooBigICMP is an ICMP "fragmentation needed" (type 3 code 4) from src to
// p's source, quoting p's header and the first 8 bytes past it, for a
// next hop of mtu.
func tooBigICMP(src netip.Addr, p ipv4, mtu int) []byte {
	quote := p.b[:min(len(p.b), p.ihl+8)]
	b := make([]byte, 28+len(quote))
	b[0], b[8], b[9] = 0x45, 64, protoICMP
	binary.BigEndian.PutUint16(b[2:4], uint16(len(b)))
	s, d := src.As4(), p.src.As4()
	copy(b[12:16], s[:])
	copy(b[16:20], d[:])
	binary.BigEndian.PutUint16(b[10:12], headerChecksum(b[:20]))
	b[20], b[21] = 3, 4
	binary.BigEndian.PutUint16(b[26:28], uint16(mtu))
	copy(b[28:], quote)
	binary.BigEndian.PutUint16(b[22:24], checksum(b[20:]))
	return b
}

// checksum is the Internet checksum of b, of any length.
func checksum(b []byte) uint16 {
	if len(b)%2 == 1 {
		b = append(slices.Clone(b), 0)
	}
	return headerChecksum(b)
}

// e2eTooBig: p is an ICMP "fragmentation needed" about an end-to-end
// packet self sent; dst is the node it was for.
func e2eTooBig(p ipv4, self netip.Addr) (dst netip.Addr, ok bool) {
	l4 := p.payload()
	if p.proto != protoICMP || len(l4) < 28 || l4[0] != 3 || l4[1] != 4 {
		return dst, false
	}
	q := l4[8:]
	if q[0]>>4 != 4 || q[9] != protoE2E || netip.AddrFrom4([4]byte(q[12:16])) != self {
		return dst, false
	}
	return netip.AddrFrom4([4]byte(q[16:20])), true
}

// tooBigLocal drops p, from the OS for node z, when it is longer than its
// path takes, answering with DF to the OS as z.
func (t *meshTUN) tooBigLocal(p ipv4, z *e2eNode, hop string) bool {
	v := t.paths.view.Load()
	if !v.on {
		return false
	}
	mtu := v.innerMTU(z, hop)
	if len(p.b) <= mtu {
		return false
	}
	t.stats.DroppedTooBig.Add(1)
	if p.dontFragment() && t.paths.allowICMP() {
		icmp := tooBigICMP(z.addr, p, mtu)
		back := make([]byte, readHeadroom+len(icmp))
		copy(back[readHeadroom:], icmp)
		t.Device.Write([][]byte{back}, readHeadroom)
	}
	return true
}

// tooBigForward drops p, which this relay would forward to hop, when it is
// longer than the link to hop takes, answering the node it is from.
func (t *meshTUN) tooBigForward(r *routing, p ipv4, hop string) bool {
	v := t.paths.view.Load()
	if !v.on {
		return false
	}
	mtu := v.linkMTU(hop)
	if len(p.b) <= mtu {
		return false
	}
	t.stats.DroppedTooBig.Add(1)
	if z := r.nodes.byAddr[p.src]; z != nil && p.dontFragment() && t.paths.allowICMP() {
		t.sendOwn(r, tooBigICMP(r.nodes.self, p, mtu), z)
	}
	return true
}

// sendOwn sends b, a packet of this node's own, to node z.
func (t *meshTUN) sendOwn(r *routing, b []byte, z *e2eNode) {
	if r.hop(z.addr) == z.id {
		t.sched.enqueue(b)
		return
	}
	if e := t.e2e.Load(); e != nil {
		e.sendControl(b, z)
	}
}

// relayTooBig takes p when it is a relay's ICMP about this node's
// end-to-end packet: the path to the node it was for is no longer large.
func (t *meshTUN) relayTooBig(p ipv4, r *routing) bool {
	dst, ok := e2eTooBig(p, r.nodes.self)
	if !ok {
		return false
	}
	if z := r.nodes.byAddr[dst]; z != nil {
		t.paths.tooBig(z.id)
	}
	return true
}

// watchLarge keeps the large links, the paths end to end and their host
// routes current until the engine closes.
func (e *engine) watchLarge() {
	tick := time.NewTicker(time.Second)
	defer tick.Stop()
	var links <-chan struct{}
	if b, ok := e.bind.(*streamBind); ok {
		links = b.LargeChanges()
	}
	for {
		select {
		case <-e.done:
			return
		case <-tick.C:
		case <-e.tun.paths.changed:
		case <-links:
		}
		e.checkLarge()
	}
}

func (e *engine) checkLarge() {
	p := e.tun.paths
	links := map[string]bool{}
	if b, ok := e.bind.(*streamBind); ok && p.view.Load().on {
		links = b.LargeLinks()
	}
	r := e.tun.route.Load()
	targets := map[string]string{}
	for _, id := range e.e2e.inUse(farProbeEvery) {
		if z := r.nodes.byID[id]; z != nil {
			if hop := r.hop(z.addr); hop != "" && hop != id {
				targets[id] = hop
			}
		}
	}
	probes, _ := p.update(links, targets)
	for _, pr := range probes {
		if z := r.nodes.byID[pr.node]; z != nil {
			e.e2e.sendControl(probePacket(r.nodes.self, z.addr, probeLarge, pr.id, largeFar), z)
		}
	}
	e.refreshHost()
}

// refreshHost applies the host routes the paths give now.
func (e *engine) refreshHost() {
	e.mu.Lock()
	defer e.mu.Unlock()
	select {
	case <-e.done:
		return
	default:
	}
	if e.cfg == nil || e.cfg.Schema == 0 {
		return
	}
	h := e.hostCfg
	h.Hosts = largeRoutes(e.tun.paths.view.Load(), e.tun.route.Load())
	if !e.hostDirty && h.equal(e.hostCfg) {
		return
	}
	e.hostDirty = true
	if err := e.host.apply(h); err != nil {
		if err.Error() != e.largeErr {
			e.largeErr = err.Error()
			e.events.add("route.error", fmt.Sprintf("large packet routes: %v", err))
		}
		return
	}
	e.hostCfg, e.hostDirty, e.largeErr = h, false, ""
}
