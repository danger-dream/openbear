package mesh

import (
	"bytes"
	"encoding/binary"
	"net/netip"
	"slices"
	"testing"
	"time"

	"golang.zx2c4.com/wireguard/conn"
	"golang.zx2c4.com/wireguard/tun/tuntest"

	"openbear/node/mesh/transport"
)

func TestLinkLadder(t *testing.T) {
	if got := linkLadder(1400); !slices.Equal(got, []int{1392, 1376, 1360, 1344, 1328, 1312, 1296, 1264, 1200, 1136, 1008, 880, 624, 576}) {
		t.Fatalf("ladder of 1400: %v", got)
	}
	// A network MTU of a multiple of 16 is not probed twice; the ladder
	// ends at minOwnMTU.
	if got := linkLadder(1392); got[0] != 1376 || got[len(got)-1] != minOwnMTU {
		t.Fatalf("ladder of 1392: %v", got)
	}
}

func probeSizes(probes []linkProbe) []int {
	var out []int
	for _, p := range probes {
		out = append(out, p.size)
	}
	return out
}

// linkLab is one link's probing on test time.
type linkLab struct {
	t   *testing.T
	p   *paths
	now time.Time
	udp map[string]transport.PathRef
}

func newLinkLab(t *testing.T) *linkLab {
	l := &linkLab{t: t, p: newPaths(1400), now: time.Unix(1000, 0), udp: map[string]transport.PathRef{"R": {ID: "a", Generation: 1}}}
	l.p.now = func() time.Time { return l.now }
	return l
}

// round sends the round due and has every probe answered that answer says,
// then ends it after linkProbeWait; it returns the round's probes, events
// and change.
func (l *linkLab) round(answer func(linkProbe) bool) ([]linkProbe, []string, bool) {
	l.t.Helper()
	probes, _, _ := l.p.probeLinks(l.udp)
	if len(probes) == 0 {
		l.t.Fatal("no round due")
	}
	for _, pr := range probes {
		if answer(pr) {
			l.p.linkAnswered(pr.peer, pr.id)
		}
	}
	l.now = l.now.Add(linkProbeWait)
	_, events, changed := l.p.probeLinks(l.udp)
	return probes, events, changed
}

// upTo answers what a link carrying n bytes would.
func upTo(n int) func(linkProbe) bool { return func(p linkProbe) bool { return p.size <= n } }

// misses runs rounds the network MTU fails on, each when due.
func (l *linkLab) misses(n, carries int) []string {
	l.t.Helper()
	var events []string
	for range n {
		_, ev, _ := l.round(upTo(carries))
		events = append(events, ev...)
		l.now = l.now.Add(linkProbeRetry)
	}
	return events
}

