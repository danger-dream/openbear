package health

import (
	"crypto/ed25519"
	"encoding/json"
	"fmt"
	"net/netip"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// epochLab is Y with a direct link up to D, the driving end, signalled
// through the relay RE.
func epochLab(t *testing.T, session string) (*Manager, *offerBackend, *directState) {
	t.Helper()
	cfg := Config{Network: "n", Self: "Y", Role: "member", Revision: 1,
		Candidates: []Candidate{{ID: "c1", Peer: "RE"}},
		Links:      []Link{{Peer: "D", Punch: true}},
		Nodes:      []Node{{ID: "D", Role: "member", Relays: []string{"RE"}}, {ID: "RE", Role: "relay"}}}
	m, b := offerManager(t, cfg, nil, map[string]bool{"RE": true})
	d := m.directs["D"]
	if d == nil || d.driver {
		t.Fatalf("setup: %+v", d)
	}
	d.state, d.session, d.n = directUp, session, 4
	d.link = &candidateState{config: Candidate{ID: "link:D", Peer: "D"}, path: Path{"dp", 1}, ready: true, routeReady: true, track: newTracker(m.cfg.Parameters, time.Now()), pending: map[uint64]string{}, early: map[uint64]time.Time{}}
	// D's punch state of the attempt that made the link.
	m.receive(signal(t, "RE", "Y", "punch", message{Punch: &PunchState{From: "D", To: "Y", Session: session, Attempt: 4}}))
	if !m.directUp("D") || d.session != session {
		t.Fatalf("setup: up %v, session %q", m.directUp("D"), d.session)
	}
	return m, b, d
}

// A relay on the way replays punch states D really signed before it
// restarted: older epochs, or a session from before D had epochs. They no
// longer start an attempt over, which took the link down.
func TestReplayedOldSessionPunchKeepsDirect(t *testing.T) {
	m, b, d := epochLab(t, "5-cur")
	for _, old := range []string{"4-old", "old-session-of-D"} {
		m.receive(signal(t, "RE", "Y", "punch", message{Punch: &PunchState{From: "D", To: "Y", Session: old, Attempt: 1}}))
		if !m.directUp("D") || d.state != directUp || d.session != "5-cur" || b.deactivates != 0 || b.releases != 0 {
			t.Fatalf("after a replayed punch of session %q: up %v, state %s, session %q, deactivated %d, released %d", old, m.directUp("D"), d.state, d.session, b.deactivates, b.releases)
		}
	}
	if m.signRejected != 0 {
		t.Fatalf("replays counted as bad signatures: %d", m.signRejected)
	}
	// D restarted for real: its next session starts over, as before.
	m.receive(signal(t, "RE", "Y", "punch", message{Punch: &PunchState{From: "D", To: "Y", Session: "6-new", Attempt: 1}}))
	if m.directUp("D") || d.state != directPunching || d.session != "6-new" || d.n != 1 {
		t.Fatalf("after D's new session: up %v, state %s, session %q/%d", m.directUp("D"), d.state, d.session, d.n)
	}
}

// Replayed offers of an older session no longer take the peer's addresses
// back to old ones.
func TestReplayedOldSessionOfferKeepsAddresses(t *testing.T) {
	m, _, d := epochLab(t, "5-cur")
	addr := func(o Offer) {
		t.Helper()
		m.receive(signal(t, "RE", "Y", "offer", message{Offer: &o}))
	}
	addr(Offer{From: "D", To: "Y", Session: "5-cur", Gen: 3, Candidates: []string{"public:203.0.113.7:51820"}})
	for _, old := range []string{"4-old", "old"} {
		addr(Offer{From: "D", To: "Y", Session: old, Gen: 1, Candidates: []string{"public:198.51.100.66:51820"}})
		if d.remoteKey != "5-cur/3" || len(d.remote) != 1 || d.remote[netip.MustParseAddrPort("203.0.113.7:51820")] == "" {
			t.Fatalf("after a replayed offer of session %q: %q %v", old, d.remoteKey, d.remote)
		}
	}
	addr(Offer{From: "D", To: "Y", Session: "6-new", Gen: 1, Candidates: []string{"public:192.0.2.9:51820"}})
	if d.remoteKey != "6-new/1" || d.remote[netip.MustParseAddrPort("192.0.2.9:51820")] == "" {
		t.Fatalf("D's new session: %q %v", d.remoteKey, d.remote)
	}
}

// A node from before epochs has sessions that are a bare nonce: each new one
// starts an attempt over, as it always did, until the node is upgraded.
// After that its old sessions are refused.
func TestSessionsOfAnUpgradingPeer(t *testing.T) {
	m, _, d := epochLab(t, "real")
	m.receive(signal(t, "RE", "Y", "punch", message{Punch: &PunchState{From: "D", To: "Y", Session: "other", Attempt: 1}}))
	if d.state != directPunching || d.session != "other" {
		t.Fatalf("a new session of a node from before epochs: state %s, session %q", d.state, d.session)
	}
	m.receive(signal(t, "RE", "Y", "punch", message{Punch: &PunchState{From: "D", To: "Y", Session: "7-up", Attempt: 1}}))
	if d.session != "7-up" {
		t.Fatalf("the first session after D's upgrade: %q", d.session)
	}
	m.receive(signal(t, "RE", "Y", "punch", message{Punch: &PunchState{From: "D", To: "Y", Session: "other", Attempt: 2}}))
	if d.session != "7-up" || d.n != 1 {
		t.Fatalf("a session from before D's upgrade, after it: %q/%d", d.session, d.n)
	}
}

// What the driving end gets back is its own session: it says nothing of the
// peer's epochs.
func TestDriverKeepsNoEpochOfItsOwnSession(t *testing.T) {
	cfg := Config{Network: "n", Self: "A", Role: "member", Revision: 1,
		Candidates: []Candidate{{ID: "c1", Peer: "RE"}},
		Links:      []Link{{Peer: "D", Punch: true}},
		Nodes:      []Node{{ID: "D", Role: "member", Relays: []string{"RE"}}, {ID: "RE", Role: "relay"}}}
	m, _ := offerManager(t, cfg, nil, map[string]bool{"RE": true})
	if d := m.directs["D"]; d == nil || !d.driver {
		t.Fatalf("setup: %+v", d)
	}
	m.receive(signal(t, "RE", "A", "punch", message{Punch: &PunchState{From: "D", To: "A", Session: "9-mine", Attempt: 1}}))
	if e, ok := m.epochs["D"]; ok {
		t.Fatalf("took epoch %d for D from the driver's own session", e)
	}
}

func TestSessionEpoch(t *testing.T) {
	for s, want := range map[string]uint64{"12-ab": 12, "0-ab": 0, "ab": 0, "": 0, "x-ab": 0, "-ab": 0, "18446744073709551615-a": ^uint64(0), "18446744073709551616-a": 0} {
		if got := sessionEpoch(s); got != want {
			t.Errorf("sessionEpoch(%q) = %d, want %d", s, got, want)
		}
	}
	if got := sessionEpoch(fmt.Sprintf("%d-%s", 42, nonce())); got != 42 {
		t.Errorf("epoch of a session as New names it: %d", got)
	}
}

// Each start takes a greater epoch than the last, kept in state.json beside
// what else it holds; the clock only raises it.
func TestNextEpochIsKeptAndRises(t *testing.T) {
	dir := t.TempDir()
	if _, err := openStore(dir, "n", "Y"); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "state.json")
	var got []uint64
	for _, at := range []int64{100, 100, 50, 1000} {
		e, err := nextEpoch(path, time.Unix(at, 0))
		if err != nil {
			t.Fatal(err)
		}
		got = append(got, e)
	}
	if fmt.Sprint(got) != "[100 101 102 1000]" {
		t.Fatalf("epochs: %v", got)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var all map[string]json.RawMessage
	if err = json.Unmarshal(raw, &all); err != nil || string(all[epochField]) != "1000" || all["health"] == nil {
		t.Fatalf("state.json: %s (%v)", raw, err)
	}
	if _, err = openStore(dir, "n", "Y"); err != nil {
		t.Fatalf("state with an epoch: %v", err)
	}
}

// A node reinstalled with a new signing key may start its epochs over.
func TestNewSigningKeyForgetsEpochs(t *testing.T) {
	m, _, _ := epochLab(t, "5-cur")
	if m.epochs["D"] != 5 {
		t.Fatalf("epoch of D: %d", m.epochs["D"])
	}
	cfg := m.cfg
	cfg.Revision++
	cfg.Nodes = append([]Node(nil), cfg.Nodes...)
	for i := range cfg.Nodes {
		if cfg.Nodes[i].ID == "D" {
			cfg.Nodes[i].SignKey = signTestKey("D reinstalled").Public().(ed25519.PublicKey)
		}
	}
	m.epochs["RE"] = 3
	m.pruneEpochs(signKeys(cfg))
	if _, ok := m.epochs["D"]; ok || m.epochs["RE"] != 3 {
		t.Fatalf("epochs after D's key changed: %v", m.epochs)
	}
}
