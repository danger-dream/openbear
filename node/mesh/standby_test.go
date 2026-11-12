package mesh

import (
	"bytes"
	"encoding/json"
	"net/netip"
	"slices"
	"strings"
	"testing"
	"time"
)

// A backup gateway carries its LAN only while the gateway announcing it
// cannot be reached; the relay owning both moves the route as attachments
// change, and members get the LAN through their entry either way.
func TestABackupGatewayTakesTheLANWhileThePrimaryIsAway(t *testing.T) {
	lan := netip.MustParsePrefix("192.168.1.0/24")
	node := func(id, addr string) Node {
		_, pub := newKey(t)
		return Node{ID: id, Role: roleMember, Address: netip.MustParseAddr(addr), PublicKey: pub, Relays: []string{"R"}}
	}
	primary, backup, other := node("P", "100.96.0.2"), node("B", "100.96.0.3"), node("M", "100.96.0.4")
	primary.Subnets, backup.StandbySubnets = []netip.Prefix{lan}, []netip.Prefix{lan}
	c := &Config{Network: Network{Prefix: meshPrefix}, Self: Self{ID: "R", Role: roleRelay, Address: netip.MustParseAddr("100.96.0.1")},
		Nodes: []Node{primary, backup, other}}
	att := &fakeAttach{owners: map[string]string{"P": "R", "B": "R", "M": "R"}}
	allowed := func(id string) []netip.Prefix {
		t.Helper()
		peers, err := planPeers(c, att)
		if err != nil {
			t.Fatal(err)
		}
		i := slices.IndexFunc(peers, func(p peerSpec) bool { return p.ID == id })
		return peers[i].Allowed
	}
	if got := allowed("P"); !slices.Contains(got, lan) {
		t.Fatalf("the primary does not carry its LAN: %v", got)
	}
	if got := allowed("B"); slices.Contains(got, lan) {
		t.Fatalf("the backup carries the LAN while the primary is attached: %v", got)
	}
	// The primary leaves: the backup takes the LAN.
	delete(att.owners, "P")
	if got := allowed("B"); !slices.Contains(got, lan) {
		t.Fatalf("the backup does not take the LAN with the primary gone: %v", got)
	}
	att.owners["P"] = "R"
	if got := allowed("B"); slices.Contains(got, lan) {
		t.Fatalf("the backup keeps the LAN after the primary is back: %v", got)
	}

	// A member's entry carries the LAN whichever gateway has it.
	m := *c
	m.Self = Self{ID: "M", Role: roleMember, Address: other.Address}
	m.Nodes = []Node{{ID: "R", Role: roleRelay, Address: c.Self.Address, PublicKey: primary.PublicKey}, primary, backup}
	m.Entry = &Entry{Mode: "fixed", Candidates: []Candidate{{ID: "c1", Relay: "R"}}}
	peers, err := planPeers(&m, &fakeAttach{entry: "c1", owners: map[string]string{}})
	if err != nil || len(peers) != 1 || !slices.Contains(peers[0].Allowed, lan) || len(peers[0].Allowed) != 2 {
		t.Fatalf("entry peers %+v, %v", peers, err)
	}
}

// The backup's own config may list the LAN as its gateway's while another
// node announces it: it is not its to announce.
func TestABackupGatewaysConfigIsValidBesideThePrimary(t *testing.T) {
	lan := netip.MustParsePrefix("192.168.1.0/24")
	l := newLab(t, spec{relays: []string{"R1"}, members: map[string][]string{"A": {"R1"}, "B": {"R1"}}, subnets: map[string][]netip.Prefix{"B": {lan}}})
	c := l.config("A")
	c.Self.Gateway = &Gateway{Subnets: []netip.Prefix{lan}, Masquerade: true}
	raw, _ := json.Marshal(c)
	if err := Validate(raw); err == nil || !strings.Contains(err.Error(), "overlaps") {
		t.Fatalf("a gateway that is no backup shares the LAN: %v", err)
	}
	c.Self.Gateway.Backup = true
	raw, _ = json.Marshal(c)
	if err := Validate(raw); err != nil {
		t.Fatal(err)
	}
}

