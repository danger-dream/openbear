package mesh

import (
	"encoding/binary"
	"net/netip"
	"slices"
	"sync"
	"testing"
	"time"

	"golang.zx2c4.com/wireguard/tun/tuntest"

	"openbear/node/mesh/wire"
)

// dfPacket is a TCP packet of size bytes with DF set, or without when df
// is false.
func dfPacket(src, dst netip.Addr, proto byte, size int, df bool) []byte {
	b := packet(src, dst, proto, 64, 7, 0, false, ports(40000, 80, size-20))
	if df {
		b[6] |= 0x40
		b[10], b[11] = 0, 0
		binary.BigEndian.PutUint16(b[10:12], headerChecksum(b[:20]))
	}
	return b
}

// checkTooBig: b is an ICMP "fragmentation needed" from src about orig, for
// a next hop of mtu, with valid checksums.
func checkTooBig(t *testing.T, b []byte, src netip.Addr, orig []byte, mtu int) {
	t.Helper()
	p, ok := parseIPv4(b)
	if !ok || p.proto != protoICMP || p.src != src || p.dst != netip.AddrFrom4([4]byte(orig[12:16])) || len(b) != 20+8+28 {
		t.Fatalf("not an ICMP from %s to the sender: % x", src, b[:min(len(b), 28)])
	}
	if headerChecksum(b[:20]) != 0 || checksum(b[20:]) != 0 {
		t.Fatal("bad checksum")
	}
	if b[20] != 3 || b[21] != 4 || binary.BigEndian.Uint16(b[24:26]) != 0 || int(binary.BigEndian.Uint16(b[26:28])) != mtu {
		t.Fatalf("type %d code %d next-hop MTU %d, want 3/4/%d", b[20], b[21], binary.BigEndian.Uint16(b[26:28]), mtu)
	}
	if string(b[28:]) != string(orig[:28]) {
		t.Fatal("the original header and 8 bytes are not quoted")
	}
}

func TestTooBigICMP(t *testing.T) {
	a, z := netip.MustParseAddr("100.96.0.11"), netip.MustParseAddr("100.96.0.12")
	orig := dfPacket(a, z, protoTCP, 9000, true)
	p, _ := parseIPv4(orig)
	checkTooBig(t, tooBigICMP(z, p, largeInner), z, orig, largeInner)
	// One about an end-to-end packet this node sent names its destination.
	outer := dfPacket(a, z, protoE2E, 1500, true)
	q, _ := parseIPv4(outer)
	icmp, _ := parseIPv4(tooBigICMP(netip.MustParseAddr("100.96.0.1"), q, 1420))
	if dst, ok := e2eTooBig(icmp, a); !ok || dst != z {
		t.Fatal("end-to-end ICMP not recognised")
	}
	if _, ok := e2eTooBig(icmp, z); ok {
		t.Fatal("taken for another node's packet")
	}
	plain, _ := parseIPv4(tooBigICMP(z, p, 1420))
	if _, ok := e2eTooBig(plain, a); ok {
		t.Fatal("taken for an end-to-end packet")
	}
}

