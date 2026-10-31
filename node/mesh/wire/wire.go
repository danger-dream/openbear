// Package wire is the version of what mesh nodes exchange with each other:
// the health control messages and the routing advertisements they carry,
// and the transport framing (the OBM preamble). One number covers them all.
//
// Each node speaks a range, ProtocolMin..Protocol. It sends a peer the
// highest version both have once it knows the peer's range, and its own
// lowest until then; it learns the range from what the peer sends (every
// control message and every stream preamble declares it). What a node does
// not know, it ignores without closing anything: an unknown control message
// type or field, a frame of an unknown plane, a datagram or message of a
// version it does not speak. A new format raises Protocol; the old one may
// be dropped by raising ProtocolMin one release later at the earliest, so
// a node one release behind keeps working with this one (N-1).
package wire

// Protocol is the newest mesh node-to-node protocol this build speaks and
// ProtocolMin the oldest.
//
// Version 2 signs what a node says on another's behalf (attachment claims
// and notices, conflicts, routing advertisements, punch signalling) and
// carries members' traffic end to end. It does not speak version 1: a mesh
// is upgraded as a whole, and nodes left on version 1 stop talking to it.
const (
	Protocol    = 2
	ProtocolMin = 2
)

// Range is the versions one node speaks.
type Range struct{ Min, Max int }

// Local is this build's range.
func Local() Range { return Range{ProtocolMin, Protocol} }

// Or is r, or Local when r is unset.
func (r Range) Or() Range {
	if r.Max == 0 {
		return Local()
	}
	return r
}

// Common is the highest version in both r and o; false when they do not
// overlap.
func (r Range) Common(o Range) (int, bool) {
	v := min(r.Max, o.Max)
	return v, v >= max(r.Min, o.Min)
}

// Send is the version to send a peer: the highest both have once its range
// is known, else this node's lowest, which the release before reads too.
func (r Range) Send(peer Range, known bool) int {
	if v, ok := r.Common(peer); known && ok {
		return v
	}
	return r.Min
}

// Has says whether r includes version v.
func (r Range) Has(v int) bool { return r.Min <= v && v <= r.Max }
