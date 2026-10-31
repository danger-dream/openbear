package health

import (
	"context"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"slices"
	"strings"
	"time"

	"openbear/node/mesh/transport"
)

// Reachability is how this node's UDP socket looks from outside. Mapping
// comes from what the reflectors and STUN servers saw; filtering from a
// test: the relay this node is attached to sends to the address its
// reflector saw, once from another port of its own and, through its relay
// links, from other relays. Filtering is measured first in a round, before
// this node sends to the other relays, and a relay that heard from the
// address recently does not send: either would let the NAT pass the test
// packet for the wrong reason.

const (
	reachEvery   = 10 * time.Minute
	reachAgain   = 2 * time.Minute // the least time between rounds a failure starts
	reachFirst   = 20 * time.Second
	reachLocal   = 15 * time.Second
	reachWait    = 2 * time.Second
	reachAckWait = 6 * time.Second
	reachPending = 5 * time.Second
	reachRecent  = 5 * time.Minute
	reachTarget  = 10 * time.Second // one test per target address this often
	maxReachWait = 32
	maxReachTo   = 1024
	maxReachFrom = 2 // other relays asked per test
)

type reachState struct {
	status    ReachStatus
	checked   bool
	filtering string
	observers []Observer
	noSocket  bool
	held      reachHeld
	// This machine's networks, for onLink, and when they were read.
	nets     []netip.Prefix
	netsAt   time.Time
	next     time.Time
	last     time.Time
	running  bool
	id       [8]byte
	relay    string
	ack      chan Reach
	localKey string
	localAt  time.Time
	// No relay to test through is a reflector: only a reflector saw the
	// address to send the tests to.
	untested bool

	// on a relay
	pending map[[8]byte]reachRequest
	rates   map[string]offerRate
	targets map[netip.Addr]time.Time
}

// reachHeld is what rounds measured while this node's public address and
// interface addresses stayed the same. A round that measures less does not
// throw it away: no test, or no second relay sending, says nothing new.
type reachHeld struct {
	addr      netip.AddrPort
	local     string
	filtering string
	mapping   string
}

type reachRequest struct {
	peer  string
	path  Path
	until time.Time
}

type reachResult struct {
	id        [8]byte
	observers []Observer
	resolved  []netip.Addr // by observer
	filtering string
	noSocket  bool
	local     []netip.Addr
}

func newReach() reachState {
	return reachState{status: ReachStatus{Verdict: "unknown", Mapping: "unknown", Filtering: "unknown", Consistent: true},
		pending: map[[8]byte]reachRequest{}, rates: map[string]offerRate{}, targets: map[netip.Addr]time.Time{}}
}

// reachVerdict judges a round. A consistent public address and open
// filtering is public; varying ports are symmetric; a stable mapping that
// filters is punchable. Anything else is unknown, unless a link punched
// through a public address proved it punchable.
func reachVerdict(observers []Observer, filtering string, noSocket, punched bool) ReachStatus {
	return reachJudge(observers, filtering, "", noSocket, punched)
}

// reachJudge is reachVerdict with the mapping an earlier round measured,
// for a round that had fewer than two observers answer.
func reachJudge(observers []Observer, filtering, heldMapping string, noSocket, punched bool) ReachStatus {
	s := ReachStatus{Mapping: "unknown", Filtering: filtering, Consistent: true, Observers: observers}
	var ok []Observer
	for _, o := range observers {
		if o.OK {
			ok = append(ok, o)
		}
	}
	switch {
	case len(observers) == 0 || noSocket:
		s.Verdict = "unknown"
		return s
	case len(ok) == 0:
		s.Verdict = "no_udp"
		return s
	}
	s.PublicAddr = ok[0].Addr
	for _, o := range ok[1:] {
		if o.Addr.Addr() != s.PublicAddr.Addr() {
			s.Consistent = false
		}
	}
	if len(ok) >= 2 {
		s.Mapping = "stable"
		for _, o := range ok[1:] {
			if o.Addr.Port() != s.PublicAddr.Port() {
				s.Mapping = "varies"
			}
		}
	} else if heldMapping != "" {
		s.Mapping = heldMapping
	}
	switch {
	case !s.Consistent:
		s.Verdict = "unknown"
	case filtering == "open":
		s.Verdict = "public"
	case s.Mapping == "varies":
		s.Verdict = "symmetric"
	case s.Mapping == "stable" && (filtering == "address" || filtering == "port"):
		s.Verdict = "punchable"
	default:
		s.Verdict = "unknown"
	}
	if s.Verdict == "unknown" && punched {
		s.Verdict = "punchable"
	}
	return s
}

