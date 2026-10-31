package health

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"fmt"
	"net"
	"net/netip"
	"slices"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
)

// A punched link is a UDP path two nodes behind NAT open toward each other:
// two members (a direct link), two relays (a relay link), or a member and
// the relay of one of its entry candidates. Each end learns its public
// address from reflectors and sends its addresses to the other through
// relays (offers).
//
// An attempt opens the path. A Linux NAT that takes a packet for a mapping
// before its own host sent toward that peer keeps the packet's flow and
// moves the host's own packets to another port, and every further packet
// from the peer keeps that flow alive; so the two ends never send to each
// other's public address at the same time. One end, the opener, first sends
// primes with a TTL meant to expire between the two NATs, which opens its
// own NAT only, and then says so; only then does the other end send its
// handshakes to the opener's public addresses, which now pass. The opener
// answers through the path they arrive on and sends nothing else to the
// other's public addresses. LAN addresses need none of this. The driving
// end numbers the attempts and the opener alternates; after a failed one
// neither end sends to the other's public addresses for punchQuiet, so that
// what the failure left in the NATs expires.
//
// A path that authenticates is then probed like any other; routes move to a
// direct or relay link only once both ends report it ready, and back when
// either stops doing so. An entry candidate gets the path as its endpoint
// and from then on is like any other candidate.
//
// Which path: a LAN address of the peer's on one of this node's own
// networks at once; otherwise every path that authenticated within a short
// window is pinged and the fastest wins, for a LAN address may be reached
// the long way round (through a VPN between two sites, say). What kind of
// address a path goes to only breaks a tie. While a link is up its driver
// now and then makes a probe attempt, which both ends run beside the link;
// a path clearly faster than the link's is prepared next to it and
// replaces it once ready. The driver chooses for both ends of a direct or
// relay link: the other end takes the path the driver prepares, as its link
// or as the next one, so that the two ends never settle on two ways.

const (
	directUp       = "direct"
	directPunching = "punching"
	directRelayed  = "relayed"

	errNoReflector  = "no reflector"
	errPunchTimeout = "punch timeout"
	errDirectLost   = "direct path lost"

	punchDirect = "direct" // a member's direct link to another member
	punchRelay  = "relay"  // a relay link between two relays
	punchEntry  = "entry"  // a member's entry candidate through a relay
	punchSite   = "site"   // the relay end of a member's punched entry

	// WireGuard repeats an unanswered initiation every 5 s; the window
	// covers three after the opener has primed.
	punchWindow = 12 * time.Second
	primeFor    = 1500 * time.Millisecond
	primeEvery  = 250 * time.Millisecond
	// Linux keeps an unanswered UDP flow for 30 s; the other end may also
	// have ended its attempt a little later.
	punchQuiet = 37 * time.Second
	stateEvery = time.Second
	// A path that authenticated has this long to be ready on both ends.
	directSettle  = 20 * time.Second
	reflectEvery  = 60 * time.Second
	reflectRetry  = 5 * time.Second
	offerRetry    = 2 * time.Second
	maxCandidates = 8
	maxLAN        = 6
	maxSites      = 64
	siteIdle      = 10 * time.Minute
	maxSignalHops = 3

	// Paths are compared for this long after the first authenticates, or
	// after the opener primed if that is later.
	compareFor    = 3 * time.Second
	trialEvery    = 250 * time.Millisecond
	maxTrialPings = 16
	// A path replaces a link that works only when this much faster: by a
	// fifth and by switchGain, over probeSamples answers at least.
	switchGain   = 5 * time.Millisecond
	probeSamples = 3
	// Two paths within this much of each other, or a tenth of the faster's
	// round trip, are as fast: the kind of address decides.
	tieMargin = time.Millisecond
	// The end that does not drive waits this much longer than the attempt
	// for the path the driver chooses, which may come at its very end.
	followGrace = 8 * time.Second
	// A link not on the peer's LAN looks for a faster path after this at
	// first, then twice as long each time up to ReprobeEvery.
	reprobeFirst = time.Minute
	// Public addresses this node offers at most: what different reflectors
	// saw, for a NAT may map the port anew for each destination.
	maxPublic = 3
	// A path a link left stays this long, for the other end to follow.
	retireAfter = 30 * time.Second
	// A punched entry candidate's configured endpoint is sent an initiation
	// this often while the entry is not up and no attempt runs: WireGuard
	// sends one at most every 5 s.
	dialEvery = 5 * time.Second
)

// ReprobeEvery is how often a link's driver looks for a faster path; a
// variable for tests.
var ReprobeEvery = 10 * time.Minute

// SimultaneousPunch is for tests only: both ends send to every address of
// the other at once, as before opening was introduced.
var SimultaneousPunch atomic.Bool

// primeTTLs by attempt: 4 passes a home router and the ISP's first hops
// and dies well before the other end's NAT; 3 and 6 cover a shorter and a
// longer way out.
var primeTTLs = [...]int{4, 4, 3, 3, 6, 6}

type directState struct {
	peer, kind string
	candidate  string         // entry: the candidate's ID
	configured netip.AddrPort // the link endpoint, if the peer has one
	state, via string
	lastError  string
	since      time.Time
	attempts   int
	backoff    time.Duration
	next       time.Time // driver: the next attempt
	lastScan   time.Time
	link       *candidateState
	local      bool // link ready on this end, as last told to the peer
	remoteUp   bool // link ready on the peer, as it last said

	remote    map[netip.AddrPort]string
	remoteKey string // the peer's last offer
	// remoteSession and remoteGen: of the newest offer taken, so an older
	// one of the same session is not.
	remoteSession string
	remoteGen     uint64
	acked         string // this end's offer the peer has confirmed
	ackDue        bool
	offerNext     time.Time

	driver         bool
	session        string // the driving end's, with n
	n              uint64
	active         bool
	opener         bool
	start, until   time.Time
	primed, opened bool
	lastPrime      time.Time
	lastState      time.Time
	touched        time.Time // site: the last signal from the member
	failedAt       time.Time
	lastDial       time.Time // entry: the last initiation to configured
	dialErr        string    // and why it could not be sent, once told

	probe       bool // this attempt looks for a faster path beside the link
	trials      map[Path]*pathTrial
	firstAuth   time.Time
	reprobe     time.Time       // driver: the next probe attempt
	reprobeGap  time.Duration   // and the time to the one after
	upcoming    *candidateState // a faster path, until it replaces link
	upcomingVia string
	retired     []retiredPath // what links left, released later
}

// retiredPath is a path a link left for another, kept until release for
// the other end, which moves a little later and probes it till then.
type retiredPath struct {
	path    Path
	release time.Time
}

// pathTrial measures a path that authenticated during an attempt.
type pathTrial struct {
	remote  netip.AddrPort
	token   string
	created time.Time
	last    time.Time
	pings   int
	sent    map[uint64]trialPing
	best    time.Duration   // the fastest answer, 0 before one
	samples []time.Duration // every answer's round trip
}

