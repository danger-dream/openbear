package health

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/netip"
	"testing"
	"time"

	"openbear/node/mesh/routing"
)

// sentOf is the newest message b sent of type typ.
func sentOf(t *testing.T, b *offerBackend, typ string) message {
	t.Helper()
	for i := len(b.sent) - 1; i >= 0; i-- {
		var v message
		json.Unmarshal(b.sent[i].raw, &v)
		if v.Type == typ {
			return v
		}
	}
	t.Fatalf("no %s sent: %v", typ, b.types())
	return message{}
}

// A member takes a conflict only from the relay its commit went to, on
// that path, answering that commit with the commit's nonce, token and
// generation, and only with a newer claim the member signed itself: no
// relay moves a member's sequence to one it made up.
func TestConflictOnlyFromTheCommitsRelay(t *testing.T) {
	nodes := []Node{{ID: "m", Role: "member"}, {ID: "R2", Role: "relay"}}
	relay, rb := offerManager(t, Config{Network: "n", Self: "R", Role: "relay", Revision: 1, Nodes: nodes}, nil, map[string]bool{"R": true})
	relay.store.state.Authorized = map[string][]string{"m": {"R"}}
	signed := claimInput("n", "m", "R2", 40).SignString(signTestKey("m"))
	relay.store.state.Claims["m"] = claim{Owner: "R2", Seq: 40, MSig: signed}
	member, _ := offerManager(t, Config{Network: "n", Self: "m", Role: "member", Revision: 1, Nodes: []Node{{ID: "R", Role: "relay"}, {ID: "R2", Role: "relay"}},
		Candidates: []Candidate{{ID: "c1", Peer: "R"}, {ID: "c2", Peer: "R2"}}}, nil, nil)
	path := Path{ID: "p1", Generation: 1}
	p := member.candidates["c1"]
	p.path, p.token = path, nonce()
	member.candidates["c2"].path = Path{ID: "p2", Generation: 1}
	seq, err := member.store.nextSequence()
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	member.proposal = newProposal(p, seq, "initial", true, now)
	relay.inbound[path] = &inbound{peer: "m", path: path, token: p.token, generation: 1, challenges: map[string]time.Time{}, lastSeen: now, prepared: true}

	// A commit not signed by the member gets no answer.
	commit := message{Token: p.token, Generation: 1, Sequence: seq, Nonce: member.proposal.nonce}
	forged := commit
	forged.MSig = claimInput("n", "m", "R", seq).SignString(signTestKey("R2"))
	relay.receive(signal(t, "m", "R", "commit", forged))
	if len(rb.sent) != 0 || relay.signRejected != 1 {
		t.Fatalf("answered a commit the member did not sign: %v", rb.types())
	}
	// The relay's answer to the member's commit.
	commit.MSig = claimInput("n", "m", "R", seq).SignString(signTestKey("m"))
	relay.receive(signal(t, "m", "R", "commit", commit))
	answer := sentOf(t, rb, "conflict")
	if answer.Claim == nil || answer.Claim.Seq != 40 || answer.Claim.MSig != signed || answer.Nonce != member.proposal.nonce || answer.Token != p.token || answer.Generation != 1 {
		t.Fatalf("conflict %+v", answer)
	}

	unchanged := func(what string, r Received) {
		t.Helper()
		before := member.store.state.Sequence
		member.receive(r)
		if member.store.state.Sequence != before || member.proposal.sequence != seq {
			t.Fatalf("%s: sequence moved to %d", what, member.store.state.Sequence)
		}
	}
	conflict := func(from string, at Path, v message) Received {
		r := signal(t, from, "m", "conflict", v)
		r.Path = at
		return r
	}
	high := answer
	high.Claim = &SignedClaim{Owner: "R2", Seq: 1 << 40, MSig: claimInput("n", "m", "R2", 1<<40).SignString(signTestKey("m"))}
	unchanged("another relay", conflict("R2", path, high))
	unchanged("another relay on its own path", conflict("R2", Path{ID: "p2", Generation: 1}, high))
	unchanged("another path", conflict("R", Path{ID: "p9", Generation: 1}, high))
	unchanged("another generation of the path", conflict("R", Path{ID: "p1", Generation: 2}, high))
	stale := high
	stale.Nonce = nonce()
	unchanged("another commit's nonce", conflict("R", path, stale))
	stale = high
	stale.Token = nonce()
	unchanged("another token", conflict("R", path, stale))
	made := answer
	made.Claim = &SignedClaim{Owner: "R2", Seq: 1 << 40, MSig: signed}
	unchanged("a sequence the member did not sign", conflict("R", path, made))
	made.Claim = &SignedClaim{Owner: "R2", Seq: 1 << 40, MSig: claimInput("n", "m", "R2", 1<<40).SignString(signTestKey("R"))}
	unchanged("a claim the relay signed", conflict("R", path, made))
	unchanged("the sequence alone", conflict("R", path, message{Sequence: 1 << 40, Nonce: answer.Nonce, Token: answer.Token, Generation: 1}))
	older := answer
	older.Claim = &SignedClaim{Owner: "R2", Seq: seq - 1, MSig: claimInput("n", "m", "R2", seq-1).SignString(signTestKey("m"))}
	unchanged("an older sequence", conflict("R", path, older))

	member.receive(conflict("R", path, answer))
	if member.proposal.sequence != 41 || member.store.state.Sequence != 41 {
		t.Fatalf("the relay's own conflict: sequence %d", member.proposal.sequence)
	}
	member.proposal = nil
	last := member.store.state.Sequence
	member.receive(conflict("R", path, high))
	if member.store.state.Sequence != last {
		t.Fatal("a conflict with no proposal moved the sequence")
	}
}

