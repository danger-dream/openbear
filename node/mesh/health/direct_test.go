package health

import (
	"context"
	"encoding/json"
	"net/netip"
	"slices"
	"strings"
	"syscall"
	"testing"
	"time"

	"openbear/node/mesh/transport"
	"openbear/node/mesh/wire"
)

type sentMessage struct {
	peer string
	raw  []byte
	path Path
}

type primeCall struct {
	at    time.Time
	addrs []netip.AddrPort
	ttl   int
}

type punchCall2 struct {
	at    time.Time
	addrs []netip.AddrPort
}

type offerBackend struct {
	policyBackend
	clock   *time.Time
	sent    []sentMessage
	punched []punchCall2
	primes  []primeCall
	paths   []PunchedPath // what Punched returns
	seen    map[[8]byte]netip.AddrPort
	recent  bool
	tests   []netip.AddrPort
	remote  netip.AddrPort // every path's other end
}

func (b *offerBackend) now() time.Time {
	if b.clock != nil {
		return *b.clock
	}
	return time.Now()
}
func (b *offerBackend) Send(peer string, path Path, _ uint64, raw []byte) error {
	b.sent = append(b.sent, sentMessage{peer, raw, path})
	return nil
}
func (b *offerBackend) Punch(_ string, addrs []netip.AddrPort, _ time.Time) error {
	b.punched = append(b.punched, punchCall2{b.now(), addrs})
	return nil
}
func (b *offerBackend) Prime(addrs []netip.AddrPort, ttl int) error {
	b.primes = append(b.primes, primeCall{b.now(), addrs, ttl})
	return nil
}
func (b *offerBackend) Punched(string) []PunchedPath { return b.paths }
func (b *offerBackend) ReflectSeen(id [8]byte) (netip.AddrPort, bool) {
	a, ok := b.seen[id]
	return a, ok
}
func (b *offerBackend) RecentlyFrom(netip.Addr, time.Duration) bool { return b.recent }
func (b *offerBackend) Remote(Path) (netip.AddrPort, bool)          { return b.remote, b.remote.IsValid() }
func (b *offerBackend) SendTest(target netip.AddrPort, _ [8]byte, _ byte) error {
	b.tests = append(b.tests, target)
	return nil
}

func (b *offerBackend) types() []string {
	var out []string
	for _, s := range b.sent {
		var v message
		json.Unmarshal(s.raw, &v)
		out = append(out, s.peer+":"+v.Type)
	}
	return out
}

func offerManager(t *testing.T, cfg Config, allowed map[string][]string, relays map[string]bool) (*Manager, *offerBackend) {
	t.Helper()
	s, err := openStore("", cfg.Network, cfg.Self)
	if err != nil {
		t.Fatal(err)
	}
	cfg.Parameters = Defaults()
	cfg = withKeys(cfg)
	b := &offerBackend{seen: map[[8]byte]netip.AddrPort{}}
	m := &Manager{cfg: cfg, signKeys: keysOf(cfg), backend: b, store: s, owners: newOwners(s, cfg.Parameters.Lease), candidates: map[string]*candidateState{}, links: map[string]*candidateState{}, inbound: map[Path]*inbound{}, changes: make(chan struct{}, 1), notices: map[string]outboundNotice{}, lastRefresh: map[string]time.Time{},
		directs: map[string]*directState{}, session: nonce(), offerRates: map[string]offerRate{}, reach: newReach(), ctx: context.Background()}
	if _, err = m.owners.configure(cfg.Revision, allowed, relays, cfg.Network, m.signKeys); err != nil {
		t.Fatal(err)
	}
	for _, c := range cfg.Candidates {
		m.candidates[c.ID] = &candidateState{config: c, track: newTracker(cfg.Parameters, time.Now()), pending: map[uint64]string{}, early: map[uint64]time.Time{}}
	}
	m.configureDirects(cfg, time.Now())
	return m, b
}

func signal(t *testing.T, from, to, typ string, v message) Received {
	t.Helper()
	v.Version, v.Network, v.Revision, v.From, v.To, v.Type = wire.ProtocolMin, "n", 1, from, to, typ
	// Offers and punch states come signed by the node they name, as every
	// node of this version sends them; a test of a bad signature sets one.
	if o := v.Offer; o != nil && o.Sig == "" {
		c := *o
		c.Sig = offerInput("n", &c).SignString(signTestKey(c.From))
		v.Offer = &c
	}
	if ps := v.Punch; ps != nil && ps.Sig == "" {
		c := *ps
		c.Sig = punchInput("n", &c).SignString(signTestKey(c.From))
		v.Punch = &c
	}
	raw, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return Received{Peer: from, Path: Path{ID: "p1", Generation: 1}, At: time.Now(), Payload: raw}
}

func offerFrom(t *testing.T, peer, to string, o Offer) Received {
	return signal(t, peer, to, "offer", message{Offer: &o})
}

// A member takes addresses only for a punched peer, and only as a relay it
// is attached to delivers them over the control plane.
func TestOfferOnlyFromRelaysForThePunchedPeer(t *testing.T) {
	nodes := []Node{{ID: "R", Role: "relay"}, {ID: "B", Role: "member"}, {ID: "C", Role: "member"}}
	m, _ := offerManager(t, Config{Network: "n", Self: "A", Role: "member", Revision: 1, Nodes: nodes, Links: []Link{{Peer: "B", Punch: true}}},
		map[string][]string{}, map[string]bool{"R": true})
	offer := Offer{From: "B", To: "A", Session: "s", Gen: 1,
		Candidates: []string{"public:203.0.113.7:51820", "lan:192.168.1.5:51820", "lan:127.0.0.1:51820", "public:[2001:db8::1]:51820", "wan:198.51.100.1:1"}}

	m.receive(offerFrom(t, "C", "A", offer))
	other := offer
	other.From = "C"
	m.receive(offerFrom(t, "R", "A", other))
	if m.directs["B"].remoteKey != "" || m.directs["C"] != nil {
		t.Fatal("took an offer from a member, or for a peer without a punched link")
	}

	m.receive(offerFrom(t, "R", "A", offer))
	saved := localNets
	defer func() { localNets = saved }()
	localNets = func(netip.Prefix) []netip.Prefix { return []netip.Prefix{netip.MustParsePrefix("192.168.1.0/24")} }
	normal, public := m.addrs(m.directs["B"])
	if !slices.Equal(normal, []netip.AddrPort{netip.MustParseAddrPort("192.168.1.5:51820")}) || !slices.Equal(public, []netip.AddrPort{netip.MustParseAddrPort("203.0.113.7:51820")}) {
		t.Fatalf("addresses %v %v, want the LAN and the public IPv4 one only", normal, public)
	}
	// A LAN address off this node's networks goes with the public ones.
	localNets = func(netip.Prefix) []netip.Prefix { return nil }
	m.reach.netsAt = time.Time{}
	normal, public = m.addrs(m.directs["B"])
	if len(normal) != 0 || !slices.Equal(public, []netip.AddrPort{netip.MustParseAddrPort("192.168.1.5:51820"), netip.MustParseAddrPort("203.0.113.7:51820")}) {
		t.Fatalf("addresses off link %v %v", normal, public)
	}
	if !m.directs["B"].ackDue {
		t.Fatal("the offer is not acknowledged")
	}
}

