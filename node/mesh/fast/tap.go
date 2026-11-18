package fast

import (
	"encoding/binary"
	"net/netip"
	"sync/atomic"
	"time"
)

// warmEvery is how often packets to a destination whose channel is not ready
// remind the channel; their connections go over the mesh meanwhile.
const warmEvery = time.Second

// Outbound looks at a packet, IPv4, this node sends into the mesh. A packet
// to a fast port is taken where capture runs in the data plane: one of a
// connection taken already, or the first of a connection while its channel is
// ready. The first packet of a connection whose channel is not ready opens
// the channel (or keeps it) and goes over the mesh, as do the rest of it.
// True when the fast relay took the packet instead.
func (s *Service) Outbound(b []byte) bool {
	st := s.state.Load()
	if st == nil || len(st.targets) == 0 || len(b) < 20 || b[0]>>4 != 4 || binary.BigEndian.Uint16(b[6:8])&0x1fff != 0 {
		return false
	}
	ihl := int(b[0]&0x0f) * 4
	var proto byte
	switch b[9] {
	case 6:
		if ihl < 20 || len(b) < ihl+20 {
			return false
		}
		proto = protoTCP
	case 17:
		if ihl < 20 || len(b) < ihl+8 {
			return false
		}
		proto = protoUDP
	default:
		return false
	}
	t, ok := st.target(netip.AddrFrom4([4]byte(b[16:20])), binary.BigEndian.Uint16(b[ihl+2:]), proto)
	if !ok {
		return false
	}
	opens := proto == protoUDP || b[ihl+13]&0x12 == 0x02
	// A datagram in fragments goes over the mesh, which carries them all.
	if tc := s.tun.Load(); tc != nil && b[6]&0x20 == 0 && tc.take(b, ihl, proto, opens, t.Node) {
		return true
	}
	if !opens || s.isReady(t.Node) {
		return false
	}
	if at := st.warmed[t.Node]; at != nil {
		now := s.clock().UnixNano()
		if last := at.Load(); now-last >= int64(warmEvery) && at.CompareAndSwap(last, now) {
			if chans := s.live.Load(); chans != nil {
				chans.warm(t.Node)
			}
		}
	}
	return false
}

func warmTimes(dests map[string]bool) map[string]*atomic.Int64 {
	out := make(map[string]*atomic.Int64, len(dests))
	for d := range dests {
		out[d] = new(atomic.Int64)
	}
	return out
}
