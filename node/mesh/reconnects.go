package mesh

import (
	"strconv"
	"time"
)

// streamConn is a TCP or TLS connection that is up.
type streamConn struct {
	peer      string // the transport's peer ID, when it knows it
	endpoint  string // as the device has it
	transport string
	id        string // differs for every connection
	outbound  bool
}

func (b *streamBind) streamConns() []streamConn {
	outbound := map[string]bool{}
	for _, s := range b.Stats() {
		if s.Outbound {
			outbound[s.Endpoint] = true
		}
	}
	var out []streamConn
	for _, p := range b.Paths() {
		if p.Transport == "udp" || !p.Connected {
			continue
		}
		out = append(out, streamConn{peer: p.PeerID, endpoint: p.Endpoint, transport: p.Transport,
			id: p.Ref.ID + "/" + strconv.FormatUint(p.Ref.Generation, 10), outbound: outbound[p.Endpoint]})
	}
	return out
}

// connTrack counts, per peer, the TCP and TLS connections that came up
// after the first ones. A dialed endpoint has one connection at a time; a
// peer dialing in can have several, so connections beyond the most seen at
// once are the new ones.
type connTrack struct {
	slots map[string]*connSlot
	count map[string]int // by peer ID
}

type connSlot struct {
	peer string
	live map[string]bool
	peak int
}

// update takes the connections up now; peerOf names their peer, or "".
func (t *connTrack) update(conns []streamConn, peerOf func(streamConn) string, known map[string]bool) {
	if t.slots == nil {
		t.slots, t.count = map[string]*connSlot{}, map[string]int{}
	}
	now := map[string]map[string]bool{}
	for _, c := range conns {
		peer := peerOf(c)
		if peer == "" {
			continue
		}
		key := peer + " in " + c.transport
		if c.outbound {
			key = peer + " out " + c.endpoint
		}
		if t.slots[key] == nil {
			t.slots[key] = &connSlot{peer: peer}
		}
		if now[key] == nil {
			now[key] = map[string]bool{}
		}
		now[key][c.id] = true
	}
	for key, s := range t.slots {
		if !known[s.peer] {
			delete(t.slots, key)
			continue
		}
		ids := now[key]
		fresh := 0
		for id := range ids {
			if !s.live[id] {
				fresh++
			}
		}
		t.count[s.peer] += fresh - max(0, len(ids)-s.peak)
		s.peak = max(s.peak, len(ids))
		s.live = ids
	}
	for peer := range t.count {
		if !known[peer] {
			delete(t.count, peer)
		}
	}
}

// watchConns follows the transport's connections for peers[].reconnects.
// A connection that comes and goes between two looks is not counted.
func (e *engine) watchConns() {
	tick := time.NewTicker(250 * time.Millisecond)
	defer tick.Stop()
	for {
		select {
		case <-e.done:
			return
		case <-tick.C:
		}
		e.trackConns()
	}
}

func (e *engine) trackConns() {
	conns := e.bind.streamConns()
	_, state := deviceState(e.dev)
	e.mu.Lock()
	defer e.mu.Unlock()
	known := map[string]bool{}
	byWire := map[string]string{}
	byLearned := map[string]string{}
	for _, p := range e.peers {
		known[p.ID] = true
		if p.Endpoint != "" {
			byWire[e.wire[p.Key+"@"+p.Endpoint]] = p.ID
		} else if st := state[p.Key]; st != nil && st.endpoint != "" {
			byLearned[st.endpoint] = p.ID
		}
	}
	e.conns.update(conns, func(c streamConn) string {
		switch {
		case c.peer != "":
			return c.peer
		case c.outbound:
			return byWire[c.endpoint]
		}
		return byLearned[c.endpoint]
	}, known)
}
