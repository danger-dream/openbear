package mesh

import (
	"net/netip"
	"runtime"
	"testing"
	"time"
)

var (
	addrA = netip.MustParseAddr("100.96.0.11")
	addrB = netip.MustParseAddr("100.96.0.12")
)

func TestRevocationDoesNotPromoteRepliesToOutboundFlows(t *testing.T) {
	for _, proto := range []byte{protoTCP, protoUDP} {
		f := newFilter([]Rule{{Proto: "any", Ports: [][2]uint16{{7778, 7778}}}})
		incoming, _ := parseIPv4(packet(addrA, addrB, proto, 64, 1, 0, false, ports(40000, 7778, 20)))
		response, _ := parseIPv4(packet(addrB, addrA, proto, 64, 2, 0, false, ports(7778, 40000, 20)))
		if !f.inbound(incoming) {
			t.Fatal("open rule refused traffic")
		}
		f.outbound(response)
		f.setRules(nil)
		f.outbound(response)
		if f.inbound(incoming) {
			t.Fatalf("protocol %d: reply made a revoked inbound flow stay open", proto)
		}
	}
}

func TestRevocationPreservesUnrelatedRepliesAndFragments(t *testing.T) {
	f := newFilter([]Rule{{Proto: "udp", Ports: [][2]uint16{{7777, 7778}}}})
	local, _ := parseIPv4(packet(addrB, addrA, protoUDP, 64, 1, 0, false, ports(40000, 9000, 20)))
	reply, _ := parseIPv4(packet(addrA, addrB, protoUDP, 64, 2, 0, false, ports(9000, 40000, 20)))
	f.outbound(local)
	for _, p := range []uint16{7777, 7778} {
		first, _ := parseIPv4(packet(addrA, addrB, protoUDP, 64, p, 0, true, ports(41000, p, 20)))
		if !f.inbound(first) {
			t.Fatal("first fragment denied")
		}
	}
	f.setRules([]Rule{{Proto: "udp", Ports: [][2]uint16{{7777, 7777}}}})
	if !f.inbound(reply) {
		t.Fatal("local connection broken")
	}
	for _, p := range []uint16{7777, 7778} {
		later, _ := parseIPv4(packet(addrA, addrB, protoUDP, 64, p, 8, false, make([]byte, 20)))
		if f.inbound(later) != (p == 7777) {
			t.Fatalf("fragment for port %d used stale rule", p)
		}
	}
}

func TestRulePortsOnlyMatchTCPAndUDP(t *testing.T) {
	f := newFilter([]Rule{{Proto: "any", Ports: [][2]uint16{{22, 22}}}, {Src: []netip.Prefix{netip.MustParsePrefix("100.96.0.0/24")}, Dst: []netip.Prefix{netip.MustParsePrefix("100.96.0.12/32")}, Proto: "udp"}})
	ping, _ := parseIPv4(packet(addrA, addrB, protoICMP, 64, 1, 0, false, icmp(8, 1, nil)))
	if f.inbound(ping) {
		t.Fatal("a rule with ports let ICMP in")
	}
	ssh, _ := parseIPv4(packet(addrA, addrB, protoTCP, 64, 2, 0, false, ports(40000, 22, 20)))
	dns, _ := parseIPv4(packet(addrA, addrB, protoUDP, 64, 3, 0, false, ports(40000, 53, 8)))
	far, _ := parseIPv4(packet(netip.MustParseAddr("100.96.1.5"), addrB, protoUDP, 64, 4, 0, false, ports(40000, 53, 8)))
	if !f.inbound(ssh) || !f.inbound(dns) || f.inbound(far) {
		t.Fatal("rule source, destination or port matching is wrong")
	}
}

func parsed(t testing.TB, b []byte) ipv4 {
	t.Helper()
	p, ok := parseIPv4(b)
	if !ok {
		t.Fatal("unparsable packet")
	}
	return p
}

// tcp is a TCP header with flags.
func tcp(sport, dport uint16, flags byte) []byte {
	l4 := ports(sport, dport, 20)
	l4[12], l4[13] = 5<<4, flags
	return l4
}