// A link no long probe is answered on linkProbeMisses rounds in a row,
// linkProbeRetry apart, a short one answered in each, fails the network MTU
// and is held to the longest size of the ladder answered; one long probe of
// the copies answered is enough to clear it, whatever else was lost.
func TestNarrowLink(t *testing.T) {
	l := newLinkLab(t)
	probes, _, _ := l.round(func(p linkProbe) bool { return false })
	// One of each while nothing went wrong.
	if got := probeSizes(probes); !slices.Equal(got, []int{linkBase, 1400}) {
		t.Fatalf("first round %v", got)
	}
	l.now = l.now.Add(linkProbeEvery)
	// Misses, then a round with one long probe of three answered and no
	// short one: the network MTU passed, the misses go.
	l.misses(linkProbeMisses-1, 1392)
	one := false
	if _, _, changed := l.round(func(p linkProbe) bool {
		if p.size == 1400 && !one {
			one = true
			return true
		}
		return false
	}); changed || l.p.udp["R"].misses != 0 || l.p.udp["R"].silent != 0 {
		t.Fatal("a round with the network MTU answered taken for a miss or for silence")
	}
	l.now = l.now.Add(linkProbeEvery)
	for i := 1; i < linkProbeMisses; i++ {
		if _, events, changed := l.round(upTo(1392)); changed || len(events) != 0 || l.p.view.Load().linkMTU("R") != 1400 {
			t.Fatalf("held after %d misses", i)
		}
		l.now = l.now.Add(linkProbeRetry - time.Second)
		if probes, _, _ := l.p.probeLinks(l.udp); len(probes) != 0 {
			t.Fatal("a miss checked again before linkProbeRetry")
		}
		l.now = l.now.Add(time.Second)
	}
	probes, events, changed := l.round(upTo(1392))
	if len(probes) != linkCopies*(2+len(linkLadder(1400))) {
		t.Fatalf("the misses not checked with the ladder: %d probes", len(probes))
	}
	if !changed || !slices.Equal(events, []string{"R: carries 1392 bytes of the network's 1400"}) {
		t.Fatalf("events %v", events)
	}
	v := l.p.view.Load()
	z, far, other := &e2eNode{id: "R"}, &e2eNode{id: "Z"}, &e2eNode{id: "Y"}
	// The neighbor's own packets are not sealed end to end: 1348 fit 1392.
	if v.linkMTU("R") != 1392 || v.pathMTU("Z", "R") != 1392 || v.innerMTU(z, "R") != 1348 || v.innerMTU(far, "R") != 1328 || v.innerMTU(other, "S") != 1348 {
		t.Fatalf("link %d path %d inner %d/%d/%d", v.linkMTU("R"), v.pathMTU("Z", "R"), v.innerMTU(z, "R"), v.innerMTU(far, "R"), v.innerMTU(other, "S"))
	}
	if !onlyKeys(v.narrowed(), "R") || len(l.p.changed) != 1 {
		t.Fatal("not narrowed")
	}
	<-l.p.changed
	// A sealed packet of the inner MTU fits the link.
	if (1328+15)&^15+e2eOverhead > 1392 {
		t.Fatal("inner MTU past the link's")
	}
	// The limit unanswered shrinks it only as many rounds in a row.
	for i := 1; i < linkProbeMisses; i++ {
		l.now = l.now.Add(linkProbeEvery)
		if _, events, _ := l.round(upTo(1344)); len(events) != 0 || l.p.view.Load().linkMTU("R") != 1392 {
			t.Fatalf("shrunk on %d rounds", i)
		}
	}
	l.now = l.now.Add(linkProbeEvery)
	if _, events, _ := l.round(upTo(1344)); !slices.Equal(events, []string{"R: carries 1344 bytes of the network's 1400"}) {
		t.Fatalf("not shrunk: %v", events)
	}
	if v := l.p.view.Load(); v.innerMTU(z, "R") != 1344 || v.innerMTU(far, "R") != 1280 {
		t.Fatalf("inner %d/%d", v.innerMTU(z, "R"), v.innerMTU(far, "R"))
	}
	// Every probe answered: judged without waiting, held no longer.
	l.now = l.now.Add(linkProbeEvery)
	probes, _, _ = l.p.probeLinks(l.udp)
	for _, pr := range probes {
		l.p.linkAnswered(pr.peer, pr.id)
	}
	if _, events, _ := l.p.probeLinks(l.udp); !slices.Equal(events, []string{"R: carries the network's 1400 bytes again"}) || l.p.view.Load().held() || len(l.p.view.Load().narrowed()) != 0 {
		t.Fatalf("not back: %v", events)
	}
}

// Short probes answered and nothing of the ladder: the link fails the
// network MTU and routes keep off it, but no limit was seen to work, so
// packets are held to nothing.
func TestLinkCarryingNothingOfTheLadder(t *testing.T) {
	l := newLinkLab(t)
	events := l.misses(linkProbeMisses, linkBase)
	if !slices.Equal(events, []string{"R: carries less than 576 bytes of the network's 1400"}) {
		t.Fatalf("events %v", events)
	}
	v := l.p.view.Load()
	if !onlyKeys(v.narrowed(), "R") || v.linkMTU("R") != 1400 || v.held() {
		t.Fatal("not failing, or held to a size never seen to work")
	}
	// Status shows what the link was seen to carry, not the network MTU.
	if v.linkShown("R") != linkBase || v.linkShown("S") != 1400 {
		t.Fatalf("shown %d/%d", v.linkShown("R"), v.linkShown("S"))
	}
	// A limit found later holds it.
	l.now = l.now.Add(linkProbeEvery)
	if _, events, _ := l.round(upTo(1200)); !slices.Equal(events, []string{"R: carries 1200 bytes of the network's 1400"}) || l.p.view.Load().linkMTU("R") != 1200 {
		t.Fatalf("events %v", events)
	}
}