// A relay forwards an offer only from the member it names, and only toward
// the relay that member's peer is attached to.
func TestRelayForwardsOffersOnlyFromTheirSender(t *testing.T) {
	nodes := []Node{{ID: "R2", Role: "relay"}, {ID: "A", Role: "member"}, {ID: "B", Role: "member"}, {ID: "C", Role: "member"}}
	m, b := offerManager(t, Config{Network: "n", Self: "R", Role: "relay", Revision: 1, Nodes: nodes, Links: []Link{{Peer: "R2"}}},
		map[string][]string{"A": {"R", "R2"}, "B": {"R"}, "C": {"R"}}, map[string]bool{"R2": true})
	now := time.Now()
	if _, err := m.owners.accept("R2", sn(Notice{Member: "A", Owner: "R2", Seq: 1, Refresh: 1, Generation: 1, LeaseMs: 30000}), now); err != nil {
		t.Fatal(err)
	}
	m.links["R2"] = &candidateState{config: Candidate{ID: "link:R2", Peer: "R2"}, ready: true, path: Path{ID: "r2", Generation: 1}}

	m.receive(offerFrom(t, "C", "R", Offer{From: "B", To: "A", Session: "s", Gen: 1}))
	if len(b.sent) != 0 {
		t.Fatalf("forwarded a forged offer to %v", b.types())
	}
	m.receive(offerFrom(t, "B", "R", Offer{From: "B", To: "A", Session: "s", Gen: 1}))
	if !slices.Equal(b.types(), []string{"R2:offer"}) {
		t.Fatalf("offer went to %v, want the owner R2", b.types())
	}
	for range 20 {
		m.receive(offerFrom(t, "B", "R", Offer{From: "B", To: "A", Session: "s", Gen: 1}))
	}
	if n := len(b.sent); n > 8 {
		t.Fatalf("forwarded %d offers in a burst from one member", n)
	}
}

// A relay in between forwards offers between two relays punching their
// link, but not one a member claims a relay sent.
func TestRelayForwardsRelayOffers(t *testing.T) {
	nodes := []Node{{ID: "S1", Role: "relay"}, {ID: "S2", Role: "relay"}, {ID: "M", Role: "member", Relays: []string{"P"}}}
	m, b := offerManager(t, Config{Network: "n", Self: "P", Role: "relay", Revision: 1, Nodes: nodes, Links: []Link{{Peer: "S1"}, {Peer: "S2"}}},
		map[string][]string{"M": {"P"}}, map[string]bool{"S1": true, "S2": true})
	for _, s := range []string{"S1", "S2"} {
		m.links[s] = &candidateState{config: Candidate{ID: "link:" + s, Peer: s}, ready: true, path: Path{ID: s, Generation: 1}}
	}
	m.receive(offerFrom(t, "M", "P", Offer{From: "S1", To: "S2", Session: "s", Gen: 1}))
	m.receive(signal(t, "M", "P", "punch", message{Punch: &PunchState{From: "S1", To: "S2", Session: "s", Attempt: 1}}))
	if len(b.sent) != 0 {
		t.Fatalf("forwarded what a member forged: %v", b.types())
	}
	m.receive(offerFrom(t, "S1", "P", Offer{From: "S1", To: "S2", Session: "s", Gen: 1}))
	m.receive(signal(t, "S1", "P", "punch", message{Punch: &PunchState{From: "S1", To: "S2", Session: "s", Attempt: 1, Opened: true}}))
	if !slices.Equal(b.types(), []string{"S2:offer", "S2:punch"}) {
		t.Fatalf("sent %v, want the offer and the punch state to S2", b.types())
	}
	var v message
	json.Unmarshal(b.sent[1].raw, &v)
	if v.Punch.Hops != 1 || !v.Punch.Opened {
		t.Fatalf("forwarded %+v", v.Punch)
	}
	b.sent = nil
	m.receive(offerFrom(t, "S1", "P", Offer{From: "S1", To: "S2", Session: "s", Gen: 1, Hops: maxSignalHops}))
	if len(b.sent) != 0 {
		t.Fatal("forwarded past the hop limit")
	}
}

// A relay answers a member punching an entry to it through another relay:
// it keeps the member's addresses and sends its own back the same way.
func TestSiteRelayAnswersAMembersEntryOffer(t *testing.T) {
	nodes := []Node{{ID: "SG", Role: "relay"}, {ID: "M", Role: "member", Relays: []string{"T", "SG"}}, {ID: "X", Role: "member", Relays: []string{"SG"}}}
	m, b := offerManager(t, Config{Network: "n", Self: "T", Role: "relay", Revision: 1, Nodes: nodes, Links: []Link{{Peer: "SG"}}, Port: 51820},
		map[string][]string{"M": {"T", "SG"}, "X": {"SG"}}, map[string]bool{"SG": true})
	now := time.Now()
	if _, err := m.owners.accept("SG", sn(Notice{Member: "M", Owner: "SG", Seq: 1, Refresh: 1, Generation: 1, LeaseMs: 30000}), now); err != nil {
		t.Fatal(err)
	}
	m.links["SG"] = &candidateState{config: Candidate{ID: "link:SG", Peer: "SG"}, ready: true, path: Path{ID: "sg", Generation: 1}}
	m.discovered, m.reflected, m.local = true, netip.MustParseAddrPort("198.51.100.9:51820"), []string{"lan:10.20.20.7:51820", "public:198.51.100.9:51820"}

	m.receive(offerFrom(t, "SG", "T", Offer{From: "X", To: "T", Session: "s", Gen: 1}))
	if m.directs["X"] != nil {
		t.Fatal("made punch state for a member not allowed to attach here")
	}
	m.receive(offerFrom(t, "SG", "T", Offer{From: "M", To: "T", Session: "s", Gen: 1, Candidates: []string{"lan:10.20.20.253:51820"}}))
	d := m.directs["M"]
	if d == nil || d.kind != punchSite || d.driver {
		t.Fatalf("site state %+v", d)
	}
	m.tickDirects(now)
	if !slices.Equal(b.types(), []string{"SG:offer"}) {
		t.Fatalf("sent %v, want the answer through SG", b.types())
	}
	var v message
	json.Unmarshal(b.sent[0].raw, &v)
	if v.Offer.To != "M" || !slices.Contains(v.Offer.Candidates, "lan:10.20.20.7:51820") || v.Offer.Ack != "s/1" {
		t.Fatalf("answer %+v", v.Offer)
	}
}

// pair runs two members punching a direct link, with the relay between
// them as a function that delivers each signal at once.
type pair struct {
	t     *testing.T
	now   time.Time
	m     map[string]*Manager
	b     map[string]*offerBackend
	start time.Time
	// rtt answers a node's pings on a path after that long; other pings go
	// unanswered.
	rtt   map[string]map[Path]time.Duration
	pongs []duePong
}

type duePong struct {
	to string
	at time.Time
	r  Received
}

func newPair(t *testing.T) *pair {
	t.Helper()
	p := &pair{t: t, now: time.Unix(1_800_000_000, 0), m: map[string]*Manager{}, b: map[string]*offerBackend{}, rtt: map[string]map[Path]time.Duration{"A": {}, "B": {}}}
	p.start = p.now
	nodes := []Node{{ID: "R", Role: "relay"}, {ID: "A", Role: "member", Relays: []string{"R"}}, {ID: "B", Role: "member", Relays: []string{"R"}}}
	addrs := map[string]string{"A": "203.0.113.1", "B": "198.51.100.2"}
	lan := map[string]string{"A": "192.168.1.10", "B": "192.168.2.20"}
	saved, savedNets := localAddrs, localNets
	t.Cleanup(func() { localAddrs, localNets = saved, savedNets })
	localNets = func(netip.Prefix) []netip.Prefix { return nil }
	for self, peer := range map[string]string{"A": "B", "B": "A"} {
		cfg := Config{Network: "n", Self: self, Role: "member", Revision: 1, Nodes: nodes, Port: 51820,
			Links: []Link{{Peer: peer, Punch: true}}, Candidates: []Candidate{{ID: "c", Peer: "R", Endpoint: "udp://192.0.2.1:1"}}}
		m, b := offerManager(t, cfg, map[string][]string{}, map[string]bool{"R": true})
		b.clock = &p.now
		m.active = "c"
		m.candidates["c"].path = Path{ID: "r", Generation: 1}
		localAddrs = func(netip.Prefix) []netip.Addr { return []netip.Addr{netip.MustParseAddr(lan[self])} }
		m.reflectedAddr(reflected{addr: netip.MustParseAddrPort(addrs[self] + ":51820")}, p.now)
		m.reflecting = true // no more discovery
		p.m[self], p.b[self] = m, b
	}
	return p
}

