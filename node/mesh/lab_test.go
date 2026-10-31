package mesh

import (
	"crypto/ecdh"
	"crypto/rand"
	"encoding/base64"
	"encoding/binary"
	"fmt"
	mrand "math/rand/v2"
	"net"
	"net/netip"
	"os"
	"slices"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"golang.zx2c4.com/wireguard/tun/tuntest"
)

var (
	meshPrefix    = netip.MustParsePrefix("100.96.0.0/16")
	controlPrefix = netip.MustParsePrefix("100.96.255.0/24")
)

// spec is a small topology; every relay links to every other one, the
// earlier name dialing.
type spec struct {
	relays  []string
	members map[string][]string // entry candidates in order
	direct  [][2]string         // the first member dials
	subnets map[string][]netip.Prefix
	rules   map[string][]Rule
}

type labNode struct {
	name   string
	addr   netip.Addr
	priv   string
	pub    string
	port   int
	member bool
}

type lab struct {
	t     *testing.T
	spec  spec
	nodes map[string]*labNode
	dirs  map[string]string
	edit  func(name string, c *Config) // applied to every generated config
}

// freePort is a UDP port free on every address, as a node binds it, for
// the node to bind a moment later. It is drawn at random from below the
// ephemeral range: a ":0" bind or a connection, also in a test process
// running beside this one, takes no port there in that moment, as one
// handed out from the range did. One process never draws a port twice.
func freePort(t *testing.T) int {
	t.Helper()
	portsMu.Lock()
	defer portsMu.Unlock()
	low := ephemeralLow()
	for range 100 {
		p := 10000 + mrand.IntN(low-10000)
		if portsDrawn[p] {
			continue
		}
		c4, err := net.ListenUDP("udp4", &net.UDPAddr{Port: p})
		if err != nil {
			continue
		}
		c4.Close()
		c, err := net.ListenUDP("udp", &net.UDPAddr{Port: p})
		if err != nil {
			continue
		}
		c.Close()
		portsDrawn[p] = true
		return p
	}
	t.Fatal("no free port")
	return 0
}

var (
	portsMu    sync.Mutex
	portsDrawn = map[int]bool{}
)

// ephemeralLow is where the ports handed out for ":0" begin: Linux says,
// macOS and Windows start at 49152.
func ephemeralLow() int {
	if b, err := os.ReadFile("/proc/sys/net/ipv4/ip_local_port_range"); err == nil {
		if f := strings.Fields(string(b)); len(f) == 2 {
			if v, err := strconv.Atoi(f[0]); err == nil && v > 11000 {
				return v
			}
		}
	}
	return 49152
}

func newKey(t *testing.T) (priv, pub string) {
	k, err := ecdh.X25519().GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return base64.StdEncoding.EncodeToString(k.Bytes()), base64.StdEncoding.EncodeToString(k.PublicKey().Bytes())
}

func newLab(t *testing.T, s spec) *lab {
	l := &lab{t: t, spec: s, nodes: map[string]*labNode{}, dirs: map[string]string{}}
	for i, r := range s.relays {
		priv, pub := newKey(t)
		l.nodes[r] = &labNode{name: r, addr: netip.AddrFrom4([4]byte{100, 96, 0, byte(1 + i)}), priv: priv, pub: pub, port: freePort(t)}
	}
	var members []string
	for m := range s.members {
		members = append(members, m)
	}
	sort.Strings(members)
	for i, m := range members {
		priv, pub := newKey(t)
		l.nodes[m] = &labNode{name: m, addr: netip.AddrFrom4([4]byte{100, 96, 0, byte(11 + i)}), priv: priv, pub: pub, port: freePort(t), member: true}
	}
	return l
}

func (l *lab) endpoint(name string) string {
	return fmt.Sprintf("udp://127.0.0.1:%d", l.nodes[name].port)
}

