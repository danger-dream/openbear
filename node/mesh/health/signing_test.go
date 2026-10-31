package health

import (
	"crypto/ed25519"
	"errors"
	"slices"
	"testing"
	"time"
)

// A relay takes an ownership notice only as the member and its owner signed
// it: not one another relay made in their name, nor one changed on the way,
// nor an older one again.
func TestOwnerNoticeSignatures(t *testing.T) {
	s, err := openStore("", "n", "B")
	if err != nil {
		t.Fatal(err)
	}
	o := newOwners(s, 30*time.Second)
	if err = configureOwners(o, 1, map[string][]string{"M": {"A", "B"}}, map[string]bool{"A": true, "B": true, "C": true}); err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	good := sn(Notice{Member: "M", Owner: "A", Seq: 5, Refresh: 1, Generation: 1, LeaseMs: 30000})

	// C, a relay, claims M attached to A at a newer sequence.
	forged := good
	forged.Seq = 6
	forged.MSig = claimInput("n", "M", "A", 6).SignString(signTestKey("C"))
	forged.OSig = noticeInput("n", forged).SignString(signTestKey("C"))
	if _, err = o.accept("C", forged, now); !errors.Is(err, errSignature) {
		t.Fatalf("a notice C signed for M and A: %v", err)
	}
	// A real one with its sequence raised on the way.
	moved := good
	moved.Seq = 6
	if _, err = o.accept("C", moved, now); !errors.Is(err, errSignature) {
		t.Fatalf("a notice changed on the way: %v", err)
	}
	// The member's claim to A, leased by B in A's name.
	other := sn(Notice{Member: "M", Owner: "B", Seq: 6, Refresh: 1, Generation: 1, LeaseMs: 30000})
	other.Owner = "A"
	if _, err = o.accept("C", other, now); !errors.Is(err, errSignature) {
		t.Fatalf("B's lease presented as A's: %v", err)
	}
	if len(s.state.Claims) != 0 {
		t.Fatalf("stored %v", s.state.Claims)
	}

	if changed, err := o.accept("C", good, now); !changed || err != nil {
		t.Fatal(changed, err)
	}
	newer := sn(Notice{Member: "M", Owner: "A", Seq: 5, Refresh: 2, Generation: 1, LeaseMs: 30000})
	if _, err = o.accept("C", newer, now.Add(10*time.Second)); err != nil {
		t.Fatal(err)
	}
	if _, err = o.accept("C", good, now.Add(20*time.Second)); !errors.Is(err, errReplay) {
		t.Fatalf("the older notice again: %v", err)
	}
	if o.verified != 2 {
		t.Fatalf("%d notices counted as verified, want 2", o.verified)
	}
	if _, ok := o.owner("M", now.Add(41*time.Second)); ok {
		t.Fatal("a replay extended the lease")
	}
}

// Claims stored before claims were signed, or under keys from before a
// node was reinstalled, count as expired: the next config drops them.
func TestStoredClaimsWithoutSignaturesAreDropped(t *testing.T) {
	dir := t.TempDir()
	s, err := openStore(dir, "n", "B")
	if err != nil {
		t.Fatal(err)
	}
	o := newOwners(s, 30*time.Second)
	allowed := map[string][]string{"M": {"A", "B"}, "N": {"A"}, "P": {"A"}}
	relays := map[string]bool{"A": true, "B": true}
	if err = configureOwners(o, 1, allowed, relays); err != nil {
		t.Fatal(err)
	}
	if _, err = o.accept("A", sn(Notice{Member: "M", Owner: "A", Seq: 3, Refresh: 1, Generation: 1, LeaseMs: 30000}), time.Now()); err != nil {
		t.Fatal(err)
	}
	p := s.copy()
	p.Claims["N"] = claim{Owner: "A", Seq: 9, Refresh: 1, Generation: 1}
	signed := sn(Notice{Member: "P", Owner: "A", Seq: 2, Refresh: 1, Generation: 1, LeaseMs: 30000})
	p.Claims["P"] = claim{Owner: "A", Seq: 2, Refresh: 1, Generation: 1, LeaseMs: 30000, MSig: signed.MSig, OSig: signed.OSig}
	if err = s.commit(p); err != nil {
		t.Fatal(err)
	}

	s2, err := openStore(dir, "n", "B")
	if err != nil {
		t.Fatal(err)
	}
	o2 := newOwners(s2, 30*time.Second)
	keys := keysOf(Config{Self: "B", Nodes: []Node{{ID: "M"}, {ID: "N"}, {ID: "P"}, {ID: "A"}}})
	// P was reinstalled: its key is another now.
	keys["P"] = signTestKey("P again").Public().(ed25519.PublicKey)
	dropped, err := o2.configure(2, allowed, relays, "n", keys)
	if err != nil {
		t.Fatal(err)
	}
	if dropped != 2 || !slices.Equal(sortedClaims(s2), []string{"M"}) {
		t.Fatalf("dropped %d, kept %v", dropped, sortedClaims(s2))
	}
}

func sortedClaims(s *store) []string {
	var out []string
	for id := range s.state.Claims {
		out = append(out, id)
	}
	slices.Sort(out)
	return out
}

