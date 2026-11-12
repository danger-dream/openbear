package mesh

import (
	"encoding/hex"
	"errors"
	"net/netip"
	"os"
	"slices"
	"sync/atomic"

	"golang.zx2c4.com/wireguard/conn"
	"golang.zx2c4.com/wireguard/tun"

	"openbear/node/mesh/transport"
	"openbear/node/mesh/wire"
)

// readHeadroom leaves room for the Linux virtio header the TUN reads into.
const readHeadroom = 16

// readBufSize fits any packet the OS hands a TUN with the largest MTU it
// may have, which large packets raise to largeInner.
const readBufSize = wire.LargeMTU

// Same allocation class as the original slot and large WG message buffer.
const readSlotCapacity = 9472

type counters struct {
	Delivered, Forwarded, DroppedACL, DroppedNotRelay, DroppedTTL, DroppedNoRoute, DroppedQueue, DroppedBad atomic.Uint64
	// DroppedPlain: packets a relay would have forwarded that are not
	// end-to-end, and packets for this node from a node other than the
	// peer they came through.
	DroppedPlain atomic.Uint64
	// DroppedTooBig: packets longer than their path takes (large.go).
	DroppedTooBig atomic.Uint64
	// What became of large send buffers: LargeSwapped packets WireGuard
	// took over in their read slot, LargeCopied ones it took as a copy;
	// LargeReused read slots that were spares, LargeAllocated new ones for
	// want of a spare; LargeDiscarded buffers left to the GC, the spares
	// being full.
	LargeSwapped, LargeCopied, LargeReused, LargeAllocated, LargeDiscarded atomic.Uint64
}

// routing is what meshTUN needs from the applied config; swapped whole.
type routing struct {
	relay   bool
	local   []netip.Prefix // this node's address and gateway subnets
	control netip.Prefix   // control plane addresses never ride the business device
	routes  []netip.Prefix // every peer's AllowedIPs
	// hops are the peers' AllowedIPs, longest first, with the node each
	// leads to: a packet goes end to end unless its next hop is the node
	// it is for.
	hops []hopPrefix
	// peerOf is the node of each peer's public key.
	peerOf map[[32]byte]string
	nodes  *e2eTable
	// Relays choose next hops independently, so a neighbouring relay forwards
	// packets whose source this node routes elsewhere, and a member's entry
	// those of a node it routes over a punched link. From relay and entry
	// peers any node's address is accepted: what relays forward is end to
	// end, from one node's address to another's.
	transit map[[32]byte]bool
	sources []netip.Prefix
	// own is a member peer's own prefixes, accepted from it also where the
	// routes reach it through another relay: a fixed path brings its
	// packets here all the same.
	own map[[32]byte][]netip.Prefix
	// gateways: a member's, while backups stand by for subnets
	// (standby.go): what it sends into them, and its probes' answers.
	gateways *standbyGateways
	// pins: the fixed paths' pairs through this node (fixedpath.go).
	pins pinTable
}

type hopPrefix struct {
	p    netip.Prefix
	peer string
}

// dropForwarded, set by the lab (TestNetnsNode), drops packets this relay
// would forward while its links stay up.
var dropForwarded func(p ipv4) bool

// hop is the peer packets for a leave through, "" for none.
func (r *routing) hop(a netip.Addr) string {
	for _, h := range r.hops {
		if h.p.Contains(a) {
			return h.peer
		}
	}
	return ""
}

// meshTUN sits between the OS TUN and WireGuard. Packets for a node that is
// not the next hop are encrypted end to end first (e2e.go). Packets
// decrypted for another node go straight back out to the next hop without
// entering the OS, on relays only and only end to end ones; packets for
// this node reach the OS past the filter. Everything WireGuard encrypts
// leaves through the scheduler.
type meshTUN struct {
	tun.Device
	filter *filter
	sched  *scheduler
	stats  *counters
	route  atomic.Pointer[routing]
	e2e    atomic.Pointer[e2eSessions]
	// mtu is the network's, which the outer device's packets may reach;
	// the OS interface has e2eOverhead less.
	mtu atomic.Int32
	// entryProbe is a member's probing of its entry (entryprobe.go).
	entryProbe *entryProbe
	// paths: which packets may be large (large.go).
	paths *paths
	// large has the packets Read finds too long for the outer device's
	// buffers, for ReadLarge; held is one Read took while large was full,
	// for its next call. Only Read's caller touches held.
	large chan queued
	// largeReading: the device reads what large holds (it has a large pool,
	// on Linux); a test may say otherwise.
	largeReading atomic.Bool
	held         queued
	// A flow keeps its order across the two readers. Read, alone, numbers
	// what it puts in large (largePut), and keeps each bucket's last number
	// (largeLast), its batch's buckets (batch) and held's (heldBucket);
	// ReadLarge counts what it took (largeTaken) and, once it is back for
	// more, what WireGuard has from it (largeDone): large is a FIFO.
	largePut, heldBucket int
	largeLast, batch     []int
	largeTaken           int
	largeDone            atomic.Int64
	// spares are the large buffers ReadLargeSwap displaced, for pump to read
	// into in place of the slots that went into the scheduler; at most
	// transport.LargeSpares, more go to the GC.
	spares chan []byte
}

