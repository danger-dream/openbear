package mesh

import (
	"bytes"
	"crypto/ecdh"
	"crypto/rand"
	"errors"
	"fmt"
	"net/netip"
	"reflect"
	"sync/atomic"
	"testing"
	"time"

	"golang.zx2c4.com/wireguard/conn"
	"golang.zx2c4.com/wireguard/conn/bindtest"
	"golang.zx2c4.com/wireguard/device"
	"golang.zx2c4.com/wireguard/tun/tuntest"

	"openbear/node/mesh/transport"
)

func TestLargeSwapOwnershipAndFallback(t *testing.T) {
	mt, ct, s, self := largeTUN(t, false, map[string]bool{"100.96.0.12": true})
	dst := netip.MustParseAddr("100.96.0.12")
	pkt := dfPacket(self, dst, protoTCP, largeInner, true)
	ct.Outbound <- pkt
	waitLarge(t, func() bool { return s.queuedBytes() == len(pkt) })
	done := make(chan struct{})
	go func() { mt.Read([][]byte{make([]byte, 2048)}, []int{0}, 0); close(done) }()
	bufs := [][]byte{make([]byte, readSlotCapacity)}
	orig := &bufs[0][0]
	sizes := []int{0}
	n, err := mt.ReadLargeSwap(bufs, sizes, readHeadroom, nil)
	if err != nil || n != 1 || sizes[0] != len(pkt) || &bufs[0][0] == orig || !bytes.Equal(bufs[0][readHeadroom:readHeadroom+len(pkt)], pkt) {
		t.Fatalf("swap n=%d sizes=%v err=%v", n, sizes, err)
	}
	// The displaced buffer and transferred packet do not alias.
	*orig = 9
	if bufs[0][0] == 9 {
		t.Fatal("pool and source aliases")
	}
	mt.sched.close()
	<-done
}

func TestLargeSwapFallbacks(t *testing.T) {
	mt, _, _, _ := largeTUN(t, false, nil)
	for _, tc := range []struct {
		name    string
		backing []byte
		offset  int
	}{
		{"no-backing", nil, 16}, {"wrong-head", make([]byte, 9472), 17}, {"short-backing", make([]byte, 9000), 16},
	} {
		t.Run(tc.name, func(t *testing.T) {
			p := bytes.Repeat([]byte{43}, 8193)
			raw := p[:len(p):len(p)]
			if len(tc.backing) == readSlotCapacity {
				copy(tc.backing[readHeadroom:], p)
				raw = tc.backing[: readHeadroom+len(p) : readSlotCapacity]
			}
			mt.large <- queued{b: raw}
			buf := make([]byte, 9472)
			before := &buf[0]
			bs := [][]byte{buf}
			sizes := []int{0}
			n, err := mt.ReadLargeSwap(bs, sizes, tc.offset, nil)
			if err != nil || n != 1 || &bs[0][0] != before || sizes[0] != len(p) || !bytes.Equal(bs[0][tc.offset:tc.offset+len(p)], p) {
				t.Fatal("fallback failed", n, err)
			}
		})
	}
	old := cap(append([]byte(nil), make([]byte, readHeadroom+readBufSize)...))
	replacement := cap(append([]byte(nil), make([]byte, readSlotCapacity)...))
	if old != replacement {
		t.Fatalf("different allocation size classes %d %d", old, replacement)
	}
}

type countedSwapTUN struct {
	*meshTUN
	swaps atomic.Int64
}

func (t *countedSwapTUN) ReadLargeSwap(bufs [][]byte, sizes []int, off int, md []conn.SendMetadata) (int, error) {
	before := make([]*byte, len(bufs))
	for i, b := range bufs {
		before[i] = &b[0]
	}
	n, err := t.meshTUN.ReadLargeSwap(bufs, sizes, off, md)
	for i := 0; i < n; i++ {
		if before[i] != &bufs[i][0] {
			t.swaps.Add(1)
		}
	}
	return n, err
}

type faultSwapBind struct {
	conn.Bind
	fail   atomic.Bool
	failed atomic.Int64
}

