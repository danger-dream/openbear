package transport

import "openbear/node/mesh/wire"

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
