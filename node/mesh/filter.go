package mesh

import (
	"net/netip"
	"sync"
	"time"
)

type flowKey struct {
	proto                 byte
	local, remote         netip.Addr
	localPort, remotePort uint16
}

type fragKey struct {
	proto    byte
	src, dst netip.Addr
	id       uint16
}

// flowTimeouts: how long a flow both sides have sent on lives after this
// member's last packet on it (outbound) or either side's (incoming). One
// only one side has sent on lives openTimeout: the scans and floods that
// fill tables are mostly that.
var flowTimeouts = map[byte]time.Duration{protoTCP: 15 * time.Minute, protoUDP: 3 * time.Minute, protoICMP: 30 * time.Second}

const openTimeout = 30 * time.Second

// fragTimeout: a datagram's fragments arrive back to back; this only bounds
// one whose last fragment never comes.
const fragTimeout = 5 * time.Second

// Bounds of the state tables. This member's own flows and the ones peers
// start are kept apart, so that peers cannot crowd out the replies to this
// member's connections, and one remote address takes at most a share of
// either. A full table, or a remote at its share, gives up the flow least
// recently used. A gateway member keeps the flows of every device behind
// it; see TestFilterGatewayAndFragmentLoad for the sizes.
const (
	maxFilterEntries   = 65536 // each of the two flow tables
	maxFlowsPerRemote  = 32768 // of this member's own flows, to one remote address
	maxIncomingPerPeer = 16384 // of the flows one remote address starts
	maxFragments       = 16384
	maxFragsPerSource  = 1024
)

// fragment is what later fragments are decided on: the first fragment's
// transport header in short.
type fragment struct {
	proto    byte
	src, dst netip.Addr
	port     uint16 // destination port, for rules
	ports    bool
	reply    flowKey // the flow of this member's the datagram answers
	replies  bool
}

type flowState struct{ established bool }

// site is a node with subnets behind it: an ICMP error about one of them
// comes from the host, the node or another host there.
type site struct {
	address netip.Addr
	subnets []netip.Prefix
}

type filter struct {
	rules    []Rule
	sites    []site
	now      func() time.Time
	mu       sync.Mutex
	flows    *lru[flowKey, flowState]
	incoming *lru[flowKey, flowState]
	frags    *lru[fragKey, fragment]
}

func newFilter(rules []Rule) *filter {
	remote := func(k flowKey) netip.Addr { return k.remote }
	return &filter{rules: rules, now: time.Now,
		flows:    newLRU[flowKey, flowState](maxFilterEntries, maxFlowsPerRemote, remote),
		incoming: newLRU[flowKey, flowState](maxFilterEntries, maxIncomingPerPeer, remote),
		frags:    newLRU[fragKey, fragment](maxFragments, maxFragsPerSource, func(k fragKey) netip.Addr { return k.src })}
}

// setRules takes effect on the next packet: traffic let in by a removed rule
// stops, while replies to this member's own traffic keep flowing.
func (f *filter) setRules(rules []Rule) {
	f.mu.Lock()
	f.rules = rules
	f.mu.Unlock()
}

func (f *filter) setSites(sites []site) {
	f.mu.Lock()
	f.sites = sites
	f.mu.Unlock()
}

func sitesOf(c *Config) []site {
	var out []site
	for _, n := range c.Nodes {
		if len(n.Subnets) > 0 {
			out = append(out, site{n.Address, n.Subnets})
		}
	}
	return out
}

