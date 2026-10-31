package mesh

import (
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"hash/maphash"
	"net"
	"net/netip"
	"os"
	"slices"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"golang.zx2c4.com/wireguard/conn"
	"golang.zx2c4.com/wireguard/device"
	"golang.zx2c4.com/wireguard/tun"
)

// End-to-end sessions (design section 7). Between two nodes that are not
// each other's next hop, a packet is encrypted by a second WireGuard device,
// the inner one, keyed with the nodes' e2e.key, and carried in an outer IPv4
// packet of protocol 254 from the sender's mesh address to the receiver's.
// Relays forward the outer packet by its destination and cannot read, alter
// or forge what is inside: the receiver's inner device authenticates it and
// checks its source against the sender's prefixes. Between next hops (a
// member and its entry relay, linked relays, a relay and a member attached
// to it, members linked directly) the hop's own WireGuard session already
// is end to end, and packets go as they are.

const (
	protoE2E = 254
	// e2eIdle: a session unused this long may make room for another.
	e2eIdle = 30 * time.Second
)

// e2eNode is another node as its inner peer has it.
type e2eNode struct {
	id       string
	key      string // hex X25519
	addr     netip.Addr
	prefixes []netip.Prefix // its address and the gateway subnets this node routes to it
}

func (n *e2eNode) equal(o *e2eNode) bool {
	return n.id == o.id && n.key == o.key && n.addr == o.addr && slices.Equal(n.prefixes, o.prefixes)
}

// e2eTable maps addresses to nodes for one applied config.
type e2eTable struct {
	self    netip.Addr
	byAddr  map[netip.Addr]*e2eNode
	byID    map[string]*e2eNode
	subnets []e2eSubnet // longest first
}

type e2eSubnet struct {
	p netip.Prefix
	n *e2eNode
}

func e2eTableFor(c *Config) (*e2eTable, error) {
	t := &e2eTable{self: c.Self.Address, byAddr: map[netip.Addr]*e2eNode{}, byID: map[string]*e2eNode{}}
	for i := range c.Nodes {
		n := &c.Nodes[i]
		k, err := keyHex(n.E2EKey)
		if err != nil {
			return nil, fmt.Errorf("node %s: e2eKey: %w", n.ID, err)
		}
		e := &e2eNode{id: n.ID, key: k, addr: n.Address, prefixes: sortPrefixes(memberPrefixes(c, n))}
		t.byAddr[n.Address], t.byID[n.ID] = e, e
		for _, p := range e.prefixes {
			if p != host(n.Address) {
				t.subnets = append(t.subnets, e2eSubnet{p, e})
			}
		}
	}
	slices.SortStableFunc(t.subnets, func(a, b e2eSubnet) int { return b.p.Bits() - a.p.Bits() })
	return t, nil
}

// owner is the node a's packets come from or go to: the node with a as its
// address, else the gateway that routes a.
func (t *e2eTable) owner(a netip.Addr) *e2eNode {
	if t == nil {
		return nil
	}
	if n := t.byAddr[a]; n != nil {
		return n
	}
	for _, s := range t.subnets {
		if s.p.Contains(a) {
			return s.n
		}
	}
	return nil
}

// e2eLimits is the inner device's budget, apart from the 16 or 128 paths
// of the outer one: inner sessions ride on the outer paths.
type e2eLimits struct {
	device      conn.DeviceLimits
	rate, burst float64 // new sessions per second
	queue       int     // packets waiting for the inner device each way
	idle        time.Duration
}

func e2eLimitsFor(c *Config) e2eLimits {
	if resourceProfile(c) == roleRelay {
		return e2eLimits{device: conn.DeviceLimits{MaxPeers: 128, Workers: 2, BatchSize: 1, QueueDepth: 4, HandshakeQueueDepth: 32, StagedDepth: 4,
			TxBuffers: 96, RxBuffers: 64, KeepaliveBuffers: 4}, rate: 32, burst: 64, queue: 1024, idle: e2eIdle}
	}
	return e2eLimits{device: conn.DeviceLimits{MaxPeers: 64, Workers: 1, BatchSize: 1, QueueDepth: 4, HandshakeQueueDepth: 32, StagedDepth: 4,
		TxBuffers: 48, RxBuffers: 32, KeepaliveBuffers: 4}, rate: 16, burst: 32, queue: 256, idle: e2eIdle}
}