// echoLab is a relay that S dialed, its routing sampling the probes it
// answers.
func echoLab(t *testing.T) (*Manager, *offerBackend) {
	t.Helper()
	m, b := offerManager(t, Config{Network: "n", Self: "R", Role: "relay", Revision: 1, Links: []Link{{Peer: "S"}}}, nil, map[string]bool{"R": true, "S": true})
	m.SetRouter(routing.New(routing.Config{Self: "R", Relays: []string{"S"}, Links: []string{"S"}}, time.Now, nil))
	m.relay.links["S"] = &routeLink{live: true, sent: map[uint64]time.Time{}, pongs: map[string]time.Time{}}
	return m, b
}

// echoProbe has S ping the relay at at over path, returning the ping and the
// relay's pong.
func echoProbe(t *testing.T, m *Manager, b *offerBackend, path Path, token string, id uint64, at time.Time) (message, message) {
	t.Helper()
	ping := message{Nonce: nonce(), Token: token, Generation: path.Generation, Probe: id}
	r := signal(t, "S", "R", "ping", ping)
	r.Path, r.At = path, at
	m.receive(r)
	return ping, sentOf(t, b, "pong")
}

func echoSeen(t *testing.T, m *Manager, ping message, echo string, path Path, at time.Time) {
	t.Helper()
	r := signal(t, "S", "R", "seen", message{Nonce: ping.Nonce, Token: ping.Token, Generation: ping.Generation, Probe: ping.Probe, Echo: echo})
	r.Path, r.At = path, at
	m.receive(r)
}

// The relay that answers probes measures from its pong to the "seen" that
// repeats the pong's echo; a prober that has repeated one cannot measure
// less by sending "seen" before the pong arrives, and one of the release
// before, which repeats none, is measured as before.
func TestSeenRepeatsThePongsEcho(t *testing.T) {
	m, b := echoLab(t)
	path := Path{ID: "p1", Generation: 1}
	token := nonce()
	at := time.Now()
	link := m.relay.links["S"]

	ping, pong := echoProbe(t, m, b, path, token, 1, at)
	if len(pong.Echo) != 32 || pong.Nonce != ping.Nonce {
		t.Fatalf("pong %+v", pong)
	}
	echoSeen(t, m, ping, "x"+pong.Echo[1:], path, at.Add(time.Millisecond))
	if link.srtt != 0 || m.inbound[path].challenges[ping.Nonce].IsZero() {
		t.Fatal("a seen with another echo was taken")
	}
	echoSeen(t, m, ping, pong.Echo, path, at.Add(40*time.Millisecond))
	if link.srtt != 40*time.Millisecond {
		t.Fatalf("srtt %v", link.srtt)
	}

	// The cheat: "seen" straight after the ping, without the echo.
	ping, _ = echoProbe(t, m, b, path, token, 2, at.Add(time.Second))
	echoSeen(t, m, ping, "", path, at.Add(time.Second+time.Millisecond))
	if link.srtt != 40*time.Millisecond {
		t.Fatalf("a seen without the echo measured: srtt %v", link.srtt)
	}
	if _, ok := link.pongs[ping.Nonce]; !ok {
		t.Fatal("the probe should be left to count as lost")
	}

	// Every prober repeats it, from the first probe on a new path.
	link.srtt = 0
	fresh := Path{ID: "p3", Generation: 1}
	ping, _ = echoProbe(t, m, b, fresh, nonce(), 4, at.Add(3*time.Second))
	echoSeen(t, m, ping, "", fresh, at.Add(3*time.Second+time.Millisecond))
	if link.srtt != 0 {
		t.Fatalf("a first probe measured without the echo: srtt %v", link.srtt)
	}
}