// step advances the clock and delivers what was sent, as the relay would.
func (p *pair) step(d time.Duration) {
	p.now = p.now.Add(d)
	due := p.pongs[:0]
	for _, x := range p.pongs {
		if x.at.After(p.now) {
			due = append(due, x)
			continue
		}
		x.r.At = x.at
		p.m[x.to].receive(x.r)
	}
	p.pongs = due
	for _, id := range []string{"A", "B"} {
		p.m[id].tickDirects(p.now)
	}
	for again := true; again; {
		again = false
		for _, from := range []string{"A", "B"} {
			b := p.b[from]
			sent := b.sent
			b.sent = nil
			for _, s := range sent {
				var v message
				if json.Unmarshal(s.raw, &v) != nil {
					continue
				}
				var to string
				switch {
				case v.Offer != nil:
					to = v.Offer.To
				case v.Punch != nil:
					to = v.Punch.To
				case v.Type == "ping":
					if rtt, ok := p.rtt[from][s.path]; ok {
						v.Type, v.From, v.To = "pong", v.To, v.From
						raw, _ := json.Marshal(v)
						p.pongs = append(p.pongs, duePong{from, p.now.Add(rtt), Received{Peer: v.From, Path: s.path, Payload: raw}})
					}
					continue
				default:
					continue
				}
				v.From, v.To = "R", to
				raw, _ := json.Marshal(v)
				p.m[to].receive(Received{Peer: "R", Path: Path{ID: "r", Generation: 1}, At: p.now, Payload: raw})
				again = true
			}
		}
	}
}

func (p *pair) public(id string) netip.AddrPort {
	return map[string]netip.AddrPort{"A": netip.MustParseAddrPort("203.0.113.1:51820"), "B": netip.MustParseAddrPort("198.51.100.2:51820")}[id]
}

func (p *pair) sentPublic(from, to string, after time.Time) []time.Time {
	var out []time.Time
	for _, c := range p.b[from].punched {
		if !c.at.Before(after) && slices.Contains(c.addrs, p.public(to)) {
			out = append(out, c.at)
		}
	}
	for _, c := range p.b[from].primes {
		if !c.at.Before(after) && slices.Contains(c.addrs, p.public(to)) {
			out = append(out, c.at)
		}
	}
	slices.SortFunc(out, func(a, b time.Time) int { return a.Compare(b) })
	return out
}

// The opener primes the other's public address with a short TTL for 1.5 s
// and never sends it a handshake; the other end sends to the opener's
// public address only after hearing it has primed. LAN addresses go out at
// once from both. The opener alternates and the TTL follows 4,4,3,3,6,6.
// After a failed attempt neither end sends to the other's public address
// for at least 35 s.
func TestPunchOpensBeforeTheOtherSends(t *testing.T) {
	p := newPair(t)
	localNets = func(netip.Prefix) []netip.Prefix {
		return []netip.Prefix{netip.MustParsePrefix("192.168.1.0/24"), netip.MustParsePrefix("192.168.2.0/24")}
	}
	type attempt struct {
		opener      string
		start       time.Time
		ttl         int
		otherPublic time.Time
	}
	var attempts []attempt
	var lastEnd time.Time
	for i := 0; i < 7*3600*5 && (len(attempts) < 7 || p.now.Sub(attempts[6].start) < 5*time.Second); i++ {
		before := map[string]int{"A": len(p.b["A"].primes), "B": len(p.b["B"].primes)}
		p.step(200 * time.Millisecond)
		for id, b := range p.b {
			if len(b.primes) > before[id] && (len(attempts) == 0 || attempts[len(attempts)-1].opener != id || p.now.Sub(attempts[len(attempts)-1].start) > time.Minute) {
				attempts = append(attempts, attempt{opener: id, start: p.now, ttl: b.primes[before[id]].ttl})
			}
		}
		if d := p.m["A"].directs["B"]; d.state == directRelayed && !d.failedAt.IsZero() {
			lastEnd = d.failedAt
		}
	}
	if len(attempts) < 7 || lastEnd.IsZero() {
		t.Fatalf("saw %d attempts", len(attempts))
	}
	wantOpener := []string{"A", "B", "A", "B", "A", "B", "A"}
	wantTTL := []int{4, 4, 3, 3, 6, 6, 4}
	for i, a := range attempts {
		if a.opener != wantOpener[i] || a.ttl != wantTTL[i] {
			t.Fatalf("attempt %d: opener %s TTL %d, want %s TTL %d", i+1, a.opener, a.ttl, wantOpener[i], wantTTL[i])
		}
		other := map[string]string{"A": "B", "B": "A"}[a.opener]
		// The opener sends the other's public address primes only.
		for _, c := range p.b[a.opener].punched {
			if !c.at.Before(a.start) && c.at.Before(a.start.Add(primeFor+punchWindow+time.Second)) && slices.Contains(c.addrs, p.public(other)) {
				t.Fatalf("attempt %d: the opener sent a handshake to the other's public address", i+1)
			}
		}
		sent := p.sentPublic(other, a.opener, a.start.Add(-time.Second))
		if len(sent) == 0 || sent[0].Sub(a.start) < primeFor {
			t.Fatalf("attempt %d: the other end sent to the opener's public address at %v, before it primed for 1.5 s", i+1, sent)
		}
		if i > 0 {
			// Nothing to either public address between the attempts.
			prev := attempts[i-1].start
			var last time.Time
			for _, id := range []string{"A", "B"} {
				o := map[string]string{"A": "B", "B": "A"}[id]
				for _, at := range p.sentPublic(id, o, prev) {
					if at.Before(a.start.Add(-time.Second)) && at.After(last) {
						last = at
					}
				}
			}
			if gap := a.start.Sub(last); gap < 35*time.Second {
				t.Fatalf("attempt %d began %v after the last packet of the one before", i+1, gap)
			}
		}
	}
	// LAN addresses from both ends at the start of an attempt.
	for _, id := range []string{"A", "B"} {
		lan := map[string]string{"A": "192.168.2.20:51820", "B": "192.168.1.10:51820"}[id]
		c := p.b[id].punched[0]
		if !slices.Contains(c.addrs, netip.MustParseAddrPort(lan)) || c.at.Sub(attempts[0].start) > time.Second {
			t.Fatalf("%s first punched %v at %v", id, c.addrs, c.at.Sub(attempts[0].start))
		}
	}
	if d := p.m["A"].directs["B"]; d.lastError != errPunchTimeout || d.attempts < 7 {
		t.Fatalf("state %+v", d)
	}
}

// A path that authenticated ends the driver's attempt.
func TestPunchAdoptsThePathThatAuthenticated(t *testing.T) {
	p := newPair(t)
	d := p.m["A"].directs["B"]
	for i := 0; i < 50 && !d.active; i++ {
		p.step(200 * time.Millisecond)
	}
	p.b["A"].paths = []PunchedPath{{Path: Path{ID: "x", Generation: 1}, Remote: p.public("B")}}
	for i := 0; i < 100 && d.active; i++ {
		p.step(200 * time.Millisecond)
	}
	if d.active || d.link == nil || d.via != "public" || d.link.config.Endpoint != "udp://198.51.100.2:51820" || d.link.config.ID != "direct:B" {
		t.Fatalf("A did not take the path: %+v", d)
	}
}

// A LAN address of the peer's on this node's own network is taken at once,
// before a public path that authenticated earlier.
func TestPunchTakesAnOnLinkLANAddressAtOnce(t *testing.T) {
	p := newPair(t)
	localNets = func(netip.Prefix) []netip.Prefix { return []netip.Prefix{netip.MustParsePrefix("192.168.2.0/24")} }
	d := p.m["A"].directs["B"]
	for i := 0; i < 50 && !d.active; i++ {
		p.step(200 * time.Millisecond)
	}
	lan := netip.MustParseAddrPort("192.168.2.20:51820")
	p.b["A"].paths = []PunchedPath{{Path: Path{ID: "x", Generation: 1}, Remote: p.public("B")}, {Path: Path{ID: "y", Generation: 1}, Remote: lan}}
	p.step(200 * time.Millisecond)
	if d.active || d.link == nil || d.via != "lan" || d.link.config.Endpoint != "udp://"+lan.String() {
		t.Fatalf("A did not take the LAN path at once: %+v", d)
	}
}