// e2eStats are kept apart from the outer counters.
type e2eStats struct {
	Sent, Received                 atomic.Uint64
	DroppedQueue, DroppedExhausted atomic.Uint64
	DroppedSource, DroppedBad      atomic.Uint64
	Created, Evicted               atomic.Uint64
}

// e2eSessions is the inner device and its peers, made on demand within the
// budget and dropped least recently used first.
type e2eSessions struct {
	dev    *device.Device
	tun    *innerTUN
	bind   *innerBind
	limits e2eLimits
	stats  e2eStats
	events func(kind, detail string)

	mu     sync.Mutex
	table  *e2eTable
	peers  map[string]*e2ePeer // node id
	tokens float64
	filled time.Time
	// trials and trialTokens: sessions a packet from another node made, not
	// proven yet (see e2ePeer.proven), and their own bucket of new ones.
	trials      int
	trialTokens float64
	proofs      chan e2eProof
	reported    time.Time // last e2e.exhausted
	refused     int       // since then
	now         func() time.Time
}

type e2ePeer struct {
	node *e2eNode
	used atomic.Int64 // unix nanoseconds
	// proven: the inner device authenticated a packet from it, or this node
	// sent to it. Until then the session is a trial: any relay or entry can
	// send a packet that claims to be from a node, so trials get a budget of
	// their own (e2eTrials), and what they receive keeps no session in use.
	proven  bool
	created time.Time
	raw     [32]byte // the node's key
}

// e2eTrials: trials at once, out of MaxPeers; a trial older than
// e2eTrialGrace makes room for a newer one. A handshake takes a round trip.
func e2eTrials(l e2eLimits) int { return max(4, l.device.MaxPeers/4) }

const e2eTrialGrace = 5 * time.Second

// newE2E starts the inner device with privateKey (base64) for c. send
// takes an outer packet to the scheduler; deliver takes decrypted packets,
// at offset in bufs.
func newE2E(c *Config, privateKey string, send func(b []byte, hash uint64), deliver func(bufs [][]byte, offset int), events func(kind, detail string)) (*e2eSessions, error) {
	key, err := keyHex(privateKey)
	if err != nil {
		return nil, errors.New("keys/e2e.key is not a base64 32-byte key")
	}
	l := e2eLimitsFor(c)
	s := &e2eSessions{limits: l, events: events, peers: map[string]*e2ePeer{}, tokens: l.burst, trialTokens: l.burst / 2, proofs: make(chan e2eProof, 256), now: time.Now}
	s.filled = s.now()
	s.tun = newInnerTUN(c.Network.MTU-e2eOverhead, l.queue, deliver)
	s.bind = newInnerBind(c.Self.Address, l, send)
	s.tun.authenticated = s.prove
	wl := newWGLog(func(msg string) { events("e2e.error", msg) })
	s.dev = device.NewDevice(s.tun, s.bind, &device.Logger{Verbosef: device.DiscardLogf, Errorf: wl.errorf})
	// Each peer's endpoint is its node and stays so: the outer source of a
	// packet does not move a session.
	s.dev.DisableSomeRoamingForBrokenMobileSemantics()
	if err := s.dev.IpcSet("private_key=" + key + "\n"); err != nil {
		s.dev.Close()
		return nil, err
	}
	if err := s.configure(c); err != nil {
		s.dev.Close()
		return nil, err
	}
	if err := s.dev.Up(); err != nil {
		s.dev.Close()
		return nil, err
	}
	return s, nil
}