// rtt is the median of the answers' round trips, 0 before one.
func (t *pathTrial) rtt() time.Duration {
	if len(t.samples) == 0 {
		return 0
	}
	s := slices.Clone(t.samples)
	slices.Sort(s)
	return s[len(s)/2]
}

type trialPing struct {
	nonce string
	at    time.Time
}

// reflected is what discovery found: the first reflector's view, and the
// others' that differ from it.
type reflected struct {
	addr netip.AddrPort
	also []netip.AddrPort
}

type offerRate struct {
	tokens float64
	at     time.Time
}

// localAddrs are this machine's IPv4 addresses a peer on the same network
// could reach, outside the mesh.
var localAddrs = func(mesh netip.Prefix) []netip.Addr {
	var out []netip.Addr
	ifaces, _ := net.Interfaces()
	for _, ifc := range ifaces {
		if ifc.Flags&net.FlagUp == 0 || ifc.Flags&net.FlagLoopback != 0 {
			continue
		}
		addrs, _ := ifc.Addrs()
		for _, a := range addrs {
			n, ok := a.(*net.IPNet)
			if !ok || n.IP.To4() == nil {
				continue
			}
			ip, _ := netip.AddrFromSlice(n.IP.To4())
			if ip.IsLoopback() || ip.IsLinkLocalUnicast() || mesh.Contains(ip) {
				continue
			}
			out = append(out, ip)
		}
	}
	slices.SortFunc(out, func(a, b netip.Addr) int { return a.Compare(b) })
	return slices.Compact(out)
}

// localNets are the IPv4 networks of this machine's interfaces, outside
// the mesh: a peer's address in one is on the same link.
var localNets = func(mesh netip.Prefix) []netip.Prefix {
	var out []netip.Prefix
	ifaces, _ := net.Interfaces()
	for _, ifc := range ifaces {
		if ifc.Flags&net.FlagUp == 0 || ifc.Flags&net.FlagLoopback != 0 {
			continue
		}
		addrs, _ := ifc.Addrs()
		for _, a := range addrs {
			n, ok := a.(*net.IPNet)
			if !ok || n.IP.To4() == nil {
				continue
			}
			ip, _ := netip.AddrFromSlice(n.IP.To4())
			ones, _ := n.Mask.Size()
			if ip.IsLoopback() || ip.IsLinkLocalUnicast() || mesh.Contains(ip) || ones == 32 {
				continue
			}
			out = append(out, netip.PrefixFrom(ip, ones).Masked())
		}
	}
	return out
}

func (m *Manager) onLink(a netip.Addr) bool {
	r := &m.reach
	if now := time.Now(); now.Sub(r.netsAt) > time.Second {
		r.nets, r.netsAt = localNets(m.cfg.Prefix), now
	}
	return slices.ContainsFunc(r.nets, func(p netip.Prefix) bool { return p.Contains(a.Unmap()) })
}

func parseEndpointAddr(endpoint string) netip.AddrPort {
	_, rest, ok := strings.Cut(endpoint, "://")
	if !ok {
		return netip.AddrPort{}
	}
	a, err := net.ResolveUDPAddr("udp4", rest)
	if err != nil {
		return netip.AddrPort{}
	}
	ap := a.AddrPort()
	return netip.AddrPortFrom(ap.Addr().Unmap(), ap.Port())
}

func (m *Manager) configureDirects(c Config, now time.Time) {
	next := map[string]*directState{}
	add := func(peer, kind string) *directState {
		d := m.directs[peer]
		if d == nil || d.kind != kind {
			if d != nil {
				m.dropLink(d)
			}
			d = &directState{peer: peer, kind: kind, state: directRelayed, since: now, next: now}
		}
		d.driver = kind == punchEntry || (kind != punchSite && c.Self < peer)
		next[peer] = d
		return d
	}
	kind := punchDirect
	if c.Role == "relay" {
		kind = punchRelay
	}
	for _, l := range c.Links {
		if l.Punch {
			add(l.Peer, kind).configured = parseEndpointAddr(l.Endpoint)
		}
	}
	for _, cand := range c.Candidates {
		if cand.Punch && c.Role != "relay" {
			d := add(cand.Peer, punchEntry)
			changed := d.configured != parseEndpointAddr(cand.Endpoint)
			d.candidate, d.configured = cand.ID, parseEndpointAddr(cand.Endpoint)
			if p := m.candidates[cand.ID]; changed && p != nil && p.config.Punch && p.config.Endpoint != "" && p.track.stats.Failures >= c.Parameters.ConsecutiveFailures {
				m.failed(p, now)
				p.retry, d.next = now, now
			}
		}
	}
	for id, d := range m.directs {
		if d.kind == punchSite && next[id] == nil && slices.Contains(m.store.state.Authorized[id], c.Self) {
			next[id] = d
		}
	}
	for id, d := range m.directs {
		if next[id] == nil {
			m.dropLink(d)
		}
	}
	if len(next) > 0 && (c.Port != m.cfg.Port || !slices.Equal(c.Reflectors, m.cfg.Reflectors)) {
		m.reflectNext = now
	}
	m.directs = next
}

// Direct says whether routes for the peer go over its punched link.
func (m *Manager) Direct(peer string) bool {
	m.mu.RLock()
	defer m.mu.RUnlock()
	d := m.directs[peer]
	return d != nil && d.link != nil && d.state == directUp
}

func (m *Manager) directOf(p *candidateState) *directState {
	for _, d := range m.directs {
		if d.link == p || (p != nil && d.upcoming == p) {
			return d
		}
	}
	return nil
}

func (m *Manager) offerKey() string {
	return fmt.Sprintf("%s/%d", m.session, m.offerGen)
}

func randomID() [8]byte {
	var id [8]byte
	rand.Read(id[:])
	return id
}

func (m *Manager) discover(now time.Time) {
	if len(m.directs) == 0 || m.reflecting || now.Before(m.reflectNext) {
		return
	}
	m.reflecting = true
	servers := slices.Clone(m.cfg.Reflectors)
	go func() {
		var found reflected
		for _, s := range servers {
			a, err := net.ResolveUDPAddr("udp4", s)
			if err != nil {
				continue
			}
			ctx, cancel := context.WithTimeout(m.ctx, time.Second)
			seen, err := m.backend.Reflect(ctx, a.AddrPort(), randomID())
			cancel()
			switch {
			case err != nil:
			case !found.addr.IsValid():
				found.addr = seen
			case seen != found.addr && !slices.Contains(found.also, seen):
				found.also = append(found.also, seen)
			}
		}
		select {
		case m.input <- found:
		case <-m.ctx.Done():
		}
	}()
}