// outbound remembers a packet this member sends so its replies are let in.
func (f *filter) outbound(p ipv4) {
	a, b, ok := p.ports()
	if !ok {
		return
	}
	if p.proto == protoICMP && p.payload()[0] != 8 {
		return
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	k := flowKey{p.proto, p.src, p.dst, a, b}
	now := f.now()
	flow := f.flows.get(k, now)
	if in := f.incoming.get(k, now); in != nil && flow == nil {
		// A reply on a flow the peer started.
		in.v.established = in.v.established || !tcpFlag(p, tcpRST)
		in.expires = now.Add(f.timeout(p.proto, in.v))
		f.incoming.touch(in)
		return
	}
	if flow == nil && tcpFlag(p, tcpSYN) && tcpFlag(p, tcpACK) {
		// The answer to a connection a peer started, whose flow is gone
		// from the table: it is not one of this member's own.
		return
	}
	if flow == nil {
		flow = f.flows.add(k, flowState{}, now)
	}
	flow.expires = now.Add(f.timeout(p.proto, flow.v))
	f.flows.touch(flow)
}

func (f *filter) timeout(proto byte, s flowState) time.Duration {
	if s.established {
		return flowTimeouts[proto]
	}
	return min(openTimeout, flowTimeouts[proto])
}

// inbound decides a packet arriving for this member.
func (f *filter) inbound(p ipv4) bool {
	now := f.now()
	f.mu.Lock()
	defer f.mu.Unlock()
	if p.frag != 0 {
		// Later fragments carry no ports: they follow their first fragment.
		e := f.frags.get(fragKey{p.proto, p.src, p.dst, p.id}, now)
		if e == nil {
			return false
		}
		v := e.v
		if p.more {
			f.frags.touch(e)
		} else {
			f.frags.remove(e)
		}
		return (v.replies && f.answer(v.reply, p, now)) || f.ruleAllows(v)
	}
	v := f.summarize(p)
	reply := v.replies && f.answer(v.reply, p, now)
	allowed := reply || f.ruleAllows(v)
	if allowed && !reply && (p.proto == protoTCP || p.proto == protoUDP) && v.ports {
		e := f.incoming.get(v.reply, now)
		if e == nil {
			e = f.incoming.add(v.reply, flowState{}, now)
		}
		e.expires = now.Add(f.timeout(p.proto, e.v))
		f.incoming.touch(e)
	}
	if allowed && p.more {
		e := f.frags.add(fragKey{p.proto, p.src, p.dst, p.id}, v, now)
		e.expires = now.Add(fragTimeout)
	}
	return allowed
}

// answer says whether this member has flow k open, and takes p, part of
// the reply, as the flow's other side.
func (f *filter) answer(k flowKey, p ipv4, now time.Time) bool {
	e := f.flows.get(k, now)
	if e == nil {
		return false
	}
	if !e.v.established && k.proto == p.proto && !tcpFlag(p, tcpRST) {
		// The flow lives on from this member's last packet.
		e.expires = e.expires.Add(f.timeout(k.proto, flowState{true}) - f.timeout(k.proto, e.v))
		e.v.established = true
	}
	f.flows.touch(e)
	return true
}

// summarize reads what deciding p and its later fragments takes.
func (f *filter) summarize(p ipv4) fragment {
	v := fragment{proto: p.proto, src: p.src, dst: p.dst}
	l4 := p.payload()
	if p.proto == protoICMP && len(l4) >= 8 && (l4[0] == 3 || l4[0] == 11) {
		// An error about a packet this member sent: match the quoted header,
		// from the host it went to or a router on the way there.
		inner, ok := parseQuoted(l4[8:])
		if !ok || inner.src != p.dst || !f.onPath(p.src, inner.dst) {
			return v
		}
		a, b, ok := inner.ports()
		v.reply, v.replies = flowKey{inner.proto, inner.src, inner.dst, a, b}, ok
		return v
	}
	a, b, ok := p.ports()
	if !ok {
		return v
	}
	if p.proto == protoICMP {
		v.reply, v.replies = flowKey{protoICMP, p.dst, p.src, a, 0}, l4[0] == 0
		return v
	}
	v.port, v.ports = b, true
	v.reply, v.replies = flowKey{p.proto, p.dst, p.src, b, a}, true
	return v
}

// onPath says whether an ICMP error from src can be about a packet to dst:
// src is dst, or dst is behind a node and src is that node or behind it
// too. Any other source is let in only as the rules allow.
func (f *filter) onPath(src, dst netip.Addr) bool {
	if src == dst {
		return true
	}
	for _, s := range f.sites {
		if anyContains(s.subnets, dst) && (src == s.address || anyContains(s.subnets, src)) {
			return true
		}
	}
	return false
}

const (
	tcpRST = 0x04
	tcpSYN = 0x02
	tcpACK = 0x10
)

func tcpFlag(p ipv4, flag byte) bool {
	l4 := p.payload()
	return p.proto == protoTCP && len(l4) >= 14 && l4[13]&flag != 0
}

// parseQuoted reads the IP header and first 8 bytes an ICMP error quotes.
func parseQuoted(b []byte) (ipv4, bool) {
	if len(b) < 20 || b[0]>>4 != 4 {
		return ipv4{}, false
	}
	ihl := int(b[0]&0x0f) * 4
	if ihl < 20 || len(b) < ihl+8 {
		return ipv4{}, false
	}
	q := append([]byte(nil), b[:ihl+8]...)
	q[2], q[3] = byte((ihl+8)>>8), byte(ihl+8) // the quote is truncated; parse it as whole
	return parseIPv4(q)
}

func (f *filter) ruleAllows(p fragment) bool {
	for _, r := range f.rules {
		if !anyContains(r.Src, p.src) || !anyContains(r.Dst, p.dst) {
			continue
		}
		switch r.Proto {
		case "", "any":
		case "tcp":
			if p.proto != protoTCP {
				continue
			}
		case "udp":
			if p.proto != protoUDP {
				continue
			}
		case "icmp":
			if p.proto != protoICMP {
				continue
			}
		default:
			continue
		}
		if len(r.Ports) > 0 && (p.proto != protoTCP && p.proto != protoUDP || !p.ports || !inRanges(r.Ports, p.port)) {
			continue
		}
		return true
	}
	return false
}

func anyContains(list []netip.Prefix, a netip.Addr) bool {
	if len(list) == 0 {
		return true
	}
	for _, p := range list {
		if p.Contains(a) {
			return true
		}
	}
	return false
}

func inRanges(ranges [][2]uint16, port uint16) bool {
	for _, r := range ranges {
		if port >= r[0] && port <= r[1] {
			return true
		}
	}
	return false
}

func (f *filter) expire() {
	now := f.now()
	f.mu.Lock()
	f.flows.expire(now)
	f.incoming.expire(now)
	f.frags.expire(now)
	f.mu.Unlock()
}

// lru is a table of at most limit entries, at most perOwner of them for one
// owner, an address the key names. Adding past either bound drops the
// owner's, or else the table's, least recently used entry.
type lru[K comparable, V any] struct {
	limit, perOwner int
	ownerOf         func(K) netip.Addr
	entries         map[K]*lruEntry[K, V]
	all             lruList[K, V]
	owners          map[netip.Addr]*lruList[K, V]
}

type lruEntry[K comparable, V any] struct {
	key     K
	expires time.Time
	v       V
	// links[0] in the table's list, links[1] in the owner's; least
	// recently used first.
	links [2]struct{ prev, next *lruEntry[K, V] }
}

type lruList[K comparable, V any] struct {
	head, tail *lruEntry[K, V]
	n          int
}

func newLRU[K comparable, V any](limit, perOwner int, ownerOf func(K) netip.Addr) *lru[K, V] {
	return &lru[K, V]{limit: limit, perOwner: perOwner, ownerOf: ownerOf, entries: map[K]*lruEntry[K, V]{}, owners: map[netip.Addr]*lruList[K, V]{}}
}

func (t *lru[K, V]) len() int { return len(t.entries) }

// get is k's entry if it has not expired.
func (t *lru[K, V]) get(k K, now time.Time) *lruEntry[K, V] {
	if e := t.entries[k]; e != nil && e.expires.After(now) {
		return e
	}
	return nil
}

// add puts a new entry for k, in place of an expired one; the caller sets
// when it expires.
func (t *lru[K, V]) add(k K, v V, now time.Time) *lruEntry[K, V] {
	if old := t.entries[k]; old != nil {
		t.remove(old)
	}
	owner := t.ownerOf(k)
	if o := t.owners[owner]; o != nil && o.n >= t.perOwner {
		t.remove(o.head)
	} else if len(t.entries) >= t.limit {
		t.remove(t.all.head)
	}
	e := &lruEntry[K, V]{key: k, v: v, expires: now}
	t.entries[k] = e
	t.all.push(e, 0)
	o := t.owners[owner]
	if o == nil {
		o = &lruList[K, V]{}
		t.owners[owner] = o
	}
	o.push(e, 1)
	return e
}

// touch makes e the most recently used.
func (t *lru[K, V]) touch(e *lruEntry[K, V]) {
	if t.all.tail == e {
		return
	}
	o := t.owners[t.ownerOf(e.key)]
	t.all.unlink(e, 0)
	t.all.push(e, 0)
	o.unlink(e, 1)
	o.push(e, 1)
}

func (t *lru[K, V]) remove(e *lruEntry[K, V]) {
	owner := t.ownerOf(e.key)
	o := t.owners[owner]
	delete(t.entries, e.key)
	t.all.unlink(e, 0)
	if o.unlink(e, 1); o.n == 0 {
		delete(t.owners, owner)
	}
}

func (t *lru[K, V]) expire(now time.Time) {
	for _, e := range t.entries {
		if !e.expires.After(now) {
			t.remove(e)
		}
	}
}

func (l *lruList[K, V]) push(e *lruEntry[K, V], i int) {
	e.links[i].prev, e.links[i].next = l.tail, nil
	if l.tail != nil {
		l.tail.links[i].next = e
	} else {
		l.head = e
	}
	l.tail = e
	l.n++
}

func (l *lruList[K, V]) unlink(e *lruEntry[K, V], i int) {
	prev, next := e.links[i].prev, e.links[i].next
	if prev != nil {
		prev.links[i].next = next
	} else {
		l.head = next
	}
	if next != nil {
		next.links[i].prev = prev
	} else {
		l.tail = prev
	}
	e.links[i].prev, e.links[i].next = nil, nil
	l.n--
}