// configure moves the sessions to c: peers of nodes gone or rekeyed are
// removed, the others take their new prefixes.
func (s *e2eSessions) configure(c *Config) error {
	t, err := e2eTableFor(c)
	if err != nil {
		return err
	}
	s.bind.setSelf(c.Self.Address, t)
	s.tun.setMTU(c.Network.MTU - e2eOverhead)
	s.mu.Lock()
	defer s.mu.Unlock()
	s.limits.device.MaxPeers = e2eLimitsFor(c).device.MaxPeers
	s.dev.SetPeerLimit(s.limits.device.MaxPeers)
	var b strings.Builder
	for id, p := range s.peers {
		n := t.byID[id]
		switch {
		case n == nil || n.key != p.node.key:
			fmt.Fprintf(&b, "public_key=%s\nremove=true\n", p.node.key)
			s.drop(id)
		case !n.equal(p.node):
			fmt.Fprintf(&b, "public_key=%s\nupdate_only=true\nreplace_allowed_ips=true\n", n.key)
			for _, a := range n.prefixes {
				fmt.Fprintf(&b, "allowed_ip=%s\n", a)
			}
			p.node = n
		}
	}
	s.table = t
	if b.Len() > 0 {
		return s.dev.IpcSet(b.String())
	}
	return nil
}

// peer returns n's session, making one if the budget allows. mine: for
// this node's own packet, which proves the session and keeps it in use; a
// packet received only gets a trial (see e2ePeer.proven), and only when it
// starts a handshake (start).
func (s *e2eSessions) peer(n *e2eNode, mine, start bool) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.now()
	s.applyProofs(now)
	if p := s.peers[n.id]; p != nil && p.node.key == n.key {
		if mine {
			p.used.Store(now.UnixNano())
			s.setProven(p)
		}
		return true
	}
	if s.table == nil || !mine && !start {
		return false
	}
	cur := s.table.byID[n.id]
	if cur == nil || cur.key != n.key {
		return false // from a config no longer applied
	}
	n = cur
	elapsed := now.Sub(s.filled).Seconds()
	s.tokens = min(s.limits.burst, s.tokens+elapsed*s.limits.rate)
	s.trialTokens = min(s.limits.burst/2, s.trialTokens+elapsed*s.limits.rate/2)
	s.filled = now
	var evict *e2ePeer
	if !mine && s.trials >= e2eTrials(s.limits) {
		// A trial makes room only among trials, from one past its grace.
		for _, p := range s.peers {
			if !p.proven && now.Sub(p.created) >= e2eTrialGrace && (evict == nil || p.created.Before(evict.created)) {
				evict = p
			}
		}
		if evict == nil {
			s.exhausted(now, fmt.Sprintf("%d end-to-end sessions not proven yet; packets from %s dropped", s.trials, n.id))
			return false
		}
	} else if len(s.peers) >= s.limits.device.MaxPeers {
		// A trial goes first, any of them for this node's own packet; then the least recently used idle one.
		for _, p := range s.peers {
			if !p.proven && (mine || now.Sub(p.created) >= e2eTrialGrace) && (evict == nil || p.created.Before(evict.created)) {
				evict = p
			}
		}
		if evict == nil {
			for _, p := range s.peers {
				if now.Sub(time.Unix(0, p.used.Load())) >= s.limits.idle && (evict == nil || p.used.Load() < evict.used.Load()) {
					evict = p
				}
			}
		}
		if evict == nil {
			s.exhausted(now, fmt.Sprintf("all %d end-to-end sessions in use in the last %s; packets for %s dropped",
				s.limits.device.MaxPeers, s.limits.idle, n.id))
			return false
		}
	}
	tokens := &s.tokens
	if !mine {
		tokens = &s.trialTokens
	}
	if *tokens < 1 {
		s.exhausted(now, fmt.Sprintf("more than %.0f new end-to-end sessions a second; packets for %s dropped", s.limits.rate, n.id))
		return false
	}
	raw, err := hex.DecodeString(n.key)
	if err != nil || len(raw) != 32 {
		return false
	}
	*tokens--
	var b strings.Builder
	if evict != nil {
		fmt.Fprintf(&b, "public_key=%s\nremove=true\n", evict.node.key)
	}
	fmt.Fprintf(&b, "public_key=%s\nendpoint=%s%s\nreplace_allowed_ips=true\n", n.key, e2eScheme, n.id)
	for _, a := range n.prefixes {
		fmt.Fprintf(&b, "allowed_ip=%s\n", a)
	}
	if err := s.dev.IpcSet(b.String()); err != nil {
		s.events("e2e.error", fmt.Sprintf("session with %s: %v", n.id, err))
		return false
	}
	if evict != nil {
		s.drop(evict.node.id)
		s.stats.Evicted.Add(1)
	}
	p := &e2ePeer{node: n, created: now, raw: [32]byte(raw)}
	p.used.Store(now.UnixNano())
	s.peers[n.id] = p
	if mine {
		p.proven = true
	} else {
		s.trials++
	}
	s.stats.Created.Add(1)
	return true
}

