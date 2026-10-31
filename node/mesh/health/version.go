package health

import (
	"time"

	"openbear/node/mesh/wire"
)

// protocolExpiry: a peer's range is forgotten when nothing came from it
// for this long, so a peer that restarted as an older release, and drops
// what is in a version it no longer speaks, gets this node's oldest again
// instead of silence on both sides. Probes come every 1 to 2 seconds on
// the paths in use.
const protocolExpiry = 15 * time.Second

type peerProtocol struct {
	r  wire.Range
	at time.Time // last message from the peer
}

// protocolBackend is a backend that sends to a peer in its own formats too
// (the transport's datagrams) and so wants to know the peer's range.
type protocolBackend interface {
	SetPeerProtocol(peer string, r wire.Range)
}

// learnProtocol takes the range a message from peer declares.
func (m *Manager) learnProtocol(peer string, v message, now time.Time) {
	if m.protocols == nil {
		m.protocols = map[string]peerProtocol{}
	}
	old, ok := m.protocols[peer]
	ok = ok && now.Sub(old.at) < protocolExpiry
	r := wire.Range{Min: v.Min, Max: v.Max}
	switch {
	case r.Max == 0 && ok:
		// An advertisement: the range stays as the probes declared it.
		r = old.r
	case r.Max == 0:
		r = wire.Range{Min: v.Version, Max: v.Version}
	case r.Min <= 0 || r.Min > r.Max:
		r.Min = r.Max
	}
	m.protocols[peer] = peerProtocol{r, now}
	if !ok || old.r != r {
		m.tellBackend(peer, r)
	}
}

// sendVersion is the version of a message to peer: the newest both speak
// while its range is fresh, this node's oldest otherwise.
func (m *Manager) sendVersion(peer string, now time.Time) int {
	p, ok := m.protocols[peer]
	return m.cfg.Protocol.Or().Send(p.r, ok && now.Sub(p.at) < protocolExpiry)
}

func (m *Manager) expireProtocols(now time.Time) {
	for peer, p := range m.protocols {
		if now.Sub(p.at) >= protocolExpiry {
			delete(m.protocols, peer)
			m.tellBackend(peer, wire.Range{})
		}
	}
}

func (m *Manager) tellBackend(peer string, r wire.Range) {
	if b, ok := m.backend.(protocolBackend); ok {
		b.SetPeerProtocol(peer, r)
	}
}

func (m *Manager) protocolSnapshot(now time.Time) map[string]int {
	out := map[string]int{}
	for peer, p := range m.protocols {
		if now.Sub(p.at) < protocolExpiry {
			out[peer] = m.sendVersion(peer, now)
		}
	}
	return out
}

// SetPeerProtocol hands a peer's range to the transport, whose datagrams to
// the peer follow it.
func (c *Control) SetPeerProtocol(peer string, r wire.Range) { c.bind.SetPeerProtocol(peer, r) }

var _ protocolBackend = (*Control)(nil)
var _ redialBackend = (*Control)(nil)
