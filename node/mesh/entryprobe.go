package mesh

import (
	"bytes"
	"crypto/rand"
	"encoding/binary"
	"fmt"
	mrand "math/rand/v2"
	"net/netip"
	"slices"
	"sync"
	"time"
)

// An entry relay that drops the business traffic it forwards but answers
// its probes is seen by end-to-end probes: every probeEvery a member sends
// one through its entry to another relay, as an end-to-end packet the entry
// cannot tell from business traffic, of a random size. The relay's data
// plane answers it in kind. After probeFailures unanswered in a row the
// member tells its health manager, which moves off the entry in failover
// mode (health/entrycheck.go).
//
// A probe is a UDP datagram from port 0 to port 0 of a node's own mesh
// address, which no application sends: type (1 request, 2 reply), 8 bytes
// of id, padding.

const (
	probeEvery    = 10 * time.Second
	probeFailures = 3
	probeMin      = 200
	probeMax      = 1300
)

const (
	probeRequest = 1
	probeReply   = 2
)

// entryProbe is a member's probing of its entry.
type entryProbe struct {
	mu       sync.Mutex
	entry    string // the relay probed through
	target   string
	id       [8]byte
	waiting  bool
	failures int
}

// isProbe: p is a probe for this node.
func isProbe(p ipv4, self netip.Addr) bool {
	l4 := p.payload()
	return p.proto == protoUDP && p.dst == self && !p.fragmented() && len(l4) >= 8+9 && binary.BigEndian.Uint32(l4[:4]) == 0
}

// probePacket is a probe of kind from src to dst with id, size bytes long.
func probePacket(src, dst netip.Addr, kind byte, id [8]byte, size int) []byte {
	size = max(size, 20+8+9)
	b := make([]byte, size)
	b[0], b[8], b[9] = 0x45, 64, protoUDP
	binary.BigEndian.PutUint16(b[2:4], uint16(size))
	binary.BigEndian.PutUint16(b[6:8], 0x4000) // DF
	s, d := src.As4(), dst.As4()
	copy(b[12:16], s[:])
	copy(b[16:20], d[:])
	binary.BigEndian.PutUint16(b[10:12], headerChecksum(b[:20]))
	binary.BigEndian.PutUint16(b[24:26], uint16(size-20)) // ports 0, checksum 0
	b[28] = kind
	copy(b[29:37], id[:])
	rand.Read(b[37:])
	return b
}

// probe answers a request for this node and takes a reply to its own.
func (t *meshTUN) probe(p ipv4, r *routing) {
	l4 := p.payload()
	var id [8]byte
	copy(id[:], l4[9:17])
	switch l4[8] {
	case probeRequest:
		z, e := r.nodes.byAddr[p.src], t.e2e.Load()
		if z != nil && e != nil {
			e.sendControl(probePacket(r.nodes.self, p.src, probeReply, id, len(p.b)), z)
		}
	case probeReply:
		if ep := t.entryProbe; ep != nil {
			ep.mu.Lock()
			if ep.waiting && bytes.Equal(ep.id[:], id[:]) && r.nodes.byAddr[p.src] != nil && r.nodes.byAddr[p.src].id == ep.target {
				ep.waiting, ep.failures = false, 0
			}
			ep.mu.Unlock()
		}
	}
}

// probeEntry runs a member's probes until the engine closes.
func (e *engine) probeEntry() {
	ep := e.tun.entryProbe
	for {
		select {
		case <-e.done:
			return
		case <-time.After(probeEvery + time.Duration(mrand.Int64N(int64(probeEvery/5)))):
		}
		entry, target := e.probeTarget()
		ep.mu.Lock()
		if ep.entry != entry || ep.target != target {
			ep.entry, ep.target, ep.waiting, ep.failures = entry, target, false, 0
		}
		if ep.waiting {
			ep.failures++
		}
		failures := ep.failures
		if failures >= probeFailures {
			ep.failures = 0
		}
		ep.waiting = false
		if target != "" {
			rand.Read(ep.id[:])
			ep.waiting = true
		}
		id := ep.id
		ep.mu.Unlock()
		if failures >= probeFailures {
			e.health.EntrySuspect(entry, target, fmt.Sprintf("%d end-to-end probes through it to %s in a row went unanswered", failures, target))
		}
		if target == "" {
			continue
		}
		r, s := e.tun.route.Load(), e.e2e
		z := r.nodes.byID[target]
		if z == nil {
			continue
		}
		size := probeMin + mrand.IntN(max(1, min(probeMax, int(s.tun.mtu.Load()))-probeMin+1))
		s.sendControl(probePacket(r.nodes.self, z.addr, probeRequest, id, size), z)
	}
}

// probeTarget is the active entry relay and another relay to probe through
// it: a ready candidate, which the health manager can tell is up, else any.
func (e *engine) probeTarget() (entry, target string) {
	snap := e.health.Snapshot()
	var ready []string
	for _, c := range snap.Candidates {
		switch {
		case c.State == "active":
			entry = c.Peer
		case c.State == "ready":
			ready = append(ready, c.Peer)
		}
	}
	if entry == "" {
		return "", ""
	}
	for _, p := range ready {
		if p != entry {
			return entry, p
		}
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	var relays []string
	for _, n := range e.cfg.Nodes {
		if n.Role == roleRelay && n.ID != entry {
			relays = append(relays, n.ID)
		}
	}
	if len(relays) == 0 {
		return entry, ""
	}
	slices.Sort(relays)
	return entry, relays[0]
}