// setProven makes p a session like this node's own. Called with s.mu.
func (s *e2eSessions) setProven(p *e2ePeer) {
	if !p.proven {
		p.proven = true
		s.trials--
	}
}

// drop forgets node id's session. Called with s.mu.
func (s *e2eSessions) drop(id string) {
	if p := s.peers[id]; p != nil {
		if !p.proven {
			s.trials--
		}
		delete(s.peers, id)
	}
}

type e2eProof struct {
	id  string
	key [32]byte
	at  time.Time
}

// prove: the inner device decrypted data from node id's key (fresh, its
// counter checked): its session is in use. A handshake alone proves
// nothing; a trial has e2eTrialGrace for its first data. Called by the
// device's receive routines, which removing a peer under
// s.mu waits for: it never waits itself, a proof past the queue is lost
// and the next one counts.
func (s *e2eSessions) prove(id string, key [32]byte) {
	select {
	case s.proofs <- e2eProof{id, key, s.now()}:
	default:
	}
}

// applyProofs takes in what prove queued. Called with s.mu.
func (s *e2eSessions) applyProofs(now time.Time) {
	for {
		select {
		case pr := <-s.proofs:
			if p := s.peers[pr.id]; p != nil && p.raw == pr.key {
				// When the data came, not now: the status shows how long it has been idle.
				p.used.Store(max(p.used.Load(), min(pr.at.UnixNano(), now.UnixNano())))
				s.setProven(p)
			}
		default:
			return
		}
	}
}

// exhausted counts a refused session, reported once a minute. Packets are
// dropped, never sent without end-to-end encryption. Called with s.mu.
func (s *e2eSessions) exhausted(now time.Time, detail string) {
	s.stats.DroppedExhausted.Add(1)
	s.refused++
	if now.Sub(s.reported) < time.Minute {
		return
	}
	if s.refused > 1 {
		detail += fmt.Sprintf(" (%d refused since the last report)", s.refused)
	}
	s.reported, s.refused = now, 0
	s.events("e2e.exhausted", detail)
}

// send encrypts packet b, of this node's own traffic, for n.
func (s *e2eSessions) send(b []byte, n *e2eNode, hash uint64) {
	if !s.peer(n, true, true) {
		return
	}
	if !s.tun.queue(b, s.bind.endpointOf(n.id), hash) {
		s.stats.DroppedQueue.Add(1)
		return
	}
	s.stats.Sent.Add(1)
}

// sendControl sends b, a probe of the data plane's own, to n; it is not
// counted as traffic.
func (s *e2eSessions) sendControl(b []byte, n *e2eNode) {
	if s.peer(n, true, true) && !s.tun.queue(b, s.bind.endpointOf(n.id), 0) {
		s.stats.DroppedQueue.Add(1)
	}
}

// receive takes the payload of an outer packet from n. Without a session for
// n only a handshake initiation is taken: the data of a session from before
// this node restarted cannot be read by a new one, and would only hold trials
// until the other end starts a handshake again (about 15 seconds).
func (s *e2eSessions) receive(b []byte, n *e2eNode) {
	if !s.peer(n, false, initiation(b)) {
		return
	}
	if !s.bind.queue(b, s.bind.endpointOf(n.id)) {
		s.stats.DroppedQueue.Add(1)
		return
	}
	s.stats.Received.Add(1)
}

// initiation: b is a WireGuard handshake initiation (message type 1).
func initiation(b []byte) bool {
	return len(b) == 148 && b[0] == 1 && b[1] == 0 && b[2] == 0 && b[3] == 0
}

