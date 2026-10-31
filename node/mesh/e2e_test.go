package mesh

import (
	"bytes"
	"crypto/ecdh"
	"crypto/rand"
	"encoding/base64"
	"net/netip"
	"sync"
	"testing"
	"time"
)

// e2eLab is end-to-end sessions of a lab's nodes joined by a relay that
// sees, and may change, every outer packet.
type e2eLab struct {
	t        *testing.T
	l        *lab
	sessions map[string]*e2eSessions
	in       map[string]chan []byte // what each node's inner device delivered

	mu     sync.Mutex
	seen   [][]byte                // every outer packet, as sent
	tamper func(outer []byte) bool // false drops it
}

func newE2ELab(t *testing.T, s spec, keys map[string]string) *e2eLab {
	x := &e2eLab{t: t, l: newLab(t, s), sessions: map[string]*e2eSessions{}, in: map[string]chan []byte{}}
	for name := range x.l.nodes {
		key := keys[name]
		if key == "" {
			key = base64.StdEncoding.EncodeToString(testE2EKey(name).Bytes())
		}
		x.in[name] = make(chan []byte, 64)
		in := x.in[name]
		deliver := func(bufs [][]byte, offset int) {
			for _, b := range bufs {
				in <- append([]byte(nil), b[offset:]...)
			}
		}
		e, err := newE2E(x.l.config(name), key, func(b []byte, _ uint64) { go x.relay(b) }, deliver, func(kind, detail string) { t.Logf("%s: %s %s", name, kind, detail) })
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(e.close)
		x.sessions[name] = e
	}
	return x
}

// relay carries an outer packet to the node it is addressed to.
func (x *e2eLab) relay(b []byte) {
	x.mu.Lock()
	x.seen = append(x.seen, append([]byte(nil), b...))
	tamper := x.tamper
	x.mu.Unlock()
	if tamper != nil && !tamper(b) {
		return
	}
	x.deliver(b)
}

func (x *e2eLab) deliver(b []byte) {
	p, ok := parseIPv4(b)
	if !ok || p.proto != protoE2E {
		x.t.Errorf("not an end-to-end packet: %x", b)
		return
	}
	for _, e := range x.sessions {
		if e.table.self == p.dst {
			if from := e.table.byAddr[p.src]; from != nil {
				e.receive(p.payload(), from)
			}
		}
	}
}

func (x *e2eLab) send(from, to string, pkt []byte) {
	e := x.sessions[from]
	e.send(append([]byte(nil), pkt...), e.table.byID[to], 0)
}

func (x *e2eLab) expect(at string, want []byte, what string) {
	x.t.Helper()
	select {
	case got := <-x.in[at]:
		if !bytes.Equal(got, want) {
			x.t.Fatalf("%s: got %x, want %x", what, got, want)
		}
	case <-time.After(5 * time.Second):
		x.t.Fatalf("%s: nothing arrived", what)
	}
}

func (x *e2eLab) expectNone(at, what string) {
	x.t.Helper()
	select {
	case got := <-x.in[at]:
		x.t.Fatalf("%s: %x arrived", what, got)
	case <-time.After(700 * time.Millisecond):
	}
}

// isData: an outer packet carrying a WireGuard data message with content.
func isData(b []byte) bool { return len(b) > 20+32 && b[20] == 4 }