func addrN(base string, i int) netip.Addr {
	a := netip.MustParseAddr(base).As4()
	a[2], a[3] = byte(i>>8), byte(i)
	return netip.AddrFrom4(a)
}

// A peer flooding the ports its rules open, from many addresses, and this
// member answering every connection, leaves room for the replies to this
// member's own new connections; one address keeps at most its share.
func TestPeersCannotCrowdOutOwnConnections(t *testing.T) {
	f := newFilter([]Rule{{Proto: "tcp", Ports: [][2]uint16{{80, 80}}}})
	for i := range 3 * maxFilterEntries {
		from := addrN("100.96.0.0", 1+i%64)
		syn := parsed(t, packet(from, addrB, protoTCP, 64, 1, 0, false, tcp(uint16(1024+i/64), 80, tcpSYN)))
		if !f.inbound(syn) {
			t.Fatal("rule refused a SYN")
		}
		f.outbound(parsed(t, packet(addrB, from, protoTCP, 64, 1, 0, false, tcp(80, uint16(1024+i/64), tcpSYN|tcpACK))))
	}
	if f.incoming.len() != maxFilterEntries || f.flows.len() != 0 {
		t.Fatalf("incoming %d, own %d", f.incoming.len(), f.flows.len())
	}
	if n := f.incoming.owners[addrN("100.96.0.0", 1)].n; n > maxIncomingPerPeer {
		t.Fatalf("one address holds %d", n)
	}
	one := netip.MustParseAddr("100.96.8.8")
	for i := range maxIncomingPerPeer + 1000 {
		f.inbound(parsed(t, packet(one, addrB, protoTCP, 64, 1, 0, false, tcp(uint16(i), 80, tcpSYN))))
	}
	if n := f.incoming.owners[one].n; n != maxIncomingPerPeer {
		t.Fatalf("one address holds %d", n)
	}
	far := netip.MustParseAddr("100.96.9.9")
	f.outbound(parsed(t, packet(addrB, far, protoTCP, 64, 2, 0, false, tcp(50000, 443, tcpSYN))))
	if !f.inbound(parsed(t, packet(far, addrB, protoTCP, 64, 3, 0, false, tcp(443, 50000, tcpSYN|tcpACK)))) {
		t.Fatal("the reply to this member's own connection was refused")
	}
}

// This member's own flows, past the table's size, give up the one least
// recently used; a flow in use keeps its replies.
func TestOwnFlowsPastTheTableDropTheLeastRecentlyUsed(t *testing.T) {
	f := newFilter(nil)
	out := func(i int) {
		f.outbound(parsed(t, packet(addrB, addrN("100.96.0.0", 1+i%200), protoUDP, 64, 1, 0, false, ports(uint16(10000+i/200), 53, 8))))
	}
	reply := func(i int) bool {
		return f.inbound(parsed(t, packet(addrN("100.96.0.0", 1+i%200), addrB, protoUDP, 64, 1, 0, false, ports(53, uint16(10000+i/200), 8))))
	}
	busy := maxFilterEntries + 5000
	for i := range busy {
		out(i)
		if i%1000 == 0 {
			out(0) // flow 0 stays in use
		}
	}
	if f.flows.len() != maxFilterEntries {
		t.Fatal(f.flows.len())
	}
	if !reply(0) || !reply(busy-1) {
		t.Fatal("a flow in use lost its replies")
	}
	if reply(1) {
		t.Fatal("the least recently used flow was kept")
	}
}