func newMeshTUN(dev tun.Device, r *routing, f *filter, s *scheduler, mtu int) *meshTUN {
	t := &meshTUN{Device: dev, filter: f, sched: s, stats: &counters{}, entryProbe: &entryProbe{}, paths: newPaths(mtu), large: make(chan queued, 64), spares: make(chan []byte, transport.LargeSpares),
		largeLast: make([]int, len(s.buckets))}
	t.route.Store(r)
	t.mtu.Store(int32(mtu))
	t.largeReading.Store(largePool)
	s.dropped = func(n int) { t.stats.DroppedQueue.Add(uint64(n)) }
	go t.pump()
	return t
}

// MTU is the network's MTU, not the OS interface's: an end-to-end packet
// of the interface's MTU is that much once wrapped, and WireGuard pads a
// packet longer than its MTU past what the transport sends.
func (t *meshTUN) MTU() (int, error) { return int(t.mtu.Load()), nil }

// takeTUNPacket gives a packet its own allocation before the next TUN read.
// Large packets and a full read slot use the same Go allocation size class;
// transfer the slot instead of copying it. Small packets retain a compact
// allocation and cannot pin an entire large slot in the scheduler.
func takeTUNPacket(bufs [][]byte, i, size int) []byte {
	p, _ := takeTUNPacketBacking(bufs, i, size, newReadSlot)
	return p
}

// newReadSlot is a TUN read slot of its own.
func newReadSlot() []byte { return make([]byte, readHeadroom+readBufSize, readSlotCapacity) }

// takeTUNPacketBacking is takeTUNPacket and the transferred slot's backing;
// next gives the slot that replaces it.
func takeTUNPacketBacking(bufs [][]byte, i, size int, next func() []byte) ([]byte, []byte) {
	if size > 8192 {
		slot := bufs[i]
		p := slot[readHeadroom : readHeadroom+size : readHeadroom+size]
		bufs[i] = next()
		if cap(slot) >= readSlotCapacity {
			return p, slot[:readSlotCapacity:readSlotCapacity]
		}
		return p, nil
	}
	return append([]byte(nil), bufs[i][readHeadroom:readHeadroom+size]...), nil
}

// readSlot replaces a read slot that went into the scheduler: a spare, a
// buffer WireGuard gave back (readLarge), or a new one.
func (t *meshTUN) readSlot() []byte {
	select {
	case b := <-t.spares:
		t.stats.LargeReused.Add(1)
		return b[: readHeadroom+readBufSize : readSlotCapacity]
	default:
		t.stats.LargeAllocated.Add(1)
		return newReadSlot()
	}
}

// spareSlot keeps b, a buffer nothing else holds any more, for a later read;
// one the spares have no room for, or not a slot's size, goes to the GC.
func (t *meshTUN) spareSlot(b []byte) {
	if cap(b) != readSlotCapacity {
		return
	}
	select {
	case t.spares <- b[:readSlotCapacity]:
	default:
		t.stats.LargeDiscarded.Add(1)
	}
}