// TestE2ERelayCannotReadAlterReplayOrImpersonate: what a relay carries
// between A and B it cannot read; changed or replayed it is dropped; the
// relay's own session with B cannot speak for A, and a key B does not know
// for it gets nowhere.
func TestE2ERelayCannotReadAlterReplayOrImpersonate(t *testing.T) {
	stranger, err := ecdh.X25519().GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	x := newE2ELab(t, spec{relays: []string{"R", "S"}, members: map[string][]string{"A": {"R"}, "B": {"R"}}},
		map[string]string{"S": base64.StdEncoding.EncodeToString(stranger.Bytes())})
	A, B, R, S := x.l.addr("A"), x.l.addr("B"), x.l.addr("R"), x.l.addr("S")
	secret := []byte("the relay must not read this sentence")
	ping := packet(A, B, protoICMP, 64, 1, 0, false, icmp(8, 1, secret))
	x.send("A", "B", ping)
	x.expect("B", ping, "A to B")
	reply := packet(B, A, protoICMP, 64, 2, 0, false, icmp(0, 1, secret))
	x.send("B", "A", reply)
	x.expect("A", reply, "B to A")

	x.mu.Lock()
	var data [][]byte
	for _, b := range x.seen {
		if bytes.Contains(b, secret) {
			t.Fatalf("the relay saw the plaintext in %x", b)
		}
		if p, _ := parseIPv4(b); p.proto != protoE2E || (p.src != A && p.src != B) {
			t.Fatalf("outer packet %x", b)
		}
		if isData(b) {
			data = append(data, b)
		}
	}
	x.mu.Unlock()
	if len(data) < 2 {
		t.Fatalf("%d data packets seen", len(data))
	}

	// Every byte of the ciphertext and of the tag is authenticated.
	for _, at := range []int{20 + 16, 20 + 16 + 20, -1} {
		x.mu.Lock()
		x.tamper = func(b []byte) bool {
			if isData(b) {
				i := at
				if i < 0 {
					i = len(b) - 1
				}
				b[i] ^= 0x40
			}
			return true
		}
		x.mu.Unlock()
		x.send("A", "B", packet(A, B, protoICMP, 64, 3, 0, false, icmp(8, 3, secret)))
		x.expectNone("B", "a packet changed on the way")
	}
	x.mu.Lock()
	x.tamper = nil
	x.mu.Unlock()

	// A packet seen before, sent again.
	for _, b := range data {
		x.deliver(append([]byte(nil), b...))
	}
	x.expectNone("B", "a replayed packet")
	x.expectNone("A", "a replayed packet")
	again := packet(A, B, protoICMP, 64, 4, 0, false, icmp(8, 4, nil))
	x.send("A", "B", again)
	x.expect("B", again, "A to B after the replays")

	// R's own session with B: its packets as R arrive, as A they do not.
	own := packet(R, B, protoICMP, 64, 5, 0, false, icmp(8, 5, nil))
	x.send("R", "B", own)
	x.expect("B", own, "R to B as itself")
	x.send("R", "B", packet(A, B, protoICMP, 64, 6, 0, false, icmp(8, 6, nil)))
	x.expectNone("B", "R speaking for A")
	// R writing A's address on the outer packet does not make it A's.
	x.mu.Lock()
	x.tamper = func(b []byte) bool {
		if p, _ := parseIPv4(b); p.src == R {
			copy(b[12:16], A.AsSlice())
			b[10], b[11] = 0, 0
			h := headerChecksum(b[:20])
			b[10], b[11] = byte(h>>8), byte(h)
		}
		return true
	}
	x.mu.Unlock()
	x.send("R", "B", packet(A, B, protoICMP, 64, 7, 0, false, icmp(8, 7, nil)))
	x.expectNone("B", "R speaking for A from A's outer address")
	x.mu.Lock()
	x.tamper = nil
	x.mu.Unlock()

	// S with a key that is not the one B has for it.
	x.send("S", "B", packet(S, B, protoICMP, 64, 8, 0, false, icmp(8, 8, nil)))
	x.expectNone("B", "S with a key B does not know")
	if h := x.sessions["B"].handshake("S"); h != 0 {
		t.Fatal("B took a handshake from a key it does not know")
	}
}