// A flow only one side has sent on lives 30 s; a reply makes it live the
// protocol's time from this member's last packet. A reset is no reply.
func TestUnansweredFlowsExpireSoon(t *testing.T) {
	now := time.Now()
	f := newFilter([]Rule{{Proto: "tcp", Ports: [][2]uint16{{22, 22}}}})
	f.now = func() time.Time { return now }
	syn := func(port uint16) {
		f.outbound(parsed(t, packet(addrB, addrA, protoTCP, 64, 1, 0, false, tcp(port, 443, tcpSYN))))
	}
	answer := func(port uint16, flags byte) bool {
		return f.inbound(parsed(t, packet(addrA, addrB, protoTCP, 64, 1, 0, false, tcp(443, port, flags))))
	}
	syn(40000)
	syn(40001)
	syn(40002)
	if !answer(40001, tcpSYN|tcpACK) || !answer(40002, tcpRST|tcpACK) {
		t.Fatal("replies refused")
	}
	now = now.Add(openTimeout + time.Second)
	f.expire()
	if answer(40000, tcpSYN|tcpACK) || answer(40002, tcpACK) {
		t.Fatal("an unanswered or reset flow outlived 30 s")
	}
	if !answer(40001, tcpACK) {
		t.Fatal("an answered flow expired")
	}
	now = now.Add(flowTimeouts[protoTCP])
	f.expire()
	if answer(40001, tcpACK) {
		t.Fatal("an idle flow outlived its time")
	}

	// A peer's connection this member never answers, a scan, does not
	// stay either; one it answers does.
	in := func(port uint16, flags byte) {
		f.inbound(parsed(t, packet(addrA, addrB, protoTCP, 64, 1, 0, false, tcp(port, 22, flags))))
	}
	in(50000, tcpSYN)
	in(50001, tcpSYN)
	f.outbound(parsed(t, packet(addrB, addrA, protoTCP, 64, 1, 0, false, tcp(22, 50000, tcpSYN|tcpACK))))
	f.outbound(parsed(t, packet(addrB, addrA, protoTCP, 64, 1, 0, false, tcp(22, 50001, tcpRST|tcpACK))))
	now = now.Add(openTimeout + time.Second)
	f.expire()
	if f.incoming.len() != 1 || f.incoming.get(flowKey{protoTCP, addrB, addrA, 22, 50000}, now) == nil {
		t.Fatalf("incoming %d", f.incoming.len())
	}
}

// Later fragments are decided on a summary of the first one's header;
// one source keeps a bounded share of the table, and the last fragment
// frees a datagram's entry.
func TestFragmentTableIsSmallAndShared(t *testing.T) {
	f := newFilter([]Rule{{Proto: "udp", Ports: [][2]uint16{{7777, 7777}}}})
	first := func(src netip.Addr, id uint16) bool {
		return f.inbound(parsed(t, packet(src, addrB, protoUDP, 64, id, 0, true, ports(41000, 7777, 1400))))
	}
	later := func(src netip.Addr, id uint16, more bool) bool {
		return f.inbound(parsed(t, packet(src, addrB, protoUDP, 64, id, 175, more, make([]byte, 1400))))
	}
	var before, after runtime.MemStats
	runtime.GC()
	runtime.ReadMemStats(&before)
	flood := netip.MustParseAddr("100.96.0.66")
	for i := range maxFragments {
		first(addrN("100.96.0.0", 1+i/maxFragsPerSource), uint16(i))
	}
	runtime.GC()
	runtime.ReadMemStats(&after)
	// Signed: other tests' goroutines may free more than this holds.
	grew := int64(after.HeapAlloc) - int64(before.HeapAlloc)
	t.Logf("%d first fragments of 1428 bytes held: %.1f MiB", f.frags.len(), float64(grew)/(1<<20))
	if grew > 16<<20 {
		t.Fatal("first fragments kept whole")
	}
	for i := range 3 * maxFragsPerSource {
		first(flood, uint16(i))
	}
	if n := f.frags.owners[flood].n; n != maxFragsPerSource || f.frags.len() != maxFragments {
		t.Fatalf("one source holds %d of %d", n, f.frags.len())
	}
	// A large fragmented flow alongside: every datagram whole, and it
	// leaves nothing behind.
	big := netip.MustParseAddr("100.96.5.5")
	for i := range 200000 {
		id := uint16(i)
		if !first(big, id) || !later(big, id, true) || !later(big, id, false) {
			t.Fatalf("datagram %d lost", i)
		}
		if i%10 == 0 {
			first(flood, uint16(i))
		}
	}
	if f.frags.owners[big] != nil {
		t.Fatal("finished datagrams kept")
	}
}