func (l *lab) config(name string) *Config {
	self := l.nodes[name]
	role := roleRelay
	if self.member {
		role = roleMember
	}
	c := &Config{Schema: ConfigSchema, Revision: 1, Network: Network{ID: "lab", Prefix: meshPrefix, ControlPrefix: controlPrefix, MTU: 1400},
		Self:  Self{ID: name, Name: name, Role: role, Address: self.addr, ControlAddress: netip.AddrFrom4([4]byte{100, 96, 255, self.addr.As4()[3]}), Listen: Listen{UDP: self.port}},
		Rules: l.spec.rules[name]}
	if sub := l.spec.subnets[name]; len(sub) > 0 {
		c.Self.Gateway = &Gateway{Subnets: sub}
	}
	var names []string
	for n := range l.nodes {
		names = append(names, n)
	}
	sort.Strings(names)
	for _, n := range names {
		if n == name {
			continue
		}
		o := l.nodes[n]
		nd := Node{ID: n, Name: n, Role: roleRelay, Address: o.addr, ControlAddress: netip.AddrFrom4([4]byte{100, 96, 255, o.addr.As4()[3]}),
			PublicKey: o.pub, SignKey: testSignPub(n), E2EKey: testE2EPub(n), Subnets: l.spec.subnets[n]}
		if o.member {
			nd.Role, nd.Relays = roleMember, l.spec.members[n]
		}
		c.Nodes = append(c.Nodes, nd)
	}
	if !self.member {
		c.NextHops = map[string]string{}
		for _, r := range l.spec.relays {
			if r == name {
				continue
			}
			link := Link{Peer: r, Kind: kindRelay}
			if name < r {
				link.Dial, link.Endpoint = true, l.endpoint(r)
			}
			c.Links = append(c.Links, link)
			c.NextHops[r] = r
		}
		c.RelayLinks = relayLinksOf(c)
		return l.edited(name, c)
	}
	for _, d := range l.spec.direct {
		switch name {
		case d[0]:
			c.Links = append(c.Links, Link{Peer: d[1], Kind: kindDirect, Dial: true, Endpoint: l.endpoint(d[1])})
		case d[1]:
			c.Links = append(c.Links, Link{Peer: d[0], Kind: kindDirect})
		}
	}
	if cands := l.spec.members[name]; len(cands) > 0 {
		c.Entry = &Entry{Mode: "fixed"}
		for i, r := range cands {
			c.Entry.Candidates = append(c.Entry.Candidates, Candidate{ID: fmt.Sprintf("c%d", i+1), Relay: r, Endpoint: l.endpoint(r)})
		}
	}
	return l.edited(name, c)
}

func (l *lab) edited(name string, c *Config) *Config {
	if l.edit != nil {
		l.edit(name, c)
	}
	return c
}

type nopHost struct{}

func (nopHost) apply(hostConfig) error { return nil }
func (nopHost) heal() []string         { return nil }
func (nopHost) ready() bool            { return true }
func (nopHost) close()                 {}

type running struct {
	*engine
	ct  *tuntest.ChannelTUN
	cfg *Config
}

func (l *lab) start(name string, attach Attachment) *running {
	l.t.Helper()
	c := l.config(name)
	if err := c.check(); err != nil {
		l.t.Fatalf("%s: %v", name, err)
	}
	if attach == nil {
		attach = newStaticAttachment(c)
	}
	ct := tuntest.NewChannelTUN()
	if l.dirs[name] == "" {
		l.dirs[name] = l.t.TempDir()
	}
	writeTestKeys(l.t, l.dirs[name], name)
	e, err := startEngine(l.dirs[name], c, l.nodes[name].priv, ct.TUN(), nopHost{}, attach, &eventLog{})
	if err != nil {
		l.t.Fatalf("%s: %v", name, err)
	}
	l.t.Cleanup(e.close)
	return &running{engine: e, ct: ct, cfg: c}
}

// up starts relays first, so members' first handshakes find them, and
// waits until every dialed peer has a session.
func (l *lab) up() map[string]*running {
	out := map[string]*running{}
	for _, r := range l.spec.relays {
		out[r] = l.start(r, nil)
	}
	var members []string
	for m := range l.spec.members {
		members = append(members, m)
	}
	sort.Strings(members)
	for _, m := range members {
		out[m] = l.start(m, nil)
	}
	for _, n := range out {
		for _, p := range peersOf(n) {
			if p.Endpoint != "" {
				kick(n.dev, p.Key)
			}
		}
	}
	waitHandshakes(l.t, out)
	return out
}