// What a path takes: the next hop's link when it is the node, end to end
// the first hop's only once a probe answered through the whole path; a
// relay's ICMP, unanswered probes or another first hop end that.
func TestPathMTUs(t *testing.T) {
	now := time.Unix(1000, 0)
	p := newPaths(1420)
	p.now = func() time.Time { return now }
	v := p.view.Load()
	z, far := &e2eNode{id: "R"}, &e2eNode{id: "Z"}
	if v.linkMTU("R") != 1420 || v.innerMTU(z, "R") != 1368 {
		t.Fatal("off: not the network MTU")
	}
	p.configure(true, 1420, 1420)
	if probes, _ := p.update(map[string]bool{"R": true}, map[string]string{"Z": "R", "Y": "S"}); len(probes) != 1 || probes[0].node != "Z" {
		t.Fatalf("probes %v", probes)
	}
	v = p.view.Load()
	if v.linkMTU("R") != wire.LargeMTU || v.linkMTU("S") != 1420 || v.innerMTU(z, "R") != largeInner || v.innerMTU(far, "R") != 1368 || v.pathMTU("Z", "R") != 1420 {
		t.Fatal("a far node large before a probe answered")
	}
	if probes, _ := p.update(map[string]bool{"R": true}, map[string]string{"Z": "R"}); len(probes) != 0 {
		t.Fatal("probed again while one is out")
	}
	p.answered("Z", [8]byte{9})
	if p.view.Load().far["Z"] {
		t.Fatal("a wrong answer taken")
	}
	p.answered("Z", p.far["Z"].id)
	v = p.view.Load()
	if v.innerMTU(far, "R") != largeFar || v.pathMTU("Z", "R") != wire.LargeMTU || len(p.changed) != 1 {
		t.Fatal("answered probe did not make the path large")
	}
	<-p.changed
	// Through another first hop, small until a probe there answers.
	if v := p.view.Load(); v.innerMTU(far, "S") != 1368 {
		t.Fatal("large through a small first hop")
	}
	p.tooBig("Z")
	if p.view.Load().far["Z"] {
		t.Fatal("ICMP from a relay kept the path large")
	}
	now = now.Add(farProbeEvery + time.Second)
	if probes, _ := p.update(map[string]bool{"R": true}, map[string]string{"Z": "R"}); len(probes) != 0 {
		t.Fatal("probed within the hold-off")
	}
	now = now.Add(farHoldOff)
	probes, _ := p.update(map[string]bool{"R": true}, map[string]string{"Z": "R"})
	if len(probes) != 1 {
		t.Fatal("not probed after the hold-off")
	}
	p.answered("Z", probes[0].id)
	// Two probes unanswered end it.
	for i := 0; i < farMisses; i++ {
		now = now.Add(farProbeEvery)
		p.update(map[string]bool{"R": true}, map[string]string{"Z": "R"})
		now = now.Add(farProbeWait)
		p.update(map[string]bool{"R": true}, map[string]string{"Z": "R"})
		if large := p.view.Load().far["Z"]; large != (i < farMisses-1) {
			t.Fatalf("after %d missed: large %v", i+1, large)
		}
	}
	// The first hop's link no longer large ends every path through it.
	p.answered("Z", p.far["Z"].id)
	now = now.Add(farProbeEvery)
	probes, _ = p.update(map[string]bool{"R": true}, map[string]string{"Z": "R"})
	p.answered("Z", probes[0].id)
	if !p.view.Load().far["Z"] {
		t.Fatal("not large again")
	}
	if _, changed := p.update(map[string]bool{}, map[string]string{"Z": "R"}); !changed || p.view.Load().far["Z"] || p.view.Load().linkMTU("R") != 1420 {
		t.Fatal("large past its first hop's link")
	}
	p.configure(false, 1420, 1420)
	if v := p.view.Load(); v.on || v.linkMTU("R") != 1420 {
		t.Fatal("still on")
	}
}

// Host routes: a node whose next hop is its own large link gets
// largeInner, one end to end on a large path largeFar, the rest none.
func TestLargeRoutes(t *testing.T) {
	self := netip.MustParseAddr("100.96.0.11")
	nodes := testNodes(self, "100.96.0.12", "100.96.0.13", "100.96.0.14", "100.96.0.15")
	r := &routing{nodes: nodes, hops: []hopPrefix{
		{host(netip.MustParseAddr("100.96.0.12")), "100.96.0.12"}, {host(netip.MustParseAddr("100.96.0.15")), "100.96.0.15"}, {meshPrefix, "100.96.0.12"}}}
	p := newPaths(1420)
	if largeRoutes(p.view.Load(), r) != nil {
		t.Fatal("routes while off")
	}
	p.configure(true, 1420, 1420)
	probes, _ := p.update(map[string]bool{"100.96.0.12": true}, map[string]string{"100.96.0.13": "100.96.0.12", "100.96.0.14": "100.96.0.12"})
	for _, pr := range probes {
		if pr.node == "100.96.0.13" {
			p.answered(pr.node, pr.id)
		}
	}
	want := []hostRoute{{netip.MustParseAddr("100.96.0.12"), largeInner}, {netip.MustParseAddr("100.96.0.13"), largeFar}}
	if got := largeRoutes(p.view.Load(), r); !slices.Equal(got, want) {
		t.Fatalf("routes %v, want %v", got, want)
	}
	if largeInner != 8948 || largeFar+15+e2eOverhead > wire.LargeMTU {
		t.Fatal("inner MTUs do not fit a large frame")
	}
}

