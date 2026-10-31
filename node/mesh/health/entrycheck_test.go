package health

import (
	"net/netip"
	"slices"
	"strings"
	"testing"
	"time"
)

// An entry that answers its probes while end-to-end probes through it to a
// ready candidate fail is left in failover mode, and kept away from; in
// fixed mode, or with a target this node cannot see answer, it is only
// reported.
func TestEntrySuspectMovesOffInFailoverOnly(t *testing.T) {
	var events []string
	m, _, now := policyManager(t)
	m.cfg.AutoReturn = false
	m.hooks.Event = func(kind, detail string) { events = append(events, kind+" "+detail) }
	m.choose(now)
	if m.proposal != nil {
		t.Fatal("moved without cause")
	}

	m.entrySuspect(entrySuspect{"B", "Z", "probes failed"}, now)
	m.choose(now)
	if m.proposal != nil || len(events) != 1 || !strings.Contains(events[0], "reported only: Z") {
		t.Fatalf("a target that is not a candidate moved it: %v", events)
	}

	m.limited = nil
	m.cfg.Mode = "fixed"
	m.entrySuspect(entrySuspect{"B", "A", "probes failed"}, now)
	m.choose(now)
	if m.proposal != nil || !strings.Contains(events[1], "fixed mode") {
		t.Fatalf("fixed mode moved: %v", events)
	}

	m.limited = nil
	m.cfg.Mode = "failover"
	m.entrySuspect(entrySuspect{"B", "A", "probes failed"}, now)
	m.choose(now)
	if m.proposal == nil || m.proposal.candidate != "preferred" || !strings.Contains(events[2], "moving off it") ||
		!strings.Contains(events[3], "end-to-end probes through the entry failed") {
		t.Fatalf("failover did not move: %+v %v", m.proposal, events)
	}
	// One snapshot: candidates come in map order, another call may list them the other way round.
	if s := m.Snapshot(); !slices.ContainsFunc(s.Candidates, func(c CandidateStatus) bool { return c.ID == "backup" && c.Suspect }) {
		t.Fatalf("status does not show the suspect: %+v", s.Candidates)
	}

	// Once moved, the suspect is not chosen back while held.
	m.proposal = nil
	m.active = "preferred"
	m.candidates["preferred"].track.stats.Failures = 3
	m.choose(now.Add(time.Minute))
	if m.proposal != nil {
		t.Fatal("went back to the suspect entry")
	}
	m.choose(now.Add(suspectHold + time.Second))
	if m.proposal == nil || m.proposal.candidate != "backup" {
		t.Fatal("the hold did not end")
	}
}

// A reflected address none of two or more candidates saw on their
// authenticated paths is not offered.
func TestReflectedAddressesNeedAnObservation(t *testing.T) {
	var events []string
	m, _, now := policyManager(t)
	m.hooks.Event = func(kind, detail string) { events = append(events, kind+" "+detail) }
	real, lie := netip.MustParseAddrPort("203.0.113.7:40000"), netip.MustParseAddrPort("198.51.100.9:40000")
	both := []netip.AddrPort{real, lie}
	if got := m.confirmed(both, now); len(got) != 2 {
		t.Fatal("filtered without observations", got)
	}
	m.observedBy("A", "203.0.113.7:51820", now)
	if got := m.confirmed(both, now); len(got) != 2 {
		t.Fatal("filtered on one observation", got)
	}
	m.observedBy("B", "203.0.113.7:6000", now)
	m.observedBy("not-a-candidate", "198.51.100.9:1", now)
	if got := m.confirmed(both, now); len(got) != 1 || got[0] != real {
		t.Fatal("kept an address no candidate saw", got)
	}
	if len(events) != 1 || !strings.HasPrefix(events[0], "reach.mismatch") || !strings.Contains(events[0], lie.String()) {
		t.Fatal(events)
	}
	m.confirmed(both, now.Add(time.Minute))
	if len(events) != 1 {
		t.Fatal("mismatch reported more than once in ten minutes")
	}
	if got := m.confirmed(both, now.Add(observedFor+time.Second)); len(got) != 2 {
		t.Fatal("old observations still count", got)
	}
}

// A candidate reached over this machine's own network sees its LAN address:
// that is no view of the internet, so it neither filters nor is reported.
func TestLANRelayObservationsDoNotCount(t *testing.T) {
	var events []string
	m, _, now := policyManager(t)
	m.hooks.Event = func(kind, detail string) { events = append(events, kind+" "+detail) }
	m.reach.nets, m.reach.netsAt = []netip.Prefix{netip.MustParsePrefix("192.168.1.0/24")}, time.Now().Add(time.Hour)
	m.candidates["preferred"].config.Endpoint = "udp://192.168.1.2:51820"
	public := netip.MustParseAddrPort("203.0.113.7:40000")
	m.observedBy("A", "192.168.1.3:51820", now)
	m.observedBy("B", "203.0.113.7:6000", now)
	if got := m.confirmed([]netip.AddrPort{public}, now); len(got) != 1 || len(events) != 0 {
		t.Fatal("a LAN relay's view counted", got, events)
	}
}