// reflectedAddr takes a discovery result and offers the peers new
// addresses if this node's changed.
func (m *Manager) reflectedAddr(v reflected, now time.Time) {
	m.reflecting = false
	m.discovered = true
	m.reflectNext = now.Add(jitter(reflectEvery, .1))
	if v.addr.IsValid() {
		m.reflected, m.alsoSeen = v.addr, v.also
	} else {
		m.reflectNext = now.Add(reflectRetry)
	}
	m.updateLocal()
}

// updateLocal makes the addresses this node offers: its LAN addresses and
// what the reflectors saw, every different one; a NAT that maps the port
// anew for each destination may have given the peer one of them.
func (m *Manager) updateLocal() {
	var local, public []string
	port := m.cfg.Port
	if port == 0 {
		port = m.backend.UDPPort()
	}
	if m.reflected.IsValid() {
		for _, a := range m.confirmed(append([]netip.AddrPort{m.reflected}, m.alsoSeen...), time.Now()) {
			if c := "public:" + a.String(); len(public) < maxPublic && !slices.Contains(public, c) {
				public = append(public, c)
			}
		}
	}
	for _, a := range localAddrs(m.cfg.Prefix) {
		if len(local) == min(maxLAN, maxCandidates-len(public)) || port == 0 {
			break
		}
		local = append(local, "lan:"+netip.AddrPortFrom(a, uint16(port)).String())
	}
	for _, c := range public {
		if !slices.Contains(local, "lan:"+strings.TrimPrefix(c, "public:")) {
			local = append(local, c)
		}
	}
	if !slices.Equal(local, m.local) {
		m.local = local
		m.offerGen++
	}
}

// signalPath is where a signal for a punched link goes first: for a
// member, its entry or another candidate that is up; for a relay, toward
// the addressee.
func (m *Manager) signalPath(to string, now time.Time) (string, Path, bool) {
	if m.cfg.Role == "relay" {
		return m.hopToward(to, "", now)
	}
	if p := m.candidates[m.active]; p != nil && p.path.ID != "" {
		return p.config.Peer, p.path, true
	}
	for _, c := range m.cfg.Candidates {
		if p := m.candidates[c.ID]; p != nil && p.ready && p.path.ID != "" {
			return p.config.Peer, p.path, true
		}
	}
	return "", Path{}, false
}

// hopToward is the next relay, or the member itself, on the way to node
// to; except is where the signal came from.
func (m *Manager) hopToward(to, except string, now time.Time) (string, Path, bool) {
	if m.roleOf(to) == "member" {
		owner, ok := m.owners.owner(to, now)
		if !ok {
			return "", Path{}, false
		}
		if owner == m.cfg.Self {
			path, ok := m.peerPath(to)
			return to, path, ok
		}
		to = owner
	}
	if path, ok := m.peerPath(to); ok {
		return to, path, true
	}
	if hop := m.cfg.NextHops[to]; hop != "" && hop != except && hop != to {
		if path, ok := m.peerPath(hop); ok {
			return hop, path, true
		}
	}
	// The configured next hop may be the punched link itself, not up.
	for _, l := range m.cfg.Links {
		if m.owners.relays[l.Peer] && l.Peer != except && l.Peer != to {
			if path, ok := m.peerPath(l.Peer); ok {
				return l.Peer, path, true
			}
		}
	}
	return "", Path{}, false
}

func (m *Manager) roleOf(id string) string {
	for _, n := range m.cfg.Nodes {
		if n.ID == id {
			return n.Role
		}
	}
	return ""
}

func (m *Manager) relaysOf(member string) []string {
	for _, n := range m.cfg.Nodes {
		if n.ID == member {
			return n.Relays
		}
	}
	return nil
}

// signalPair says whether from and to may be the two ends of a punched link.
func (m *Manager) signalPair(from, to string) bool {
	rf, rt := m.roleOf(from), m.roleOf(to)
	if from == m.cfg.Self {
		rf = m.cfg.Role
	}
	switch {
	case from == to || rf == "" || rt == "":
		return false
	case rf == rt:
		return true
	case rf == "member":
		return slices.Contains(m.relaysOf(from), to)
	default:
		return slices.Contains(m.relaysOf(to), from)
	}
}

// routeSignal takes an offer or punch state: on its addressee it says
// whether it came the way signals come; on a relay in between it forwards
// it and returns false. signed checks From's signature, on the addressee and
// on every relay before it forwards, so a forged signal goes no further than
// the first hop.
func (m *Manager) routeSignal(r Received, v message, from, to string, hops *int, signed func() bool, now time.Time) bool {
	if from == "" || to == "" || from == m.cfg.Self {
		return false
	}
	if to == m.cfg.Self {
		return (m.owners.relays[r.Peer] || (r.Peer == from && m.cfg.Role == "relay")) && m.verified(signed(), v.Type, from, r.Peer)
	}
	if m.cfg.Role != "relay" || *hops >= maxSignalHops || !m.signalPair(from, to) {
		return false
	}
	fromRelay := m.owners.relays[r.Peer] && m.linked(r.Peer)
	if !fromRelay && (from != r.Peer || !m.authorizedPeer(r.Peer)) {
		return false
	}
	if !m.offerAllowed(from, now) || !m.verified(signed(), v.Type, from, r.Peer) {
		return false
	}
	hop, path, ok := m.hopToward(to, r.Peer, now)
	if !ok {
		return false
	}
	*hops++
	m.send(hop, path, v, 0)
	return false
}

// offerAllowed keeps one node from making relays forward much: offers are
// resent only until answered, punch states only while an attempt lasts.
func (m *Manager) offerAllowed(from string, now time.Time) bool {
	r, ok := m.offerRates[from]
	if !ok {
		r = offerRate{tokens: 8, at: now}
	}
	r.tokens = min(8, r.tokens+now.Sub(r.at).Seconds()*3)
	r.at = now
	if r.tokens < 1 {
		m.offerRates[from] = r
		return false
	}
	r.tokens--
	m.offerRates[from] = r
	return true
}

// signalState is the punched state a signal from peer is for, made on a
// relay for a member that punches an entry through it.
func (m *Manager) signalState(peer string, now time.Time) *directState {
	if d := m.directs[peer]; d != nil {
		return d
	}
	if m.cfg.Role != "relay" || m.roleOf(peer) != "member" || !slices.Contains(m.store.state.Authorized[peer], m.cfg.Self) {
		return nil
	}
	for id, d := range m.directs {
		if d.kind == punchSite && now.Sub(d.touched) > siteIdle && !d.active {
			delete(m.directs, id)
		}
	}
	if len(m.directs) >= maxSites {
		return nil
	}
	d := &directState{peer: peer, kind: punchSite, state: directRelayed, since: now}
	m.directs[peer] = d
	if !m.discovered {
		m.reflectNext = now
	}
	return d
}