// E2EStatus is what status.json shows of the sessions.
type E2EStatus struct {
	Sessions         int    `json:"sessions"`
	Limit            int    `json:"limit"`
	Created          uint64 `json:"created"`
	Evicted          uint64 `json:"evicted"`
	Sent             uint64 `json:"sent"`
	Received         uint64 `json:"received"`
	DroppedExhausted uint64 `json:"droppedExhausted"`
	DroppedQueue     uint64 `json:"droppedQueue"`
	// DroppedSource: decrypted packets whose destination is not this node,
	// or plain packets for it from a node that is not their outer peer.
	DroppedSource uint64 `json:"droppedSource"`
	DroppedBad    uint64 `json:"droppedBad"`
	InnerMTU      int    `json:"innerMtu"`
	// Peers are the sessions, by node.
	Peers []E2EPeerStatus `json:"peers,omitempty"`
}

type E2EPeerStatus struct {
	ID                    string `json:"id"`
	LastHandshakeUnixNano string `json:"lastHandshakeUnixNano"`
	RxBytes               uint64 `json:"rxBytes"`
	TxBytes               uint64 `json:"txBytes"`
	IdleMs                int64  `json:"idleMs"`
}

func (s *e2eSessions) status() *E2EStatus {
	s.mu.Lock()
	n := len(s.peers)
	now := s.now()
	s.applyProofs(now)
	var peers []E2EPeerStatus
	keys := map[string]string{}
	for id, p := range s.peers {
		peers = append(peers, E2EPeerStatus{ID: id, IdleMs: now.Sub(time.Unix(0, p.used.Load())).Milliseconds()})
		keys[id] = p.node.key
	}
	s.mu.Unlock()
	if len(peers) > 0 {
		_, states := deviceState(s.dev)
		for i := range peers {
			peers[i].LastHandshakeUnixNano = "0"
			if st := states[keys[peers[i].ID]]; st != nil {
				peers[i].LastHandshakeUnixNano = strconv.FormatInt(st.handshakeNano, 10)
				peers[i].RxBytes, peers[i].TxBytes = st.rx, st.tx
			}
		}
		slices.SortFunc(peers, func(a, b E2EPeerStatus) int { return strings.Compare(a.ID, b.ID) })
	}
	return &E2EStatus{Peers: peers, Sessions: n, Limit: s.limits.device.MaxPeers, Created: s.stats.Created.Load(), Evicted: s.stats.Evicted.Load(),
		Sent: s.stats.Sent.Load(), Received: s.stats.Received.Load(), DroppedExhausted: s.stats.DroppedExhausted.Load(),
		DroppedQueue: s.stats.DroppedQueue.Load(), DroppedSource: s.stats.DroppedSource.Load(), DroppedBad: s.stats.DroppedBad.Load(),
		InnerMTU: int(s.tun.mtu.Load())}
}

// handshake is the last handshake with node id, 0 for none or no session.
func (s *e2eSessions) handshake(id string) int64 {
	s.mu.Lock()
	p := s.peers[id]
	s.mu.Unlock()
	if p == nil {
		return 0
	}
	_, peers := deviceState(s.dev)
	if st := peers[p.node.key]; st != nil {
		return st.handshakeNano
	}
	return 0
}

// rerouted starts a handshake with each node whose next hop changed: one
// lost on the old way (a punched link that went down while the other end
// still used it) would otherwise be tried again only after RekeyTimeout.
func (s *e2eSessions) rerouted(old, r *routing) {
	if old == nil {
		return
	}
	s.mu.Lock()
	var keys []string
	for _, p := range s.peers {
		if old.hop(p.node.addr) != r.hop(p.node.addr) {
			keys = append(keys, p.node.key)
		}
	}
	s.mu.Unlock()
	for _, k := range keys {
		kick(s.dev, k)
	}
}

func (s *e2eSessions) close() {
	s.dev.Close()
}

// innerTUN is the inner device's TUN: this node's packets for other nodes
// in, decrypted packets of theirs out.
type innerTUN struct {
	out     chan innerPacket
	deliver func(bufs [][]byte, offset int)
	// authenticated is told of each packet written: the node it came from and its key.
	authenticated func(id string, key [32]byte)
	mtu           atomic.Int32
	events        chan tun.Event
	closed        chan struct{}
	once          sync.Once
}

type innerPacket struct {
	b    []byte
	ep   conn.Endpoint
	hash uint64
}