func waitHandshakes(t *testing.T, nodes map[string]*running) {
	t.Helper()
	deadline := time.Now().Add(15 * time.Second)
	for name, n := range nodes {
		for {
			_, st := deviceState(n.dev)
			done := true
			for _, p := range peersOf(n) {
				if s := st[p.Key]; p.Endpoint != "" && (s == nil || s.handshakeNano == 0) {
					done = false
				}
			}
			if done {
				break
			}
			if time.Now().After(deadline) {
				t.Fatalf("%s: handshakes did not complete", name)
			}
			time.Sleep(50 * time.Millisecond)
		}
	}
}

func packet(src, dst netip.Addr, proto byte, ttl byte, id uint16, fragOff uint16, more bool, l4 []byte) []byte {
	b := make([]byte, 20+len(l4))
	b[0] = 0x45
	binary.BigEndian.PutUint16(b[2:], uint16(len(b)))
	binary.BigEndian.PutUint16(b[4:], id)
	ff := fragOff
	if more {
		ff |= 0x2000
	}
	binary.BigEndian.PutUint16(b[6:], ff)
	b[8], b[9] = ttl, proto
	copy(b[12:], src.AsSlice())
	copy(b[16:], dst.AsSlice())
	binary.BigEndian.PutUint16(b[10:], headerChecksum(b[:20]))
	copy(b[20:], l4)
	return b
}

func ports(sport, dport uint16, size int) []byte {
	l4 := make([]byte, size)
	binary.BigEndian.PutUint16(l4[0:], sport)
	binary.BigEndian.PutUint16(l4[2:], dport)
	return l4
}

func icmp(typ byte, id uint16, rest []byte) []byte {
	l4 := make([]byte, 8+len(rest))
	l4[0] = typ
	binary.BigEndian.PutUint16(l4[4:], id)
	copy(l4[8:], rest)
	return l4
}

func send(n *running, pkt []byte) { n.ct.Outbound <- pkt }

func recv(n *running, within time.Duration) []byte {
	select {
	case p := <-n.ct.Inbound:
		return p
	case <-time.After(within):
		return nil
	}
}

func expect(t *testing.T, n *running, what string) []byte {
	t.Helper()
	p := recv(n, 5*time.Second)
	if p == nil {
		t.Fatalf("%s: nothing arrived", what)
	}
	return p
}

func expectNone(t *testing.T, n *running, what string) {
	t.Helper()
	if p := recv(n, 400*time.Millisecond); p != nil {
		t.Fatalf("%s: unexpected packet %x", what, p[:20])
	}
}

func (l *lab) addr(name string) netip.Addr { return l.nodes[name].addr }

// peersOf reads the peer list the engine rewrites when routes change.
func peersOf(n *running) []peerSpec {
	n.engine.mu.Lock()
	defer n.engine.mu.Unlock()
	return slices.Clone(n.peers)
}

func allowedOf(n *running, peer string) []netip.Prefix {
	_, st := deviceState(n.dev)
	for _, p := range peersOf(n) {
		if p.ID == peer {
			if s := st[p.Key]; s != nil {
				return sortPrefixes(s.allowed)
			}
		}
	}
	return nil
}

func handshakeOf(n *running, peer string) int64 {
	_, st := deviceState(n.dev)
	for _, p := range peersOf(n) {
		if p.ID == peer {
			if s := st[p.Key]; s != nil {
				return s.handshakeNano
			}
		}
	}
	return -1
}

var allowICMP = []Rule{{Proto: "icmp"}}

func twoMembers(rulesA, rulesB []Rule) spec {
	return spec{relays: []string{"R"}, members: map[string][]string{"A": {"R"}, "B": {"R"}},
		rules: map[string][]Rule{"A": rulesA, "B": rulesB}}
}

