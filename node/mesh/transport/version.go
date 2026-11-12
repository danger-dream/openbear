package transport

import (
	"time"

	"openbear/node/mesh/wire"
)

// protocol is the mesh protocol range this node speaks.
func (b *Bind) protocol() wire.Range {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.opt.Protocol
}

// SetPeerProtocol tells the bind which mesh protocols peer id speaks, as
// its control messages declare them; a zero range forgets it. UDP to the
// peer goes out in the newest both speak, and in this node's oldest while
// the range is not known.
func (b *Bind) SetPeerProtocol(id string, r wire.Range) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if p := b.peers[id]; p != nil {
		p.protocol = r
	}
	if r.Max == 0 {
		// Forgotten as the peer went silent, perhaps restarted as an older
		// release: what its paths were last heard in no longer says what
		// it reads.
		for _, ep := range b.paths {
			if ep.peer == id {
				ep.rxVersion = 0
			}
		}
	}
}

// PeerProtocol is the mesh protocol UDP to peer id goes out in.
func (b *Bind) PeerProtocol(id string) int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.datagramVersion(id, 0)
}

// datagramVersion is the protocol of a datagram to peer (empty while the
// path's peer is not known) on a path last heard in version heard (0 for
// not yet): the peer's declared range decides; without it the version the
// path was heard in, which its sender evidently reads; else this node's
// oldest, which the release before reads too. Called with b.mu held.
func (b *Bind) datagramVersion(peer string, heard int) int {
	local := b.opt.Protocol
	if p := b.peers[peer]; peer != "" && p != nil && p.protocol.Max > 0 {
		return local.Send(p.protocol, true)
	}
	if local.Has(heard) {
		return heard
	}
	return local.Min
}

// SetPeerLarge tells the bind whether peer id says it takes large frames,
// as each of its control messages of wire.LargeFrames or later does. Its
// streams open now take them from then on, whatever it says later.
func (b *Bind) SetPeerLarge(id string, large bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	p := b.peers[id]
	if p == nil {
		return
	}
	changed := p.large != large
	p.large = large
	if large && b.run != nil {
		for s := range b.run.sessions {
			if s.ep.peer == id && s.parent == nil && s.version >= wire.LargeFrames && !s.peerLarge {
				s.peerLarge = true
				changed = true
			}
		}
	}
	if changed {
		b.largeChanged()
	}
}

// LargeLinks are the peers whose active path takes large frames now and
// that ask for them, when this node sends them.
func (b *Bind) LargeLinks() map[string]bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	out := map[string]bool{}
	if !b.opt.SendLarge {
		return out
	}
	for id, p := range b.peers {
		ep, err := b.path(p.active)
		if err != nil || !p.large || !b.info(ep, 0, time.Time{}).Connected {
			continue
		}
		if b.sendLimit(ep, ep.current) == largeLimit {
			out[id] = true
		}
	}
	return out
}

// LargeChanges is signalled when LargeLinks may have changed: a path
// connected, closed, was committed or deactivated, or a peer said
// otherwise.
func (b *Bind) LargeChanges() <-chan struct{} { return b.largeCh }

// largeChanged signals LargeChanges. Called with b.mu held.
func (b *Bind) largeChanged() {
	select {
	case b.largeCh <- struct{}{}:
	default:
	}
}