// The peer's LAN address off this node's networks, reached through a VPN
// say, is no LAN path: it loses to a faster public path though it
// authenticated first, and wins only by being faster. The choice waits for
// the window.
func TestPunchTakesTheFastestPath(t *testing.T) {
	for _, c := range []struct {
		name           string
		lanRTT, pubRTT time.Duration
		want           string
	}{
		{"public faster", 37 * time.Millisecond, 3 * time.Millisecond, "public"},
		{"routed faster", 2 * time.Millisecond, 30 * time.Millisecond, "routed"},
		// Within a millisecond or a tenth: the kind of address decides.
		{"about as fast", 20 * time.Millisecond, 21 * time.Millisecond, "public"},
	} {
		t.Run(c.name, func(t *testing.T) {
			p := newPair(t)
			d := p.m["A"].directs["B"]
			for i := 0; i < 50 && !d.active; i++ {
				p.step(200 * time.Millisecond)
			}
			lanPath, pubPath := Path{ID: "y", Generation: 1}, Path{ID: "x", Generation: 1}
			p.rtt["A"][lanPath], p.rtt["A"][pubPath] = c.lanRTT, c.pubRTT
			p.b["A"].paths = []PunchedPath{{Path: lanPath, Remote: netip.MustParseAddrPort("192.168.2.20:51820")}}
			p.step(200 * time.Millisecond)
			if !d.active {
				t.Fatalf("A took the first path without comparing: %+v", d)
			}
			first := p.now
			p.step(time.Second)
			p.b["A"].paths = append(p.b["A"].paths, PunchedPath{Path: pubPath, Remote: p.public("B")})
			for i := 0; i < 100 && d.active; i++ {
				p.step(50 * time.Millisecond)
			}
			if d.active || d.link == nil || d.via != c.want {
				t.Fatalf("A took %+v", d)
			}
			if took := p.now.Sub(first); took < compareFor || took > compareFor+3*time.Second {
				t.Fatalf("decided %v after the first path", took)
			}
		})
	}
}

// Unmeasured, the paths go by kind: the public one before the peer's LAN
// address off this node's networks, though that authenticated first.
func TestPunchWithoutAnswersTakesTheBestKind(t *testing.T) {
	p := newPair(t)
	d := p.m["A"].directs["B"]
	for i := 0; i < 50 && !d.active; i++ {
		p.step(200 * time.Millisecond)
	}
	p.b["A"].paths = []PunchedPath{{Path: Path{ID: "y", Generation: 1}, Remote: netip.MustParseAddrPort("192.168.2.20:51820")}, {Path: Path{ID: "x", Generation: 1}, Remote: p.public("B")}}
	for i := 0; i < 200 && d.active; i++ {
		p.step(100 * time.Millisecond)
	}
	if d.link == nil || d.via != "public" {
		t.Fatalf("A took %+v", d)
	}
	// Not on the peer's LAN: once up, it looks for a faster path within a
	// minute, then twice as long each time.
	d.state, d.link.path = directUp, Path{ID: "x", Generation: 1}
	var gaps []time.Duration
	for i := 0; i < 6; i++ {
		d.reprobe = time.Time{}
		if i > 0 {
			d.reprobe = p.now
		}
		p.m["A"].startProbe(d, p.now)
		d.active, d.probe = false, false
		gaps = append(gaps, d.reprobeGap)
	}
	if !slices.Equal(gaps, []time.Duration{time.Minute, 2 * time.Minute, 4 * time.Minute, 8 * time.Minute, 10 * time.Minute, 10 * time.Minute}) {
		t.Fatalf("probe gaps %v", gaps)
	}
}

func TestViaOf(t *testing.T) {
	saved := localNets
	t.Cleanup(func() { localNets = saved })
	localNets = func(netip.Prefix) []netip.Prefix { return []netip.Prefix{netip.MustParsePrefix("10.20.20.0/24")} }
	m := &Manager{}
	d := &directState{configured: netip.MustParseAddrPort("10.20.20.9:51998"), remote: map[netip.AddrPort]string{
		netip.MustParseAddrPort("10.20.20.7:51998"):      "lan",
		netip.MustParseAddrPort("10.10.10.251:51999"):    "lan",
		netip.MustParseAddrPort("192.0.2.79:51820"):      "lan", // a relay's own public interface
		netip.MustParseAddrPort("192.0.2.34:51998"):      "public",
		netip.MustParseAddrPort("192.0.2.34:43173"):      "public",
		netip.MustParseAddrPort("100.100.100.100:51998"): "public",
	}}
	for addr, want := range map[string]string{
		"10.20.20.7:51998":            "lan",    // offered, on this node's network
		"10.20.20.9:51998":            "lan",    // configured, on it
		"10.10.10.251:51999":          "routed", // offered, through a VPN
		"10.20.20.2:51998":            "routed", // on this network, but a router's that masquerades for the peer
		"10.10.10.254:51998":          "routed", // likewise, elsewhere
		"192.0.2.79:51820":            "public", // offered as LAN, an internet address
		"192.0.2.34:43173":            "public", // offered
		"192.0.2.34:40000":            "public", // the peer's NAT, not offered
		"100.100.100.100:5199":        "routed", // carrier-grade NAT space
		"203.0.113.9:51998":           "public", // anything else on the internet
		"[::ffff:10.10.10.254]:51998": "routed",
	} {
		if got := m.viaOf(d, netip.MustParseAddrPort(addr)); got != want {
			t.Errorf("%s: %s, want %s", addr, got, want)
		}
	}
	d.configured = netip.MustParseAddrPort("198.51.100.7:51820")
	if got := m.viaOf(d, d.configured); got != "configured" {
		t.Errorf("configured: %s", got)
	}
}

func TestFasterPath(t *testing.T) {
	saved := localNets
	t.Cleanup(func() { localNets = saved })
	localNets = func(netip.Prefix) []netip.Prefix { return nil }
	m := &Manager{}
	d := &directState{remote: map[netip.AddrPort]string{netip.MustParseAddrPort("192.168.2.20:51820"): "lan", netip.MustParseAddrPort("198.51.100.2:51820"): "public"}}
	trial := func(addr string, ms ...float64) *pathTrial {
		t := &pathTrial{remote: netip.MustParseAddrPort(addr)}
		for _, x := range ms {
			t.samples = append(t.samples, time.Duration(x*float64(time.Millisecond)))
		}
		return t
	}
	routed, public := "192.168.2.20:51820", "198.51.100.2:51820"
	for _, c := range []struct {
		name string
		a, b *pathTrial
		want bool
	}{
		{"faster", trial(public, 3, 3, 3), trial(routed, 37, 37), true},
		{"slower", trial(public, 30, 31), trial(routed, 2, 2), false},
		{"the median, not one lucky answer", trial(routed, 1, 30, 30, 31), trial(public, 20, 20, 20), false},
		{"within a millisecond: kind", trial(public, 3.9), trial(routed, 3), true},
		{"within a tenth: kind", trial(public, 54), trial(routed, 50), true},
		{"within a tenth, the other way", trial(routed, 50), trial(public, 54), false},
		{"beyond a tenth", trial(public, 56), trial(routed, 50), false},
	} {
		if got := m.faster(d, c.a, c.b); got != c.want {
			t.Errorf("%s: %v", c.name, got)
		}
	}
	for _, c := range []struct {
		rtt, link time.Duration
		want      bool
	}{
		{3 * time.Millisecond, 37 * time.Millisecond, true},
		{30 * time.Millisecond, 37 * time.Millisecond, false}, // not by a fifth
		{4 * time.Millisecond, 8 * time.Millisecond, false},   // not by 5 ms
		{5 * time.Millisecond, 10 * time.Millisecond, true},
		{0, 10 * time.Millisecond, false},
	} {
		if got := clearlyFaster(c.rtt, c.link); got != c.want {
			t.Errorf("%v against a link of %v: %v", c.rtt, c.link, got)
		}
	}
}