func TestRelayForwardsWithoutTheOS(t *testing.T) {
	l := newLab(t, twoMembers(allowICMP, allowICMP))
	n := l.up()
	a, r, b := n["A"], n["R"], n["B"]
	send(a, packet(l.addr("A"), l.addr("B"), protoICMP, 64, 1, 0, false, icmp(8, 7, []byte("ping"))))
	got := expect(t, b, "echo at B")
	// The relay forwards the end-to-end packet and lowers its outer TTL;
	// the packet inside arrives as A sent it.
	if got[8] != 64 || headerChecksum(got[:20]) != 0 {
		t.Fatalf("TTL at B = %d, checksum %x: the packet inside was changed", got[8], headerChecksum(got[:20]))
	}
	expectNone(t, r, "R's own OS")
	send(b, packet(l.addr("B"), l.addr("A"), protoICMP, 64, 2, 0, false, icmp(0, 7, []byte("ping"))))
	expect(t, a, "echo reply at A")
	if r.tun.stats.Forwarded.Load() < 2 || r.tun.stats.Delivered.Load() != 0 {
		t.Fatalf("R forwarded %d, delivered %d", r.tun.stats.Forwarded.Load(), r.tun.stats.Delivered.Load())
	}
	st := r.status()
	if !st.Ready || len(st.Peers) != 2 || st.Owners["A"].Relay != "R" || st.Counters.Forwarded < 2 {
		t.Fatalf("relay status %+v", st)
	}
	if as := a.status(); as.Entry == nil || as.Entry.Candidate != "c1" || as.Entry.Relay != "R" || as.Peers[0].LastHandshakeUnixNano == "0" {
		t.Fatalf("member status %+v", as)
	}
	if a.e2e.status().Sessions != 1 || b.e2e.status().Sessions != 1 || a.e2e.handshake("B") == 0 {
		t.Fatalf("end-to-end sessions: A %+v, B %+v", a.e2e.status(), b.e2e.status())
	}
}

// R1 reaches R3 through R2 while R2 reaches R1 through R3, as relays that
// choose next hops independently can. R2 has nothing routed to R1, so only
// transit acceptance lets it take A's packets from R1.
func TestAsymmetricRelayPathsForward(t *testing.T) {
	l := newLab(t, spec{relays: []string{"R1", "R2", "R3"}, members: map[string][]string{"A": {"R1"}, "B": {"R3"}},
		rules: map[string][]Rule{"A": allowICMP, "B": allowICMP}})
	l.edit = func(name string, c *Config) {
		switch name {
		case "R1":
			c.NextHops["R3"] = "R2"
		case "R2":
			c.NextHops["R1"] = "R3"
		}
	}
	n := l.up()
	if got := allowedOf(n["R2"], "R1"); len(got) != 0 {
		t.Fatalf("R2 routes %v to R1, want nothing", got)
	}
	send(n["A"], packet(l.addr("A"), l.addr("B"), protoICMP, 64, 1, 0, false, icmp(8, 7, []byte("ping"))))
	expect(t, n["B"], "echo at B")
	if n["R2"].tun.stats.Forwarded.Load() == 0 {
		t.Fatal("R2 forwarded nothing")
	}
	send(n["B"], packet(l.addr("B"), l.addr("A"), protoICMP, 64, 2, 0, false, icmp(0, 7, []byte("ping"))))
	expect(t, n["A"], "echo reply at A")

	// A member stays held to its own addresses.
	send(n["A"], packet(l.addr("R2"), l.addr("B"), protoICMP, 64, 3, 0, false, icmp(8, 7, []byte("spoof"))))
	expectNone(t, n["B"], "spoofed source from a member")
}