// The prober repeats what the pong asked; to a relay of the release
// before, whose pong carries nothing, its "seen" is as before.
func TestProberRepeatsTheEcho(t *testing.T) {
	m, b := offerManager(t, Config{Network: "n", Self: "m", Role: "member", Revision: 1, Candidates: []Candidate{{ID: "c1", Peer: "R"}}}, nil, nil)
	p := m.candidates["c1"]
	p.path, p.token = Path{ID: "p1", Generation: 1}, nonce()
	now := time.Now()
	for i, echo := range []string{"e1", ""} {
		id := uint64(i + 1)
		p.track.queue(id, now)
		p.track.written(id, now, now, nil)
		p.pending[id] = nonce()
		r := signal(t, "R", "m", "pong", message{Nonce: p.pending[id], Token: p.token, Generation: 1, Probe: id, Echo: echo})
		r.At = now.Add(20 * time.Millisecond)
		m.receive(r)
		if v := sentOf(t, b, "seen"); v.Probe != id || v.Echo != echo {
			t.Fatalf("seen %+v for echo %q", v, echo)
		}
	}
	if len(p.echoes) != 0 {
		t.Fatal("echoes kept after their probes", p.echoes)
	}
}

// One peer flooding the control plane queues behind its own messages: the
// others' are taken in turn, and with every queue full the longest loses.
func TestInboxFairByPeer(t *testing.T) {
	b := newInbox()
	for i := range inboxPerPeer {
		if !b.push(Received{Peer: "X", Payload: []byte(fmt.Sprint(i))}) {
			t.Fatal(i)
		}
	}
	if b.push(Received{Peer: "X"}) {
		t.Fatal("past the peer's bound")
	}
	b.push(Received{Peer: "A", Payload: []byte("a")})
	var order []string
	for range 3 {
		r, _ := b.pop()
		order = append(order, r.Peer)
	}
	if fmt.Sprint(order) != "[X A X]" {
		t.Fatalf("order %v", order)
	}
	for b.total > 0 {
		b.pop()
	}

	for _, p := range []string{"W", "X", "Y"} {
		for range inboxPerPeer {
			b.push(Received{Peer: p})
		}
	}
	for range inboxPerPeer - 1 {
		b.push(Received{Peer: "Z"})
	}
	b.push(Received{Peer: "Z", Payload: []byte("last")})
	if b.total != inboxTotal {
		t.Fatal(b.total)
	}
	if !b.push(Received{Peer: "A"}) || b.total != inboxTotal || len(b.queues["A"]) != 1 {
		t.Fatal("a quiet peer's message lost to the full inbox")
	}
	n := 0
	for {
		r, ok := b.pop()
		if !ok {
			break
		}
		n++
		if r.Peer == "A" && n > 5 {
			t.Fatalf("A's message came %dth", n)
		}
	}
	if n != inboxTotal || len(b.queues) != 0 || len(b.order) != 0 {
		t.Fatal(n, b.queues, b.order)
	}
}