// upPair has A, the driver, with a link to B up over B's LAN address with
// the given round trip, and the offers exchanged.
func upPair(t *testing.T, linkRTT time.Duration) (*pair, *directState) {
	p := newPair(t)
	m := p.m["A"]
	d := m.directs["B"]
	link := Path{ID: "l", Generation: 1}
	d.link = &candidateState{config: Candidate{ID: "direct:B", Peer: "B", Endpoint: "udp://192.168.2.20:51820"}, path: link, ready: true, routeReady: true,
		track: newTracker(m.cfg.Parameters, p.now), pending: map[uint64]string{}, early: map[uint64]time.Time{}, next: p.now}
	d.link.track.stats.SRTT = linkRTT
	d.state, d.via, d.local, d.remoteUp = directUp, "lan", true, true
	d.next = p.now.Add(time.Hour)
	p.rtt["A"][link] = linkRTT
	for i := 0; i < 50 && (d.remoteKey == "" || d.acked != m.offerKey()); i++ {
		p.step(200 * time.Millisecond)
	}
	if d.remoteKey == "" || d.acked != m.offerKey() {
		t.Fatal("offers not exchanged")
	}
	return p, d
}

// While a link is up its driver looks for a faster path now and then; one
// that is faster by a fifth and by 5 ms is prepared beside the link, which
// stays up meanwhile. The peer keeps its link through the probe attempt.
func TestProbeFindsAFasterPath(t *testing.T) {
	for _, c := range []struct {
		name            string
		link, candidate time.Duration
		switches        bool
	}{
		{"much faster", 37 * time.Millisecond, 2 * time.Millisecond, true},
		{"not by a fifth", 40 * time.Millisecond, 35 * time.Millisecond, false},
		{"not by 5 ms", 8 * time.Millisecond, 4 * time.Millisecond, false},
		{"by 5 ms", 9 * time.Millisecond, 4 * time.Millisecond, true},
	} {
		t.Run(c.name, func(t *testing.T) {
			p, d := upPair(t, c.link)
			if d.reprobe.IsZero() {
				t.Fatal("no probe scheduled")
			}
			if wait := d.reprobe.Sub(p.now); wait < ReprobeEvery*8/10 {
				t.Fatalf("probe in %v", wait)
			}
			attempts := d.attempts
			d.reprobe = p.now
			pub := Path{ID: "x", Generation: 1}
			p.rtt["A"][pub] = c.candidate
			p.step(200 * time.Millisecond)
			if !d.active || !d.probe {
				t.Fatalf("no probe attempt: %+v", d)
			}
			p.step(2 * time.Second)
			p.b["A"].paths = []PunchedPath{{Path: d.link.path, Remote: netip.MustParseAddrPort("192.168.2.20:51820"), Active: true}, {Path: pub, Remote: p.public("B")}}
			for i := 0; i < 400 && d.active; i++ {
				p.step(50 * time.Millisecond)
				if d.state != directUp || d.link == nil || d.link.path.ID != "l" {
					t.Fatalf("the link went down during the probe: %+v", d)
				}
			}
			if d.active || d.attempts != attempts {
				t.Fatalf("probe attempt did not end: %+v", d)
			}
			if got := d.upcoming != nil; got != c.switches {
				t.Fatalf("faster path prepared %v, want %v", got, c.switches)
			}
			if c.switches && (d.upcoming.config.Endpoint != "udp://"+p.public("B").String() || d.upcomingVia != "public") {
				t.Fatalf("prepared %+v", d.upcoming.config)
			}
		})
	}
}

// A probe attempt does not take the peer's link down.
func TestProbeKeepsThePeersLink(t *testing.T) {
	p := newPair(t)
	m := p.m["B"]
	d := m.directs["A"]
	d.link = &candidateState{config: Candidate{ID: "direct:A", Peer: "A", Endpoint: "udp://192.168.1.10:51820"}, path: Path{ID: "l", Generation: 1}, ready: true, routeReady: true,
		track: newTracker(m.cfg.Parameters, p.now), pending: map[uint64]string{}, early: map[uint64]time.Time{}}
	d.state, d.local, d.remoteUp = directUp, true, true
	ps := PunchState{From: "A", To: "B", Session: "s", Attempt: 9, Probe: true}
	m.receive(signal(t, "R", "B", "punch", message{Punch: &ps}))
	if !d.active || !d.probe || d.link == nil || d.state != directUp {
		t.Fatalf("B: %+v", d)
	}
	ps.Attempt, ps.Probe = 10, false
	m.receive(signal(t, "R", "B", "punch", message{Punch: &ps}))
	if !d.active || d.probe || d.link != nil || d.state != directPunching {
		t.Fatalf("a normal attempt should start over: %+v", d)
	}
}

func TestReachVerdict(t *testing.T) {
	ob := func(addr string, ok bool) Observer {
		o := Observer{ID: addr, Kind: "relay", OK: ok}
		if ok {
			o.Addr = netip.MustParseAddrPort(addr)
		}
		return o
	}
	same := []Observer{ob("203.0.113.1:51820", true), ob("203.0.113.1:51820", true)}
	for _, c := range []struct {
		name      string
		observers []Observer
		filtering string
		punched   bool
		verdict   string
		mapping   string
		consist   bool
	}{
		{"open", same, "open", false, "public", "stable", true},
		{"address", same, "address", false, "punchable", "stable", true},
		{"port", same, "port", false, "punchable", "stable", true},
		{"varies", []Observer{ob("203.0.113.1:40001", true), ob("203.0.113.1:40002", true)}, "port", false, "symmetric", "varies", true},
		{"no udp", []Observer{ob("x:1", false), ob("y:1", false)}, "unknown", false, "no_udp", "unknown", true},
		{"inconsistent", []Observer{ob("203.0.113.1:51820", true), ob("198.51.100.1:51820", true)}, "open", false, "unknown", "stable", false},
		{"one observer", []Observer{ob("203.0.113.1:51820", true), ob("y:1", false)}, "port", false, "unknown", "unknown", true},
		{"one observer, punched", []Observer{ob("203.0.113.1:51820", true)}, "port", true, "punchable", "unknown", true},
		{"untested filtering", same, "unknown", false, "unknown", "stable", true},
	} {
		s := reachVerdict(c.observers, c.filtering, false, c.punched)
		if s.Verdict != c.verdict || s.Mapping != c.mapping || s.Consistent != c.consist {
			t.Errorf("%s: %s/%s/%v, want %s/%s/%v", c.name, s.Verdict, s.Mapping, s.Consistent, c.verdict, c.mapping, c.consist)
		}
	}
	if s := reachVerdict([]Observer{ob("x:1", false)}, "unknown", true, false); s.Verdict != "unknown" {
		t.Errorf("without a UDP socket: %s", s.Verdict)
	}
}

// A relay tests only the address its own reflector saw, for nodes allowed
// to ask, a few times each; other relays test only for linked relays, once
// per address in a while, and not an address that has been talking to them.
func TestReachTestsAreBounded(t *testing.T) {
	nodes := []Node{{ID: "B", Role: "relay"}, {ID: "C", Role: "relay"}, {ID: "D", Role: "relay"}, {ID: "M", Role: "member", Relays: []string{"A"}}, {ID: "X", Role: "member"}}
	m, b := offerManager(t, Config{Network: "n", Self: "A", Role: "relay", Revision: 1, Nodes: nodes, Links: []Link{{Peer: "B"}, {Peer: "C"}, {Peer: "D"}}},
		map[string][]string{"M": {"A"}}, map[string]bool{"B": true, "C": true, "D": true})
	for _, s := range []string{"B", "C", "D"} {
		m.links[s] = &candidateState{config: Candidate{ID: "link:" + s, Peer: s}, ready: true, path: Path{ID: s, Generation: 1}}
	}
	id := randomID()
	ask := func(from string, rid [8]byte, target string) {
		r := Reach{ID: encodeID(rid), Target: target}
		m.receive(signal(t, from, "A", "reach", message{Reach: &r}))
	}
	now := time.Now()
	ask("X", id, "")
	if len(m.reach.pending) != 0 {
		t.Fatal("took a request from a node not allowed here")
	}
	ask("M", id, "192.0.2.99:1")
	b.seen[id] = netip.MustParseAddrPort("203.0.113.5:51820")
	m.reachServe(now)
	if !slices.Equal(b.tests, []netip.AddrPort{netip.MustParseAddrPort("203.0.113.5:51820")}) {
		t.Fatalf("tested %v, want only what the reflector saw", b.tests)
	}
	if got := b.types(); !slices.Equal(got, []string{"B:reach-relay", "C:reach-relay", "M:reach-ack"}) && !slices.Equal(got, []string{"C:reach-relay", "B:reach-relay", "M:reach-ack"}) {
		t.Fatalf("sent %v, want two other relays asked and the answer", got)
	}
	for range 5 {
		ask("M", randomID(), "")
	}
	if len(m.reach.pending) > 2 {
		t.Fatalf("%d requests from one member in a row", len(m.reach.pending))
	}

	b.tests = nil
	relay := func(from string, target string) {
		r := Reach{ID: encodeID(randomID()), Target: target}
		m.receive(signal(t, from, "A", "reach-relay", message{Reach: &r}))
	}
	relay("M", "203.0.113.6:1")
	relay("B", "203.0.113.6:1")
	relay("C", "203.0.113.6:1")
	if !slices.Equal(b.tests, []netip.AddrPort{netip.MustParseAddrPort("203.0.113.6:1")}) {
		t.Fatalf("tested %v, want one test for a linked relay", b.tests)
	}
	b.recent = true
	relay("B", "203.0.113.7:1")
	if len(b.tests) != 1 {
		t.Fatal("tested an address that has been talking to this relay")
	}
}

