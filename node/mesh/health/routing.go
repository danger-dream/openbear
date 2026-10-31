package health

import (
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"openbear/node/mesh/routing"
	"openbear/node/mesh/sign"
)

// msgAdvert carries a relay's link-state advertisement (package routing)
// to another relay. Only relays send it, only over a relay link, and only
// relays take it.
const msgAdvert = "route-advert"

// relayRouting feeds the router what this relay's links measure and
// carries its advertisements. See README.md, "Relay routing".
type relayRouting struct {
	router *routing.Router
	links  map[string]*routeLink
	errAt  map[string]time.Time // last routing.error event by peer
	dirty  bool                 // a next hop changed; routes are due
}

// routeLink is one relay link as routing samples it. The dialing end of a
// link, and both ends of a punched one, sample their own probes; the end
// that accepted a dialed link samples the probes it answers: from its pong
// to the prober's "seen" is one round trip, and a ping that does not come
// when due is a lost probe.
type routeLink struct {
	live     bool
	sent     map[uint64]time.Time // own probes not yet answered
	pongs    map[string]time.Time // answered probes not yet seen, by nonce
	lastPing time.Time
	srtt     time.Duration
}

// SetRouter makes this relay feed r and exchange its advertisements; nil
// stops routing and forgets every link sample.
func (m *Manager) SetRouter(r *routing.Router) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if r == nil {
		m.relay = relayRouting{}
		return
	}
	m.relay = relayRouting{router: r, links: map[string]*routeLink{}, errAt: map[string]time.Time{}}
}

func (m *Manager) routingOn() bool { return m.relay.router != nil && m.cfg.Role == "relay" }

// relayLink returns the configured link to peer if peer is a relay.
func (m *Manager) relayLink(peer string) (Link, bool) {
	if peer == m.cfg.Self || !m.owners.relays[peer] {
		return Link{}, false
	}
	for _, l := range m.cfg.Links {
		if l.Peer == peer {
			return l, true
		}
	}
	return Link{}, false
}

// prober is the path this relay probes peer through itself, if it does.
func (m *Manager) prober(l Link) *candidateState {
	if l.Punch {
		if d := m.directs[l.Peer]; d != nil {
			return d.link
		}
		return nil
	}
	if l.Dial {
		return m.links[l.Peer]
	}
	return nil
}

// routeLive says whether the link carries traffic: the path committed on
// the dialing end, punched and up on both, or prepared by the dialing end.
func (m *Manager) routeLive(l Link) bool {
	switch {
	case l.Punch:
		return m.directUp(l.Peer)
	case l.Dial:
		p := m.links[l.Peer]
		return p != nil && p.path.ID != "" && p.routeReady
	}
	for _, in := range m.inbound {
		if in.peer == l.Peer && in.prepared {
			return true
		}
	}
	return false
}

func (m *Manager) routeLink(peer string) *routeLink {
	rl := m.relay.links[peer]
	if rl == nil {
		rl = &routeLink{sent: map[uint64]time.Time{}, pongs: map[string]time.Time{}}
		m.relay.links[peer] = rl
	}
	return rl
}

// routeDeadline is how long routing waits for a probe's answer before it
// counts as lost: one probe interval, more on a slow link. The health
// tracker's own timeout is at least minTimeout, too slow to find a link
// down within about 3 s.
func (m *Manager) routeDeadline(srtt time.Duration) time.Duration {
	return max(m.cfg.Parameters.ActiveInterval, min(m.cfg.Parameters.MaxTimeout, 3*srtt))
}

// routeSent: probe id went out on p.
func (m *Manager) routeSent(p *candidateState, id uint64, now time.Time) {
	if !m.routingOn() {
		return
	}
	l, ok := m.relayLink(p.config.Peer)
	if !ok || m.prober(l) != p {
		return
	}
	if rl := m.routeLink(l.Peer); rl.live && len(rl.sent) < 16 {
		rl.sent[id] = now
	}
}

// routeReply: probe id on p came back after rtt.
func (m *Manager) routeReply(p *candidateState, id uint64, rtt time.Duration) {
	if !m.routingOn() {
		return
	}
	rl := m.relay.links[p.config.Peer]
	if rl == nil {
		return
	}
	if _, ok := rl.sent[id]; !ok {
		return // counted lost already, or not a routed link
	}
	delete(rl.sent, id)
	m.relay.dirty = m.relay.router.Probe(p.config.Peer, rtt) || m.relay.dirty
}

// routePinged: this relay answered a probe from peer.
func (m *Manager) routePinged(peer, nonce string, now time.Time) {
	if !m.routingOn() {
		return
	}
	l, ok := m.relayLink(peer)
	if !ok || l.Dial || l.Punch {
		return
	}
	rl := m.routeLink(peer)
	rl.lastPing = now
	if rl.live && len(rl.pongs) < 16 {
		rl.pongs[nonce] = now
	}
}

// routeSeen: the prober saw this relay's answer.
func (m *Manager) routeSeen(peer, nonce string, now time.Time) {
	if !m.routingOn() {
		return
	}
	rl := m.relay.links[peer]
	if rl == nil {
		return
	}
	at, ok := rl.pongs[nonce]
	if !ok {
		return
	}
	delete(rl.pongs, nonce)
	rtt := now.Sub(at)
	if rl.srtt == 0 {
		rl.srtt = rtt
	} else {
		rl.srtt = (7*rl.srtt + rtt) / 8
	}
	m.relay.dirty = m.relay.router.Probe(peer, rtt) || m.relay.dirty
}