// standbyLab: members P announcing lan, B standing by for it, and M, all
// entering relay R.
func standbyLab(t *testing.T, lan netip.Prefix, direct [][2]string) *lab {
	icmpOnly := map[string][]Rule{"M": allowICMP, "P": allowICMP, "B": allowICMP}
	l := newLab(t, spec{relays: []string{"R"}, members: map[string][]string{"M": {"R"}, "P": {"R"}, "B": {"R"}},
		direct: direct, subnets: map[string][]netip.Prefix{"P": {lan}}, rules: icmpOnly})
	l.edit = func(name string, c *Config) {
		for i := range c.Nodes {
			if c.Nodes[i].ID == "B" {
				c.Nodes[i].StandbySubnets = []netip.Prefix{lan}
			}
		}
		if name == "B" {
			c.Self.Gateway = &Gateway{Subnets: []netip.Prefix{lan}, Backup: true}
		}
	}
	return l
}

// A relay sends a subnet to its backup gateway end to end too while the
// gateway announcing it is away, takes the subnet's packets only from that
// backup meanwhile, and goes back with the gateway.
func TestARelaysEndToEndTableFollowsTheBackupGateway(t *testing.T) {
	lan := netip.MustParsePrefix("192.168.1.0/24")
	l := standbyLab(t, lan, nil)
	c := l.config("R")
	if err := c.check(); err != nil {
		t.Fatal(err)
	}
	att := &fakeAttach{owners: map[string]string{"P": "R", "B": "R", "M": "R"}}
	lanHost := netip.MustParseAddr("192.168.1.5")
	owner := func() string {
		t.Helper()
		tb, err := e2eTableFor(c, att)
		if err != nil {
			t.Fatal(err)
		}
		r := routingFor(c, att, nil, nil)
		if z := r.nodes.owner(lanHost); z == nil || z.id != tb.owner(lanHost).id {
			t.Fatalf("routing and sessions disagree: %v", z)
		}
		return tb.owner(lanHost).id
	}
	if got := owner(); got != "P" {
		t.Fatalf("with the gateway attached, %s gets the LAN", got)
	}
	delete(att.owners, "P")
	if got := owner(); got != "B" {
		t.Fatalf("with the gateway away, %s gets the LAN", got)
	}
	if tb, _ := e2eTableFor(c, att); slices.Contains(tb.byID["P"].prefixes, lan) {
		t.Fatalf("the absent gateway still speaks for the LAN: %v", tb.byID["P"].prefixes)
	}
	// Neither reachable: nothing better is known than the gateway announcing it.
	delete(att.owners, "B")
	if got := owner(); got != "P" {
		t.Fatalf("with both away, %s gets the LAN", got)
	}
	att.owners["B"], att.owners["P"] = "R", "R"
	if got := owner(); got != "P" {
		t.Fatalf("with the gateway back, %s gets the LAN", got)
	}
}