// pump moves what the OS sends into the scheduler, through the end-to-end
// device where the next hop is not the destination's node.
func (t *meshTUN) pump() {
	batch := t.Device.BatchSize()
	bufs, sizes := make([][]byte, batch), make([]int, batch)
	for i := range bufs {
		bufs[i] = newReadSlot()
	}
	next := t.readSlot
	for {
		n, err := t.Device.Read(bufs, sizes, readHeadroom)
		for i := 0; i < n; i++ {
			pkt, backing := takeTUNPacketBacking(bufs, i, sizes[i], next)
			p, ok := parseIPv4(pkt)
			if !ok {
				continue // IPv6 and other noise the OS sends to any interface
			}
			r := t.route.Load()
			if r.control.Contains(p.src) || r.control.Contains(p.dst) || !containsAny(r.routes, p.dst) {
				t.stats.DroppedNoRoute.Add(1)
				continue
			}
			if containsAny(r.local, p.dst) {
				// macOS routes the address of a point-to-point utun into the
				// tunnel; without this it would travel to the relay and back.
				back := make([]byte, readHeadroom+len(pkt))
				copy(back[readHeadroom:], pkt)
				t.Device.Write([][]byte{back}, readHeadroom)
				continue
			}
			z := r.nodes.owner(p.dst)
			if z == nil {
				t.stats.DroppedNoRoute.Add(1)
				continue
			}
			if r.gateways != nil {
				r.gateways.use(p.dst)
			}
			hop := r.hop(p.dst)
			if h, ok := r.pins.local(r.nodes.self, z.addr); ok {
				// A fixed path's first relay; end to end even when the
				// destination is a next hop of its own.
				hop = h.nextID
			}
			if t.tooBigLocal(p, z, hop) {
				continue
			}
			t.filter.outbound(p)
			if hop == z.id {
				t.sched.enqueueBacking(p.b, backing, -1)
				continue
			}
			e := t.e2e.Load()
			if e == nil {
				t.stats.DroppedNoRoute.Add(1)
				continue
			}
			e.send(p.b, z, uint64(t.sched.bucketOf(p.b))+1)
		}
		if err != nil && !errors.Is(err, tun.ErrTooManySegments) {
			t.sched.close()
			return
		}
	}
}

// sendOuter queues an end-to-end packet in the bucket id-1 its inner flow
// was given, or by its own header for 0. A pair on fixed paths goes over the
// one in use, marked with its rank, and a probe of one path (pathFlow and its
// rank) over that path; marked 0 while it takes the routes.
func (t *meshTUN) sendOuter(b []byte, id uint64) {
	p, ok := parseIPv4(b)
	if !ok {
		t.sched.enqueueAt(b, int(id)-1)
		return
	}
	pins := t.route.Load().pins
	bucket, h, pinned := int(id)-1, pinHop{}, false
	if id&pathFlow != 0 {
		bucket = -1
		h, pinned = pins.ranked(p.src, p.dst, int(id&^pathFlow))
	} else {
		h, pinned = pins.local(p.src, p.dst)
	}
	switch {
	case pinned:
		markPath(b, h.rank)
		t.sched.enqueuePeer(b, bucket, h.next)
		return
	case pins.source(p.src, p.dst):
		markPath(b, 0)
	}
	t.sched.enqueueAt(b, bucket)
}

// deliverInner takes what the end-to-end device decrypted: its source is
// already checked against the sending node's prefixes, so only packets for
// this node pass, through the filter, to the OS. Decrypted packets are
// never forwarded.
func (t *meshTUN) deliverInner(bufs [][]byte, offset int) {
	r := t.route.Load()
	out := make([][]byte, 0, len(bufs))
	for _, b := range bufs {
		p, ok := parseIPv4(b[offset:])
		switch {
		case !ok || r.control.Contains(p.src) || r.control.Contains(p.dst):
			t.e2eStats(func(s *e2eStats) { s.DroppedBad.Add(1) })
		case !containsAny(r.local, p.dst):
			t.e2eStats(func(s *e2eStats) { s.DroppedSource.Add(1) })
		case t.relayTooBig(p, r):
		case isProbe(p, r.nodes.self):
			t.probe(p, r)
		case !t.filter.inbound(p):
			t.stats.DroppedACL.Add(1)
		default:
			out = append(out, b)
			t.stats.Delivered.Add(1)
		}
	}
	if len(out) > 0 {
		t.Device.Write(out, offset)
	}
}

func (t *meshTUN) e2eStats(f func(*e2eStats)) {
	if e := t.e2e.Load(); e != nil {
		f(&e.stats)
	}
}

// Read hands a packet longer than the network MTU, which the buffers are
// sized for, on to ReadLarge. While ReadLarge falls behind, Read waits for
// it rather than dropping, which leaves the packets waiting to the
// scheduler, whose limits and CoDel decide what goes. A device with no large
// reader (no large pool: other platforms than Linux) is never handed one: a
// packet over the MTU that fits the buffer goes as usual, one that does not is
// dropped.
//
// The two readers encrypt and send apart, so no packet goes to one while an
// earlier one of its flow is still with the other: a flow with packets in
// large, or with ReadLarge, sends the rest there too, and one for large waits
// for the end of this batch when its flow is in it. WireGuard has a batch once
// its reader is back for the next.
func (t *meshTUN) Read(bufs [][]byte, sizes []int, offset int) (int, error) {
	return t.ReadWithMetadata(bufs, sizes, offset, nil)
}