// An ICMP error about this member's flow comes in from the host the flow
// goes to, or from the node a subnet it goes to is behind, or as the rules
// allow; not from any other peer.
func TestICMPErrorsComeFromThePath(t *testing.T) {
	gw := netip.MustParseAddr("100.96.0.20")
	lan := netip.MustParsePrefix("192.168.7.0/24")
	lanHost := netip.MustParseAddr("192.168.7.9")
	other := netip.MustParseAddr("100.96.0.30")
	f := newFilter(nil)
	f.setSites([]site{{gw, []netip.Prefix{lan}}})
	sent := packet(addrB, addrA, protoUDP, 64, 1, 0, false, ports(40000, 53, 8))
	toLAN := packet(addrB, lanHost, protoUDP, 64, 2, 0, false, ports(40001, 53, 8))
	f.outbound(parsed(t, sent))
	f.outbound(parsed(t, toLAN))
	errorFrom := func(src netip.Addr, about []byte) bool {
		return f.inbound(parsed(t, packet(src, addrB, protoICMP, 64, 9, 0, false, icmp(3, 0, about[:28]))))
	}
	if !errorFrom(addrA, sent) || !errorFrom(gw, toLAN) || !errorFrom(netip.MustParseAddr("192.168.7.1"), toLAN) || !errorFrom(lanHost, toLAN) {
		t.Fatal("an error from the path was refused")
	}
	if errorFrom(other, sent) || errorFrom(other, toLAN) || errorFrom(gw, sent) {
		t.Fatal("an error from off the path came in")
	}
	f.setRules([]Rule{{Src: []netip.Prefix{netip.PrefixFrom(other, 32)}, Proto: "icmp"}})
	if !errorFrom(other, sent) {
		t.Fatal("an error the rules allow was refused")
	}
}

// TestFilterGatewayAndFragmentLoad: a gateway member with 300 devices
// behind it, 200 connections each to 40 peers, a third of them to one
// (60000 flows), while 8000 connections from one remote office come in to
// a server behind it; every reply and every request gets through, round
// after round. The sizes in filter.go hold this with room to spare.
func TestFilterGatewayAndFragmentLoad(t *testing.T) {
	f := newFilter([]Rule{{Proto: "tcp", Ports: [][2]uint16{{445, 445}}}})
	server := netip.MustParseAddr("192.168.1.250")
	office := netip.MustParseAddr("100.96.3.1")
	remote := func(i int) netip.Addr {
		if i%3 == 0 {
			return netip.MustParseAddr("100.96.2.1")
		}
		return addrN("100.96.2.0", 2+i%39)
	}
	type conn struct {
		dev, peer netip.Addr
		port      uint16
	}
	var own []conn
	for d := range 300 {
		dev := addrN("192.168.0.0", 256+d)
		for c := range 200 {
			own = append(own, conn{dev, remote(d*200 + c), uint16(20000 + c)})
		}
	}
	var before, after runtime.MemStats
	runtime.GC()
	runtime.ReadMemStats(&before)
	start := time.Now()
	packets := 0
	for round := range 3 {
		for _, c := range own {
			f.outbound(parsed(t, packet(c.dev, c.peer, protoTCP, 64, 1, 0, false, tcp(c.port, 443, tcpACK))))
			if !f.inbound(parsed(t, packet(c.peer, c.dev, protoTCP, 64, 1, 0, false, tcp(443, c.port, tcpACK)))) {
				t.Fatalf("round %d: reply to %v refused", round, c)
			}
		}
		for i := range 8000 {
			if !f.inbound(parsed(t, packet(office, server, protoTCP, 64, 1, 0, false, tcp(uint16(30000+i), 445, tcpACK)))) {
				t.Fatal("office connection refused")
			}
			f.outbound(parsed(t, packet(server, office, protoTCP, 64, 1, 0, false, tcp(445, uint16(30000+i), tcpACK))))
		}
		packets += 2*len(own) + 16000
	}
	took := time.Since(start)
	runtime.GC()
	runtime.ReadMemStats(&after)
	t.Logf("%d own flows, %d incoming: %.1f MiB; %.0f ns a packet", f.flows.len(), f.incoming.len(),
		float64(after.HeapAlloc-before.HeapAlloc)/(1<<20), float64(took.Nanoseconds())/float64(packets))
	if f.flows.len() != len(own) || f.incoming.len() != 8000 {
		t.Fatalf("flows %d, incoming %d", f.flows.len(), f.incoming.len())
	}
}