// TestE2ESessionsRunOut: past the budget, a node whose sessions all saw use
// in the last e2eIdle gets packets for a new one dropped and reported,
// never sent without encryption; once one is idle it makes room.
func TestE2ESessionsRunOut(t *testing.T) {
	x := newE2ELab(t, spec{relays: []string{"R"}, members: map[string][]string{"A": {"R"}, "B": {"R"}}}, nil)
	A, B, R := x.l.addr("A"), x.l.addr("B"), x.l.addr("R")
	a := x.sessions["A"]
	var events []string
	var mu sync.Mutex
	a.events = func(kind, detail string) {
		mu.Lock()
		defer mu.Unlock()
		events = append(events, kind+" "+detail)
	}
	a.limits.device.MaxPeers = 1
	now := time.Now()
	a.mu.Lock()
	a.now = func() time.Time { return now }
	a.mu.Unlock()
	ping := packet(A, B, protoICMP, 64, 1, 0, false, icmp(8, 1, nil))
	x.send("A", "B", ping)
	x.expect("B", ping, "A to B")

	x.mu.Lock()
	before := len(x.seen)
	x.mu.Unlock()
	x.send("A", "R", packet(A, R, protoICMP, 64, 2, 0, false, icmp(8, 2, nil)))
	x.expectNone("R", "a packet past the budget")
	x.mu.Lock()
	after := len(x.seen)
	x.mu.Unlock()
	st := a.status()
	mu.Lock()
	reported := len(events) == 1 && bytes.HasPrefix([]byte(events[0]), []byte("e2e.exhausted"))
	mu.Unlock()
	if after != before || st.DroppedExhausted != 1 || st.Sessions != 1 || !reported {
		t.Fatalf("outer packets %d -> %d, status %+v, events %v", before, after, st, events)
	}

	a.mu.Lock()
	now = now.Add(e2eIdle + time.Second)
	a.mu.Unlock()
	later := packet(A, R, protoICMP, 64, 3, 0, false, icmp(8, 3, nil))
	x.send("A", "R", later)
	x.expect("R", later, "A to R once B's session is idle")
	if st := a.status(); st.Evicted != 1 || st.Sessions != 1 || a.handshake("B") != 0 {
		t.Fatalf("status %+v", st)
	}
}

// The outer header is a valid IPv4 header of protocol 254 between nodes.
func TestOuterPacket(t *testing.T) {
	src, dst := netip.MustParseAddr("100.96.0.11"), netip.MustParseAddr("100.96.0.12")
	b := outerPacket(src, dst, []byte{4, 0, 0, 0})
	p, ok := parseIPv4(b)
	if !ok || p.proto != protoE2E || p.src != src || p.dst != dst || headerChecksum(b[:20]) != 0 || p.fragmented() || b[6]&0x40 == 0 {
		t.Fatalf("%x", b)
	}
	if e2eOverhead != 20+16+16 {
		t.Fatal("e2eOverhead")
	}
}

// A gateway's subnet below the mesh's addresses sorts before the gateway's
// own: packets for it still go to the gateway.
func TestE2ETableFindsEverySubnet(t *testing.T) {
	l := newLab(t, spec{relays: []string{"R"}, members: map[string][]string{"A": {"R"}, "B": {"R"}},
		subnets: map[string][]netip.Prefix{"B": {netip.MustParsePrefix("10.20.0.0/24"), netip.MustParsePrefix("192.0.2.0/24")}}})
	tb, err := e2eTableFor(l.config("A"))
	if err != nil {
		t.Fatal(err)
	}
	for _, a := range []string{"10.20.0.5", "192.0.2.5"} {
		if n := tb.owner(netip.MustParseAddr(a)); n == nil || n.id != "B" {
			t.Fatalf("%s goes to %v", a, n)
		}
	}
	if n := tb.owner(l.addr("B")); n == nil || n.id != "B" {
		t.Fatal("B's own address")
	}
}