// A peer answering nothing teaches nothing and is probed less often; a
// link no longer UDP, or on another path, starts afresh.
func TestLinkSilentAndMoved(t *testing.T) {
	l := newLinkLab(t)
	l.misses(linkProbeMisses, 1392)
	if l.p.view.Load().linkMTU("R") != 1392 {
		t.Fatal("not narrow")
	}
	l.now = l.now.Add(linkProbeEvery)
	for i := 1; i <= 2; i++ {
		if _, events, changed := l.round(upTo(0)); changed || len(events) != 0 || l.p.view.Load().linkMTU("R") != 1392 {
			t.Fatal("learned from silence")
		}
		l.now = l.now.Add(linkProbeEvery*time.Duration(i) - time.Second)
		if probes, _, _ := l.p.probeLinks(l.udp); len(probes) != 0 {
			t.Fatalf("probed again within %d rounds", i)
		}
		l.now = l.now.Add(time.Second)
	}
	// The peer's link moves to another path: the limit goes, a round is due.
	l.udp["R"] = transport.PathRef{ID: "b", Generation: 1}
	probes, _, changed := l.p.probeLinks(l.udp)
	if !changed || l.p.view.Load().held() || len(probes) != 2 {
		t.Fatalf("the old path's limit kept: changed %v, %d probes", changed, len(probes))
	}
	l.now = l.now.Add(linkProbeWait)
	l.p.probeLinks(l.udp)
	l.now = l.now.Add(linkProbeEvery)
	l.misses(linkProbeMisses, 1392)
	if _, _, changed := l.p.probeLinks(map[string]transport.PathRef{}); !changed || l.p.view.Load().linkMTU("R") != 1400 || l.p.view.Load().held() {
		t.Fatal("a link gone kept its limit")
	}
}

func onlyKeys(m map[string]bool, keys ...string) bool {
	if len(m) != len(keys) {
		return false
	}
	for _, k := range keys {
		if !m[k] {
			return false
		}
	}
	return true
}

func narrowTUN(t *testing.T, relay bool, hops []hopPrefix, addrs ...string) (*meshTUN, *tuntest.ChannelTUN, *scheduler, netip.Addr) {
	t.Helper()
	ct := tuntest.NewChannelTUN()
	self := netip.MustParseAddr("100.96.0.11")
	r := &routing{relay: relay, local: []netip.Prefix{host(self)}, control: controlPrefix, routes: []netip.Prefix{meshPrefix}, nodes: testNodes(self, addrs...), hops: hops,
		peerOf: map[[32]byte]string{}}
	for i, a := range addrs {
		r.peerOf[[32]byte{byte(i + 1)}] = a
	}
	s := newScheduler(16, 1<<20)
	mt := newMeshTUN(ct.TUN(), r, newFilter(nil), s, 1400)
	t.Cleanup(func() { mt.Close() })
	return mt, ct, s, self
}

// narrowLink holds the link to peer to limit.
func narrowLink(p *paths, peer string, limit int) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.udp[peer] = &linkSizes{limit: limit, misses: linkProbeMisses, next: p.now().Add(time.Hour)}
	p.publish()
}

// A relay without large packets drops an end-to-end packet longer than a
// narrow link to its next hop carries and answers its sender with what the
// link carries; one that fits, or goes over another link, goes on.
func TestRelayAnswersForANarrowLink(t *testing.T) {
	from, to := netip.MustParseAddr("100.96.0.12"), netip.MustParseAddr("100.96.0.13")
	mt, _, s, self := narrowTUN(t, true, []hopPrefix{{host(from), "100.96.0.12"}, {host(to), "100.96.0.13"}}, "100.96.0.12", "100.96.0.13")
	pkt := dfPacket(from, to, protoE2E, 1400, true)
	mt.Write([][]byte{append([]byte(nil), pkt...)}, 0)
	if mt.stats.Forwarded.Load() != 1 || mt.stats.DroppedTooBig.Load() != 0 {
		t.Fatal("not forwarded while the link carries the network MTU")
	}
	for s.tryDequeue() != nil {
	}
	narrowLink(mt.paths, "100.96.0.13", 1392)
	mt.Write([][]byte{append([]byte(nil), pkt...)}, 0)
	if mt.stats.DroppedTooBig.Load() != 1 || mt.stats.Forwarded.Load() != 1 {
		t.Fatal("forwarded past the narrow link's MTU")
	}
	checkTooBig(t, s.tryDequeue(), self, pkt, 1392)
	mt.Write([][]byte{dfPacket(from, to, protoE2E, 1392, true), dfPacket(to, from, protoE2E, 1400, true)}, 0)
	if mt.stats.Forwarded.Load() != 3 || mt.stats.DroppedTooBig.Load() != 1 {
		t.Fatal("what fits not forwarded")
	}
}