// reachRelay is the relay to test through, one that is also a reflector
// if it can: for a member its entry or another candidate that is up, for a
// relay a linked relay.
func (m *Manager) reachRelay() (string, Path, bool) {
	var peers []string
	if m.cfg.Role == "relay" {
		for _, l := range m.cfg.Links {
			if m.owners.relays[l.Peer] {
				peers = append(peers, l.Peer)
			}
		}
	} else {
		if p := m.candidates[m.active]; p != nil {
			peers = append(peers, p.config.Peer)
		}
		for _, c := range m.cfg.Candidates {
			if p := m.candidates[c.ID]; p != nil && p.ready {
				peers = append(peers, c.Peer)
			}
		}
	}
	first, firstPath, found := "", Path{}, false
	for _, peer := range peers {
		path, ok := m.peerPath(peer)
		if p := m.candidates[m.active]; m.cfg.Role != "relay" && p != nil && p.config.Peer == peer && p.path.ID != "" {
			path, ok = p.path, true
		}
		if !ok {
			continue
		}
		if slices.ContainsFunc(m.cfg.Reflectors, func(s string) bool { return m.reflectorOf(s, netip.Addr{}) == peer }) {
			return peer, path, true
		}
		if !found {
			first, firstPath, found = peer, path, true
		}
	}
	return first, firstPath, found
}

func (m *Manager) reachTick(now time.Time) {
	m.reachServe(now)
	r := &m.reach
	if now.Sub(r.localAt) >= reachLocal {
		r.localAt = now
		key := fmt.Sprint(localAddrs(m.cfg.Prefix))
		if r.localKey != "" && key != r.localKey {
			r.next = now
		}
		r.localKey = key
	}
	if r.running || now.Before(r.next) || len(m.cfg.Reflectors)+len(m.cfg.STUN) == 0 {
		return
	}
	relay, path, ok := m.reachRelay()
	if !ok && r.last.IsZero() && now.Sub(m.started) < reachFirst {
		return
	}
	r.running, r.last = true, now
	r.next = now.Add(jitter(reachEvery, .1))
	r.id, r.relay, r.ack = randomID(), "", make(chan Reach, 2)
	reflects := ok && slices.ContainsFunc(m.cfg.Reflectors, func(s string) bool { return m.reflectorOf(s, netip.Addr{}) == relay })
	r.untested = !reflects
	testing := reflects && m.backend.WatchTests(r.id)
	if testing {
		r.relay = relay
		m.send(relay, path, message{Type: "reach", Reach: &Reach{ID: hex.EncodeToString(r.id[:])}}, 0)
	}
	// The relay that tests sees this round's request first.
	reflectors := slices.Clone(m.cfg.Reflectors)
	first := 0
	if testing {
		for i, s := range reflectors {
			if m.reflectorOf(s, netip.Addr{}) == relay {
				reflectors[first], reflectors[i] = reflectors[i], reflectors[first]
				first++
			}
		}
		if first == 0 {
			first = len(reflectors)
		}
	}
	go m.reachRound(r.id, testing, reflectors, first, slices.Clone(m.cfg.STUN), r.ack, m.cfg.Prefix)
}