func largeTUN(t *testing.T, relay bool, links map[string]bool) (*meshTUN, *tuntest.ChannelTUN, *scheduler, netip.Addr) {
	t.Helper()
	ct := tuntest.NewChannelTUN()
	self := netip.MustParseAddr("100.96.0.11")
	nodes := testNodes(self, "100.96.0.12", "100.96.0.13")
	var hops []hopPrefix
	for _, a := range []string{"100.96.0.12", "100.96.0.13"} {
		hops = append(hops, hopPrefix{host(netip.MustParseAddr(a)), a})
	}
	r := &routing{relay: relay, local: []netip.Prefix{host(self)}, control: controlPrefix, routes: []netip.Prefix{meshPrefix}, nodes: nodes, hops: hops}
	s := newScheduler(16, 1<<20)
	mt := newMeshTUN(ct.TUN(), r, newFilter(nil), s, 1420)
	t.Cleanup(func() { mt.Close() })
	mt.paths.configure(true, 1420, 1420)
	mt.paths.update(links, nil)
	return mt, ct, s, self
}

// A packet from the OS longer than its path takes is dropped, and with DF
// answered to the OS as from its destination with the path's MTU.
func TestTooBigFromTheOS(t *testing.T) {
	mt, ct, s, self := largeTUN(t, false, map[string]bool{"100.96.0.12": true})
	big, small := netip.MustParseAddr("100.96.0.12"), netip.MustParseAddr("100.96.0.13")
	for _, c := range []struct {
		dst  netip.Addr
		size int
		df   bool
		mtu  int // of the ICMP, 0 for none
	}{{big, largeInner + 1, true, largeInner}, {small, 1369, true, 1368}, {big, largeInner + 1, false, 0}} {
		pkt := dfPacket(self, c.dst, protoTCP, c.size, c.df)
		ct.Outbound <- append([]byte(nil), pkt...)
		select {
		case got := <-ct.Inbound:
			if c.mtu == 0 {
				t.Fatal("ICMP for a packet without DF")
			}
			checkTooBig(t, got, c.dst, pkt, c.mtu)
		case <-time.After(300 * time.Millisecond):
			if c.mtu != 0 {
				t.Fatalf("no ICMP for %d bytes to %s", c.size, c.dst)
			}
		}
	}
	if mt.stats.DroppedTooBig.Load() != 3 || s.queuedBytes() != 0 {
		t.Fatalf("dropped %d, queued %d", mt.stats.DroppedTooBig.Load(), s.queuedBytes())
	}
	ct.Outbound <- dfPacket(self, big, protoTCP, largeInner, true)
	waitLarge(t, func() bool { return s.queuedBytes() == largeInner })
}