// ReadWithMetadata (conn.MetadataReader) is Read giving each packet the peer
// it was queued for, if any.
func (t *meshTUN) ReadWithMetadata(bufs [][]byte, sizes []int, offset int, metadata []conn.SendMetadata) (int, error) {
	small := int(t.mtu.Load())
	t.batch = t.batch[:0]
	n := 0
	for n < len(bufs) {
		var q queued
		var bucket int
		if t.held.b != nil {
			q, bucket, t.held = t.held, t.heldBucket, queued{}
		} else if n == 0 {
			var err error
			if q, bucket, err = t.sched.dequeueFlow(); err != nil {
				return 0, os.ErrClosed
			}
		} else {
			if q, bucket = t.sched.tryDequeueFlow(); q.b == nil {
				break
			}
		}
		p := packetData(q.b)
		if !t.largeReading.Load() {
			if len(p) > len(bufs[n])-offset {
				t.stats.DroppedTooBig.Add(1)
				continue
			}
		} else if len(p) > small || len(p) > len(bufs[n])-offset || t.largeOwes(bucket) {
			if n > 0 && slices.Contains(t.batch, bucket) {
				t.held, t.heldBucket = q, bucket
				break
			}
			select {
			case t.large <- q:
				t.putLarge(bucket)
				continue
			default:
			}
			if n > 0 {
				t.held, t.heldBucket = q, bucket
				break
			}
			select {
			case t.large <- q:
				t.putLarge(bucket)
			case <-t.sched.done:
				return 0, os.ErrClosed
			}
			continue
		}
		t.batch = append(t.batch, bucket)
		sizes[n] = copy(bufs[n][offset:], p)
		if n < len(metadata) {
			metadata[n].Peer = peerKey(q.peer)
		}
		n++
	}
	return n, nil
}

// largeOwes: bucket has packets in large or with ReadLarge (Read's caller).
func (t *meshTUN) largeOwes(bucket int) bool {
	return bucket >= 0 && bucket < len(t.largeLast) && int64(t.largeLast[bucket]) > t.largeDone.Load()
}

func (t *meshTUN) putLarge(bucket int) {
	t.largePut++
	if bucket >= 0 && bucket < len(t.largeLast) {
		t.largeLast[bucket] = t.largePut
	}
}

// ReadLarge (conn.LargeReader) gives the outer device's large reader what
// Read handed on.
func (t *meshTUN) ReadLarge(bufs [][]byte, sizes []int, offset int, metadata []conn.SendMetadata) (int, error) {
	return t.readLarge(bufs, sizes, offset, metadata, false)
}

// ReadLargeSwap lets WG adopt an exclusively owned slot instead of copying.
func (t *meshTUN) ReadLargeSwap(bufs [][]byte, sizes []int, offset int, metadata []conn.SendMetadata) (int, error) {
	return t.readLarge(bufs, sizes, offset, metadata, true)
}

func (t *meshTUN) readLarge(bufs [][]byte, sizes []int, offset int, metadata []conn.SendMetadata, swap bool) (int, error) {
	t.largeReading.Store(true)
	t.largeDone.Add(int64(t.largeTaken))
	t.largeTaken = 0
	var q queued
	select {
	case q = <-t.large:
	case <-t.sched.done:
		return 0, os.ErrClosed
	}
	n := 0
	for {
		t.largeTaken++
		raw := q.b
		p := packetData(raw)
		if len(p) <= len(bufs[n])-offset && n < len(metadata) {
			metadata[n].Peer = peerKey(q.peer)
		}
		if len(p) <= len(bufs[n])-offset {
			if swap && offset == readHeadroom && packetIsSlot(raw) && cap(raw) == len(bufs[n]) && cap(raw) >= offset+len(p)+31 {
				// The buffer WireGuard read into is ours from now on
				// (conn.LargeSwapReader): a read slot for pump.
				displaced := bufs[n]
				bufs[n] = raw[:cap(raw)]
				sizes[n] = len(p)
				t.spareSlot(displaced)
				t.stats.LargeSwapped.Add(1)
			} else {
				sizes[n] = copy(bufs[n][offset:], p)
				t.stats.LargeCopied.Add(1)
			}
			n++
		} else {
			t.stats.DroppedQueue.Add(1)
		}
		if n == len(bufs) {
			return n, nil
		}
		select {
		case q = <-t.large:
		default:
			return n, nil
		}
	}
}