func encodeID(id [8]byte) string {
	const hexdigits = "0123456789abcdef"
	var s strings.Builder
	for _, c := range id {
		s.WriteByte(hexdigits[c>>4])
		s.WriteByte(hexdigits[c&15])
	}
	return s.String()
}

// The filtering a round finds follows the tests that arrived.
func TestReachRoundFiltering(t *testing.T) {
	for _, c := range []struct {
		seen byte
		want string
	}{{1 << transport.TestOther, "open"}, {1 << transport.TestRelay, "address"}, {0, "port"}} {
		b := &reachBackend{seen: c.seen}
		m := &Manager{backend: b, input: make(chan any, 1), ctx: context.Background()}
		ack := make(chan Reach, 1)
		ack <- Reach{Seen: true}
		go m.reachRound([8]byte{1}, true, []string{"192.0.2.1:1", "192.0.2.2:1"}, 1, nil, ack, netip.Prefix{})
		v := (<-m.input).(reachResult)
		if v.filtering != c.want || len(v.observers) != 2 {
			t.Errorf("seen %b: %s with %d observers, want %s", c.seen, v.filtering, len(v.observers), c.want)
		}
	}
}

type reachBackend struct {
	policyBackend
	seen byte
}

func (b *reachBackend) Reflect(context.Context, netip.AddrPort, [8]byte) (netip.AddrPort, error) {
	return netip.MustParseAddrPort("203.0.113.1:51820"), nil
}
func (b *reachBackend) TestsSeen([8]byte) byte { return b.seen }

// Each entry candidate shows what last happened to it.
func TestCandidateStates(t *testing.T) {
	if candErr(context.DeadlineExceeded) != candTimeout || candErr(transport.ErrSecurity) != candPin || candErr(syscall.ECONNREFUSED) != candRefused {
		t.Fatal("failure phrases")
	}
	m, _ := offerManager(t, Config{Network: "n", Self: "M", Role: "member", Revision: 1, Mode: "failover",
		Nodes:      []Node{{ID: "T", Role: "relay"}, {ID: "SG", Role: "relay"}, {ID: "LAX", Role: "relay"}},
		Candidates: []Candidate{{ID: "c1", Peer: "T", Punch: true}, {ID: "c2", Peer: "SG", Endpoint: "tls://192.0.2.1:1"}, {ID: "c3", Peer: "LAX", Endpoint: "tcp://192.0.2.2:1"}}},
		map[string][]string{}, map[string]bool{})
	now := time.Now()
	m.active = "c2"
	m.candidates["c3"].lastErr = candTimeout
	m.candidateStates(now)
	for id, want := range map[string]string{"c1": "punching", "c2": "active", "c3": "failing"} {
		if got := m.candidates[id].state; got != want {
			t.Errorf("%s: %s, want %s", id, got, want)
		}
	}
	if startable(m.candidates["c1"]) {
		t.Fatal("a punched candidate starts before it has a path")
	}
}

// While the addresses stay, a round that measures less keeps what earlier
// ones did: the mapping with one observer, the filtering without a test,
// and open against a test only the asked relay could send. A port result,
// or another public address, does not keep it.
func TestReachKeepsWhatItMeasured(t *testing.T) {
	pub := netip.MustParseAddrPort("198.51.100.7:51820")
	ok := func(id string, a netip.AddrPort) Observer { return Observer{ID: id, Kind: "relay", Addr: a, OK: true} }
	two := []Observer{ok("SG", pub), ok("LAX", pub)}
	one := []Observer{ok("SG", pub), {ID: "LAX", Kind: "relay"}}
	var r reachState
	judge := func(obs []Observer, filtering string) ReachStatus {
		f := r.hold(reachResult{observers: obs, filtering: filtering})
		mapping := ""
		if r.held.addr.IsValid() {
			mapping = r.held.mapping
		}
		return reachJudge(obs, f, mapping, false, false)
	}
	for i, c := range []struct {
		obs                        []Observer
		filtering                  string
		verdict, mapping, filtered string
	}{
		{two, "open", "public", "stable", "open"},
		{one, "unknown", "public", "stable", "open"}, // nothing new
		{one, "address", "public", "stable", "open"}, // no other relay sent
		{two, "port", "punchable", "stable", "port"}, // measured again
		{one, "unknown", "punchable", "stable", "port"},
		{[]Observer{ok("SG", netip.MustParseAddrPort("198.51.100.8:51820"))}, "unknown", "unknown", "unknown", "unknown"}, // another address
	} {
		s := judge(c.obs, c.filtering)
		if s.Verdict != c.verdict || s.Mapping != c.mapping || s.Filtering != c.filtered {
			t.Fatalf("round %d: %s/%s/%s, want %s/%s/%s", i+1, s.Verdict, s.Mapping, s.Filtering, c.verdict, c.mapping, c.filtered)
		}
	}
}

// A relay dialed by the one asking has no endpoint in its config; its
// reflector is named by where its UDP path comes from.
func TestReflectorOfADialingRelay(t *testing.T) {
	nodes := []Node{{ID: "SG", Role: "relay"}, {ID: "HOME", Role: "relay"}}
	m, b := offerManager(t, Config{Network: "n", Self: "LAX", Role: "relay", Revision: 1, Nodes: nodes, Links: []Link{{Peer: "HOME"}, {Peer: "SG"}}},
		map[string][]string{}, map[string]bool{"SG": true, "HOME": true})
	b.paths = []PunchedPath{{Path: Path{ID: "p", Generation: 1}, Remote: netip.MustParseAddrPort("192.0.2.79:51999"), Active: true}}
	if id := m.reflectorOf("192.0.2.79:51999", netip.Addr{}); id != "SG" && id != "HOME" {
		t.Fatalf("reflector named %q", id)
	}
	b.paths = nil
	if id := m.reflectorOf("192.0.2.79:51999", netip.Addr{}); id != "" {
		t.Fatalf("named %q without a path", id)
	}
}