// A relay drops an end-to-end packet longer than the link to its next hop
// takes and answers its sender; one that fits goes on, a large one handed
// to the outer device's large reader.
func TestRelayTooBigForTheNextHop(t *testing.T) {
	from, to := netip.MustParseAddr("100.96.0.12"), netip.MustParseAddr("100.96.0.13")
	mt, _, s, self := largeTUN(t, true, map[string]bool{"100.96.0.12": true})
	pkt := dfPacket(from, to, protoE2E, 2000, true)
	mt.Write([][]byte{append([]byte(nil), pkt...)}, 0)
	if mt.stats.DroppedTooBig.Load() != 1 || mt.stats.Forwarded.Load() != 0 {
		t.Fatal("forwarded past the next hop's MTU")
	}
	icmp := s.tryDequeue()
	checkTooBig(t, icmp, self, pkt, 1420)
	for range 30 {
		mt.Write([][]byte{append([]byte(nil), pkt...)}, 0)
	}
	if n := s.queuedBytes(); n == 0 || n > int(tooBigRate)*len(icmp) {
		t.Fatalf("%d bytes of ICMP, not what the rate allows", n)
	}
	for s.tryDequeue() != nil {
	}
	back := dfPacket(to, from, protoE2E, 8000, true)
	mt.Write([][]byte{back}, 0)
	if mt.stats.Forwarded.Load() != 1 {
		t.Fatal("a large packet for a large link not forwarded")
	}
	go mt.Read([][]byte{make([]byte, 2048)}, []int{0}, 0)
	sizes := []int{0}
	if n, _ := mt.ReadLarge([][]byte{make([]byte, 9472)}, sizes, 0, nil); n != 1 || sizes[0] != 8000 {
		t.Fatal("the large reader did not get it")
	}
}

func waitLarge(t *testing.T, f func() bool) {
	t.Helper()
	end := time.Now().Add(2 * time.Second)
	for !f() {
		if time.Now().After(end) {
			t.Fatal("timeout")
		}
		time.Sleep(5 * time.Millisecond)
	}
}

// The link to a node loses large frames in the middle of a transfer and
// gets them back: from the loss a packet too long for the path is answered
// with "fragmentation needed" for the small MTU, not sent; once back the
// path takes 9000 again and its host route returns.
func TestLargeLostAndRegained(t *testing.T) {
	mt, ct, s, self := largeTUN(t, false, map[string]bool{"100.96.0.12": true})
	z := netip.MustParseAddr("100.96.0.12")
	r := mt.route.Load()
	send := func() []byte {
		pkt := dfPacket(self, z, protoTCP, largeInner, true)
		ct.Outbound <- append([]byte(nil), pkt...)
		return pkt
	}
	send()
	waitLarge(t, func() bool { return s.queuedBytes() == largeInner })
	s.tryDequeue()
	if _, changed := mt.paths.update(map[string]bool{}, nil); !changed {
		t.Fatal("no change")
	}
	if got := largeRoutes(mt.paths.view.Load(), r); len(got) != 0 {
		t.Fatalf("host routes %v after the loss", got)
	}
	pkt := send()
	select {
	case got := <-ct.Inbound:
		checkTooBig(t, got, z, pkt, 1368)
	case <-time.After(2 * time.Second):
		t.Fatal("no ICMP after the loss")
	}
	if s.queuedBytes() != 0 {
		t.Fatal("sent past the path MTU")
	}
	mt.paths.update(map[string]bool{"100.96.0.12": true}, nil)
	v := mt.paths.view.Load()
	if v.linkMTU("100.96.0.12") != wire.LargeMTU {
		t.Fatal("path MTU not back at 9000")
	}
	if got := largeRoutes(v, r); !slices.Equal(got, []hostRoute{{z, largeInner}}) {
		t.Fatalf("host routes %v", got)
	}
	send()
	waitLarge(t, func() bool { return s.queuedBytes() == largeInner })
}

