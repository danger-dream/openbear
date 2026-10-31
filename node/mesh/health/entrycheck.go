package health

import (
	"fmt"
	"net/netip"
	"slices"
	"strings"
	"time"
)

// Two checks of what a relay could lie about without its probes showing it.
//
// A reflector, or whoever sees its 8-byte request id on the way, may report
// a public address this member does not have. Each entry candidate puts in
// its pongs the address it saw the ping come from on the authenticated
// control path; with two or more of those, a reflected address whose IP
// none of them saw is not offered for punching.
//
// An entry relay may drop the business traffic it forwards and still answer
// probes. The data plane sends end-to-end probes through the entry to
// another relay (package mesh, entryprobe.go) and reports when they keep
// failing; in failover mode, with that relay a candidate answering its own
// probes, this node then moves off the entry and keeps away from it for
// suspectHold. In fixed mode it is only reported.

const (
	// observedFor: how long a candidate's observation counts.
	observedFor   = 3 * reflectEvery
	mismatchEvery = 10 * time.Minute
	suspectHold   = 10 * time.Minute
)

type observation struct {
	addr netip.AddrPort
	at   time.Time
}

// observedBy keeps what entry candidate peer saw this node's ping come from.
func (m *Manager) observedBy(peer, seen string, now time.Time) {
	a, err := netip.ParseAddrPort(seen)
	if err != nil || !a.Addr().Is4() {
		return
	}
	if m.observed == nil {
		m.observed = map[string]observation{}
	}
	m.observed[peer] = observation{a, now}
}

// observedIPs are the IPs fresh observations saw with the candidates that
// saw each, and how many observations there are.
func (m *Manager) observedIPs(now time.Time) (map[netip.Addr][]string, int) {
	ips := map[netip.Addr][]string{}
	n := 0
	for peer, o := range m.observed {
		p := m.candidateOf(peer)
		if now.Sub(o.at) > observedFor || p == nil {
			continue
		}
		// A relay on this machine's own network sees its LAN address, which
		// says nothing of how the internet sees it.
		if e := parseEndpointAddr(p.config.Endpoint); e.IsValid() && m.onLink(e.Addr()) {
			continue
		}
		ips[o.addr.Addr()] = append(ips[o.addr.Addr()], peer)
		n++
	}
	return ips, n
}

func (m *Manager) candidateOf(peer string) *candidateState {
	for _, p := range m.candidates {
		if p.config.Peer == peer {
			return p
		}
	}
	return nil
}

// confirmed filters reflected addresses: with two or more observations,
// only those whose IP one of them saw are kept.
func (m *Manager) confirmed(addrs []netip.AddrPort, now time.Time) []netip.AddrPort {
	ips, n := m.observedIPs(now)
	if n < 2 {
		return addrs
	}
	var out, dropped []netip.AddrPort
	for _, a := range addrs {
		if ips[a.Addr()] != nil {
			out = append(out, a)
		} else {
			dropped = append(dropped, a)
		}
	}
	if (len(dropped) > 0 || len(ips) > 1) && now.Sub(m.mismatchAt) >= mismatchEvery {
		m.mismatchAt = now
		var seen []string
		for ip, peers := range ips {
			slices.Sort(peers)
			seen = append(seen, fmt.Sprintf("%s by %s", ip, strings.Join(peers, ",")))
		}
		slices.Sort(seen)
		m.emit("reach.mismatch", fmt.Sprintf("reflected %v not offered; relays saw %s", dropped, strings.Join(seen, "; ")))
	}
	return out
}

type entrySuspect struct {
	peer, target, reason string
}

// EntrySuspect reports that end-to-end probes through entry relay peer to
// relay target kept failing.
func (m *Manager) EntrySuspect(peer, target, reason string) bool {
	return m.enqueue(entrySuspect{peer, target, reason})
}

func (m *Manager) entrySuspect(v entrySuspect, now time.Time) {
	cur := m.candidates[m.active]
	// With its own probes failing the entry is dead to choose already.
	if cur == nil || cur.config.Peer != v.peer || !cur.ready || cur.track.stats.Failures != 0 {
		return
	}
	// Probes to a target this node does not see answer for itself prove
	// nothing about the entry.
	t := m.candidateOf(v.target)
	verified := t != nil && t != cur && t.ready && t.routeReady && t.track.stats.Failures == 0
	action := "reported only (fixed mode)"
	switch {
	case m.cfg.Mode != "failover":
	case !verified:
		action = "reported only: " + v.target + " is not a ready candidate to tell the entry from it"
	default:
		action = "moving off it"
		cur.suspect = now.Add(suspectHold)
	}
	m.emitLimited("entry.suspect", fmt.Sprintf("%s answers probes but %s; %s", v.peer, v.reason, action))
	m.changed()
}