// A relay's "fragmentation needed" about this node's end-to-end packet holds
// the path to its destination to what it says: the OS gets the same for a
// packet longer than that, the node a host route of it, and after
// shortHold the path takes the network MTU again. The same from a node
// that is no relay says nothing.
func TestShortPathFromARelay(t *testing.T) {
	relay, far := netip.MustParseAddr("100.96.0.12"), netip.MustParseAddr("100.96.0.13")
	mt, ct, _, self := narrowTUN(t, false, []hopPrefix{{host(relay), "100.96.0.12"}, {meshPrefix, "100.96.0.12"}}, "100.96.0.12", "100.96.0.13")
	now := time.Unix(1000, 0)
	mt.paths.now = func() time.Time { return now }
	r := mt.route.Load()
	q, _ := parseIPv4(dfPacket(self, far, protoE2E, 1400, true))
	fromMember, _ := parseIPv4(tooBigICMP(far, q, 1200))
	if !mt.relayTooBig(fromMember, r) || mt.paths.view.Load().held() {
		t.Fatal("a member's word on a path taken")
	}
	r.nodes.byID["100.96.0.12"].relay = true
	icmp, _ := parseIPv4(tooBigICMP(relay, q, 1392))
	if !mt.relayTooBig(icmp, r) {
		t.Fatal("the relay's ICMP not taken")
	}
	v := mt.paths.view.Load()
	if n := r.nodes.byID["100.96.0.13"]; v.innerMTU(n, "100.96.0.12") != 1328 || v.innerMTU(r.nodes.byID["100.96.0.12"], "100.96.0.12") != 1348 {
		t.Fatal("the path not held")
	}
	if got := mtuRoutes(v, r); !slices.Equal(got, []hostRoute{{far, 1328}}) {
		t.Fatalf("host routes %v", got)
	}
	pkt := dfPacket(self, far, protoTCP, 1329, true)
	ct.Outbound <- append([]byte(nil), pkt...)
	select {
	case got := <-ct.Inbound:
		checkTooBig(t, got, far, pkt, 1328)
	case <-time.After(2 * time.Second):
		t.Fatal("no ICMP to the OS")
	}
	// A larger MTU said later does not raise it.
	icmp, _ = parseIPv4(tooBigICMP(relay, q, 1396))
	mt.relayTooBig(icmp, r)
	if mt.paths.view.Load().short["100.96.0.13"] != 1392 {
		t.Fatal("raised")
	}
	// A fixed path's first relay is the hop host routes are figured for.
	run := &pinRun{}
	run.active.Store(true)
	r.pins = pinTable{{self, far}: {{nextID: "100.96.0.14", run: run}}}
	narrowLink(mt.paths, "100.96.0.12", 1200)
	if got := mtuRoutes(mt.paths.view.Load(), r); !slices.Equal(got, []hostRoute{{relay, 1200}, {far, 1328}}) {
		t.Fatalf("host routes with a fixed path %v", got)
	}
	r.pins = nil
	now = now.Add(shortHold)
	mt.paths.probeLinks(nil)
	if mt.paths.view.Load().short["100.96.0.13"] != 0 {
		t.Fatal("held past shortHold")
	}
}