// A flood from one peer does not keep the manager from another's message.
func TestManagerTakesEveryPeerInTurn(t *testing.T) {
	m, err := New(t.TempDir(), withKeys(Config{Network: "n", Self: "R", Role: "relay", Revision: 1, Parameters: Defaults()}), &offerBackend{}, Hooks{})
	if err != nil {
		t.Fatal(err)
	}
	defer m.Close()
	ping := func(peer string) Received {
		r := signal(t, peer, "R", "ping", message{Nonce: nonce(), Token: nonce(), Generation: 1, Probe: 1})
		r.Path.ID = "p-" + peer
		return r
	}
	m.mu.Lock() // the manager waits while the queue fills
	for range 4 * inboxPerPeer {
		m.Receive(ping("X"))
	}
	if !m.Receive(ping("A")) {
		t.Fatal("A's message refused while X flooded")
	}
	m.mu.Unlock()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		m.mu.RLock()
		in := m.inbound[Path{ID: "p-A", Generation: 1}]
		m.mu.RUnlock()
		if in != nil {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("A's message not taken")
}

// A relay writes the claims that notices bring once a second, however many
// come; a member's sequence is written before it is used, every time.
func TestRelayClaimsShareOneWrite(t *testing.T) {
	dir := t.TempDir()
	s, err := openStore(dir, "n", "R")
	if err != nil {
		t.Fatal(err)
	}
	save, writes := s.save, 0
	s.save = func(p persistent) error { writes++; return save(p) }
	o := newOwners(s, 30*time.Second)
	members := map[string][]string{}
	for i := range 50 {
		members[fmt.Sprint("m", i)] = []string{"A", "R"}
	}
	if err = configureOwners(o, 1, members, map[string]bool{"A": true, "R": true}); err != nil {
		t.Fatal(err)
	}
	m := &Manager{store: s}
	writes = 0
	now := time.Now()
	for i := range 50 {
		if _, err = o.accept("A", sn(Notice{Member: fmt.Sprint("m", i), Owner: "A", Seq: 1, Refresh: 1, Generation: 1, LeaseMs: 30000}), now); err != nil {
			t.Fatal(err)
		}
	}
	if writes != 0 || len(s.state.Claims) != 50 {
		t.Fatal("notices written one by one", writes)
	}
	m.flushState(now, false)
	if writes != 0 {
		t.Fatal("written within the second")
	}
	m.flushState(now.Add(2*stateFlush), false)
	m.flushState(now.Add(2*stateFlush), false)
	if writes != 1 {
		t.Fatal(writes)
	}
	s2, err := openStore(dir, "n", "R")
	if err != nil || len(s2.state.Claims) != 50 {
		t.Fatal("claims not on disk", err)
	}

	// A failed write is tried again.
	o.accept("A", sn(Notice{Member: "m1", Owner: "A", Seq: 2, Refresh: 1, Generation: 1, LeaseMs: 30000}), now)
	s.save = func(persistent) error { writes++; return errors.New("disk full") }
	m.flushState(now.Add(3*stateFlush), true)
	if !s.dirty || s.state.Claims["m1"].Seq != 2 {
		t.Fatal("staged claim lost to a failed write")
	}
	s.save = func(p persistent) error { writes++; return save(p) }
	m.flushState(now.Add(5*stateFlush), false)
	if s.dirty {
		t.Fatal("not written again")
	}

	before := writes
	for range 3 {
		if _, err = s.nextSequence(); err != nil {
			t.Fatal(err)
		}
	}
	if writes != before+3 {
		t.Fatal("sequence not written each time", writes-before)
	}
}

// A relay asked by a linked relay tests only an address that relay's
// reflector can have seen a node send from: an internet one, or a private
// one when the relay asking is itself on a private network here.
func TestReachRelayTestsOnlyWhatAReflectorSees(t *testing.T) {
	m, b := offerManager(t, Config{Network: "n", Self: "A", Role: "relay", Revision: 1, Links: []Link{{Peer: "B"}}}, nil, map[string]bool{"B": true})
	relay := func(target string) {
		clear(m.reach.rates) // the rate a relay may ask at is another test's
		r := Reach{ID: encodeID(randomID()), Target: target}
		m.receive(signal(t, "B", "A", "reach-relay", message{Reach: &r}))
	}
	b.remote = netip.MustParseAddrPort("198.51.100.2:51820")
	for _, target := range []string{"127.0.0.1:22", "[::1]:22", "0.0.0.0:1", "255.255.255.255:9", "224.0.0.251:5353", "169.254.169.254:80",
		"[fe80::1]:22", "10.0.0.5:22", "192.168.1.1:80", "172.16.0.1:53", "100.64.0.1:80", "[fd00::1]:22", "203.0.113.9:0"} {
		relay(target)
	}
	if len(b.tests) != 0 {
		t.Fatalf("tested %v for a relay on the internet", b.tests)
	}
	relay("203.0.113.9:40000")
	relay("[2001:db8::9]:40000")
	if len(b.tests) != 2 {
		t.Fatalf("tested %v, want both internet addresses", b.tests)
	}
	// Relays on one private network: what its reflector sees is private.
	b.remote = netip.MustParseAddrPort("10.205.0.2:51820")
	relay("10.205.1.10:40000")
	relay("127.0.0.1:22")
	if len(b.tests) != 3 || b.tests[2] != netip.MustParseAddrPort("10.205.1.10:40000") {
		t.Fatalf("tested %v", b.tests)
	}
}