func (t *meshTUN) Write(bufs [][]byte, offset int) (int, error) {
	return t.WriteWithMetadata(bufs, offset, nil)
}

// WriteWithMetadata takes what the outer device decrypted, with the peer
// each packet came from.
func (t *meshTUN) WriteWithMetadata(bufs [][]byte, offset int, metadata []conn.ReceiveMetadata) (int, error) {
	out := make([][]byte, 0, len(bufs))
	r := t.route.Load()
	for i, b := range bufs {
		p, ok := parseIPv4(b[offset:])
		from := ""
		if i < len(metadata) {
			from = r.peerOf[metadata[i].PublicKey]
		}
		switch {
		case !ok || r.control.Contains(p.src) || r.control.Contains(p.dst):
			t.stats.DroppedBad.Add(1)
		case p.proto == protoE2E && p.dst == r.nodes.self:
			z := r.nodes.byAddr[p.src]
			e := t.e2e.Load()
			if z == nil || e == nil || p.fragmented() {
				t.stats.DroppedBad.Add(1)
				continue
			}
			e.receive(p.payload(), z)
		case containsAny(r.local, p.dst):
			// Plain only from the node it is from: anything relayed is end
			// to end, and a relay cannot speak for another node.
			if x := r.nodes.owner(p.src); x == nil || x.id != from {
				t.stats.DroppedPlain.Add(1)
				continue
			}
			if t.relayTooBig(p, r) {
				continue
			}
			if t.filter.inbound(p) {
				out = append(out, b)
				t.stats.Delivered.Add(1)
			} else {
				t.stats.DroppedACL.Add(1)
			}
		case !r.relay:
			t.stats.DroppedNotRelay.Add(1)
		case p.proto != protoE2E || r.nodes.byAddr[p.src] == nil || r.nodes.byAddr[p.dst] == nil:
			t.stats.DroppedPlain.Add(1)
		case !containsAny(r.routes, p.dst):
			t.stats.DroppedNoRoute.Add(1)
		default:
			h, pinned := r.pins.forward(p.src, p.dst, from)
			if rank, marked := p.pathRank(); marked {
				h, pinned = r.pins.marked(p.src, p.dst, from, rank)
			}
			hop := r.hop(p.dst)
			if pinned {
				hop = h.nextID
			}
			switch {
			case dropForwarded != nil && dropForwarded(p):
			case t.tooBigForward(r, p, hop):
			case !decTTL(p.b):
				t.stats.DroppedTTL.Add(1)
			case pinned:
				t.sched.enqueuePeer(append([]byte(nil), p.b...), -1, h.next)
				t.stats.Forwarded.Add(1)
			default:
				t.sched.enqueue(append([]byte(nil), p.b...))
				t.stats.Forwarded.Add(1)
			}
		}
	}
	if len(out) > 0 {
		if _, err := t.Device.Write(out, offset); err != nil {
			return 0, err
		}
	}
	return len(bufs), nil
}

func (t *meshTUN) AuthorizeSource(key [32]byte, src []byte) bool {
	r := t.route.Load()
	a, ok := netip.AddrFromSlice(src)
	if !ok || r.control.Contains(a) {
		return false
	}
	if r.transit[key] {
		return containsAny(r.sources, a)
	}
	return containsAny(r.own[key], a)
}

func (t *meshTUN) Close() error {
	t.sched.close()
	return t.Device.Close()
}

// hopsOf is every peer's AllowedIPs with its node, longest first.
func hopsOf(peers []peerSpec) (hops []hopPrefix, peerOf map[[32]byte]string) {
	peerOf = map[[32]byte]string{}
	for _, p := range peers {
		for _, a := range p.Allowed {
			hops = append(hops, hopPrefix{a, p.ID})
		}
		if k, err := hex.DecodeString(p.Key); err == nil && len(k) == 32 {
			peerOf[[32]byte(k)] = p.ID
		}
	}
	slices.SortStableFunc(hops, func(a, b hopPrefix) int { return b.p.Bits() - a.p.Bits() })
	return hops, peerOf
}

func containsAny(list []netip.Prefix, a netip.Addr) bool {
	for _, p := range list {
		if p.Contains(a) {
			return true
		}
	}
	return false
}