func newInnerTUN(mtu, queue int, deliver func(bufs [][]byte, offset int)) *innerTUN {
	t := &innerTUN{out: make(chan innerPacket, queue), deliver: deliver, events: make(chan tun.Event, 4), closed: make(chan struct{})}
	t.mtu.Store(int32(mtu))
	t.events <- tun.EventUp
	return t
}

func (t *innerTUN) setMTU(mtu int) {
	if t.mtu.Swap(int32(mtu)) != int32(mtu) {
		select {
		case t.events <- tun.EventMTUUpdate:
		default:
		}
	}
}

// queue takes b, which the caller no longer uses; false: the queue is full.
func (t *innerTUN) queue(b []byte, ep conn.Endpoint, hash uint64) bool {
	select {
	case t.out <- innerPacket{b, ep, hash}:
		return true
	default:
		return false
	}
}

func (t *innerTUN) File() *os.File { return nil }

func (t *innerTUN) Read(bufs [][]byte, sizes []int, offset int) (int, error) {
	return t.ReadWithMetadata(bufs, sizes, offset, nil)
}

// ReadWithMetadata gives each packet its node's endpoint and the hash of
// its flow, which the scheduler queues the encrypted packet by.
func (t *innerTUN) ReadWithMetadata(bufs [][]byte, sizes []int, offset int, metadata []conn.SendMetadata) (int, error) {
	select {
	case <-t.closed:
		return 0, os.ErrClosed
	case p := <-t.out:
		sizes[0] = copy(bufs[0][offset:], p.b)
		if len(metadata) > 0 {
			metadata[0] = conn.SendMetadata{Endpoint: p.ep, ID: p.hash}
		}
		return 1, nil
	}
}

func (t *innerTUN) Write(bufs [][]byte, offset int) (int, error) {
	t.deliver(bufs, offset)
	return len(bufs), nil
}

// WriteWithMetadata (conn.MetadataWriter): Write, told which node each
// packet is from. Only data the inner device decrypted, with a fresh
// counter and a source its sender may use, comes here.
func (t *innerTUN) WriteWithMetadata(bufs [][]byte, offset int, metadata []conn.ReceiveMetadata) (int, error) {
	if t.authenticated != nil {
		for _, md := range metadata {
			if ep, ok := md.Endpoint.(*e2eEndpoint); ok {
				t.authenticated(ep.id, md.PublicKey)
			}
		}
	}
	return t.Write(bufs, offset)
}

func (t *innerTUN) MTU() (int, error)        { return int(t.mtu.Load()), nil }
func (t *innerTUN) Name() (string, error)    { return "e2e", nil }
func (t *innerTUN) Events() <-chan tun.Event { return t.events }
func (t *innerTUN) BatchSize() int           { return 1 }
func (t *innerTUN) Close() error {
	t.once.Do(func() { close(t.closed) })
	return nil
}

// innerBind is the inner device's bind: what it sends gets the outer
// header and goes to the scheduler; outer packets for this node come in.
type innerBind struct {
	limits e2eLimits
	send   func(b []byte, hash uint64)
	in     chan innerPacket
	seed   maphash.Seed

	mu     sync.Mutex
	self   netip.Addr
	table  *e2eTable
	eps    map[string]*e2eEndpoint
	closed chan struct{}
}

const e2eScheme = "e2e:"

// e2eEndpoint is a node; the inner device's peers are fixed to theirs.
type e2eEndpoint struct {
	id   string
	addr netip.Addr
}

func (e *e2eEndpoint) ClearSrc()           {}
func (e *e2eEndpoint) SrcToString() string { return "" }
func (e *e2eEndpoint) DstToString() string { return e2eScheme + e.id }
func (e *e2eEndpoint) DstToBytes() []byte  { b := e.addr.As4(); return b[:] }
func (e *e2eEndpoint) DstIP() netip.Addr   { return e.addr }
func (e *e2eEndpoint) SrcIP() netip.Addr   { return netip.Addr{} }

func newInnerBind(self netip.Addr, l e2eLimits, send func(b []byte, hash uint64)) *innerBind {
	b := &innerBind{limits: l, send: send, in: make(chan innerPacket, l.queue), seed: maphash.MakeSeed(), self: self,
		eps: map[string]*e2eEndpoint{}, closed: make(chan struct{})}
	close(b.closed)
	return b
}