func (m *Manager) sendOffer(d *directState, now time.Time) {
	d.offerNext = now.Add(offerRetry)
	if !m.discovered {
		return
	}
	hop, path, ok := m.signalPath(d.peer, now)
	if !ok {
		return
	}
	mine := m.offerKey()
	o := Offer{From: m.cfg.Self, To: d.peer, Session: m.session, Gen: m.offerGen, Ack: d.remoteKey, Reply: d.acked == mine, Candidates: m.local}
	o.Sig = offerInput(m.cfg.Network, &o).SignString(m.cfg.SignKey)
	if m.send(hop, path, message{Type: "offer", Offer: &o}, 0) == nil {
		d.ackDue = false
	}
}

func (m *Manager) receiveOffer(r Received, v message, now time.Time) {
	o := v.Offer
	if o == nil || len(o.Candidates) > maxCandidates ||
		!m.routeSignal(r, v, o.From, o.To, &o.Hops, func() bool { return offerInput(m.cfg.Network, o).VerifyString(m.signKeys[o.From], o.Sig) }, now) {
		return
	}
	if d := m.signalState(o.From, now); d != nil && m.freshSession(o.From, o.Session) {
		d.touched = now
		m.acceptOffer(d, *o, now)
	}
}

// sessionEpoch is the epoch a session's name starts with ("<epoch>-<nonce>"),
// 0 for a node from before epochs, whose sessions are a bare nonce.
func sessionEpoch(session string) uint64 {
	epoch, _, ok := strings.Cut(session, "-")
	if !ok {
		return 0
	}
	n, err := strconv.ParseUint(epoch, 10, 64)
	if err != nil {
		return 0
	}
	return n
}

// freshSession tells whether a session of peer's, in a signal it signed, is
// not older than the newest one seen of it, and takes it if it is newer.
// Within a session gen and attempt keep a replay from going back; an older
// session, recorded before peer restarted, would start over and take the
// link down. A session of a node from before epochs is taken only from one
// not yet seen with them.
func (m *Manager) freshSession(peer, session string) bool {
	epoch := sessionEpoch(session)
	if epoch < m.epochs[peer] {
		return false
	}
	if epoch > m.epochs[peer] {
		if m.epochs == nil {
			m.epochs = map[string]uint64{}
		}
		m.epochs[peer] = epoch
	}
	return true
}

// pruneEpochs keeps the epochs of the nodes whose signing keys stay in keys.
// A reinstalled node may start its epochs over; what it signed before no
// longer verifies.
func (m *Manager) pruneEpochs(keys map[string]ed25519.PublicKey) {
	for id := range m.epochs {
		if !keys[id].Equal(m.signKeys[id]) {
			delete(m.epochs, id)
		}
	}
}

func (m *Manager) acceptOffer(d *directState, o Offer, now time.Time) {
	if o.Session == d.remoteSession && o.Gen < d.remoteGen {
		// An older offer of the same session, replayed: its addresses are
		// no longer the peer's.
		return
	}
	d.remoteSession, d.remoteGen = o.Session, o.Gen
	key := fmt.Sprintf("%s/%d", o.Session, o.Gen)
	if key != d.remoteKey {
		d.remoteKey = key
		d.remote = map[netip.AddrPort]string{}
		for _, c := range o.Candidates {
			kind, addr, _ := strings.Cut(c, ":")
			a, err := netip.ParseAddrPort(addr)
			if err != nil || (kind != "lan" && kind != "public") || !a.Addr().Is4() || a.Port() == 0 || a.Addr().IsUnspecified() || a.Addr().IsLoopback() || a.Addr().IsMulticast() {
				continue
			}
			d.remote[a] = kind
		}
		d.ackDue = true
		if d.active {
			// Addresses that changed during an attempt join it.
			m.punchAddrs(d, now)
		} else if d.driver && d.link == nil && d.kind != punchSite {
			d.next = maxTime(now, d.failedAt.Add(punchQuiet))
		}
	}
	mine := m.offerKey()
	if o.Ack == mine {
		d.acked = mine
	}
	if !o.Reply || o.Ack != mine {
		d.ackDue = true
	}
}

// addrs splits the peer's addresses into those either end may send to at
// once (configured endpoint, LAN) and its public ones, without this node's
// own addresses.
// addrs are where this end may send handshakes: normal ones at once,
// public ones once the opener primed. A LAN address of the peer's off this
// node's networks may be a long way round and goes with the public ones, so
// that the first copy of one initiation to arrive, by the fastest way,
// authenticates; with the public ones it also counts as one.
func (m *Manager) addrs(d *directState) (normal, public []netip.AddrPort) {
	own := func(a netip.AddrPort) bool {
		return slices.ContainsFunc(m.local, func(c string) bool { return strings.HasSuffix(c, ":"+a.String()) })
	}
	add := func(list *[]netip.AddrPort, a netip.AddrPort) {
		if a.IsValid() && !own(a) && !slices.Contains(normal, a) && !slices.Contains(public, a) && len(normal)+len(public) < maxCandidates {
			*list = append(*list, a)
		}
	}
	add(&normal, d.configured)
	for _, kind := range []string{"lan", "public"} {
		var list []netip.AddrPort
		for a, k := range d.remote {
			if k == kind {
				list = append(list, a)
			}
		}
		slices.SortFunc(list, func(a, b netip.AddrPort) int { return a.Compare(b) })
		for _, a := range list {
			if kind == "lan" && m.onLink(a.Addr()) {
				add(&normal, a)
			} else {
				add(&public, a)
			}
		}
	}
	return normal, public
}

// openerOf is the node that primes in attempt n: the lesser ID first.
func (m *Manager) openerOf(peer string, n uint64) string {
	first, second := m.cfg.Self, peer
	if second < first {
		first, second = second, first
	}
	if n%2 == 1 {
		return first
	}
	return second
}

func primeTTL(n uint64) int { return primeTTLs[(n-1)%uint64(len(primeTTLs))] }

// begin starts attempt n of the driving end's session; a probe attempt
// keeps the link that is up.
func (m *Manager) begin(d *directState, session string, n uint64, now time.Time, probe bool) {
	probe = probe && d.state == directUp && d.link != nil && d.upcoming == nil
	if !probe {
		if d.state == directUp && d.link != nil {
			m.directLost(d, now)
		} else if d.link != nil {
			m.dropLink(d)
		}
	}
	m.endTrials(d)
	d.session, d.n, d.active, d.probe = session, n, true, probe
	d.opener = m.openerOf(d.peer, n) == m.cfg.Self
	d.start, d.until = now, now.Add(primeFor+punchWindow)
	d.primed, d.opened = false, false
	d.lastPrime, d.lastState = time.Time{}, time.Time{}
	if !probe {
		d.attempts++
		d.state, d.since = directPunching, now
	}
	_, public := m.addrs(d)
	if d.opener && len(public) == 0 {
		d.primed = true
	}
	if SimultaneousPunch.Load() {
		d.opener, d.opened = false, true
	}
	m.punchAddrs(d, now)
	if d.opener && len(public) > 0 {
		d.lastPrime = now
		if err := m.backend.Prime(public, primeTTL(n)); err != nil {
			m.emit("path.error", d.peer+": prime: "+err.Error())
		}
	}
	m.sendPunchState(d, now)
}

