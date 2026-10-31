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
)

// readHeadroom leaves room for the Linux virtio header the TUN reads into.
const readHeadroom = 16

// readBufSize fits any packet the OS hands a TUN with the largest MTU Validate allows.
const readBufSize = 4096

type counters struct {
	Delivered, Forwarded, DroppedACL, DroppedNotRelay, DroppedTTL, DroppedNoRoute, DroppedQueue, DroppedBad atomic.Uint64
	// DroppedPlain: packets a relay would have forwarded that are not
	// end-to-end, and packets for this node from a node other than the
	// peer they came through.
	DroppedPlain atomic.Uint64
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
}

type hopPrefix struct {
	p    netip.Prefix
	peer string
}

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
}

func newMeshTUN(dev tun.Device, r *routing, f *filter, s *scheduler, mtu int) *meshTUN {
	t := &meshTUN{Device: dev, filter: f, sched: s, stats: &counters{}, entryProbe: &entryProbe{}}
	t.route.Store(r)
	t.mtu.Store(int32(mtu))
	s.dropped = func(n int) { t.stats.DroppedQueue.Add(uint64(n)) }
	go t.pump()
	return t
}

// MTU is the network's MTU, not the OS interface's: an end-to-end packet
// of the interface's MTU is that much once wrapped, and WireGuard pads a
// packet longer than its MTU past what the transport sends.
func (t *meshTUN) MTU() (int, error) { return int(t.mtu.Load()), nil }

// pump moves what the OS sends into the scheduler, through the end-to-end
// device where the next hop is not the destination's node.
func (t *meshTUN) pump() {
	batch := t.Device.BatchSize()
	bufs, sizes := make([][]byte, batch), make([]int, batch)
	for i := range bufs {
		bufs[i] = make([]byte, readHeadroom+readBufSize)
	}
	for {
		n, err := t.Device.Read(bufs, sizes, readHeadroom)
		for i := 0; i < n; i++ {
			pkt := append([]byte(nil), bufs[i][readHeadroom:readHeadroom+sizes[i]]...)
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
			t.filter.outbound(p)
			if r.hop(p.dst) == z.id {
				t.sched.enqueue(p.b)
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
// was given, or by its own header for 0.
func (t *meshTUN) sendOuter(b []byte, id uint64) {
	if id == 0 {
		t.sched.enqueue(b)
		return
	}
	t.sched.enqueueAt(b, int(id-1))
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

func (t *meshTUN) Read(bufs [][]byte, sizes []int, offset int) (int, error) {
	p, err := t.sched.dequeue()
	if err != nil {
		return 0, os.ErrClosed
	}
	n := 0
	for {
		sizes[n] = copy(bufs[n][offset:], p)
		n++
		if n == len(bufs) {
			return n, nil
		}
		if p = t.sched.tryDequeue(); p == nil {
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
		case !decTTL(p.b):
			t.stats.DroppedTTL.Add(1)
		default:
			t.sched.enqueue(append([]byte(nil), p.b...))
			t.stats.Forwarded.Add(1)
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
	if !r.transit[key] {
		return false
	}
	a, ok := netip.AddrFromSlice(src)
	return ok && !r.control.Contains(a) && containsAny(r.sources, a)
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