// reachRound asks the observers from outside the manager's lock. When a
// relay tests, the first reflectors are that relay's: they are asked with
// the round's ID, again until the relay says it saw this node, and only
// then are the others.
func (m *Manager) reachRound(id [8]byte, testing bool, reflectors []string, first int, stun []string, ack <-chan Reach, prefix netip.Prefix) {
	res := reachResult{id: id, filtering: "unknown", local: localAddrs(prefix)}
	closed := 0
	type answer struct {
		o      Observer
		ip     netip.Addr
		closed bool
	}
	ask := func(server, kind string, rid [8]byte) answer {
		a := answer{o: Observer{ID: server, Kind: kind}}
		addr, err := net.ResolveUDPAddr("udp4", server)
		if err == nil {
			a.ip = addr.AddrPort().Addr().Unmap()
			ctx, cancel := context.WithTimeout(m.ctx, time.Second)
			if kind == "stun" {
				a.o.Addr, err = m.backend.STUN(ctx, addr.AddrPort())
			} else {
				a.o.Addr, err = m.backend.Reflect(ctx, addr.AddrPort(), rid)
			}
			cancel()
		}
		a.o.OK = err == nil
		a.closed = errors.Is(err, net.ErrClosed)
		return a
	}
	add := func(a answer) {
		res.observers = append(res.observers, a.o)
		res.resolved = append(res.resolved, a.ip)
		if a.closed {
			closed++
		}
	}
	sleep := func(d time.Duration) bool {
		select {
		case <-time.After(d):
			return true
		case <-m.ctx.Done():
			return false
		}
	}
	next := 0
	if testing {
		var got *Reach
		wait := func(d time.Duration) {
			select {
			case a := <-ack:
				got = &a
			case <-time.After(d):
			case <-m.ctx.Done():
			}
		}
		var answers []answer
		for try := 0; try < 3 && got == nil; try++ {
			answers = answers[:0]
			for i := 0; i < first; i++ {
				answers = append(answers, ask(reflectors[i], "relay", id))
			}
			wait(time.Second)
		}
		for _, a := range answers {
			add(a)
		}
		next = first
		if got == nil {
			wait(reachAckWait)
		}
		if got != nil && got.Seen && sleep(reachWait) {
			seen := m.backend.TestsSeen(id)
			switch {
			case seen&(1<<transport.TestOther) != 0:
				res.filtering = "open"
			case seen&(1<<transport.TestRelay) != 0:
				res.filtering = "address"
			default:
				res.filtering = "port"
			}
		}
		m.backend.StopTests(id)
	}
	for ; next < len(reflectors); next++ {
		add(ask(reflectors[next], "relay", randomID()))
	}
	for _, s := range stun {
		add(ask(s, "stun", [8]byte{}))
	}
	res.noSocket = closed > 0 && closed == len(res.observers)
	select {
	case m.input <- res:
	case <-m.ctx.Done():
	}
}

func (m *Manager) reachDone(v reachResult, now time.Time) {
	r := &m.reach
	if v.id != r.id {
		return
	}
	r.running = false
	for i := range v.observers {
		if v.observers[i].Kind == "relay" {
			var ip netip.Addr
			if i < len(v.resolved) {
				ip = v.resolved[i]
			}
			if id := m.reflectorOf(v.observers[i].ID, ip); id != "" {
				v.observers[i].ID = id
			}
		}
	}
	// Without NAT a test from another port of the relay's passes only a
	// host firewall that takes the listener's port from anywhere; another
	// relay may not have been free to test.
	if v.filtering == "address" {
		for _, o := range v.observers {
			if o.OK && slices.Contains(v.local, o.Addr.Addr()) && (m.cfg.Port == 0 || int(o.Addr.Port()) == m.cfg.Port) {
				v.filtering = "open"
			}
		}
	}
	// A relay linked to no reflector, site relays only, cannot be tested.
	// Without NAT its own firewall takes the listener's port, the mesh's
	// rule does, so only a filter upstream is left untold.
	if v.filtering == "unknown" && r.untested && m.cfg.Role == "relay" && withoutNAT(v.observers, v.local, m.cfg.Port) {
		v.filtering = "open"
	}
	r.checked = true
	r.status.CheckedAt = now
	if v.filtering == "unknown" && !r.untested {
		// The relay did not answer: soon again rather than in ten minutes.
		r.next = maxTime(now, minTime(r.next, r.last.Add(reachAgain)))
	}
	v.filtering = r.hold(v)
	r.observers, r.filtering, r.noSocket = v.observers, v.filtering, v.noSocket
	for _, o := range v.observers {
		if o.OK && o.Kind == "relay" {
			if m.reflected != o.Addr || !m.discovered {
				m.reflected, m.discovered = o.Addr, true
				m.updateLocal()
			}
			break
		}
	}
	m.reachUpdate(now)
}

// withoutNAT: two observers or more answered, each with an address of this
// machine's own and the listener's port.
func withoutNAT(observers []Observer, local []netip.Addr, port int) bool {
	n := 0
	for _, o := range observers {
		if !o.OK {
			continue
		}
		if !slices.Contains(local, o.Addr.Addr().Unmap()) || (port != 0 && int(o.Addr.Port()) != port) {
			return false
		}
		n++
	}
	return n >= 2
}