// punchAddrs sends handshakes to what this end may send to now: the
// normal addresses, and public ones once the opener has primed.
func (m *Manager) punchAddrs(d *directState, now time.Time) {
	normal, public := m.addrs(d)
	if !d.opener && d.opened {
		normal = append(normal, public...)
	}
	if len(normal) == 0 {
		return
	}
	if err := m.backend.Punch(d.peer, normal, d.until); err != nil {
		m.emit("path.error", d.peer+": punch: "+err.Error())
	}
}

func (m *Manager) sendPunchState(d *directState, now time.Time) {
	d.lastState = now
	hop, path, ok := m.signalPath(d.peer, now)
	if !ok {
		return
	}
	ps := PunchState{From: m.cfg.Self, To: d.peer, Session: d.session, Attempt: d.n, Opened: d.opener && d.primed, Probe: d.probe}
	ps.Sig = punchInput(m.cfg.Network, &ps).SignString(m.cfg.SignKey)
	m.send(hop, path, message{Type: "punch", Punch: &ps}, 0)
}

func (m *Manager) receivePunch(r Received, v message, now time.Time) {
	ps := v.Punch
	if ps == nil || ps.Session == "" || ps.Attempt == 0 ||
		!m.routeSignal(r, v, ps.From, ps.To, &ps.Hops, func() bool { return punchInput(m.cfg.Network, ps).VerifyString(m.signKeys[ps.From], ps.Sig) }, now) {
		return
	}
	d := m.signalState(ps.From, now)
	if d == nil {
		return
	}
	// The driving end's session is From's own only on the other end.
	if !d.driver && !m.freshSession(ps.From, ps.Session) {
		return
	}
	d.touched = now
	current := ps.Session == d.session && ps.Attempt == d.n
	if !d.driver && !current && (ps.Session != d.session || ps.Attempt > d.n) {
		m.begin(d, ps.Session, ps.Attempt, now, ps.Probe)
		current = true
	}
	if current && d.active && ps.Opened && !d.opener && !d.opened {
		d.opened = true
		m.punchAddrs(d, now)
	}
}

func (m *Manager) dropLink(d *directState) {
	if p := d.link; p != nil && p.path.ID != "" {
		m.backend.Deactivate(d.peer, p.path)
		m.backend.Release(p.path)
	}
	if p := d.upcoming; p != nil && p.path.ID != "" {
		m.backend.Deactivate(d.peer, p.path)
		m.backend.Release(p.path)
	}
	d.upcoming = nil
	d.trials = nil
	d.probe = false
	for _, r := range d.retired {
		m.backend.Release(r.path)
	}
	d.retired = nil
	if d.kind != punchEntry && d.kind != punchSite {
		for _, p := range m.backend.Punched(d.peer) {
			if !p.Active {
				m.backend.Release(p.Path)
			}
		}
	}
	d.link = nil
	d.local = false
	d.remoteUp = false
}

func (m *Manager) punchFailed(d *directState, now time.Time) {
	m.dropLink(d)
	d.active = false
	d.failedAt = now
	if d.kind == punchSite {
		return
	}
	d.state, d.since = directRelayed, now
	// A retry failing as the one before is no event (path.failed for an
	// entry candidate, likewise).
	before := d.lastError
	d.lastError = errPunchTimeout
	if !m.reflected.IsValid() {
		d.lastError = errNoReflector
	}
	if d.kind == punchEntry {
		if p := m.candidates[d.candidate]; p != nil {
			p.meter.unreachable(now)
			p.lastErr = candPunchTimeout
			m.pathFailed(p, errPunchTimeout)
		} else {
			m.emit("path.failed", d.candidate+": "+errPunchTimeout)
		}
	} else if d.lastError != before {
		m.emit("direct.failed", d.peer+": "+errPunchTimeout)
	}
	if d.backoff == 0 {
		d.backoff = m.cfg.Parameters.ColdRetry
	} else {
		d.backoff = min(300*time.Second, d.backoff*2)
	}
	d.next = now.Add(max(punchQuiet, min(300*time.Second, jitter(d.backoff, .2))))
	if retry := now.Add(reflectRetry); retry.Before(m.reflectNext) {
		m.reflectNext = retry
	}
	m.reachAfterFailure(now)
}

func (m *Manager) directLost(d *directState, now time.Time) {
	m.dropLink(d)
	d.active = false
	d.state, d.since = directRelayed, now
	d.lastError = errDirectLost
	m.emit("direct.down", d.peer+": "+errDirectLost)
	m.routes()
	m.reachUpdate(now)
	// The mapping may have changed under the path: look again and try once
	// at once before backing off.
	d.backoff = 0
	d.next = now.Add(time.Second)
	m.reflectNext = now
}

func (m *Manager) tickDirects(now time.Time) {
	m.discover(now)
	mine := m.offerKey()
	for _, d := range m.directs {
		for len(d.retired) > 0 && !now.Before(d.retired[0].release) {
			m.backend.Release(d.retired[0].path)
			d.retired = d.retired[1:]
		}
		if d.kind == punchSite && d.remoteKey == "" {
			continue
		}
		if d.ackDue || (d.acked != mine && !now.Before(d.offerNext)) {
			m.sendOffer(d, now)
		}
		if d.active {
			m.tickAttempt(d, now)
			if d.active && d.probe && d.link != nil {
				m.tickLink(d, now)
			}
			continue
		}
		if d.link == nil {
			if m.dialConfigured(d, now) {
				continue
			}
			m.startAttempt(d, now)
			continue
		}
		m.tickLink(d, now)
		if d.upcoming != nil {
			m.tickUpcoming(d, now)
		} else {
			m.startProbe(d, now)
		}
	}
}

// dialConfigured: a punched entry candidate with an endpoint in its config
// sends it initiations while the entry is not up and no attempt runs, from
// the start and without the addresses the relays exchange; a path to it
// that authenticates is taken at once. It says whether it took one.
func (m *Manager) dialConfigured(d *directState, now time.Time) bool {
	if d.kind != punchEntry || !d.configured.IsValid() {
		return false
	}
	p := m.candidates[d.candidate]
	if p == nil || p.config.Endpoint != "" || now.Before(p.retry) {
		return false
	}
	for _, x := range m.backend.Punched(d.peer) {
		if x.Remote == d.configured {
			m.adopt(d, x.Remote, 0, now)
			return true
		}
	}
	if now.Sub(d.lastDial) >= dialEvery {
		d.lastDial = now
		err := m.backend.Punch(d.peer, []netip.AddrPort{d.configured}, now.Add(dialEvery+time.Second))
		if msg := fmt.Sprint(err); err != nil && msg != d.dialErr {
			m.emit("path.error", d.peer+": dial: "+msg)
		}
		d.dialErr = ""
		if err != nil {
			d.dialErr = err.Error()
		}
	}
	return false
}