// A member that sends into a subnet a backup stands by for probes the
// gateway it sends to, moves the subnet, routes and end-to-end table alike,
// to the backup once the gateway stops answering and the backup answers,
// and back once the gateway answered three in a row. It stays when no
// other candidate answers, and probes nothing it does not send to.
func TestAMemberMovesASubnetToItsBackupWhileTheGatewayDoesNotAnswer(t *testing.T) {
	lan := netip.MustParsePrefix("192.168.1.0/24")
	lanHost := netip.MustParseAddr("192.168.1.5")
	l := standbyLab(t, lan, nil)
	c := l.config("M")
	if err := c.check(); err != nil {
		t.Fatal(err)
	}
	g := &standbyGateways{}
	c.standby = g
	g.configure(c)
	att := &fakeAttach{entry: "c1", owners: map[string]string{}}
	now := time.Unix(1000, 0)
	round := func(answer ...string) map[string]bool {
		t.Helper()
		now = now.Add(gatewayProbeEvery)
		sends, _ := g.tick(now)
		asked := map[string]bool{}
		for _, s := range sends {
			asked[s.node] = true
			if slices.Contains(answer, s.node) {
				g.answered(s.node, s.id)
			}
		}
		return asked
	}
	owner := func() string {
		t.Helper()
		tb, err := e2eTableFor(c, att)
		if err != nil {
			t.Fatal(err)
		}
		peers, err := planPeers(c, att)
		if err != nil || len(peers) != 1 || !slices.Contains(peers[0].Allowed, lan) {
			t.Fatalf("the entry does not carry the LAN: %+v, %v", peers, err)
		}
		return tb.owner(lanHost).id
	}
	if asked := round(); len(asked) != 0 {
		t.Fatalf("probed %v with nothing sent", asked)
	}
	g.use(lanHost)
	if asked := round("P", "B"); !asked["P"] || asked["B"] {
		t.Fatalf("asked %v while the gateway answers", asked)
	}
	for range 5 {
		g.use(lanHost)
		round("P", "B")
	}
	if owner() != "P" {
		t.Fatal("moved off a gateway that answers")
	}
	// Neither answers: nothing better to go to.
	for range 5 {
		g.use(lanHost)
		round()
	}
	if owner() != "P" {
		t.Fatal("moved to a backup that does not answer either")
	}
	for range 2 {
		g.use(lanHost)
		round("B")
	}
	if got := owner(); got != "B" {
		t.Fatalf("the LAN goes to %s with only the backup answering", got)
	}
	tb, _ := e2eTableFor(c, att)
	if slices.Contains(tb.byID["P"].prefixes, lan) || !slices.Contains(tb.byID["B"].prefixes, lan) {
		t.Fatalf("P speaks for %v, B for %v", tb.byID["P"].prefixes, tb.byID["B"].prefixes)
	}
	// The gateway is back: three answers in a row, not one.
	g.use(lanHost)
	if asked := round("B", "P"); !asked["P"] || !asked["B"] {
		t.Fatalf("asked %v on the backup", asked)
	}
	g.use(lanHost)
	round("B")
	for range 2 {
		g.use(lanHost)
		round("B", "P")
	}
	if owner() != "B" {
		t.Fatal("went back before three answers in a row")
	}
	g.use(lanHost)
	round("B", "P")
	g.use(lanHost)
	round("B", "P") // the next round counts the third answer
	if got := owner(); got != "P" {
		t.Fatalf("the LAN stays with %s after the gateway is back", got)
	}
	// A config with the same candidates keeps the choice; one without the
	// backup has nothing to choose.
	g.configure(l.config("M"))
	if g.chosen(lan) != "P" {
		t.Fatal("choice lost")
	}
	l.edit = nil
	g.configure(l.config("M"))
	if g.any() || g.chosen(lan) != "" {
		t.Fatal("a subnet without a backup is still chosen for")
	}
}

// A leaf member routes as a relay does but sees no attachments: it moves each
// subnet as its own probes of that subnet found, whatever they found for the
// gateway's other subnets, and as a backup gateway it keeps to the gateway
// announcing its LAN, as a member does.
func TestALeafMemberGoesByItsProbesOfEachSubnet(t *testing.T) {
	lan1, lan2 := netip.MustParsePrefix("192.168.1.0/24"), netip.MustParsePrefix("192.168.2.0/24")
	node := func(id, addr string) Node {
		_, pub := newKey(t)
		return Node{ID: id, Role: roleMember, Address: netip.MustParseAddr(addr), PublicKey: pub, Relays: []string{"R"}}
	}
	primary, backup, leaf := node("P", "100.96.0.2"), node("B", "100.96.0.3"), node("M", "100.96.0.4")
	primary.Subnets, backup.StandbySubnets = []netip.Prefix{lan1, lan2}, []netip.Prefix{lan1, lan2}
	c := &Config{Network: Network{Prefix: meshPrefix}, Self: Self{ID: "M", Role: roleMember, Leaf: true, Address: leaf.Address},
		Nodes: []Node{primary, backup}}
	g := &standbyGateways{}
	c.standby = g
	g.configure(c)
	att := &fakeAttach{owners: map[string]string{}}
	if a, b := gatewayOf(c, att, lan1), gatewayOf(c, att, lan2); a != "P" || b != "P" {
		t.Fatalf("before any probe: %s %s", a, b)
	}
	// Only lan2 is sent into: the gateway stops answering there, the backup answers.
	now := time.Unix(1000, 0)
	for range 5 {
		g.use(netip.MustParseAddr("192.168.2.5"))
		now = now.Add(gatewayProbeEvery)
		sends, _ := g.tick(now)
		for _, s := range sends {
			if s.node == "B" {
				g.answered(s.node, s.id)
			}
		}
	}
	if g.chosen(lan1) != "P" || g.chosen(lan2) != "B" {
		t.Fatalf("probes chose %s %s", g.chosen(lan1), g.chosen(lan2))
	}
	if a, b := gatewayOf(c, att, lan1), gatewayOf(c, att, lan2); a != "P" || b != "B" {
		t.Fatalf("lan1 goes to %s, lan2 to %s", a, b)
	}

	// The backup, a leaf member: not its LAN while the gateway announces it.
	bc := *c
	bc.Self = Self{ID: "B", Role: roleMember, Leaf: true, Address: backup.Address, Gateway: &Gateway{Subnets: []netip.Prefix{lan1, lan2}, Backup: true}}
	bc.Nodes = []Node{primary, leaf}
	bc.standby = &standbyGateways{}
	bc.standby.configure(&bc)
	if got := gatewayOf(&bc, att, lan1); got != "P" {
		t.Fatalf("the backup takes the LAN from its gateway: %s", got)
	}
}