// While the large reader falls behind, Read neither drops a large packet
// nor reorders it: with small ones already read it hands those on and keeps
// the large one for its next call, which waits for room.
func TestReadWaitsForTheLargeReader(t *testing.T) {
	mt, _, s, self := largeTUN(t, false, map[string]bool{"100.96.0.12": true})
	dst := netip.MustParseAddr("100.96.0.12")
	for range cap(mt.large) {
		mt.large <- queued{}
	}
	s.enqueue(dfPacket(self, dst, protoTCP, 1000, false))
	s.enqueue(dfPacket(self, dst, protoTCP, 8000, false))
	bufs, sizes := [][]byte{make([]byte, 2048), make([]byte, 2048)}, []int{0, 0}
	if n, err := mt.Read(bufs, sizes, 0); n != 1 || err != nil || sizes[0] != 1000 || len(packetData(mt.held.b)) != 8000 {
		t.Fatalf("read %d (%v), sizes %v, held %d", n, err, sizes, len(packetData(mt.held.b)))
	}
	done := make(chan int)
	go func() {
		n, _ := mt.Read(bufs, sizes, 0)
		done <- n
	}()
	select {
	case <-done:
		t.Fatal("Read went on with the large reader's queue full")
	case <-time.After(100 * time.Millisecond):
	}
	<-mt.large
	// Another flow's: one of the large packet's own would follow it to the large reader.
	s.enqueue(otherFlow(s, self, dst, 900))
	select {
	case n := <-done:
		if n != 1 || sizes[0] != 900 {
			t.Fatalf("read %d, sizes %v", n, sizes)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Read still waiting with room for the large packet")
	}
	for range cap(mt.large) - 1 {
		<-mt.large
	}
	if p := <-mt.large; len(packetData(p.b)) != 8000 || mt.stats.DroppedQueue.Load() != 0 {
		t.Fatalf("large packet %d bytes, dropped %d", len(packetData(p.b)), mt.stats.DroppedQueue.Load())
	}
}

// otherFlow is a TCP packet of size bytes from src to dst in another bucket than dfPacket's.
func otherFlow(s *scheduler, src, dst netip.Addr, size int) []byte {
	flow := s.bucketOf(dfPacket(src, dst, protoTCP, size, false))
	for port := uint16(40001); ; port++ {
		b := packet(src, dst, protoTCP, 64, 7, 0, false, ports(port, 80, size-20))
		if s.bucketOf(b) != flow {
			return b
		}
	}
}

// The readers encrypt and send apart: a flow's packets after one handed to the large reader go
// that way too until WireGuard has it, and one for the large reader waits for the batch its
// flow's earlier packets are in. Other flows go on as usual.
func TestAFlowKeepsItsOrderAcrossTheReaders(t *testing.T) {
	mt, _, s, self := largeTUN(t, false, map[string]bool{"100.96.0.12": true})
	dst := netip.MustParseAddr("100.96.0.12")
	flow := func(size int) []byte { return dfPacket(self, dst, protoTCP, size, false) }
	bufs, sizes := [][]byte{make([]byte, 2048), make([]byte, 2048), make([]byte, 2048)}, []int{0, 0, 0}
	large := [][]byte{make([]byte, 9472), make([]byte, 9472), make([]byte, 9472)}
	read := func(want ...int) {
		t.Helper()
		n, err := mt.Read(bufs, sizes, 0)
		if err != nil || !slices.Equal(sizes[:n], want) {
			t.Fatalf("Read %v (%v), want %v", sizes[:n], err, want)
		}
	}
	readLarge := func(want ...int) {
		t.Helper()
		n, err := mt.ReadLarge(large, sizes, 0, nil)
		if err != nil || !slices.Equal(sizes[:n], want) {
			t.Fatalf("ReadLarge %v (%v), want %v", sizes[:n], err, want)
		}
	}
	s.enqueue(flow(8000))
	s.enqueue(flow(1000))
	s.enqueue(otherFlow(s, self, dst, 700))
	read(700)
	readLarge(8000, 1000)
	// WireGuard may not have the large reader's batch yet.
	s.enqueue(flow(1100))
	s.enqueue(otherFlow(s, self, dst, 710))
	read(710)
	readLarge(1100)
	// Back for more: it has them all.
	got := make(chan []int, 1)
	go func() {
		bufs, sizes := [][]byte{make([]byte, 9472), make([]byte, 9472)}, []int{0, 0}
		n, _ := mt.ReadLarge(bufs, sizes, 0, nil)
		got <- sizes[:n]
	}()
	for deadline := time.Now().Add(2 * time.Second); mt.largeDone.Load() != 3; time.Sleep(time.Millisecond) {
		if time.Now().After(deadline) {
			t.Fatal("the large reader not back")
		}
	}
	s.enqueue(flow(1200))
	s.enqueue(flow(8100))
	s.enqueue(flow(1300))
	read(1200)
	if len(packetData(mt.held.b)) != 8100 || len(mt.large) != 0 {
		t.Fatalf("held %d bytes, %d queued for the large reader", len(packetData(mt.held.b)), len(mt.large))
	}
	s.enqueue(otherFlow(s, self, dst, 720))
	read(720)
	var after []int
	select {
	case after = <-got:
	case <-time.After(2 * time.Second):
		t.Fatal("the large reader got nothing")
	}
	for len(mt.large) > 0 {
		after = append(after, len(packetData((<-mt.large).b)))
	}
	if !slices.Equal(after, []int{8100, 1300}) {
		t.Fatalf("the large reader got %v", after)
	}
}

type countingHost struct {
	mu             sync.Mutex
	applied, after int
	closed         bool
}

func (h *countingHost) apply(hostConfig) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.applied++
	if h.closed {
		h.after++
	}
	return nil
}
func (h *countingHost) heal() []string { return nil }
func (h *countingHost) ready() bool    { return true }
func (h *countingHost) close() {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.closed = true
}