// A packet from the OS without DF longer than its path takes goes on in
// fragments that fit, which put together are the packet; one with DF is
// answered to the OS.
func TestFragmentsWithoutDF(t *testing.T) {
	peer := netip.MustParseAddr("100.96.0.12")
	mt, ct, s, self := narrowTUN(t, false, []hopPrefix{{host(peer), "100.96.0.12"}}, "100.96.0.12")
	narrowLink(mt.paths, "100.96.0.12", 1200)
	pkt := dfPacket(self, peer, protoUDP, 1300, false)
	ct.Outbound <- append([]byte(nil), pkt...)
	waitLarge(t, func() bool { return s.queuedBytes() == 1300+20 })
	var data []byte
	for i, want := range []struct {
		size int
		off  uint16
		more bool
	}{{1196, 0, true}, {124, 147, false}} {
		f, ok := parseIPv4(s.tryDequeue())
		if !ok || len(f.b) != want.size || f.frag != want.off || f.more != want.more || f.dontFragment() || headerChecksum(f.b[:20]) != 0 || f.id != binary.BigEndian.Uint16(pkt[4:6]) {
			t.Fatalf("fragment %d: %d bytes at %d, more %v", i, len(f.b), f.frag, f.more)
		}
		data = append(data, f.b[20:]...)
	}
	if !bytes.Equal(data, pkt[20:]) {
		t.Fatal("the fragments are not the packet")
	}
	df := dfPacket(self, peer, protoTCP, 1201, true)
	ct.Outbound <- append([]byte(nil), df...)
	select {
	case got := <-ct.Inbound:
		checkTooBig(t, got, peer, df, 1200)
	case <-time.After(2 * time.Second):
		t.Fatal("no ICMP to the OS")
	}
	// A fragment of the OS's own is split again where it was.
	first, _ := parseIPv4(dfPacket(self, peer, protoUDP, 1300, false))
	first.more, first.frag = true, 100
	parts := fragments(first, 1000)
	if len(parts) != 2 {
		t.Fatalf("%d parts", len(parts))
	}
	a, _ := parseIPv4(parts[0])
	b, _ := parseIPv4(parts[1])
	if a.frag != 100 || !a.more || b.frag != 100+uint16(len(a.b)-20)/8 || !b.more {
		t.Fatal("offsets or more fragments wrong")
	}
	// Options: the first fragment has them all, the later ones those marked
	// to be copied, padded; a no-op and the end of the list go.
	opts := []byte{130, 4, 0xaa, 0xbb, 1, 7, 3, 4, 0, 0, 0, 0}
	with := append(append(slices.Clone(pkt[:20]), opts...), pkt[20:]...)
	with[0] = 0x45 + byte(len(opts)/4)
	binary.BigEndian.PutUint16(with[2:4], uint16(len(with)))
	with[10], with[11] = 0, 0
	binary.BigEndian.PutUint16(with[10:12], headerChecksum(with[:32]))
	q, ok := parseIPv4(with)
	if !ok || q.ihl != 32 {
		t.Fatal("bad packet with options")
	}
	parts = fragments(q, 700)
	var back []byte
	for i, part := range parts {
		f, ok := parseIPv4(part)
		want := []byte{130, 4, 0xaa, 0xbb}
		if i == 0 {
			want = opts
		}
		if !ok || len(f.b) > 700 || !bytes.Equal(f.b[20:f.ihl], want) || headerChecksum(f.b[:f.ihl]) != 0 || f.frag != uint16(len(back)/8) {
			t.Fatalf("fragment %d: header % x at %d", i, f.b[:f.ihl], f.frag)
		}
		back = append(back, f.b[f.ihl:]...)
	}
	if len(parts) != 2 || !bytes.Equal(back, pkt[20:]) {
		t.Fatalf("%d fragments, data not the packet's", len(parts))
	}
}

// A link probe straight from the peer is answered to it, short, over the
// link it came by, and never reaches the OS; the answer is taken for the
// round out.
func TestLinkProbeAnswered(t *testing.T) {
	peer := netip.MustParseAddr("100.96.0.12")
	mt, ct, s, self := narrowTUN(t, true, []hopPrefix{{meshPrefix, "100.96.0.13"}}, "100.96.0.12", "100.96.0.13")
	md := []conn.ReceiveMetadata{{PublicKey: [32]byte{1}}}
	mt.WriteWithMetadata([][]byte{probePacket(peer, self, probeLink, [8]byte{7}, 1400)}, 0, md)
	reply, ok := parseIPv4(s.tryDequeue())
	if !ok || reply.src != self || reply.dst != peer || len(reply.b) > 64 || reply.payload()[8] != probeLinkReply || reply.payload()[9] != 7 {
		t.Fatal("no short reply to the peer")
	}
	select {
	case <-ct.Inbound:
		t.Fatal("the probe reached the OS")
	case <-time.After(50 * time.Millisecond):
	}
	probes, _, _ := mt.paths.probeLinks(map[string]transport.PathRef{"100.96.0.12": {ID: "a"}})
	mt.WriteWithMetadata([][]byte{probePacket(peer, self, probeLinkReply, probes[0].id, 0)}, 0, md)
	if !mt.paths.udp["100.96.0.12"].got[probes[0].size] {
		t.Fatal("the answer not taken")
	}
	// From another node through the peer: plain, so not its own to answer.
	mt.WriteWithMetadata([][]byte{probePacket(netip.MustParseAddr("100.96.0.13"), self, probeLink, [8]byte{8}, 1400)}, 0, md)
	if s.tryDequeue() != nil {
		t.Fatal("answered a probe the peer did not send")
	}
}