func TestTransitSourcesOnlyFromRelays(t *testing.T) {
	l := newLab(t, spec{relays: []string{"R1", "R2"}, members: map[string][]string{"A": {"R1"}},
		subnets: map[string][]netip.Prefix{"R2": {netip.MustParsePrefix("10.20.0.0/24")}}})
	c := l.config("R1")
	var peers []peerSpec
	for _, name := range []string{"R2", "A"} {
		kind := kindRelay
		if name == "A" {
			kind = kindMember
		}
		k, _ := keyHex(l.nodes[name].pub)
		peers = append(peers, peerSpec{ID: name, Kind: kind, Key: k})
	}
	tun := &meshTUN{}
	tun.route.Store(routingFor(c, peers))
	key := func(name string) [32]byte {
		b, _ := base64.StdEncoding.DecodeString(l.nodes[name].pub)
		return [32]byte(b)
	}
	for _, tc := range []struct {
		from, src string
		want      bool
	}{
		{"R2", "100.96.0.11", true},
		// Not an address of a node: relays forward only end-to-end packets,
		// whose sources are nodes' addresses, also for a gateway's subnet.
		{"R2", "100.96.3.4", false},
		{"R2", "10.20.0.9", false},
		{"R2", "100.96.255.2", false},
		{"R2", "192.168.1.1", false},
		{"A", "100.96.0.11", false},
	} {
		if got := tun.AuthorizeSource(key(tc.from), netip.MustParseAddr(tc.src).AsSlice()); got != tc.want {
			t.Errorf("from %s src %s: %v, want %v", tc.from, tc.src, got, tc.want)
		}
	}
	// A member takes nodes' addresses from its entry, not from a node it
	// is linked to directly.
	c.Self.Role = roleMember
	peers[0].Kind, peers[1].Kind = kindEntry, kindDirect
	tun.route.Store(routingFor(c, peers))
	if !tun.AuthorizeSource(key("R2"), netip.MustParseAddr("100.96.0.11").AsSlice()) ||
		tun.AuthorizeSource(key("R2"), netip.MustParseAddr("10.20.0.9").AsSlice()) {
		t.Error("a member's entry: only nodes' addresses")
	}
	if tun.AuthorizeSource(key("A"), l.addr("R2").AsSlice()) {
		t.Error("a member accepted another node's address from a direct peer")
	}
}

// testNodes is a table of nodes at addrs, this one at self.
func testNodes(self netip.Addr, addrs ...string) *e2eTable {
	t := &e2eTable{self: self, byAddr: map[netip.Addr]*e2eNode{}, byID: map[string]*e2eNode{}}
	for _, a := range addrs {
		n := &e2eNode{id: a, addr: netip.MustParseAddr(a)}
		n.prefixes = []netip.Prefix{host(n.addr)}
		t.byAddr[n.addr], t.byID[a] = n, n
	}
	return t
}

func TestMemberDoesNotForward(t *testing.T) {
	ct := tuntest.NewChannelTUN()
	self := netip.MustParseAddr("100.96.0.11")
	r := &routing{local: []netip.Prefix{host(self)}, control: controlPrefix, routes: []netip.Prefix{meshPrefix},
		nodes: testNodes(self, "100.96.0.12", "100.96.0.13")}
	s := newScheduler(16, 1<<20)
	mt := newMeshTUN(ct.TUN(), r, newFilter(nil), s, 1400)
	defer mt.Close()
	src, dst := netip.MustParseAddr("100.96.0.12"), netip.MustParseAddr("100.96.0.13")
	pkt := outerPacket(src, dst, make([]byte, 32))
	mt.Write([][]byte{append([]byte(nil), pkt...)}, 0)
	if mt.stats.DroppedNotRelay.Load() != 1 || s.queuedBytes() != 0 {
		t.Fatalf("member passed on a packet for someone else: notRelay %d queued %d", mt.stats.DroppedNotRelay.Load(), s.queuedBytes())
	}
	relay := *r
	relay.relay = true
	mt.route.Store(&relay)
	mt.Write([][]byte{append([]byte(nil), pkt...)}, 0)
	if mt.stats.Forwarded.Load() != 1 || s.queuedBytes() != len(pkt) {
		t.Fatal("relay did not forward")
	}
	// A relay forwards only end-to-end packets between nodes.
	for _, bad := range [][]byte{
		packet(src, dst, protoICMP, 64, 1, 0, false, icmp(8, 1, nil)),
		outerPacket(netip.MustParseAddr("100.96.3.4"), dst, make([]byte, 32)),
		outerPacket(src, netip.MustParseAddr("100.96.3.4"), make([]byte, 32)),
	} {
		mt.Write([][]byte{bad}, 0)
	}
	if mt.stats.DroppedPlain.Load() != 3 || mt.stats.Forwarded.Load() != 1 {
		t.Fatalf("relay forwarded what is not end to end: plain dropped %d, forwarded %d", mt.stats.DroppedPlain.Load(), mt.stats.Forwarded.Load())
	}
	last := outerPacket(src, dst, make([]byte, 32))
	last[8] = 1
	last[10], last[11] = 0, 0
	binary.BigEndian.PutUint16(last[10:12], headerChecksum(last[:20]))
	mt.Write([][]byte{last}, 0)
	if mt.stats.DroppedTTL.Load() != 1 {
		t.Fatal("relay forwarded an outer TTL of 1")
	}
	ctrl := packet(netip.MustParseAddr("100.96.255.12"), netip.MustParseAddr("100.96.255.11"), protoICMP, 64, 1, 0, false, icmp(8, 1, nil))
	mt.Write([][]byte{ctrl}, 0)
	if mt.stats.DroppedBad.Load() != 1 {
		t.Fatal("control address accepted on the business device")
	}
}