// startAttempt begins the driver's next attempt when it is due and both
// ends know each other's addresses.
func (m *Manager) startAttempt(d *directState, now time.Time) {
	if !d.driver || !m.discovered || now.Before(d.next) || d.acked != m.offerKey() || d.remoteKey == "" {
		return
	}
	if d.kind == punchEntry {
		p := m.candidates[d.candidate]
		if p == nil || p.config.Endpoint != "" || now.Before(p.retry) {
			return
		}
	}
	if normal, public := m.addrs(d); len(normal)+len(public) == 0 {
		return
	}
	d.next = now.Add(punchQuiet)
	m.begin(d, m.session, d.n+1, now, false)
}

// startProbe begins a probe attempt when the driver's link is up, its
// time has come and the peer has addresses other than the link's.
func (m *Manager) startProbe(d *directState, now time.Time) {
	if !d.driver || (d.kind != punchDirect && d.kind != punchRelay) || d.state != directUp || d.link == nil || d.link.path.ID == "" {
		return
	}
	if d.reprobe.IsZero() {
		d.reprobeGap = ReprobeEvery
		if d.via != "lan" {
			d.reprobeGap = min(reprobeFirst, ReprobeEvery)
		}
		d.reprobe = now.Add(jitter(d.reprobeGap, .1))
		return
	}
	if now.Before(d.reprobe) || d.acked != m.offerKey() || d.remoteKey == "" {
		return
	}
	d.reprobeGap = min(2*d.reprobeGap, ReprobeEvery)
	d.reprobe = now.Add(jitter(d.reprobeGap, .1))
	current := parseEndpointAddr(d.link.config.Endpoint)
	normal, public := m.addrs(d)
	if !slices.ContainsFunc(append(normal, public...), func(a netip.AddrPort) bool { return a != current }) {
		return
	}
	m.begin(d, m.session, d.n+1, now, true)
}

func (m *Manager) tickAttempt(d *directState, now time.Time) {
	_, public := m.addrs(d)
	if d.opener && len(public) > 0 {
		every := primeEvery
		if d.primed {
			every = time.Second
		}
		if now.Sub(d.lastPrime) >= every {
			d.lastPrime = now
			if err := m.backend.Prime(public, primeTTL(d.n)); err != nil {
				m.emit("path.error", d.peer+": prime: "+err.Error())
			}
		}
		if !d.primed && now.Sub(d.start) >= primeFor {
			d.primed = true
			m.sendPunchState(d, now)
		}
	}
	if now.Sub(d.lastState) >= stateEvery {
		m.sendPunchState(d, now)
	}
	if now.Sub(d.lastScan) >= 200*time.Millisecond || now.After(d.until) {
		d.lastScan = now
		if m.adoptPunched(d, now) {
			return
		}
	}
	m.pingTrials(d, now)
	end := d.until
	if m.follows(d) {
		end = end.Add(followGrace)
	}
	if now.After(end) {
		if d.probe {
			m.endProbe(d, now)
		} else {
			m.punchFailed(d, now)
		}
	}
}

// follows says whether this end takes the path the other end chooses: the
// end of a direct or relay link that does not drive it.
func (m *Manager) follows(d *directState) bool {
	return !d.driver && (d.kind == punchDirect || d.kind == punchRelay)
}

func (m *Manager) tickLink(d *directState, now time.Time) {
	p := d.link
	if m.trackTick(p, now) && p.path.ID != "" && now.Sub(p.lastKick) >= m.cfg.Parameters.HandshakeGrace {
		m.backend.Handshake(p.config.Peer, p.path)
		p.lastKick = now
	}
	if p.preparing {
		return
	}
	failing := p.path.ID == "" || p.track.stats.Failures >= m.cfg.Parameters.ConsecutiveFailures
	if d.state == directUp && (failing || !d.remoteUp) {
		m.directLost(d, now)
		return
	}
	local := p.ready && p.routeReady && !failing
	if d.state != directUp && (p.path.ID == "" || (!local && now.Sub(p.since) > 10*time.Second) || now.Sub(p.since) > directSettle) {
		m.punchFailed(d, now)
		return
	}
	if !now.Before(p.next) || local != d.local {
		// A change of readiness goes to the peer with the next ping.
		d.local = local
		m.probe(p, now)
	}
	if p.ready && !p.routeReady {
		if now.Sub(p.lastWarm) >= time.Second {
			m.backend.Warm(p.config.Peer, p.path)
			p.lastWarm = now
		}
		m.send(p.config.Peer, p.path, message{Type: "prepare", Token: p.token, Generation: p.path.Generation}, 0)
	}
	if local && d.remoteUp && d.state != directUp {
		d.state, d.since = directUp, now
		d.lastError = ""
		d.backoff = 0
		m.emit("direct.up", d.peer+" via "+d.via)
		m.routes()
		m.reachUpdate(now)
	}
}

// adoptPunched decides on the paths an attempt opened and says whether it
// did: a LAN address of the peer's on this node's network at once,
// otherwise the fastest of those that authenticated by the end of the
// window. A probe attempt only prepares one that beats the link.
func (m *Manager) adoptPunched(d *directState, now time.Time) bool {
	if d.probe && d.link == nil {
		d.probe = false
	}
	if m.follows(d) {
		return false // followPrepared
	}
	var current netip.AddrPort
	if d.probe {
		current = parseEndpointAddr(d.link.config.Endpoint)
	}
	var found []PunchedPath
	for _, p := range m.backend.Punched(d.peer) {
		if d.probe && (p.Path == d.link.path || p.Remote == current) {
			continue
		}
		found = append(found, p)
	}
	if len(found) == 0 {
		return false
	}
	if d.kind == punchSite {
		// The member prepares, probes and commits the path.
		d.active = false
		return true
	}
	if !d.probe {
		for _, p := range found {
			if m.viaOf(d, p.Remote) == "lan" {
				m.adopt(d, p.Remote, 0, now)
				return true
			}
		}
	}
	if d.firstAuth.IsZero() {
		d.firstAuth = now
	}
	if d.trials == nil {
		d.trials = map[Path]*pathTrial{}
	}
	limit := 3
	if d.probe {
		limit = 2 // the peer answers pings on three paths of this node's
	}
	for _, p := range found {
		if d.trials[p.Path] == nil && len(d.trials) < limit {
			d.trials[p.Path] = &pathTrial{remote: p.Remote, token: nonce(), created: now, sent: map[uint64]trialPing{}}
		}
	}
	late := now.After(d.until)
	if !late {
		if now.Before(maxTime(d.firstAuth, d.start.Add(primeFor)).Add(compareFor)) {
			return false
		}
		for _, t := range d.trials {
			if t.best == 0 && now.Sub(t.created) < time.Second {
				return false
			}
		}
	}
	var best *pathTrial
	for _, p := range found {
		t := d.trials[p.Path]
		if t == nil || t.best == 0 {
			continue
		}
		if best == nil || m.faster(d, t, best) {
			best = t
		}
	}
	if d.probe {
		if cur := d.link.track.stats.SRTT; best != nil && len(best.samples) >= probeSamples && clearlyFaster(best.rtt(), cur) {
			m.prepareFaster(d, best.remote, cur, best.rtt(), now)
		}
		m.endProbe(d, now)
		return true
	}
	if best == nil {
		// No path answered a ping: the first that authenticated of the best
		// kind.
		pick := found[0]
		for _, p := range found {
			if viaRank(m.viaOf(d, p.Remote)) < viaRank(m.viaOf(d, pick.Remote)) {
				pick = p
			}
		}
		m.adopt(d, pick.Remote, 0, now)
	} else {
		m.adopt(d, best.remote, best.rtt(), now)
	}
	return true
}

