package health

import (
	"slices"
	"testing"
	"time"

	"openbear/node/mesh/routing"
)

// RFCHost-JP2 (2026-11-16) let its neighbors' packets in but none of theirs
// back: their paths stayed committed, and each sent it every claim once a
// second until the lease ran out, one owner.unconfirmed event per member.
func TestNoticesBackOffAndANeighborIsNamedOnceAMinute(t *testing.T) {
	m, _, _ := policyManager(t)
	var events []string
	m.hooks.Event = func(kind, detail string) {
		if kind == "owner.unconfirmed" {
			events = append(events, detail)
		}
	}
	m.cfg.Links = []Link{{Peer: "R2"}}
	m.owners.relays["R2"] = true
	m.broadcast(Notice{Member: "x", Owner: "m", Seq: 1, Refresh: 1, LeaseMs: 30000}, "")
	base := m.notices["R2:x"].next
	for _, step := range []struct{ at, retry time.Duration }{
		{0, time.Second}, {time.Second, 2 * time.Second}, {2 * time.Second, 2 * time.Second}, {3 * time.Second, 4 * time.Second},
		{7 * time.Second, 8 * time.Second}, {15 * time.Second, noticeRetryMax}, {25 * time.Second, noticeRetryMax},
	} {
		m.tick(base.Add(step.at))
		if got := m.notices["R2:x"].retry; got != step.retry {
			t.Fatalf("at %v the next send waits %v, want %v", step.at, got, step.retry)
		}
	}
	// A refresh goes out at once and keeps the wait it had reached.
	m.broadcast(Notice{Member: "x", Owner: "m", Seq: 1, Refresh: 2, LeaseMs: 30000}, "")
	if o := m.notices["R2:x"]; o.retry != noticeRetryMax || o.next.After(time.Now()) {
		t.Fatalf("a refresh of an unanswered notice: %+v", o)
	}

	m.tick(base.Add(30 * time.Second))
	if len(m.notices) != 0 || !slices.Equal(events, []string{"R2: 1 leases expired without ACK"}) {
		t.Fatalf("an expired lease: notices %+v, events %q", m.notices, events)
	}
	for _, member := range []string{"y", "z"} {
		m.broadcast(Notice{Member: member, Owner: "m", Seq: 1, Refresh: 1, LeaseMs: 30000}, "")
	}
	m.tick(base.Add(61 * time.Second))
	if len(events) != 1 {
		t.Fatalf("a neighbor named again within a minute: %q", events)
	}
	m.tick(base.Add(91 * time.Second))
	if !slices.Equal(events, []string{"R2: 1 leases expired without ACK", "R2: 2 leases expired without ACK"}) {
		t.Fatalf("leases expired since the last event: %q", events)
	}
}

func TestNoNoticesOverALinkRoutingFindsDown(t *testing.T) {
	m, _, now := policyManager(t)
	m.cfg.Role = "relay"
	m.cfg.Links = []Link{{Peer: "R2", Dial: true}}
	m.owners.relays["R2"] = true
	m.links["R2"] = &candidateState{config: Candidate{ID: "link:R2", Peer: "R2"}, path: Path{"r2", 1}, ready: true, routeReady: true,
		track: newTracker(m.cfg.Parameters, now), pending: map[uint64]string{}, early: map[uint64]time.Time{}}
	r := routing.New(routing.Config{Self: "m", Relays: []string{"R2"}, Links: []string{"R2"}}, nil, nil)
	m.SetRouter(r)
	n := Notice{Member: "x", Owner: "m", Seq: 1, Refresh: 1, LeaseMs: 30000}
	m.broadcast(n, "")
	if _, ok := m.notices["R2:x"]; !ok {
		t.Fatal("a committed link routing has nothing against got no notice")
	}
	for range 3 {
		r.Lost("R2")
	}
	m.tick(time.Now())
	if len(m.notices) != 0 || m.pendingNotices(time.Now().Add(noticeGrace)) != 0 {
		t.Fatalf("a link routing finds down kept its notices: %+v", m.notices)
	}
	m.broadcast(n, "")
	if len(m.notices) != 0 {
		t.Fatalf("a link routing finds down got a notice: %+v", m.notices)
	}
	if got := m.Snapshot().PropagationPending; got != 0 {
		t.Fatalf("pending propagation counted a link routing finds down: %d", got)
	}
	// Answering again, it gets the next refresh.
	r.Probe("R2", 10*time.Millisecond)
	n.Refresh = 2
	m.broadcast(n, "")
	if _, ok := m.notices["R2:x"]; !ok {
		t.Fatal("a link back up got no notice")
	}
}

// Every revision of the mesh reached every node, and each dropped its entry
// switch under way and the notices waiting for an ACK.
func TestARevisionKeepsAnEntrySwitchAndTheNoticesItLeavesAsTheyWere(t *testing.T) {
	m, _, now := policyManager(t)
	nodes := func(xRelays ...string) []Node {
		return []Node{{ID: "A", Role: "relay"}, {ID: "B", Role: "relay"}, {ID: "R2", Role: "relay"}, {ID: "x", Role: "member", Relays: xRelays}}
	}
	cfg := m.cfg
	cfg.Nodes, cfg.Links = nodes("R2"), []Link{{Peer: "R2"}}
	if err := m.configure(withKeys(cfg)); err != nil {
		t.Fatal(err)
	}
	m.proposal = newProposal(m.candidates["preferred"], 7, "test", false, now)
	m.broadcast(Notice{Member: "x", Owner: "R2", Seq: 1, Refresh: 1, LeaseMs: 30000}, "")
	if _, ok := m.notices["R2:x"]; !ok {
		t.Fatal("no notice to keep")
	}

	cfg.Revision = 2
	if err := m.configure(withKeys(cfg)); err != nil {
		t.Fatal(err)
	}
	if m.proposal == nil || m.proposal.candidate != "preferred" {
		t.Fatal("a revision that left the candidate as it was dropped its switch")
	}
	if _, ok := m.notices["R2:x"]; !ok {
		t.Fatal("a revision that left the member as it was dropped its notice")
	}

	cfg.Revision = 3
	cfg.Nodes = nodes("R2", "A")
	cfg.Candidates = []Candidate{{ID: "preferred", Peer: "C"}, {ID: "backup", Peer: "B"}}
	if err := m.configure(withKeys(cfg)); err != nil {
		t.Fatal(err)
	}
	if m.proposal != nil {
		t.Fatal("a switch to a candidate the revision changed went on")
	}
	if len(m.notices) != 0 {
		t.Fatalf("a notice about a member whose relays changed went on: %+v", m.notices)
	}
}