func TestOwnAddressStaysOnTheMachine(t *testing.T) {
	ct := tuntest.NewChannelTUN()
	self := netip.MustParseAddr("100.96.0.11")
	r := &routing{local: []netip.Prefix{host(self)}, control: controlPrefix, routes: []netip.Prefix{meshPrefix}}
	s := newScheduler(16, 1<<20)
	mt := newMeshTUN(ct.TUN(), r, newFilter(nil), s, 1400)
	defer mt.Close()
	pkt := packet(self, self, protoICMP, 64, 1, 0, false, icmp(8, 1, nil))
	ct.Outbound <- append([]byte(nil), pkt...)
	select {
	case got := <-ct.Inbound:
		if string(got) != string(pkt) {
			t.Fatal("packet to the own address came back changed")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("packet to the own address did not come back to the OS")
	}
	if s.queuedBytes() != 0 {
		t.Fatal("packet to the own address was queued for the tunnel")
	}
}

func TestFilterIsDefaultDenyWithReplies(t *testing.T) {
	sshFromA := []Rule{{Src: []netip.Prefix{netip.MustParsePrefix("100.96.0.11/32")}, Proto: "tcp", Ports: [][2]uint16{{22, 22}}}}
	l := newLab(t, twoMembers(nil, sshFromA))
	n := l.up()
	a, b := n["A"], n["B"]
	A, B := l.addr("A"), l.addr("B")
	send(a, packet(A, B, protoTCP, 64, 1, 0, false, ports(40000, 80, 20)))
	expectNone(t, b, "A to B:80 (no rule)")
	send(a, packet(A, B, protoTCP, 64, 2, 0, false, ports(40001, 22, 20)))
	expect(t, b, "A to B:22 (rule)")
	send(b, packet(B, A, protoTCP, 64, 3, 0, false, ports(22, 40001, 20)))
	expect(t, a, "B:22 reply at A")
	send(b, packet(B, A, protoTCP, 64, 4, 0, false, ports(40002, 8080, 20)))
	expectNone(t, a, "B to A:8080")
	send(a, packet(A, B, protoUDP, 64, 5, 0, false, ports(40003, 53, 8)))
	expectNone(t, b, "A to B udp 53")
	if b.tun.stats.DroppedACL.Load() != 2 || a.tun.stats.DroppedACL.Load() != 1 {
		t.Fatalf("dropped at B %d, at A %d", b.tun.stats.DroppedACL.Load(), a.tun.stats.DroppedACL.Load())
	}
}

// Relays lower the TTL of the outer packet only (TestMemberDoesNotForward
// has one stop at a relay): what A sends B with TTL 1 arrives so.
func TestRelaysLeaveTheInnerTTL(t *testing.T) {
	l := newLab(t, twoMembers(allowICMP, allowICMP))
	n := l.up()
	send(n["A"], packet(l.addr("A"), l.addr("B"), protoICMP, 1, 1, 0, false, icmp(8, 1, nil)))
	if got := expect(t, n["B"], "TTL 1 end to end"); got[8] != 1 {
		t.Fatalf("TTL %d", got[8])
	}
	if n["R"].tun.stats.DroppedTTL.Load() != 0 {
		t.Fatalf("R dropped %d for TTL", n["R"].tun.stats.DroppedTTL.Load())
	}
}

func TestFragmentsFollowTheirFirstFragment(t *testing.T) {
	mdns := []Rule{{Proto: "udp", Ports: [][2]uint16{{5353, 5353}}}}
	l := newLab(t, twoMembers(nil, mdns))
	n := l.up()
	a, b := n["A"], n["B"]
	A, B := l.addr("A"), l.addr("B")
	send(a, packet(A, B, protoUDP, 64, 77, 0, true, ports(5353, 5353, 64)))
	send(a, packet(A, B, protoUDP, 64, 77, 8, false, make([]byte, 40)))
	expect(t, b, "first fragment")
	expect(t, b, "second fragment")
	send(a, packet(A, B, protoUDP, 64, 78, 0, true, ports(5353, 53, 64)))
	send(a, packet(A, B, protoUDP, 64, 78, 8, false, make([]byte, 40)))
	expectNone(t, b, "fragments of a refused datagram")
	send(a, packet(A, B, protoUDP, 64, 79, 8, false, make([]byte, 40)))
	expectNone(t, b, "a fragment whose first part never came")
}

func TestICMPErrorsReachTheSender(t *testing.T) {
	l := newLab(t, twoMembers(nil, nil))
	n := l.up()
	a, b := n["A"], n["B"]
	A, B := l.addr("A"), l.addr("B")
	sent := packet(B, A, protoUDP, 64, 9, 0, false, ports(41000, 9999, 8))
	send(b, sent)
	expectNone(t, a, "B to A:9999 (A has no rules)")
	send(a, packet(A, B, protoICMP, 64, 10, 0, false, icmp(3, 0, sent[:28])))
	expect(t, b, "ICMP error about B's own packet")
	other := packet(B, A, protoUDP, 64, 11, 0, false, ports(41001, 9999, 8))
	send(a, packet(A, B, protoICMP, 64, 12, 0, false, icmp(3, 0, other[:28])))
	expectNone(t, b, "ICMP error about a packet B never sent")
}

func TestDirectLinkAndGatewaySubnets(t *testing.T) {
	lan := netip.MustParsePrefix("192.0.2.0/24")
	s := spec{relays: []string{"R"}, members: map[string][]string{"A": {"R"}, "B": {"R"}, "C": {"R"}},
		direct: [][2]string{{"A", "B"}}, subnets: map[string][]netip.Prefix{"B": {lan}},
		rules: map[string][]Rule{"B": allowICMP, "C": allowICMP}}
	l := newLab(t, s)
	n := l.up()
	a, b, c, r := n["A"], n["B"], n["C"], n["R"]
	lanHost := netip.MustParseAddr("192.0.2.7")
	send(a, packet(l.addr("A"), lanHost, protoICMP, 64, 1, 0, false, icmp(8, 1, nil)))
	expect(t, b, "A to B's LAN over the direct link")
	if f := r.tun.stats.Forwarded.Load(); f != 0 || a.e2e.status().Sessions != 0 {
		t.Fatalf("the direct link went through the relay (%d forwarded) or end to end (%+v)", f, a.e2e.status())
	}
	send(c, packet(l.addr("C"), lanHost, protoICMP, 64, 2, 0, false, icmp(8, 2, nil)))
	expect(t, b, "C to B's LAN through the relay")
	if r.tun.stats.Forwarded.Load() == 0 || c.e2e.status().Sent != 1 {
		t.Fatalf("relay forwarded %d, C sent %+v", r.tun.stats.Forwarded.Load(), c.e2e.status())
	}
	want := []netip.Prefix{lan, host(l.addr("B"))}
	if got := allowedOf(a, "B"); !slices.Equal(got, sortPrefixes(want)) {
		t.Fatalf("A's direct peer B carries %v", got)
	}
	if got := allowedOf(a, "R"); !slices.Equal(got, []netip.Prefix{meshPrefix}) {
		t.Fatalf("A's entry carries %v", got)
	}
	if got := allowedOf(c, "R"); !slices.Equal(got, sortPrefixes([]netip.Prefix{meshPrefix, lan})) {
		t.Fatalf("C's entry carries %v", got)
	}
	if got := allowedOf(r, "B"); !slices.Equal(got, sortPrefixes(want)) {
		t.Fatalf("R's member B carries %v", got)
	}
}
