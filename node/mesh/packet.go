package mesh

import (
	"encoding/binary"
	"net/netip"
)

const (
	protoICMP = 1
	protoTCP  = 6
	protoUDP  = 17
)

type ipv4 struct {
	b        []byte
	ihl      int
	proto    byte
	src, dst netip.Addr
	id       uint16
	frag     uint16 // fragment offset in 8-byte units
	more     bool
}

func parseIPv4(b []byte) (p ipv4, ok bool) {
	if len(b) < 20 || b[0]>>4 != 4 {
		return p, false
	}
	ihl := int(b[0]&0x0f) * 4
	total := int(binary.BigEndian.Uint16(b[2:4]))
	if ihl < 20 || total < ihl || total > len(b) {
		return p, false
	}
	ff := binary.BigEndian.Uint16(b[6:8])
	return ipv4{b: b[:total], ihl: ihl, proto: b[9], src: netip.AddrFrom4([4]byte(b[12:16])), dst: netip.AddrFrom4([4]byte(b[16:20])),
		id: binary.BigEndian.Uint16(b[4:6]), frag: ff & 0x1fff, more: ff&0x2000 != 0}, true
}

func (p ipv4) fragmented() bool { return p.frag != 0 || p.more }

// payload is the transport header and data; nil for a non-first fragment.
func (p ipv4) payload() []byte {
	if p.frag != 0 {
		return nil
	}
	return p.b[p.ihl:]
}

// ports reads TCP/UDP ports, or for ICMP echo the identifier as the first value.
func (p ipv4) ports() (a, b uint16, ok bool) {
	l4 := p.payload()
	switch p.proto {
	case protoTCP, protoUDP:
		if len(l4) < 4 {
			return 0, 0, false
		}
		return binary.BigEndian.Uint16(l4[0:2]), binary.BigEndian.Uint16(l4[2:4]), true
	case protoICMP:
		if len(l4) < 8 || (l4[0] != 0 && l4[0] != 8) {
			return 0, 0, false
		}
		return binary.BigEndian.Uint16(l4[4:6]), 0, true
	}
	return 0, 0, false
}

// decTTL lowers the TTL by one and patches the header checksum (RFC 1624);
// false means the packet must not be forwarded any further.
func decTTL(b []byte) bool {
	if b[8] <= 1 {
		return false
	}
	old := binary.BigEndian.Uint16(b[8:10])
	b[8]--
	sum := uint32(^binary.BigEndian.Uint16(b[10:12])) + uint32(^old) + uint32(binary.BigEndian.Uint16(b[8:10]))
	for sum > 0xffff {
		sum = sum&0xffff + sum>>16
	}
	binary.BigEndian.PutUint16(b[10:12], ^uint16(sum))
	return true
}

func headerChecksum(h []byte) uint16 {
	var sum uint32
	for i := 0; i+1 < len(h); i += 2 {
		sum += uint32(binary.BigEndian.Uint16(h[i:]))
	}
	for sum > 0xffff {
		sum = sum&0xffff + sum>>16
	}
	return ^uint16(sum)
}