// routingTick counts lost probes, follows links coming and going, and
// sends what advertisements are due.
func (m *Manager) routingTick(now time.Time) {
	if !m.routingOn() {
		return
	}
	r := m.relay.router
	interval := m.cfg.Parameters.ActiveInterval
	var live []string
	configured := map[string]bool{}
	for _, l := range m.cfg.Links {
		if _, ok := m.relayLink(l.Peer); !ok {
			continue
		}
		configured[l.Peer] = true
		rl := m.routeLink(l.Peer)
		up := m.routeLive(l)
		if up != rl.live {
			rl.live = up
			clear(rl.sent)
			clear(rl.pongs)
			rl.lastPing = now
			if !up {
				m.relay.dirty = r.Gone(l.Peer) || m.relay.dirty
			}
		}
		if !up {
			continue
		}
		live = append(live, l.Peer)
		srtt := rl.srtt
		if p := m.prober(l); p != nil {
			srtt = p.track.stats.SRTT
		}
		deadline := m.routeDeadline(srtt)
		for id, at := range rl.sent {
			if now.Sub(at) > deadline {
				delete(rl.sent, id)
				m.relay.dirty = r.Lost(l.Peer) || m.relay.dirty
			}
		}
		for n, at := range rl.pongs {
			if now.Sub(at) > deadline {
				delete(rl.pongs, n)
				m.relay.dirty = r.Lost(l.Peer) || m.relay.dirty
			}
		}
		if !l.Dial && !l.Punch {
			// A probe not even received: due every interval.
			for now.Sub(rl.lastPing) > interval+interval/2 {
				rl.lastPing = rl.lastPing.Add(interval)
				m.relay.dirty = r.Lost(l.Peer) || m.relay.dirty
			}
		}
	}
	for peer := range m.relay.links {
		if !configured[peer] {
			delete(m.relay.links, peer)
		}
	}
	m.relay.dirty = r.Tick() || m.relay.dirty
	budget := m.advertBudget()
	for _, msg := range r.Sync(budget) {
		m.sendAdvert(msg)
	}
	for _, msg := range r.Advert(budget, live) {
		m.sendAdvert(msg)
	}
	if m.relay.dirty {
		m.relay.dirty = false
		m.routes()
	}
}

// sendAdvert sends an advertisement in its format, JSON in "advert" or
// the compact form in "ac", with its origin's signature in "as".
func (m *Manager) sendAdvert(msg routing.Message) {
	path, ok := m.peerPath(msg.Peer)
	if !ok {
		return
	}
	v := message{Type: msgAdvert, AdvertSig: msg.Sig}
	if msg.Format == routing.FormatCompact {
		v.AdvertCompact = string(msg.Raw)
	} else {
		v.Advert = msg.Raw
	}
	m.send(msg.Peer, path, v, 0)
}

// advertBudget is the largest advertisement, in each format, a control
// message between any two relays holds: relays forward advertisements as
// their origin signed them, so the longest relay ids are both ends, with
// room for the revision to grow to 19 digits.
func (m *Manager) advertBudget() routing.Budget {
	longest := m.cfg.Self
	for _, n := range m.cfg.Nodes {
		if n.Role == "relay" && len(n.ID) > len(longest) {
			longest = n.ID
		}
	}
	for _, l := range m.cfg.Links {
		if len(l.Peer) > len(longest) {
			longest = l.Peer
		}
	}
	envelope := message{Version: m.cfg.Protocol.Or().Max, Network: m.cfg.Network, Revision: 1e18, From: longest, To: longest, Type: msgAdvert,
		AdvertSig: make([]byte, sign.Size)}
	plain, compact := envelope, envelope
	plain.Advert = json.RawMessage(`{}`)
	compact.AdvertCompact = "x"
	rawPlain, _ := json.Marshal(plain)
	rawCompact, _ := json.Marshal(compact)
	return routing.Budget{JSON: maxMessage - (len(rawPlain) - 2), Compact: maxMessage - (len(rawCompact) - 1)}
}

// receiveAdvert takes an advertisement from a linked relay and forwards a
// new one to the other linked relays.
func (m *Manager) receiveAdvert(r Received, v message, now time.Time) {
	format, raw := routing.FormatJSON, []byte(v.Advert)
	if v.AdvertCompact != "" {
		format, raw = routing.FormatCompact, []byte(v.AdvertCompact)
	}
	msg := routing.Message{Format: format, Raw: raw, Sig: v.AdvertSig}
	if !m.routingOn() || len(raw) == 0 {
		return
	}
	if _, ok := m.relayLink(r.Peer); !ok {
		return
	}
	forward, changed, err := m.relay.router.Receive(r.Peer, msg, m.advertBudget())
	if errors.Is(err, routing.ErrSignature) {
		m.verified(false, "routing advertisement", "its origin", r.Peer)
		return
	}
	if err != nil {
		// One event a minute per neighbor: a misbehaving relay must not
		// flood the event list.
		if now.Sub(m.relay.errAt[r.Peer]) >= time.Minute {
			m.relay.errAt[r.Peer] = now
			m.emit("routing.error", fmt.Sprintf("advertisement from %s: %v", r.Peer, err))
		}
		return
	}
	for _, msg := range forward {
		m.sendAdvert(msg)
	}
	m.relay.dirty = changed || m.relay.dirty
}