// faster says whether trial a beats b: by its median round trip, or by the
// kind of address when the two are about as fast.
func (m *Manager) faster(d *directState, a, b *pathTrial) bool {
	ra, rb := a.rtt(), b.rtt()
	if margin := max(tieMargin, min(ra, rb)/10); ra < rb-margin || ra > rb+margin {
		return ra < rb
	}
	if ka, kb := viaRank(m.viaOf(d, a.remote)), viaRank(m.viaOf(d, b.remote)); ka != kb {
		return ka < kb
	}
	return ra < rb
}

// clearlyFaster says whether rtt beats a link's smoothed round trip enough
// to move the link: by a fifth and by switchGain.
func clearlyFaster(rtt, link time.Duration) bool {
	return rtt > 0 && link > 0 && rtt*5 < link*4 && link-rtt >= switchGain
}

// viaRank orders the kinds of address for a tie: the LAN first, a private
// address reached through some router last.
func viaRank(via string) int {
	switch via {
	case "lan":
		return 0
	case "routed":
		return 2
	}
	return 1
}

var sharedSpace = netip.MustParsePrefix("100.64.0.0/10")

// privateAddr: an address that is no internet one.
func privateAddr(a netip.Addr) bool {
	return a.IsPrivate() || a.IsLinkLocalUnicast() || sharedSpace.Contains(a)
}

// viaOf says what kind of address remote is: lan, an address of the peer's
// own (offered, or configured: the controller fills in a LAN address when
// both are on one network) on one of this node's networks; configured;
// public, offered as such or an internet address; routed, any other private
// address, as a LAN of the peer's reached through a VPN or a router that
// masquerades the peer's packets.
func (m *Manager) viaOf(d *directState, remote netip.AddrPort) string {
	own := d.remote[remote] == "lan" || remote == d.configured
	switch {
	case own && m.onLink(remote.Addr()):
		return "lan"
	case remote == d.configured:
		return "configured"
	case d.remote[remote] != "public" && privateAddr(remote.Addr().Unmap()):
		return "routed"
	}
	return "public"
}

// adopt makes remote the attempt's path; rtt is what it measured, if it was.
func (m *Manager) adopt(d *directState, remote netip.AddrPort, rtt time.Duration, now time.Time) {
	m.endTrials(d)
	d.active = false
	d.via = m.viaOf(d, remote)
	if rtt > 0 {
		m.emit("direct.chosen", fmt.Sprintf("%s via %s %s, %.1f ms", d.peer, d.via, remote, float64(rtt)/float64(time.Millisecond)))
	}
	d.reprobe, d.reprobeGap = time.Time{}, 0
	endpoint := "udp://" + remote.String()
	switch d.kind {
	case punchEntry:
		p := m.candidates[d.candidate]
		if p == nil {
			return
		}
		d.state, d.since, d.lastError = directUp, now, ""
		d.backoff = 0
		p.config.Endpoint = endpoint
		p.retry = now
		if m.active == "" || m.cfg.Candidates[0].ID == p.config.ID {
			m.start(p, now)
		}
	default:
		// A punched relay link looks like a dialed one to whatever follows
		// relay links by ID.
		id := "direct:" + d.peer
		if d.kind == punchRelay {
			id = "link:" + d.peer
		}
		d.link = &candidateState{config: Candidate{ID: id, Peer: d.peer, Endpoint: endpoint},
			track: newTracker(m.cfg.Parameters, now), pending: map[uint64]string{}, early: map[uint64]time.Time{}}
		m.start(d.link, now)
	}
}

func (m *Manager) linkID(d *directState) string {
	if d.kind == punchRelay {
		return "link:" + d.peer
	}
	return "direct:" + d.peer
}

// pingTrials measures the paths of an attempt that authenticated. The peer
// answers a ping on any path; with the link's readiness, so that its view of
// the link does not change.
func (m *Manager) pingTrials(d *directState, now time.Time) {
	for path, t := range d.trials {
		if now.Sub(t.last) < trialEvery || t.pings >= maxTrialPings {
			continue
		}
		t.last = now
		t.pings++
		m.nextID++
		n := nonce()
		t.sent[m.nextID] = trialPing{n, now}
		m.send(d.peer, path, message{Type: "ping", Nonce: n, Token: t.token, Generation: path.Generation, Probe: m.nextID, Direct: d.local, Trial: true}, 0)
	}
}

// trialPong takes the answer to a trial ping, if it is one.
func (m *Manager) trialPong(r Received, v message, now time.Time) bool {
	d := m.directs[r.Peer]
	if d == nil {
		return false
	}
	t := d.trials[r.Path]
	if t == nil || t.token != v.Token || v.Generation != r.Path.Generation {
		return false
	}
	ping, ok := t.sent[v.Probe]
	if !ok || ping.nonce != v.Nonce {
		return true
	}
	delete(t.sent, v.Probe)
	rtt := max(now.Sub(ping.at), time.Microsecond)
	t.samples = append(t.samples, rtt)
	if t.best == 0 || rtt < t.best {
		t.best = rtt
	}
	m.send(r.Peer, r.Path, message{Type: "seen", Nonce: v.Nonce, Token: v.Token, Generation: v.Generation, Probe: v.Probe, Echo: v.Echo}, 0)
	return true
}

// endTrials releases the paths an attempt measured and does not use.
func (m *Manager) endTrials(d *directState) {
	for path := range d.trials {
		if (d.link == nil || d.link.path != path) && (d.upcoming == nil || d.upcoming.path != path) {
			m.backend.Release(path)
		}
	}
	d.trials = nil
	d.firstAuth = time.Time{}
}

// endProbe ends a probe attempt; the link stays as it is.
func (m *Manager) endProbe(d *directState, now time.Time) {
	d.active, d.probe = false, false
	keep := Path{}
	if d.upcoming != nil {
		// Not prepared yet: keep its path out of what is released.
		for path, t := range d.trials {
			if "udp://"+t.remote.String() == d.upcoming.config.Endpoint {
				keep = path
			}
		}
		delete(d.trials, keep)
	}
	m.endTrials(d)
	if m.follows(d) {
		m.releaseUnused(d)
	} else if d.reprobeGap > 0 {
		d.reprobe = now.Add(jitter(d.reprobeGap, .1))
	}
}