// A punched entry candidate with an endpoint in its config handshakes with
// it from the start, with no relay to exchange addresses through, takes the
// path once it authenticates and reports how it was found.
func TestEntryDialsItsConfiguredEndpoint(t *testing.T) {
	for _, c := range []struct {
		name string
		nets []netip.Prefix
		via  string
	}{
		{"same LAN", []netip.Prefix{netip.MustParsePrefix("10.20.20.0/24")}, "lan"},
		{"elsewhere", nil, "configured"},
	} {
		t.Run(c.name, func(t *testing.T) {
			saved := localNets
			t.Cleanup(func() { localNets = saved })
			localNets = func(netip.Prefix) []netip.Prefix { return c.nets }
			nodes := []Node{{ID: "T", Role: "relay"}, {ID: "SG", Role: "relay"}}
			cfg := Config{Network: "n", Self: "M", Role: "member", Revision: 1, Nodes: nodes, Port: 51820,
				Candidates: []Candidate{{ID: "c1", Peer: "T", Punch: true, Endpoint: "udp://10.20.20.7:51998"}, {ID: "c2", Peer: "SG", Endpoint: "udp://192.0.2.1:51820"}}}
			m, b := offerManager(t, cfg, map[string][]string{}, map[string]bool{"T": true, "SG": true})
			ctx, cancel := context.WithCancel(context.Background())
			t.Cleanup(cancel)
			m.ctx = ctx
			p := m.candidates["c1"]
			p.config.Endpoint = "" // as configure leaves a punched candidate
			now := time.Unix(1_800_000_000, 0)
			b.clock = &now
			target := netip.MustParseAddrPort("10.20.20.7:51998")

			m.tickDirects(now)
			if len(b.punched) != 1 || !slices.Equal(b.punched[0].addrs, []netip.AddrPort{target}) {
				t.Fatalf("no initiation to the configured endpoint at start: %+v", b.punched)
			}
			if len(b.sent) != 0 {
				t.Fatalf("signals with no relay up: %v", b.types())
			}
			for i := 0; i < 4; i++ {
				now = now.Add(time.Second)
				m.tickDirects(now)
			}
			if len(b.punched) != 1 {
				t.Fatalf("initiations %d within 5 s", len(b.punched))
			}
			now = now.Add(time.Second)
			m.tickDirects(now)
			if len(b.punched) != 2 {
				t.Fatalf("initiations %d after 5 s", len(b.punched))
			}

			b.paths = []PunchedPath{{Path: Path{ID: "x", Generation: 1}, Remote: target}}
			now = now.Add(200 * time.Millisecond)
			m.tickDirects(now)
			d := m.directs["T"]
			if p.config.Endpoint != "udp://"+target.String() || d.state != directUp || d.via != c.via || !p.preparing {
				t.Fatalf("not taken: endpoint %q state %s via %s preparing %v", p.config.Endpoint, d.state, d.via, p.preparing)
			}
			var via string
			for _, x := range m.Snapshot().Candidates {
				if x.ID == "c1" {
					via = x.Via
				} else if x.Via != "" {
					t.Fatalf("%s has via %q", x.ID, x.Via)
				}
			}
			if via != c.via {
				t.Fatalf("snapshot via %q, want %q", via, c.via)
			}

			// Lost, it waits for the candidate's retry, then dials again.
			n := len(b.punched)
			b.paths = nil
			p.retry = now.Add(10 * time.Second)
			m.entryLost(p, now)
			for i := 0; i < 9; i++ {
				now = now.Add(time.Second)
				m.tickDirects(now)
			}
			if len(b.punched) != n || p.config.Endpoint != "" {
				t.Fatalf("dialed before the retry: %d", len(b.punched)-n)
			}
			now = now.Add(2 * time.Second)
			m.tickDirects(now)
			if len(b.punched) != n+1 {
				t.Fatal("did not dial again after the retry")
			}
		})
	}
}

// During an attempt the configured endpoint on this node's network is
// taken at once, before a public path that authenticated first.
func TestEntryAttemptTakesTheConfiguredLANAtOnce(t *testing.T) {
	saved := localNets
	t.Cleanup(func() { localNets = saved })
	localNets = func(netip.Prefix) []netip.Prefix { return []netip.Prefix{netip.MustParsePrefix("10.20.20.0/24")} }
	nodes := []Node{{ID: "T", Role: "relay"}}
	cfg := Config{Network: "n", Self: "M", Role: "member", Revision: 1, Nodes: nodes, Port: 51820,
		Candidates: []Candidate{{ID: "c1", Peer: "T", Punch: true, Endpoint: "udp://10.20.20.7:51998"}}}
	m, b := offerManager(t, cfg, map[string][]string{}, map[string]bool{"T": true})
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	m.ctx = ctx
	now := time.Unix(1_800_000_000, 0)
	b.clock = &now
	d := m.directs["T"]
	d.remote = map[netip.AddrPort]string{netip.MustParseAddrPort("198.51.100.2:40000"): "public"}
	m.begin(d, m.session, 1, now, false)
	b.paths = []PunchedPath{{Path: Path{ID: "a", Generation: 1}, Remote: netip.MustParseAddrPort("198.51.100.2:40000")}, {Path: Path{ID: "b", Generation: 1}, Remote: netip.MustParseAddrPort("10.20.20.7:51998")}}
	now = now.Add(300 * time.Millisecond)
	m.tickDirects(now)
	if d.active || d.via != "lan" || m.candidates["c1"].config.Endpoint != "udp://10.20.20.7:51998" {
		t.Fatalf("active %v via %s endpoint %s", d.active, d.via, m.candidates["c1"].config.Endpoint)
	}
}

// A relay linked to site relays only has no reflector to test through.
// Without NAT, its own address and port seen by two observers, it is public;
// behind NAT, as a member, or when a test could run it stays unknown.
func TestReachUntestedRelayWithoutNAT(t *testing.T) {
	pub := netip.MustParseAddrPort("192.0.2.203:51999")
	ok := func(id string, a netip.AddrPort) Observer { return Observer{ID: id, Kind: "relay", Addr: a, OK: true} }
	mapped := netip.AddrPortFrom(netip.AddrFrom16(pub.Addr().As16()), pub.Port())
	local := []netip.Addr{pub.Addr()}
	for i, c := range []struct {
		obs  []Observer
		port int
		want bool
	}{
		{[]Observer{ok("SG", pub), ok("VLINE", mapped)}, 51999, true},
		{[]Observer{ok("SG", pub), {ID: "VLINE", Kind: "relay"}}, 51999, false},                               // one answer
		{[]Observer{ok("SG", pub), ok("VLINE", netip.MustParseAddrPort("198.51.100.9:51999"))}, 51999, false}, // NAT
		{[]Observer{ok("SG", pub), ok("VLINE", netip.AddrPortFrom(pub.Addr(), 40000))}, 51999, false},         // another port
	} {
		if got := withoutNAT(c.obs, local, c.port); got != c.want {
			t.Errorf("case %d: %v, want %v", i+1, got, c.want)
		}
	}
	for _, c := range []struct {
		role     string
		untested bool
		want     string
	}{{"relay", true, "public"}, {"relay", false, "unknown"}, {"member", true, "unknown"}} {
		m, _ := offerManager(t, Config{Network: "n", Self: "BER", Role: c.role, Revision: 1, Port: 51999}, map[string][]string{}, map[string]bool{})
		m.reach.running, m.reach.id, m.reach.untested = true, [8]byte{7}, c.untested
		m.reachDone(reachResult{id: [8]byte{7}, observers: []Observer{ok("SG", pub), ok("VLINE", pub)}, filtering: "unknown", local: local}, time.Now())
		if got := m.reach.status.Verdict; got != c.want {
			t.Errorf("%s untested=%v: %s, want %s", c.role, c.untested, got, c.want)
		}
	}
}

// A punch that keeps failing the same way is one event, not one per retry
// (every ColdRetry, up to 5 minutes apart), for a direct link and for a
// punched entry candidate alike; after the link was up, a failure is one
// again.
func TestRepeatedPunchFailuresAreOneEvent(t *testing.T) {
	m, _ := offerManager(t, Config{Network: "n", Self: "M", Role: "member", Revision: 1, Mode: "failover",
		Nodes:      []Node{{ID: "T", Role: "relay"}},
		Candidates: []Candidate{{ID: "c1", Peer: "T", Punch: true}}},
		map[string][]string{}, map[string]bool{})
	var events []string
	m.hooks.Event = func(kind, detail string) { events = append(events, kind+" "+detail) }
	now := time.Now()
	peer := &directState{peer: "B", kind: punchDirect, state: directRelayed}
	entry := m.directs["T"]
	if entry == nil || entry.kind != punchEntry {
		t.Fatalf("no punched entry: %+v", m.directs)
	}
	for range 5 {
		m.punchFailed(peer, now)
		m.punchFailed(entry, now)
	}
	peer.lastError = "" // up in between
	m.punchFailed(peer, now)
	want := []string{"direct.failed B: " + errPunchTimeout, "path.failed c1: " + errPunchTimeout, "direct.failed B: " + errPunchTimeout}
	if !slices.Equal(events, want) {
		t.Fatalf("events %q, want %q", events, want)
	}
}