// A member linked directly to the gateway reaches the LAN over the link,
// not also through its entry, which carries the LAN only once the backup has
// it.
func TestADirectLinkToTheGatewayCarriesTheLANAlone(t *testing.T) {
	lan := netip.MustParsePrefix("192.168.1.0/24")
	l := standbyLab(t, lan, [][2]string{{"M", "P"}})
	c := l.config("M")
	g := &standbyGateways{}
	c.standby = g
	g.configure(c)
	att := &fakeAttach{entry: "c1", owners: map[string]string{}}
	carries := func() map[string]bool {
		t.Helper()
		peers, err := planPeers(c, att)
		if err != nil {
			t.Fatal(err)
		}
		out := map[string]bool{}
		for _, p := range peers {
			if slices.Contains(p.Allowed, lan) {
				out[p.ID] = true
			}
		}
		return out
	}
	if got := carries(); len(got) != 1 || !got["P"] {
		t.Fatalf("the LAN goes to %v", got)
	}
	g.mu.Lock()
	g.lans[lan].chosen = "B"
	g.mu.Unlock()
	if got := carries(); len(got) != 1 || !got["R"] {
		t.Fatalf("with the backup chosen, the LAN goes to %v", got)
	}
}

// Through running data planes: M's packets for the LAN reach P; with P
// gone they reach B, encrypted for B, and B's replies from the LAN reach
// M; with P back they reach P again, and M no longer takes the LAN's
// packets from B.
func TestBackupGatewayTakesEndToEndTrafficWhileTheGatewayIsAway(t *testing.T) {
	lan := netip.MustParsePrefix("192.168.1.0/24")
	lanHost := netip.MustParseAddr("192.168.1.5")
	l := standbyLab(t, lan, nil)
	n := l.up()
	m, b := n["M"], n["B"]
	ping := func(id uint16) []byte {
		return packet(l.addr("M"), lanHost, protoICMP, 64, id, 0, false, icmp(8, id, nil))
	}
	drain := func(x *running) {
		for recv(x, 200*time.Millisecond) != nil {
		}
	}
	await := func(want string) {
		t.Helper()
		deadline := time.Now().Add(30 * time.Second)
		for m.gateways.chosen(lan) != want {
			if time.Now().After(deadline) {
				t.Fatalf("the LAN stays with %s", m.gateways.chosen(lan))
			}
			send(m, ping(1))
			m.checkGateways(time.Now())
			time.Sleep(300 * time.Millisecond)
		}
	}
	send(m, ping(2))
	if got := expect(t, n["P"], "M to the LAN through P"); !bytes.Equal(got, ping(2)) {
		t.Fatalf("P got %x", got)
	}
	n["P"].close()
	await("B")
	drain(b)
	send(m, ping(3))
	if got := expect(t, b, "M to the LAN through B"); !bytes.Equal(got, ping(3)) {
		t.Fatalf("B got %x", got)
	}
	reply := packet(lanHost, l.addr("M"), protoICMP, 63, 3, 0, false, icmp(0, 3, nil))
	send(b, reply)
	if got := expect(t, m, "the LAN's reply through B"); !bytes.Equal(got, reply) {
		t.Fatalf("M got %x", got)
	}
	if !slices.ContainsFunc(m.events.snapshot(), func(e Event) bool {
		return e.Kind == "gateway.standby" && e.Detail == lan.String()+": P -> B (unanswered)"
	}) {
		t.Fatalf("no event: %+v", m.events.snapshot())
	}

	p := l.start("P", nil)
	await("P")
	drain(p)
	drain(b)
	send(m, ping(4))
	if got := expect(t, p, "M to the LAN through P again"); !bytes.Equal(got, ping(4)) {
		t.Fatalf("P got %x", got)
	}
	drain(m)
	send(b, packet(lanHost, l.addr("M"), protoICMP, 63, 4, 0, false, icmp(0, 4, nil)))
	expectNone(t, m, "B speaking for the LAN once P is back")
}