// A large-route refresh racing the engine's close never puts host state back after its cleanup.
func TestNoHostRefreshAfterClose(t *testing.T) {
	host := &countingHost{}
	tun := &meshTUN{paths: newPaths(1420)}
	e := &engine{cfg: &Config{Schema: 1}, host: host, tun: tun, done: make(chan struct{}), events: &eventLog{}, hostDirty: true}
	e.refreshHost()
	if host.applied != 1 {
		t.Fatalf("applied %d before close", host.applied)
	}
	var wg sync.WaitGroup
	stop := make(chan struct{})
	wg.Add(1)
	go func() {
		defer wg.Done()
		for {
			select {
			case <-stop:
				return
			default:
			}
			e.mu.Lock()
			e.hostDirty = true
			e.mu.Unlock()
			e.refreshHost()
		}
	}()
	time.Sleep(20 * time.Millisecond)
	close(e.done)
	e.mu.Lock()
	host.close()
	e.mu.Unlock()
	time.Sleep(20 * time.Millisecond)
	close(stop)
	wg.Wait()
	if host.after != 0 {
		t.Fatalf("host applied %d times after it was cleaned", host.after)
	}
}

// Without a large reader (macOS, Windows) a packet over a lowered MTU still goes when it fits the
// buffer, one that does not is dropped, and Read never waits on a queue nothing empties.
func TestReadWithoutALargeReader(t *testing.T) {
	s := newScheduler(16, 1<<20)
	mt := newMeshTUN(tuntest.NewChannelTUN().TUN(), nil, newFilter(nil), s, 1400)
	mt.largeReading.Store(false)
	mt.mtu.Store(1200)
	self, dst := netip.MustParseAddr("100.96.0.1"), netip.MustParseAddr("100.96.0.2")
	for range cap(mt.large) + 1 {
		s.enqueue(dfPacket(self, dst, protoTCP, 1300, false))
	}
	s.enqueue(dfPacket(self, dst, protoTCP, 1800, false))
	s.enqueue(dfPacket(self, dst, protoTCP, 800, false))
	bufs, sizes, got := make([][]byte, 8), make([]int, 8), []int{}
	for i := range bufs {
		bufs[i] = make([]byte, 1500)
	}
	done := make(chan struct{})
	go func() {
		defer close(done)
		for len(got) < cap(mt.large)+2 {
			n, err := mt.Read(bufs, sizes, 0)
			if err != nil {
				return
			}
			got = append(got, sizes[:n]...)
		}
	}()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("Read blocked on the large queue")
	}
	if len(got) != cap(mt.large)+2 || got[len(got)-1] != 800 || len(mt.large) != 0 || mt.stats.DroppedTooBig.Load() != 1 {
		t.Fatalf("read %d packets (last %v), %d queued for large, %d dropped", len(got), got[len(got)-1:], len(mt.large), mt.stats.DroppedTooBig.Load())
	}
}