// The end that does not drive a link takes no path of its own: it waits,
// past the attempt's end, for the one the driver prepares, takes that, and
// releases what else the attempt opened.
func TestFollowerTakesTheDriversPath(t *testing.T) {
	p := newPair(t)
	m := p.m["B"]
	d := m.directs["A"]
	for i := 0; i < 50 && !d.active; i++ {
		p.step(200 * time.Millisecond)
	}
	if d.driver || !d.active {
		t.Fatalf("B: %+v", d)
	}
	routed, public := Path{ID: "y", Generation: 1}, Path{ID: "x", Generation: 1}
	p.rtt["B"][routed], p.rtt["B"][public] = 2*time.Millisecond, 30*time.Millisecond
	p.b["B"].paths = []PunchedPath{{Path: routed, Remote: netip.MustParseAddrPort("192.168.1.10:51820")}, {Path: public, Remote: p.public("A")}}
	for p.now.Before(d.until.Add(followGrace - time.Second)) {
		p.step(200 * time.Millisecond)
	}
	if !d.active || d.link != nil || len(d.trials) != 0 {
		t.Fatalf("B chose on its own: %+v", d)
	}
	released := p.b["B"].releases

	// A prepares the public path.
	in := &inbound{peer: "A", path: public, token: nonce(), generation: 1, challenges: map[string]time.Time{}, lastSeen: time.Now()}
	m.inbound[public] = in
	r := signal(t, "A", "B", "prepare", message{Token: in.token, Generation: 1})
	r.Path = public
	m.receive(r)
	if d.active || d.link == nil || d.via != "public" || d.link.config.Endpoint != "udp://"+p.public("A").String() {
		t.Fatalf("B did not take A's path: %+v", d)
	}
	if p.b["B"].releases != released+1 {
		t.Fatalf("released %d paths, want the other one", p.b["B"].releases-released)
	}

	// With the link up A prepares another path: B makes it the next one
	// and keeps the link meanwhile; the same again changes nothing.
	d.state, d.link.path, d.link.preparing = directUp, public, false
	for i := 0; i < 2; i++ {
		m.followPrepared("A", routed, p.now)
		if d.link.path != public || d.upcoming == nil || d.upcoming.config.Endpoint != "udp://192.168.1.10:51820" || d.upcomingVia != "routed" {
			t.Fatalf("B: link %+v next %+v", d.link, d.upcoming)
		}
	}
	m.followPrepared("A", public, p.now)
	if d.upcoming == nil {
		t.Fatal("the link's own path dropped the next one")
	}
}

// Without the driver's path the follower's attempt ends a little after the
// driver's would.
func TestFollowerGivesUpAfterTheGrace(t *testing.T) {
	p := newPair(t)
	d := p.m["B"].directs["A"]
	for i := 0; i < 50 && !d.active; i++ {
		p.step(200 * time.Millisecond)
	}
	p.b["B"].paths = []PunchedPath{{Path: Path{ID: "x", Generation: 1}, Remote: p.public("A")}}
	until := d.until
	for d.active && p.now.Before(until.Add(time.Minute)) {
		p.step(200 * time.Millisecond)
	}
	if took := p.now.Sub(until); d.active || d.link != nil || took < followGrace || took > followGrace+time.Second {
		t.Fatalf("ended %v after the attempt: %+v", took, d)
	}
}

// A ping measuring a path that arrives on one the peer probes already is
// answered without touching that one's state; a new path takes the place
// of one no longer probed.
func TestTrialPingsLeaveTheLinkAlone(t *testing.T) {
	p := newPair(t)
	m, b := p.m["B"], p.b["B"]
	link := Path{ID: "l", Generation: 1}
	in := &inbound{peer: "A", path: link, token: nonce(), generation: 1, challenges: map[string]time.Time{}, lastSeen: time.Now(), prepared: true}
	m.inbound[link] = in
	ping := func(path Path, token string, trial bool) bool {
		b.sent = nil
		r := signal(t, "A", "B", "ping", message{Nonce: nonce(), Token: token, Generation: path.Generation, Probe: 7, Trial: trial})
		r.Path = path
		m.receive(r)
		for _, s := range b.sent {
			var v message
			json.Unmarshal(s.raw, &v)
			if v.Type == "pong" && s.path == path && v.Token == token {
				return true
			}
		}
		return false
	}
	if !ping(link, nonce(), true) || m.inbound[link] != in || !in.prepared {
		t.Fatalf("trial ping on the link: answered, and the link's state %+v", m.inbound[link])
	}
	// Three paths of A's probed: a fourth is not answered, unless one of
	// them is no longer probed.
	for _, id := range []string{"a", "b"} {
		m.inbound[Path{ID: id, Generation: 1}] = &inbound{peer: "A", path: Path{ID: id, Generation: 1}, token: nonce(), generation: 1, challenges: map[string]time.Time{"n": time.Now()}, lastSeen: time.Now()}
	}
	in.challenges["n"] = time.Now()
	if ping(Path{ID: "c", Generation: 1}, nonce(), true) {
		t.Fatal("a fourth path answered")
	}
	stale := m.inbound[Path{ID: "a", Generation: 1}]
	stale.challenges, stale.lastSeen = map[string]time.Time{}, time.Now().Add(-20*time.Second)
	if !ping(Path{ID: "c", Generation: 1}, nonce(), true) || m.inbound[Path{ID: "a", Generation: 1}] != nil || m.inbound[link] != in {
		t.Fatal("the path no longer probed was not replaced")
	}
}

// Every different address the reflectors saw is offered, with the LAN
// addresses cut to keep the offer within maxCandidates.
func TestOffersWhatEveryReflectorSaw(t *testing.T) {
	saved := localAddrs
	t.Cleanup(func() { localAddrs = saved })
	var lan []netip.Addr
	for i := 1; i <= 7; i++ {
		lan = append(lan, netip.AddrFrom4([4]byte{10, 0, 0, byte(i)}))
	}
	localAddrs = func(netip.Prefix) []netip.Addr { return lan[:1] }
	nodes := []Node{{ID: "R", Role: "relay"}, {ID: "A", Role: "member", Relays: []string{"R"}}, {ID: "B", Role: "member", Relays: []string{"R"}}}
	m, _ := offerManager(t, Config{Network: "n", Self: "A", Role: "member", Revision: 1, Nodes: nodes, Port: 51998, Links: []Link{{Peer: "B", Punch: true}}}, map[string][]string{}, map[string]bool{"R": true})
	seen := map[string]netip.AddrPort{"192.0.2.1:1": netip.MustParseAddrPort("192.0.2.34:43173"), "192.0.2.2:1": netip.MustParseAddrPort("192.0.2.34:43173"), "192.0.2.4:1": netip.MustParseAddrPort("192.0.2.34:51998")}
	m.backend = &reflectingBackend{m.backend.(*offerBackend), seen}
	m.cfg.Reflectors = []string{"192.0.2.1:1", "192.0.2.2:1", "192.0.2.3:1", "192.0.2.4:1"}
	m.input = make(chan any, 1)
	m.discover(time.Now())
	m.reflectedAddr((<-m.input).(reflected), time.Now())
	if want := []string{"lan:10.0.0.1:51998", "public:192.0.2.34:43173", "public:192.0.2.34:51998"}; !slices.Equal(m.local, want) {
		t.Fatalf("offered %v, want %v", m.local, want)
	}
	localAddrs = func(netip.Prefix) []netip.Addr { return lan }
	m.alsoSeen = []netip.AddrPort{netip.MustParseAddrPort("192.0.2.34:51998"), netip.MustParseAddrPort("192.0.2.34:40000"), netip.MustParseAddrPort("192.0.2.34:40001")}
	m.updateLocal()
	if len(m.local) != maxCandidates || !slices.Contains(m.local, "public:192.0.2.34:43173") || !slices.Contains(m.local, "public:192.0.2.34:40000") || slices.Contains(m.local, "public:192.0.2.34:40001") {
		t.Fatalf("offered %v", m.local)
	}
}

type reflectingBackend struct {
	*offerBackend
	seen map[string]netip.AddrPort
}

func (b *reflectingBackend) Reflect(_ context.Context, server netip.AddrPort, _ [8]byte) (netip.AddrPort, error) {
	if a, ok := b.seen[netip.AddrPortFrom(server.Addr().Unmap(), server.Port()).String()]; ok {
		return a, nil
	}
	return netip.AddrPort{}, context.DeadlineExceeded
}