func (b *faultSwapBind) Send(bufs [][]byte, ep conn.Endpoint) error {
	if b.fail.Load() {
		for _, p := range bufs {
			if len(p) >= 4 && p[0] == 4 && p[1] == 0 && p[2] == 0 && p[3] == 0 {
				b.failed.Add(1)
				return errors.New("injected transport send failure")
			}
		}
	}
	return b.Bind.Send(bufs, ep)
}

// Runs the root module's actual WireGuard reader, asynchronous encryptor,
// sender and receiver. Pool size one forces each element through reuse.
func TestLargeSwapWireGuardSend(t *testing.T) {
	ct := tuntest.NewChannelTUN()
	dst := netip.MustParseAddr("100.96.0.12")
	self := netip.MustParseAddr("100.96.0.11")
	nodes := testNodes(self, dst.String())
	r := &routing{local: []netip.Prefix{host(self)}, control: controlPrefix, routes: []netip.Prefix{meshPrefix}, nodes: nodes, hops: []hopPrefix{{host(dst), dst.String()}}}
	mt := newMeshTUN(ct.TUN(), r, newFilter(nil), newScheduler(16, 1<<20), 1420)
	mt.paths.configure(true, 1420, 1420)
	mt.paths.update(map[string]bool{dst.String(): true}, nil)
	wrapped := &countedSwapTUN{meshTUN: mt}
	rx := tuntest.NewChannelTUN()
	limits := conn.DeviceLimits{MaxPeers: 4, Workers: 1, BatchSize: 1, QueueDepth: 4, TxBuffers: 16, RxBuffers: 16, KeepaliveBuffers: 4, MessageBufferSize: 9472, LargeBufferSize: 9472, LargeBatchSize: 1, LargeTxBuffers: 1, LargeRxBuffers: 2}
	binds := bindtest.NewChannelBinds()
	fault := &faultSwapBind{Bind: binds[0]}
	a := device.NewDevice(wrapped, boundedTestBind{fault, limits}, device.NewLogger(device.LogLevelSilent, ""))
	z := device.NewDevice(rx.TUN(), boundedTestBind{binds[1], limits}, device.NewLogger(device.LogLevelSilent, ""))
	defer a.Close()
	defer z.Close()
	key := func() *ecdh.PrivateKey {
		k, e := ecdh.X25519().GenerateKey(rand.Reader)
		if e != nil {
			t.Fatal(e)
		}
		return k
	}
	ka, kz := key(), key()
	if e := a.IpcSet(fmt.Sprintf("private_key=%x\npublic_key=%x\nendpoint=127.0.0.1:1\nallowed_ip=%s/32\n", ka.Bytes(), kz.PublicKey().Bytes(), dst)); e != nil {
		t.Fatal(e)
	}
	if e := z.IpcSet(fmt.Sprintf("private_key=%x\npublic_key=%x\nendpoint=127.0.0.1:2\nallowed_ip=%s/32\n", kz.Bytes(), ka.PublicKey().Bytes(), self)); e != nil {
		t.Fatal(e)
	}
	if e := a.Up(); e != nil {
		t.Fatal(e)
	}
	if e := z.Up(); e != nil {
		t.Fatal(e)
	}
	for i := 0; i < 12; i++ {
		p := dfPacket(self, dst, protoTCP, largeInner, true)
		p[len(p)-1] = byte(i)
		select {
		case ct.Outbound <- p:
		case <-time.After(3 * time.Second):
			t.Fatal("send blocked")
		}
		select {
		case got := <-rx.Inbound:
			if !bytes.Equal(got, p) {
				t.Fatalf("packet %d corrupt", i)
			}
		case <-time.After(5 * time.Second):
			t.Fatalf("receive %d timed out", i)
		}
	}
	if wrapped.swaps.Load() != 12 {
		t.Fatalf("only %d packets swapped", wrapped.swaps.Load())
	}
	// Each buffer WireGuard gave back was the slot the next packet was read
	// into: only the first packet's needed a new one.
	if st := mt.stats; st.LargeSwapped.Load() != 12 || st.LargeReused.Load() != 11 || st.LargeAllocated.Load() != 1 || st.LargeDiscarded.Load() != 0 || st.LargeCopied.Load() != 0 || len(mt.spares) != 1 {
		t.Fatalf("slots: swapped %d reused %d allocated %d discarded %d copied %d, %d spares", st.LargeSwapped.Load(), st.LargeReused.Load(), st.LargeAllocated.Load(), st.LargeDiscarded.Load(), st.LargeCopied.Load(), len(mt.spares))
	}
	// A failing transport still returns the unique element to its single-slot
	// pool. A subsequent packet must be encrypted correctly from a fresh slot.
	fault.fail.Store(true)
	p := dfPacket(self, dst, protoTCP, largeInner, true)
	p[len(p)-1] = 0xaa
	select {
	case ct.Outbound <- p:
	case <-time.After(3 * time.Second):
		t.Fatal("failed send blocked")
	}
	waitLarge(t, func() bool { return fault.failed.Load() > 0 })
	fault.fail.Store(false)
	p = dfPacket(self, dst, protoTCP, largeInner, true)
	p[len(p)-1] = 0xbb
	select {
	case ct.Outbound <- p:
	case <-time.After(3 * time.Second):
		t.Fatal("pool not returned after send failure")
	}
	select {
	case got := <-rx.Inbound:
		if !bytes.Equal(got, p) {
			t.Fatal("post-failure packet corrupted")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("post-failure packet stuck")
	}
	// Remove and recreate the peer, forcing key establishment again.
	if e := a.IpcSet(fmt.Sprintf("public_key=%x\nremove=true\n", kz.PublicKey().Bytes())); e != nil {
		t.Fatal(e)
	}
	if e := a.IpcSet(fmt.Sprintf("public_key=%x\nendpoint=127.0.0.1:1\nallowed_ip=%s/32\n", kz.PublicKey().Bytes(), dst)); e != nil {
		t.Fatal(e)
	}
	p = dfPacket(self, dst, protoTCP, largeInner, true)
	p[len(p)-1] = 0xcc
	select {
	case ct.Outbound <- p:
	case <-time.After(3 * time.Second):
		t.Fatal("reconnect send blocked")
	}
	select {
	case got := <-rx.Inbound:
		if !bytes.Equal(got, p) {
			t.Fatal("reconnected packet corrupted")
		}
	case <-time.After(12 * time.Second):
		t.Fatal("reconnected packet stuck")
	}
	if wrapped.swaps.Load() != 15 {
		t.Fatalf("swap count after failure/reconnect %d", wrapped.swaps.Load())
	}
	// Saturate the producer while the single large TX element is recycled by
	// the asynchronous encryptor and sender. Scheduler drops are permitted;
	// every delivered packet must be intact and unique, and the final marker
	// proves the pipeline recovered without a stuck or double-returned slot.
	producerDone := make(chan error, 1)
	go func() {
		for i := 0; i <= 128; i++ {
			b := dfPacket(self, dst, protoTCP, largeInner, true)
			if i == 128 {
				b[len(b)-1] = 0xfe
			} else {
				b[len(b)-1] = byte(i)
			}
			select {
			case ct.Outbound <- b:
			case <-time.After(5 * time.Second):
				producerDone <- errors.New("producer stalled")
				return
			}
		}
		producerDone <- nil
	}()
	seen := [128]bool{}
	producerFinished := false
	deadline := time.After(15 * time.Second)
	for {
		select {
		case got := <-rx.Inbound:
			if len(got) != largeInner {
				t.Fatalf("burst length %d", len(got))
			}
			id := got[len(got)-1]
			expected := dfPacket(self, dst, protoTCP, largeInner, true)
			expected[len(expected)-1] = id
			if !bytes.Equal(got, expected) {
				t.Fatalf("burst payload %d corrupt", id)
			}
			if id == 0xfe {
				if !producerFinished {
					if e := <-producerDone; e != nil {
						t.Fatal(e)
					}
				}
				return
			}
			if id >= 128 || seen[id] {
				t.Fatalf("duplicate or unexpected burst %d", id)
			}
			seen[id] = true
		case e := <-producerDone:
			if e != nil {
				t.Fatal(e)
			}
			producerFinished = true
			producerDone = nil
		case <-deadline:
			t.Fatal("burst failed to drain")
		}
	}
}

// A buffer WireGuard gives back for a packet it took over is the slot pump
// reads a later large packet into; a slot is made new only without a spare.
// A packet handed over stays WireGuard's alone: no later read lands in it.
func TestLargeSendSlotsRecycled(t *testing.T) {
	mt, ct, _, self := largeTUN(t, false, map[string]bool{"100.96.0.12": true})
	dst := netip.MustParseAddr("100.96.0.12")
	go mt.Read([][]byte{make([]byte, 2048)}, []int{0}, 0) // hands large packets on until closed
	var handed, sent, given [][]byte
	for i := range 4 {
		p := dfPacket(self, dst, protoTCP, largeInner, true)
		p[len(p)-1] = byte(i)
		ct.Outbound <- p
		buf := make([]byte, readSlotCapacity) // what WireGuard read into
		bufs, sizes := [][]byte{buf}, []int{0}
		if n, err := mt.ReadLargeSwap(bufs, sizes, readHeadroom, nil); err != nil || n != 1 || &bufs[0][0] == &buf[0] || !bytes.Equal(bufs[0][readHeadroom:readHeadroom+sizes[0]], p) {
			t.Fatalf("packet %d not handed over: %d, %v", i, n, err)
		}
		// pump had replaced the slot of packet i-1 with the buffer given
		// back for packet i-2, and read packet i into it.
		if i >= 2 && &bufs[0][0] != &given[i-2][0] {
			t.Fatalf("packet %d not read into the buffer given back for packet %d", i, i-2)
		}
		handed, sent, given = append(handed, bufs[0]), append(sent, p), append(given, buf)
	}
	for i, h := range handed {
		if !bytes.Equal(h[readHeadroom:readHeadroom+len(sent[i])], sent[i]) {
			t.Fatalf("packet %d changed after it was handed over", i)
		}
	}
	if st := mt.stats; st.LargeSwapped.Load() != 4 || st.LargeReused.Load() != 3 || st.LargeAllocated.Load() != 1 || st.LargeDiscarded.Load() != 0 || st.LargeCopied.Load() != 0 || len(mt.spares) != 1 {
		t.Fatalf("swapped %d reused %d allocated %d discarded %d copied %d, %d spares", st.LargeSwapped.Load(), st.LargeReused.Load(), st.LargeAllocated.Load(), st.LargeDiscarded.Load(), st.LargeCopied.Load(), len(mt.spares))
	}
}

// The TUN keeps at most transport.LargeSpares spares, of the size the
// bind's budget counts them at (transport's TestTwoPlaneFixedBufferBudget);
// more go to the GC.
func TestLargeSendSparesBounded(t *testing.T) {
	mt, _, _, _ := largeTUN(t, false, nil)
	b, err := transport.New(transport.Options{LargeFrames: true})
	if err != nil {
		t.Fatal(err)
	}
	defer b.Close()
	if l := b.DeviceLimits(); l.LargeBufferSize != readSlotCapacity || cap(mt.spares) != transport.LargeSpares {
		t.Fatalf("%d spares of %d bytes, large buffers of %d", cap(mt.spares), readSlotCapacity, l.LargeBufferSize)
	}
	if s := mt.readSlot(); len(s) != readHeadroom+readBufSize || cap(s) != readSlotCapacity || mt.stats.LargeAllocated.Load() != 1 {
		t.Fatal("no new slot without a spare")
	}
	for range transport.LargeSpares + 2 {
		mt.spareSlot(make([]byte, readSlotCapacity))
	}
	mt.spareSlot(make([]byte, readSlotCapacity-1))
	mt.spareSlot(make([]byte, 2048, readSlotCapacity+1))
	if len(mt.spares) != transport.LargeSpares || mt.stats.LargeDiscarded.Load() != 2 {
		t.Fatalf("%d spares, %d discarded", len(mt.spares), mt.stats.LargeDiscarded.Load())
	}
	if s := mt.readSlot(); len(s) != readHeadroom+readBufSize || cap(s) != readSlotCapacity || mt.stats.LargeReused.Load() != 1 {
		t.Fatal("a spare not made a read slot")
	}
}

func TestQueueRepresentationSizeAndMarker(t *testing.T) {
	// No larger than the pre-change layout, including alignment, on every
	// architecture on which this root test is run.
	type originalQueued struct {
		b  []byte
		at time.Time
	}
	if reflect.TypeOf(queued{}).Size() > reflect.TypeOf(originalQueued{}).Size() {
		t.Fatal("queue metadata grew")
	}
	s := newScheduler(1, 1<<20)
	for _, size := range []int{1, 1400, 8192, 8193, 9000, 9016, 9472, 10000} {
		b := make([]byte, size, max(size, readSlotCapacity))
		for i := range b {
			b[i] = byte(i)
		}
		s.enqueue(b)
		q := s.tryDequeuePacket()
		if packetIsSlot(q.b) || !bytes.Equal(packetData(q.b), b) || cap(q.b) != len(q.b) {
			t.Fatalf("ordinary %d mistaken for owned", size)
		}
	}
	back := make([]byte, readSlotCapacity)
	p := back[readHeadroom : readHeadroom+8193]
	s.enqueueBacking(p, back, -1)
	q := s.tryDequeuePacket()
	if !packetIsSlot(q.b) || len(packetData(q.b)) != len(p) || &q.b[0] != &back[0] {
		t.Fatal("owned marker lost")
	}
}

func TestLargeSwapHeldAndQueueDrop(t *testing.T) {
	mt, _, s, self := largeTUN(t, false, nil)
	dst := netip.MustParseAddr("100.96.0.12")
	for range cap(mt.large) {
		mt.large <- queued{}
	}
	small := dfPacket(self, dst, protoTCP, 1000, false)
	slot := make([]byte, readSlotCapacity)
	big := dfPacket(self, dst, protoTCP, 8193, false)
	copy(slot[readHeadroom:], big)
	s.enqueue(small)
	s.enqueueBacking(slot[readHeadroom:readHeadroom+len(big)], slot, -1)
	bs := [][]byte{make([]byte, 2048), make([]byte, 2048)}
	sizes := make([]int, 2)
	if n, e := mt.Read(bs, sizes, 0); e != nil || n != 1 || !packetIsSlot(mt.held.b) {
		t.Fatalf("held n=%d err=%v", n, e)
	}
	<-mt.large
	done := make(chan struct{})
	go func() { mt.Read(bs, sizes, 0); close(done) }()
	for range cap(mt.large) - 1 {
		<-mt.large
	}
	out := [][]byte{make([]byte, readSlotCapacity)}
	original := &out[0][0]
	lens := []int{0}
	n, e := mt.ReadLargeSwap(out, lens, readHeadroom, nil)
	if e != nil || n != 1 || &out[0][0] != &slot[0] || &out[0][0] == original || !bytes.Equal(out[0][readHeadroom:readHeadroom+len(big)], big) {
		t.Fatalf("held swap n=%d e=%v", n, e)
	}
	mt.sched.close()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("close blocked")
	}
	// Dropped owned packets must release their scheduler references and retain
	// the same byte-based limits; no pool buffer is needed to enqueue them.
	s2 := newScheduler(1, 65536)
	for i := 0; i < 20; i++ {
		b := make([]byte, readSlotCapacity)
		p := b[readHeadroom : readHeadroom+8193]
		copy(p, big)
		s2.enqueueBacking(p, b, -1)
	}
	if s2.queuedBytes() > s2.maxBytes || s2.queuedBytes() > s2.flowBytes {
		t.Fatal("queue budget exceeded")
	}
	last := 0
	for q := s2.tryDequeuePacket(); q.b != nil; q = s2.tryDequeuePacket() {
		if !packetIsSlot(q.b) || &q.b[readHeadroom] != &packetData(q.b)[0] {
			t.Fatal("ownership lost")
		}
		last++
	}
	if last == 0 || last >= 20 {
		t.Fatalf("queue did not drop: %d", last)
	}
}