// releaseUnused: the end that follows releases the paths its attempt
// opened that the driver did not choose.
func (m *Manager) releaseUnused(d *directState) {
	used := func(c *candidateState, p PunchedPath) bool {
		return c != nil && (c.path == p.Path || c.config.Endpoint == "udp://"+p.Remote.String())
	}
	for _, p := range m.backend.Punched(d.peer) {
		if !p.Active && !used(d.link, p) && !used(d.upcoming, p) {
			m.backend.Release(p.Path)
		}
	}
}

// followPrepared: on the end that follows, the driver prepared path, a
// punched path to it. That becomes the link, or with a link up the next
// one, which replaces it once ready as a faster one would.
func (m *Manager) followPrepared(peer string, path Path, now time.Time) {
	d := m.directs[peer]
	if d == nil || !m.follows(d) {
		return
	}
	var remote netip.AddrPort
	for _, p := range m.backend.Punched(peer) {
		if p.Path == path {
			remote = p.Remote
		}
	}
	endpoint := "udp://" + remote.String()
	if !remote.IsValid() || (d.link != nil && d.link.config.Endpoint == endpoint) || (d.upcoming != nil && d.upcoming.config.Endpoint == endpoint) {
		return
	}
	if d.link != nil && d.state != directUp {
		// Not up yet: the driver's path instead.
		if p := d.link; p.path.ID != "" && p.path != path {
			m.backend.Deactivate(d.peer, p.path)
			m.backend.Release(p.path)
		}
		d.link = nil
	}
	if d.link == nil {
		m.adopt(d, remote, 0, now)
		m.emit("direct.chosen", fmt.Sprintf("%s via %s %s, as %s chose", d.peer, d.via, remote, d.peer))
		m.releaseUnused(d)
		return
	}
	if d.active && d.probe {
		m.endProbe(d, now)
	}
	if d.upcoming != nil {
		m.abortUpcoming(d, "the peer prepared another path")
	}
	d.upcomingVia = m.viaOf(d, remote)
	m.emit("direct.better", fmt.Sprintf("%s via %s %s, as %s chose", d.peer, d.upcomingVia, remote, d.peer))
	d.upcoming = &candidateState{config: Candidate{ID: m.linkID(d) + "/next", Peer: d.peer, Endpoint: endpoint},
		track: newTracker(m.cfg.Parameters, now), pending: map[uint64]string{}, early: map[uint64]time.Time{}}
	m.start(d.upcoming, now)
}

// prepareFaster starts a path to remote beside the link.
func (m *Manager) prepareFaster(d *directState, remote netip.AddrPort, was, rtt time.Duration, now time.Time) {
	d.upcomingVia = m.viaOf(d, remote)
	m.emit("direct.better", fmt.Sprintf("%s via %s %s: %.1f ms, link %.1f ms", d.peer, d.upcomingVia, remote, float64(rtt)/float64(time.Millisecond), float64(was)/float64(time.Millisecond)))
	d.upcoming = &candidateState{config: Candidate{ID: m.linkID(d) + "/next", Peer: d.peer, Endpoint: "udp://" + remote.String()},
		track: newTracker(m.cfg.Parameters, now), pending: map[uint64]string{}, early: map[uint64]time.Time{}}
	m.start(d.upcoming, now)
}

// tickUpcoming makes the faster path ready and then the link; the "ready"
// answer has committed it already.
func (m *Manager) tickUpcoming(d *directState, now time.Time) {
	p := d.upcoming
	if m.trackTick(p, now) && p.path.ID != "" && now.Sub(p.lastKick) >= m.cfg.Parameters.HandshakeGrace {
		m.backend.Handshake(p.config.Peer, p.path)
		p.lastKick = now
	}
	if p.preparing {
		return
	}
	if p.path.ID == "" || now.Sub(p.since) > directSettle || p.track.stats.Failures >= m.cfg.Parameters.ConsecutiveFailures {
		m.abortUpcoming(d, "not ready")
		return
	}
	if !now.Before(p.next) {
		m.probe(p, now)
	}
	if p.ready && !p.routeReady {
		if now.Sub(p.lastWarm) >= time.Second {
			m.backend.Warm(p.config.Peer, p.path)
			p.lastWarm = now
		}
		m.send(p.config.Peer, p.path, message{Type: "prepare", Token: p.token, Generation: p.path.Generation}, 0)
		return
	}
	if !p.ready || !p.routeReady {
		return
	}
	old := d.link
	p.config.ID = old.config.ID
	d.link, d.upcoming = p, nil
	d.via = d.upcomingVia
	if old.path.ID != "" && old.path != p.path {
		m.backend.Deactivate(d.peer, old.path)
		d.retired = append(d.retired, retiredPath{old.path, now.Add(retireAfter)})
	}
	m.emit("direct.switched", fmt.Sprintf("%s via %s %s", d.peer, d.via, p.config.Endpoint))
	m.routes()
}

// abortUpcoming gives up the faster path and keeps the link.
func (m *Manager) abortUpcoming(d *directState, why string) {
	p := d.upcoming
	d.upcoming = nil
	if p == nil {
		return
	}
	if p.path.ID != "" {
		if p.routeReady && d.link != nil && d.link.path.ID != "" {
			m.backend.Commit(d.peer, d.link.path)
		}
		m.backend.Deactivate(d.peer, p.path)
		m.backend.Release(p.path)
	}
	m.emit("direct.better", d.peer+": "+why)
}

// entryLost puts a punched entry candidate back to punching after its
// path failed.
func (m *Manager) entryLost(p *candidateState, now time.Time) {
	d := m.directs[p.config.Peer]
	if !p.config.Punch || d == nil || d.kind != punchEntry || p.config.Endpoint == "" {
		return
	}
	p.config.Endpoint = ""
	d.state, d.since = directRelayed, now
	d.next = maxTime(p.retry, d.failedAt.Add(punchQuiet))
	m.reflectNext = now
}

func (m *Manager) directUp(peer string) bool {
	d := m.directs[peer]
	return d != nil && d.link != nil && d.state == directUp
}

func (m *Manager) punchedVia(via string) bool {
	for _, d := range m.directs {
		if d.state == directUp && d.via == via && (d.link != nil || d.kind == punchEntry) {
			return true
		}
	}
	return false
}

func (m *Manager) directSnapshot() map[string]DirectStatus {
	out := map[string]DirectStatus{}
	for id, d := range m.directs {
		if d.kind != punchDirect && d.kind != punchRelay {
			continue
		}
		via := ""
		if d.state == directUp {
			via = d.via
		}
		out[id] = DirectStatus{State: d.state, Via: via, LastError: d.lastError, Since: d.since, Attempts: d.attempts}
	}
	return out
}