// reachUpdate judges the last round again, as a link punched through a
// public address counts too.
func (m *Manager) reachUpdate(now time.Time) {
	r := &m.reach
	if !r.checked {
		return
	}
	mapping := ""
	if r.held.addr.IsValid() {
		mapping = r.held.mapping
	}
	s := reachJudge(r.observers, r.filtering, mapping, r.noSocket, m.punchedVia("public"))
	s.CheckedAt = r.status.CheckedAt
	if s.Verdict != r.status.Verdict {
		m.emit("reach.changed", r.status.Verdict+" -> "+s.Verdict)
	}
	r.status = s
}

// hold keeps what earlier rounds measured under the same addresses and
// returns the filtering to judge by. Open filtering is what a test from
// another relay proved; a round in which only the asked relay's test came
// through may only have had no other relay to send one, or one that skipped
// the address or could not send, and does not take it back. A port result,
// the asked relay's own test filtered too, does.
func (r *reachState) hold(v reachResult) string {
	var pub netip.AddrPort
	ok := 0
	for _, o := range v.observers {
		if o.OK {
			if !pub.IsValid() {
				pub = o.Addr
			}
			ok++
		}
	}
	local := fmt.Sprint(v.local)
	if !pub.IsValid() {
		return v.filtering
	}
	if r.held.addr != pub || r.held.local != local {
		r.held = reachHeld{addr: pub, local: local}
	}
	filtering := v.filtering
	switch {
	case filtering == "unknown":
		filtering = r.held.filtering
		if filtering == "" {
			filtering = "unknown"
		}
	case filtering == "address" && r.held.filtering == "open":
		filtering = "open"
	default:
		r.held.filtering = filtering
	}
	if ok >= 2 {
		r.held.mapping = "stable"
		for _, o := range v.observers {
			if o.OK && o.Addr.Port() != pub.Port() {
				r.held.mapping = "varies"
			}
		}
	}
	return filtering
}

func (m *Manager) reachAfterFailure(now time.Time) {
	r := &m.reach
	if again := r.last.Add(reachAgain); again.Before(r.next) {
		r.next = maxTime(now, again)
	}
}

// reflectorOf is the relay a reflector belongs to: the one with an
// endpoint on the same host name, or on ip if the reflector resolved to it.
func (m *Manager) reflectorOf(server string, ip netip.Addr) string {
	host, _, err := net.SplitHostPort(server)
	if err != nil {
		return ""
	}
	match := func(endpoint string) bool {
		_, rest, ok := strings.Cut(endpoint, "://")
		h, _, err := net.SplitHostPort(rest)
		if !ok || err != nil {
			return false
		}
		a, err := netip.ParseAddr(h)
		return h == host || (ip.IsValid() && err == nil && a.Unmap() == ip)
	}
	for _, c := range m.cfg.Candidates {
		if match(c.Endpoint) {
			return c.Peer
		}
	}
	for _, l := range m.cfg.Links {
		if match(l.Endpoint) {
			return l.Peer
		}
	}
	// A relay that dials this one, so that there is no endpoint for it here,
	// sends from its listener: its reflector is where its UDP path comes
	// from.
	_, port, _ := net.SplitHostPort(server)
	addr, err := netip.ParseAddrPort(server)
	if err != nil && ip.IsValid() {
		addr, err = netip.ParseAddrPort(net.JoinHostPort(ip.String(), port))
	}
	if err != nil {
		return ""
	}
	addr = netip.AddrPortFrom(addr.Addr().Unmap(), addr.Port())
	for _, l := range m.cfg.Links {
		if !m.owners.relays[l.Peer] {
			continue
		}
		for _, p := range m.backend.Punched(l.Peer) {
			if netip.AddrPortFrom(p.Remote.Addr().Unmap(), p.Remote.Port()) == addr {
				return l.Peer
			}
		}
	}
	return ""
}

func boolOrder(b bool) int {
	if b {
		return 1
	}
	return 0
}

func minTime(a, b time.Time) time.Time {
	if a.Before(b) {
		return a
	}
	return b
}