// Offers and punch states are taken only as the node they name signed them:
// a relay can neither make one in a member's name nor change one it
// forwards, and checks before it forwards.
func TestPunchSignalsSigned(t *testing.T) {
	nodes := []Node{{ID: "R", Role: "relay"}, {ID: "B", Role: "member"}}
	m, _ := offerManager(t, Config{Network: "n", Self: "A", Role: "member", Revision: 1, Nodes: nodes, Links: []Link{{Peer: "B", Punch: true}}},
		map[string][]string{}, map[string]bool{"R": true})
	offer := Offer{From: "B", To: "A", Session: "s", Gen: 2, Candidates: []string{"public:203.0.113.7:51820"}}

	forged := offer
	forged.Sig = offerInput("n", &forged).SignString(signTestKey("R"))
	m.receive(offerFrom(t, "R", "A", forged))
	changed := offer
	changed.Sig = offerInput("n", &offer).SignString(signTestKey("B"))
	changed.Candidates = []string{"public:198.51.100.66:51820"}
	m.receive(offerFrom(t, "R", "A", changed))
	if m.directs["B"].remoteKey != "" || m.signRejected != 2 {
		t.Fatalf("took a forged or changed offer: %q, %d rejected", m.directs["B"].remoteKey, m.signRejected)
	}

	m.receive(offerFrom(t, "R", "A", offer))
	if m.directs["B"].remoteKey != "s/2" {
		t.Fatalf("the signed offer was not taken: %q", m.directs["B"].remoteKey)
	}
	if sec := m.Snapshot().Security; sec.Verified != 1 || sec.Rejected != 2 || sec.RejectedBy["R"] != 2 || len(sec.RejectedBy) != 1 {
		t.Fatalf("security status %+v", sec)
	}
	// B's older offer of the session, replayed with its old addresses.
	old := offer
	old.Gen, old.Candidates = 1, []string{"public:198.51.100.66:51820"}
	m.receive(offerFrom(t, "R", "A", old))
	if m.directs["B"].remoteKey != "s/2" {
		t.Fatalf("an older offer replaced the newer: %q", m.directs["B"].remoteKey)
	}

	before := m.signRejected
	ps := PunchState{From: "B", To: "A", Session: "s", Attempt: 1}
	ps.Sig = punchInput("n", &ps).SignString(signTestKey("R"))
	m.receive(signal(t, "R", "A", "punch", message{Punch: &ps}))
	if m.signRejected != before+1 {
		t.Fatal("a punch state R signed for B was not refused")
	}
}

// A relay does not forward an offer a member forged in another's name.
func TestRelayChecksOffersBeforeForwarding(t *testing.T) {
	nodes := []Node{{ID: "R2", Role: "relay"}, {ID: "A", Role: "member"}, {ID: "B", Role: "member"}}
	m, b := offerManager(t, Config{Network: "n", Self: "R", Role: "relay", Revision: 1, Nodes: nodes, Links: []Link{{Peer: "R2"}}},
		map[string][]string{"A": {"R", "R2"}, "B": {"R"}}, map[string]bool{"R2": true})
	now := time.Now()
	if _, err := m.owners.accept("R2", sn(Notice{Member: "A", Owner: "R2", Seq: 1, Refresh: 1, Generation: 1, LeaseMs: 30000}), now); err != nil {
		t.Fatal(err)
	}
	m.links["R2"] = &candidateState{config: Candidate{ID: "link:R2", Peer: "R2"}, ready: true, path: Path{ID: "r2", Generation: 1}}
	o := Offer{From: "B", To: "A", Session: "s", Gen: 1}
	o.Sig = offerInput("n", &o).SignString(signTestKey("A"))
	m.receive(offerFrom(t, "B", "R", o))
	if len(b.sent) != 0 {
		t.Fatalf("forwarded an offer B did not sign: %v", b.types())
	}
}

// A node whose signing key the config does not carry (the controller leaves
// out a member it has none for) is taken at its word in nothing: what it
// signs with a key of its own is refused like a forgery.
func TestNothingIsTakenFromANodeWithoutAKey(t *testing.T) {
	nodes := []Node{{ID: "R", Role: "relay"}, {ID: "B", Role: "member"}}
	m, _ := offerManager(t, Config{Network: "n", Self: "A", Role: "member", Revision: 1, Nodes: nodes, Links: []Link{{Peer: "B", Punch: true}}},
		map[string][]string{}, map[string]bool{"R": true})
	delete(m.signKeys, "B")
	o := Offer{From: "B", To: "A", Session: "s", Gen: 1, Candidates: []string{"public:203.0.113.7:51820"}}
	o.Sig = offerInput("n", &o).SignString(signTestKey("B"))
	m.receive(offerFrom(t, "R", "A", o))
	ps := PunchState{From: "B", To: "A", Session: "s", Attempt: 1}
	ps.Sig = punchInput("n", &ps).SignString(signTestKey("B"))
	m.receive(signal(t, "R", "A", "punch", message{Punch: &ps}))
	if m.directs["B"].remoteKey != "" || m.signRejected != 2 {
		t.Fatalf("took B's word without its key: %q, %d rejected", m.directs["B"].remoteKey, m.signRejected)
	}

	s, err := openStore("", "n", "R2")
	if err != nil {
		t.Fatal(err)
	}
	ow := newOwners(s, 30*time.Second)
	keys := map[string]ed25519.PublicKey{"R": signTestKey("R").Public().(ed25519.PublicKey)}
	if _, err := ow.configure(1, map[string][]string{"M": {"R"}}, map[string]bool{"R": true}, "n", keys); err != nil {
		t.Fatal(err)
	}
	if _, err := ow.accept("R", sn(Notice{Member: "M", Owner: "R", Seq: 1, Refresh: 1, Generation: 1, LeaseMs: 30000}), time.Now()); !errors.Is(err, errSignature) {
		t.Fatalf("a notice for M, whose key the config lacks: %v", err)
	}
}