// TestE2ETrialsCannotTakeProvenSessions: from the 10-30 check, 64 four-byte
// packets claiming to be from 64 nodes took every session, and kept them in
// use. A packet only received gets a trial, within a budget of its own; it
// keeps no session in use; this node's own packets push a trial out; a
// trial whose node completes a handshake becomes a session like any other.
func TestE2ETrialsCannotTakeProvenSessions(t *testing.T) {
	members := map[string][]string{}
	for _, m := range []string{"A", "B", "C", "D", "E", "F", "G", "H"} {
		members[m] = []string{"R"}
	}
	x := newE2ELab(t, spec{relays: []string{"R"}, members: members}, nil)
	A, B, R := x.l.addr("A"), x.l.addr("B"), x.l.addr("R")
	a := x.sessions["A"]
	now := time.Now()
	a.mu.Lock()
	a.limits.device.MaxPeers = 5 // trials: 4
	a.now = func() time.Time { return now }
	a.mu.Unlock()
	proven := func(id string) (bool, bool) {
		a.mu.Lock()
		defer a.mu.Unlock()
		a.applyProofs(a.now()) // as the next decision would
		p := a.peers[id]
		return p != nil, p != nil && p.proven
	}
	ping := packet(A, B, protoICMP, 64, 1, 0, false, icmp(8, 1, nil))
	x.send("A", "B", ping)
	x.expect("B", ping, "A to B")

	// What a relay could send claiming to be from a node: shaped as a handshake initiation.
	junk := append([]byte{1, 0, 0, 0}, make([]byte, 144)...)
	// The data of a session this node no longer has makes no trial at all.
	a.receive(append([]byte{4, 0, 0, 0}, make([]byte, 60)...), a.table.byID["H"])
	if st := a.status(); st.Sessions != 1 {
		t.Fatalf("a stale session's data made a trial: %+v", st)
	}
	for _, id := range []string{"C", "D", "E", "F", "G"} {
		a.receive(junk, a.table.byID[id])
	}
	if st := a.status(); st.Sessions != 5 || st.DroppedExhausted != 1 {
		t.Fatalf("after junk from five nodes: %+v", st)
	}
	if in, ok := proven("B"); !in || !ok {
		t.Fatal("B's session lost to trials")
	}
	if in, ok := proven("C"); !in || ok {
		t.Fatal("C's junk is not a trial")
	}
	// Junk from B keeps nothing in use: its session goes idle all the same.
	a.mu.Lock()
	now = now.Add(e2eIdle + time.Second)
	a.mu.Unlock()
	a.receive(junk, a.table.byID["B"])
	for _, p := range a.status().Peers {
		if p.ID == "B" && p.IdleMs < e2eIdle.Milliseconds() {
			t.Fatalf("junk kept B in use: idle %d ms", p.IdleMs)
		}
	}
	// This node's own packet to a new node pushes a trial out, not B.
	toR := packet(A, R, protoICMP, 64, 2, 0, false, icmp(8, 2, nil))
	x.send("A", "R", toR)
	x.expect("R", toR, "A to R past the trials")
	if in, _ := proven("B"); !in {
		t.Fatal("B's session went instead of a trial")
	}
	if st := a.status(); st.Sessions != 5 || st.Evicted != 1 {
		t.Fatalf("after A's own packet: %+v", st)
	}
	// B answers: authenticated, its session is in use again.
	back := packet(B, A, protoICMP, 64, 3, 0, false, icmp(0, 1, nil))
	x.send("B", "A", back)
	x.expect("A", back, "B to A")
	for _, p := range a.status().Peers {
		if p.ID == "B" && p.IdleMs != 0 {
			t.Fatalf("B's answer did not keep it in use: idle %d ms", p.IdleMs)
		}
	}
	// H, a real node, starts a session with A past the grace of the junk ones: its trial is proven.
	a.mu.Lock()
	now = now.Add(e2eTrialGrace)
	a.mu.Unlock()
	H := x.l.addr("H")
	fromH := packet(H, A, protoICMP, 64, 4, 0, false, icmp(8, 4, nil))
	x.send("H", "A", fromH)
	x.expect("A", fromH, "H to A")
	if in, ok := proven("H"); !in || !ok {
		t.Fatalf("H's session %v proven %v", in, ok)
	}
}