func takeToken(rates map[string]offerRate, key string, burst, perSecond float64, now time.Time) bool {
	r, ok := rates[key]
	if !ok {
		if len(rates) >= maxReachTo {
			return false
		}
		r = offerRate{tokens: burst, at: now}
	}
	r.tokens = min(burst, r.tokens+now.Sub(r.at).Seconds()*perSecond)
	r.at = now
	ok = r.tokens >= 1
	if ok {
		r.tokens--
	}
	rates[key] = r
	return ok
}

func reachID(s string) ([8]byte, bool) {
	var id [8]byte
	b, err := hex.DecodeString(s)
	if err != nil || len(b) != len(id) {
		return id, false
	}
	copy(id[:], b)
	return id, true
}

// receiveReach serves the three reach messages.
func (m *Manager) receiveReach(r Received, v message, now time.Time) {
	if v.Reach == nil {
		return
	}
	id, ok := reachID(v.Reach.ID)
	if !ok {
		return
	}
	switch v.Type {
	case "reach":
		// A node attached here or a linked relay asks about itself; the
		// address tested is the one this reflector saw, never one it names.
		if m.cfg.Role != "relay" || !m.authorizedPeer(r.Peer) || len(m.reach.pending) >= maxReachWait || !takeToken(m.reach.rates, r.Peer, 3, 1.0/20, now) {
			return
		}
		m.reach.pending[id] = reachRequest{r.Peer, r.Path, now.Add(reachPending)}
	case "reach-relay":
		target, err := netip.ParseAddrPort(v.Reach.Target)
		if m.cfg.Role != "relay" || err != nil || !m.owners.relays[r.Peer] || !m.linked(r.Peer) || !takeToken(m.reach.rates, "relay "+r.Peer, 10, 2, now) {
			return
		}
		ip := target.Addr().Unmap()
		// The target is what the asking relay's reflector saw a node send
		// from: an internet address, or a private one only when that relay
		// is itself on a private network that this one reaches it over.
		if !ip.IsGlobalUnicast() || target.Port() == 0 || (privateAddr(ip) && !m.privatePath(r.Path)) {
			return
		}
		if at, ok := m.reach.targets[ip]; ok && now.Sub(at) < reachTarget {
			return
		}
		if len(m.reach.targets) >= maxReachTo {
			for k, at := range m.reach.targets {
				if now.Sub(at) >= reachTarget {
					delete(m.reach.targets, k)
				}
			}
			if len(m.reach.targets) >= maxReachTo {
				return
			}
		}
		m.reach.targets[ip] = now
		if !m.backend.RecentlyFrom(ip, reachRecent) {
			m.backend.SendTest(target, id, transport.TestOther)
		}
	case "reach-ack":
		if m.reach.running && id == m.reach.id && r.Peer == m.reach.relay {
			select {
			case m.reach.ack <- *v.Reach:
			default:
			}
		}
	}
}

// privatePath says whether path's other end is at a private address.
func (m *Manager) privatePath(path Path) bool {
	a, ok := m.backend.Remote(path)
	return ok && privateAddr(a.Addr())
}

// reachServe runs the tests asked for once the reflector has seen the
// request, and tells the asker.
func (m *Manager) reachServe(now time.Time) {
	for id, q := range m.reach.pending {
		target, seen := m.backend.ReflectSeen(id)
		if !seen && now.Before(q.until) {
			continue
		}
		delete(m.reach.pending, id)
		reply := Reach{ID: hex.EncodeToString(id[:]), Seen: seen}
		if seen {
			m.backend.SendTest(target, id, transport.TestRelay)
			// Relays this one dials first: they are reachable from outside,
			// a site relay behind NAT may not be able to send at all.
			links := slices.Clone(m.cfg.Links)
			slices.SortStableFunc(links, func(a, b Link) int {
				return boolOrder(b.Endpoint != "") - boolOrder(a.Endpoint != "")
			})
			for _, l := range links {
				if reply.Others == maxReachFrom {
					break
				}
				if !m.owners.relays[l.Peer] || l.Peer == q.peer {
					continue
				}
				if path, ok := m.peerPath(l.Peer); ok {
					m.send(l.Peer, path, message{Type: "reach-relay", Reach: &Reach{ID: reply.ID, Target: target.String()}}, 0)
					reply.Others++
				}
			}
		}
		m.send(q.peer, q.path, message{Type: "reach-ack", Reach: &reply}, 0)
	}
}