func (b *innerBind) setSelf(self netip.Addr, t *e2eTable) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.self, b.table = self, t
	for id, ep := range b.eps {
		if n := t.byID[id]; n == nil || n.addr != ep.addr {
			delete(b.eps, id)
		}
	}
}

// endpointOf is node id's endpoint, nil for a node not in the config.
func (b *innerBind) endpointOf(id string) conn.Endpoint {
	b.mu.Lock()
	defer b.mu.Unlock()
	if ep := b.eps[id]; ep != nil {
		return ep
	}
	if b.table == nil || b.table.byID[id] == nil {
		return nil
	}
	ep := &e2eEndpoint{id: id, addr: b.table.byID[id].addr}
	b.eps[id] = ep
	return ep
}

func (b *innerBind) DeviceLimits() conn.DeviceLimits { return b.limits.device }

func (b *innerBind) Open(uint16) ([]conn.ReceiveFunc, uint16, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	select {
	case <-b.closed:
	default:
		return nil, 0, conn.ErrBindAlreadyOpen
	}
	closed := make(chan struct{})
	b.closed = closed
	return []conn.ReceiveFunc{func(packets [][]byte, sizes []int, eps []conn.Endpoint) (int, error) {
		select {
		case <-closed:
			return 0, net.ErrClosed
		case p := <-b.in:
			sizes[0] = copy(packets[0], p.b)
			eps[0] = p.ep
			return 1, nil
		}
	}}, 0, nil
}

func (b *innerBind) Close() error {
	b.mu.Lock()
	defer b.mu.Unlock()
	select {
	case <-b.closed:
	default:
		close(b.closed)
	}
	return nil
}

func (b *innerBind) SetMark(uint32) error { return nil }
func (b *innerBind) BatchSize() int       { return 1 }

func (b *innerBind) ParseEndpoint(s string) (conn.Endpoint, error) {
	id, ok := strings.CutPrefix(s, e2eScheme)
	if !ok {
		return nil, conn.ErrWrongEndpointType
	}
	if ep := b.endpointOf(id); ep != nil {
		return ep, nil
	}
	return nil, fmt.Errorf("no node %q", id)
}

// queue takes b, an outer packet's payload, copying it; false: full.
func (b *innerBind) queue(p []byte, ep conn.Endpoint) bool {
	if ep == nil {
		return false
	}
	select {
	case b.in <- innerPacket{b: append([]byte(nil), p...), ep: ep}:
		return true
	default:
		return false
	}
}

func (b *innerBind) Send(bufs [][]byte, ep conn.Endpoint) error {
	return b.SendWithMetadata(bufs, [32]byte{}, conn.SendMetadata{Endpoint: ep})
}

// SendWithMetadata wraps each WireGuard message in the outer header to
// its node and queues it by the inner flow's hash; handshakes and
// keepalives, without one, by the node.
func (b *innerBind) SendWithMetadata(bufs [][]byte, _ [32]byte, md conn.SendMetadata) error {
	ep, ok := md.Endpoint.(*e2eEndpoint)
	if !ok {
		return conn.ErrWrongEndpointType
	}
	b.mu.Lock()
	self := b.self
	b.mu.Unlock()
	hash := md.ID
	if hash == 0 {
		a := ep.addr.As4()
		hash = maphash.Bytes(b.seed, a[:])
	}
	for _, m := range bufs {
		b.send(outerPacket(self, ep.addr, m), hash)
	}
	return nil
}

// outerPacket is msg in an IPv4 header of protocol 254 from src to dst.
func outerPacket(src, dst netip.Addr, msg []byte) []byte {
	p := make([]byte, 20+len(msg))
	p[0], p[8], p[9] = 0x45, 64, protoE2E
	binary.BigEndian.PutUint16(p[2:4], uint16(len(p)))
	binary.BigEndian.PutUint16(p[6:8], 0x4000) // DF
	s, d := src.As4(), dst.As4()
	copy(p[12:16], s[:])
	copy(p[16:20], d[:])
	binary.BigEndian.PutUint16(p[10:12], headerChecksum(p[:20]))
	copy(p[20:], msg)
	return p
}
