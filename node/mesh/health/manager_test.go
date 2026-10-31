package health

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/netip"
	"openbear/node/mesh/wire"
	"slices"
	"testing"
	"time"
)

type policyBackend struct{ commits, releases, deactivates, warms int }

func (b *policyBackend) Prepare(context.Context, Candidate) (Path, error) {
	return Path{}, errors.New("unreachable")
}
func (b *policyBackend) Send(string, Path, uint64, []byte) error         { return nil }
func (b *policyBackend) Commit(string, Path) error                       { b.commits++; return nil }
func (b *policyBackend) Deactivate(string, Path) error                   { b.deactivates++; return nil }
func (b *policyBackend) Release(Path) error                              { b.releases++; return nil }
func (b *policyBackend) Remove(string) error                             { return nil }
func (b *policyBackend) Handshake(string, Path) error                    { return nil }
func (b *policyBackend) Warm(string, Path)                               { b.warms++ }
func (b *policyBackend) DataReady(string, Path) bool                     { return true }
func (b *policyBackend) Punch(string, []netip.AddrPort, time.Time) error { return nil }
func (b *policyBackend) Punched(string) []PunchedPath                    { return nil }
func (b *policyBackend) Reflect(context.Context, netip.AddrPort, [8]byte) (netip.AddrPort, error) {
	return netip.AddrPort{}, context.DeadlineExceeded
}
func (b *policyBackend) STUN(context.Context, netip.AddrPort) (netip.AddrPort, error) {
	return netip.AddrPort{}, context.DeadlineExceeded
}
func (b *policyBackend) Remote(Path) (netip.AddrPort, bool)           { return netip.AddrPort{}, false }
func (b *policyBackend) Prime([]netip.AddrPort, int) error            { return nil }
func (b *policyBackend) UDPPort() int                                 { return 0 }
func (b *policyBackend) ReflectSeen([8]byte) (netip.AddrPort, bool)   { return netip.AddrPort{}, false }
func (b *policyBackend) RecentlyFrom(netip.Addr, time.Duration) bool  { return false }
func (b *policyBackend) SendTest(netip.AddrPort, [8]byte, byte) error { return nil }
func (b *policyBackend) WatchTests([8]byte) bool                      { return false }
func (b *policyBackend) TestsSeen([8]byte) byte                       { return 0 }
func (b *policyBackend) StopTests([8]byte)                            {}
func policyManager(t *testing.T) (*Manager, *policyBackend, time.Time) {
	t.Helper()
	now := time.Now()
	s, err := openStore("", "n", "m")
	if err != nil {
		t.Fatal(err)
	}
	b := &policyBackend{}
	cfg := withKeys(Config{Network: "n", Self: "m", Role: "member", Revision: 1, Mode: "failover", AutoReturn: true, Parameters: Defaults(), Candidates: []Candidate{{ID: "preferred", Peer: "A"}, {ID: "backup", Peer: "B"}}})
	m := &Manager{cfg: cfg, signKeys: keysOf(cfg), backend: b, store: s, owners: newOwners(s, cfg.Parameters.Lease), candidates: map[string]*candidateState{}, links: map[string]*candidateState{}, inbound: map[Path]*inbound{}, changes: make(chan struct{}, 1), notices: map[string]outboundNotice{}, lastRefresh: map[string]time.Time{}}
	for _, c := range cfg.Candidates {
		p := &candidateState{config: c, path: Path{c.ID, 1}, token: nonce(), ready: true, routeReady: true, track: newTracker(cfg.Parameters, now), pending: map[uint64]string{}, early: map[uint64]time.Time{}}
		m.candidates[c.ID] = p
	}
	m.active = "backup"
	m.since = now
	return m, b, now
}
func TestReturnPolicyAndTenMinuteBoundaryFlapping(t *testing.T) {
	m, _, now := policyManager(t)
	preferred := m.candidates["preferred"]
	for second := 0; second < 600; second++ {
		at := now.Add(time.Duration(second) * time.Second)
		if second%55 == 0 {
			preferred.track.healthySince = at
		}
		preferred.track.samples = append(preferred.track.samples, sample{at: at})
		preferred.track.tick(at)
		m.choose(at)
		if m.proposal != nil {
			t.Fatal("boundary flapping caused return", second)
		}
	}
	preferred.track.healthySince = now.Add(600 * time.Second)
	for second := 600; second <= 660; second++ {
		at := now.Add(time.Duration(second) * time.Second)
		preferred.track.samples = append(preferred.track.samples, sample{at: at})
		preferred.track.tick(at)
		m.choose(at)
		if second < 660 && m.proposal != nil {
			t.Fatal("early return", second)
		}
	}
	if m.proposal == nil || m.proposal.candidate != "preferred" {
		t.Fatal("stable preferred path did not return")
	}
	m.proposal = nil
	m.cfg.AutoReturn = false
	m.choose(now.Add(661 * time.Second))
	if m.proposal != nil {
		t.Fatal("autoReturn=false returned")
	}
	m.candidates["backup"].track.stats.Failures = 3
	m.since = now.Add(660 * time.Second)
	m.choose(now.Add(661 * time.Second))
	if m.proposal == nil {
		t.Fatal("cooldown blocked hard failure")
	}
}
func TestSustainedDegradationNeedsTwoCompleteWindows(t *testing.T) {
	m, _, now := policyManager(t)
	m.cfg.AutoReturn = false
	active := m.candidates["backup"].track
	spare := m.candidates["preferred"].track
	for second := 0; second < 120; second++ {
		at := now.Add(time.Duration(second) * time.Second)
		active.samples = append(active.samples, sample{at: at, failed: second%3 != 0})
		spare.samples = append(spare.samples, sample{at: at})
		active.tick(at)
		spare.tick(at)
		m.choose(at)
		if m.proposal != nil {
			t.Fatal("switched before two windows", second)
		}
	}
	active.tick(now.Add(120 * time.Second))
	spare.tick(now.Add(120 * time.Second))
	m.choose(now.Add(120 * time.Second))
	if m.proposal == nil {
		t.Fatal("did not move from sustained degraded path")
	}
}
func TestFailedStandbyAttemptsNeverCommitActive(t *testing.T) {
	m, b, now := policyManager(t)
	m.cfg.AutoReturn = false
	active := m.candidates["backup"]
	trial := m.candidates["preferred"]
	for i := 0; i < 100; i++ {
		trial.path = Path{"trial", uint64(i + 1)}
		m.failed(trial, now.Add(time.Duration(i)*time.Second))
		m.choose(now.Add(time.Duration(i) * time.Second))
		if m.proposal != nil || m.active != "backup" || active.path.ID != "backup" || m.switches != 0 {
			t.Fatal("failure changed active", i)
		}
	}
	if b.commits != 0 || b.releases != 100 {
		t.Fatal("bad lifecycle counts", b)
	}
}
func TestParameterRelationsAndBackoffCap(t *testing.T) {
	if err := Defaults().Validate(); err != nil {
		t.Fatal(err)
	}
	for _, change := range []func(*Parameters){func(p *Parameters) { p.MinTimeout = 4 * time.Second }, func(p *Parameters) { p.LeaseRefresh = p.Lease }, func(p *Parameters) { p.ColdRetry = 301 * time.Second }, func(p *Parameters) { p.ReadySuccesses = 17 }, func(p *Parameters) { p.ActiveInterval = 4 * time.Second }} {
		p := Defaults()
		change(&p)
		if p.Validate() == nil {
			t.Fatal("invalid health parameters accepted", p)
		}
	}
	m, _, now := policyManager(t)
	p := m.candidates["preferred"]
	for i := 0; i < 20; i++ {
		m.failed(p, now)
		if p.retry.Sub(now) > 300*time.Second {
			t.Fatal("jitter exceeded hard retry cap")
		}
	}
}

func TestFixedDropsOtherCandidates(t *testing.T) {
	m, _, _ := policyManager(t)
	cfg := m.cfg
	cfg.Mode = "fixed"
	if err := m.configure(cfg); err != nil {
		t.Fatal(err)
	}
	if len(m.candidates) != 1 || m.candidates["preferred"] == nil {
		t.Fatal("fixed kept backups")
	}
}

// status.json's error is for what the user has to act on: a standby that
// fails while the entry carries traffic is only an event, and the error
// is a stable phrase rather than the Go error behind it.
func TestErrorOnlyForProblemsTheUserSees(t *testing.T) {
	m, _, now := policyManager(t)
	var events []string
	m.hooks.Event = func(kind, detail string) { events = append(events, kind+" "+detail) }
	expect := func(step, err, event string) {
		t.Helper()
		if got := m.Snapshot().Error; got != err {
			t.Fatalf("%s: error %q, want %q", step, got, err)
		}
		if event != "" && !slices.Contains(events, event) {
			t.Fatalf("%s: no %q in %q", step, event, events)
		}
	}

	m.mu.Lock()
	m.hooks.Routes = func() error { return errors.New("netlink: file exists") }
	m.routes()
	m.mu.Unlock()
	expect("routes failing", "route update failed", "route.error netlink: file exists")
	m.mu.Lock()
	m.hooks.Routes = nil
	m.routes()
	m.mu.Unlock()
	expect("routes applied", "", "")

	// On the backup entry, the preferred one fails to connect and, once up,
	// stops answering probes.
	m.prepared(prepared{candidate: "preferred", err: fmt.Errorf("dial: %w", context.DeadlineExceeded), revision: 1})
	expect("standby connect timeout", "", "path.failed preferred: connect timeout")
	preferred := m.candidates["preferred"]
	preferred.path, preferred.ready, preferred.since = Path{"preferred", 2}, true, now
	preferred.track.stats.Failures = m.cfg.Parameters.ConsecutiveFailures
	m.tick(now)
	expect("standby probe timeout", "", "path.failed preferred: probe timeout")
	if m.active != "backup" {
		t.Fatal("the entry moved", m.active)
	}

	// Without an entry and nothing ready, the member has none.
	m.active = ""
	m.candidates["backup"].ready = false
	m.prepared(prepared{candidate: "backup", err: errors.New("dial tcp: connection reset"), revision: 1})
	expect("no entry", "no ready entry", "path.failed backup: connect failed")
	if !slices.Contains(events, "entry.unavailable no ready entry") {
		t.Fatal("no entry.unavailable event", events)
	}
}

func TestLinkRetryIsCapped(t *testing.T) {
	m, _, now := policyManager(t)
	link := &candidateState{config: Candidate{ID: "link:R", Peer: "R"}, track: newTracker(m.cfg.Parameters, now), pending: map[uint64]string{}, early: map[uint64]time.Time{}}
	m.links["R"] = link
	preferred := m.candidates["preferred"]
	for i := 0; i < 8; i++ {
		m.failed(link, now)
		m.failed(preferred, now)
	}
	if d := link.retry.Sub(now); d > linkRetryLimit*6/5 {
		t.Fatalf("a lost link waits %v to try again", d)
	}
	if d := preferred.retry.Sub(now); d < 4*time.Minute {
		t.Fatalf("an entry candidate backs off only %v", d)
	}
}

func TestRoutineRefreshIsNotPendingPropagation(t *testing.T) {
	m, _, _ := policyManager(t)
	m.cfg.Links = []Link{{Peer: "R2"}}
	m.owners.relays["R2"] = true
	n := Notice{Member: "x", Owner: "m", Seq: 1, Refresh: 1, LeaseMs: 30000}
	m.broadcast(n, "")
	if got := m.pendingNotices(time.Now()); got != 0 {
		t.Fatalf("a claim awaiting its first ACK counts as pending: %d", got)
	}
	if got := m.pendingNotices(time.Now().Add(noticeGrace)); got != 1 {
		t.Fatalf("a claim unanswered past the grace is not pending: %d", got)
	}
	o := m.notices["R2:x"]
	o.since = time.Now().Add(-2 * noticeGrace)
	m.notices["R2:x"] = o
	n.Refresh = 2
	m.broadcast(n, "")
	if got := m.pendingNotices(time.Now()); got != 1 {
		t.Fatalf("a refresh of a claim never answered hides it: %d", got)
	}
}

// TestColdChecksAreNoEvents: a failover member checks a cold candidate
// every ColdRetry and releases it again; each check that finds it ready
// was a path.ready, and filled the event log (100 entries) within two
// hours. Only a change of outcome is an event now: the first ready, a
// failure, the same failure again is not, and ready after it is.
func TestColdChecksAreNoEvents(t *testing.T) {
	m, _, now := policyManager(t)
	var events []string
	m.hooks.Event = func(kind, detail string) { events = append(events, kind+" "+detail) }
	cold := m.candidates["preferred"]
	gen := uint64(1)
	check := func() {
		t.Helper()
		gen++
		cold.path, cold.token, cold.ready, cold.routeReady = Path{"preferred", gen}, nonce(), true, false
		raw, _ := json.Marshal(message{Version: wire.ProtocolMin, Network: "n", Revision: 1, From: "A", To: "m", Type: "ready", Token: cold.token, Generation: gen})
		m.receive(Received{Peer: "A", Path: cold.path, At: now, Payload: raw})
		if !cold.routeReady {
			t.Fatal("the ready message was not taken")
		}
		// Released after the check, as tick does.
		cold.path, cold.ready, cold.routeReady = Path{}, false, false
	}
	fail := func() {
		m.prepared(prepared{candidate: "preferred", err: fmt.Errorf("dial: %w", context.DeadlineExceeded), revision: 1})
	}
	for range 30 {
		check()
	}
	fail()
	fail()
	check()
	check()
	want := []string{"path.ready preferred", "path.failed preferred: connect timeout", "path.ready preferred"}
	if !slices.Equal(events, want) {
		t.Fatalf("events %q, want %q", events, want)
	}
}

// An entry candidate's measurement: answers and timeouts of its probes
// across the paths it gets, a cold candidate's checks included, and a
// failed attempt to connect; nothing measured is no round trip or loss.
func TestCandidateMeasurement(t *testing.T) {
	m, _, now := policyManager(t)
	cold := m.candidates["preferred"]
	if v := m.Snapshot().Candidates; slices.ContainsFunc(v, func(c CandidateStatus) bool { return c.Measure.Recent != 0 || c.Measure.Probes != 0 }) {
		t.Fatalf("measured before any probe: %+v", v)
	}
	measure := func() Measurement {
		for _, c := range m.Snapshot().Candidates {
			if c.ID == "preferred" {
				return c.Measure
			}
		}
		return Measurement{}
	}
	// A check: a new path, four probes answered in 10, 30, 20, 40 ms and
	// one timed out.
	gen := uint64(1)
	at := now.Add(-10 * time.Minute) // a failed attempt is counted at time.Now()
	check := func(rtts ...time.Duration) {
		t.Helper()
		gen++
		cold.path, cold.track = Path{"preferred", gen}, newTracker(m.cfg.Parameters, at)
		for _, rtt := range rtts {
			m.probe(cold, at)
			id := m.nextID
			m.written(WriteResult{ID: id, Started: at, Completed: at})
			if rtt > 0 {
				m.success(cold, id, at.Add(rtt))
			} else {
				at = at.Add(5 * time.Second)
				m.trackTick(cold, at)
			}
			at = at.Add(time.Second)
		}
		cold.path = Path{}
	}
	ms := time.Millisecond
	check(10*ms, 30*ms, 20*ms, 0, 40*ms)
	v := measure()
	if v.Probes != 5 || v.Timeouts != 1 || v.Recent != 5 || v.Loss != .2 || v.RTT != 30*ms || v.CheckedAt.Before(at.Add(-2*time.Second)) {
		t.Fatalf("after a check: %+v", v)
	}
	// The next check, two minutes on: the window is its own; the median of
	// the last five answers.
	at = at.Add(2 * time.Minute)
	check(50*ms, 60*ms, 70*ms, 80*ms, 90*ms, 100*ms)
	if v = measure(); v.Probes != 11 || v.Timeouts != 1 || v.Recent != 6 || v.Loss != 0 || v.RTT != 80*ms {
		t.Fatalf("after the second check: %+v", v)
	}
	// It cannot connect, minutes later: a probe and a timeout, no answer
	// within the minute.
	m.prepared(prepared{candidate: "preferred", err: errors.New("dial tcp: connection refused"), revision: 1})
	if v = measure(); v.Probes != 12 || v.Timeouts != 2 || v.Recent != 1 || v.Loss != 1 || v.RTT != 0 || v.CheckedAt.IsZero() {
		t.Fatalf("after a failed attempt: %+v", v)
	}
	// Another endpoint under the same ID keeps the counts, not the rest.
	cfg := m.cfg
	cfg.Candidates = slices.Clone(cfg.Candidates)
	cfg.Candidates[0].Endpoint = "tcp://192.0.2.9:1"
	if err := m.configure(cfg); err != nil {
		t.Fatal(err)
	}
	if v = measure(); v.Probes != 12 || v.Timeouts != 2 || v.Recent != 0 || v.CheckedAt.IsZero() {
		t.Fatalf("after a new endpoint: %+v", v)
	}
}

// On Windows time.Now moves in steps of up to 15.625 ms: a probe answered
// within one reads its sending and its answer alike, or the answer before
// the write returned. Each is answered with a round trip: the meter has
// one, as the peer's last sample and the smoothed one.
func TestAnswerWithinAClockStep(t *testing.T) {
	m, _, now := policyManager(t)
	p := m.candidates["backup"] // active
	at := now
	// 1: the answer comes after the write's report, stamped alike.
	m.probe(p, at)
	m.written(WriteResult{ID: m.nextID, Started: at, Completed: at})
	m.success(p, m.nextID, at)
	if s := p.track.stats; s.Acked != 1 || s.RTT <= 0 || s.SRTT <= 0 {
		t.Fatalf("an answer within the step: %+v", s)
	}
	// 2: the answer, stamped 2 ms into a write that took 3, comes before
	// the write's report.
	at = at.Add(time.Second)
	m.probe(p, at)
	id := m.nextID
	p.early[id] = at.Add(2 * time.Millisecond)
	m.written(WriteResult{ID: id, Started: at, Completed: at.Add(3 * time.Millisecond)})
	if s := p.track.stats; s.Acked != 2 || s.RTT != 2*time.Millisecond {
		t.Fatalf("an answer within the write, before its report: %+v", s)
	}
	// 3: the same answer after the report.
	at = at.Add(time.Second)
	m.probe(p, at)
	m.written(WriteResult{ID: m.nextID, Started: at, Completed: at.Add(3 * time.Millisecond)})
	m.success(p, m.nextID, at.Add(2*time.Millisecond))
	if s := p.track.stats; s.Acked != 3 || s.RTT != 2*time.Millisecond || s.TimedOut != 0 {
		t.Fatalf("an answer within the write, after its report: %+v", s)
	}
	var v Measurement
	for _, c := range m.Snapshot().Candidates {
		if c.ID == "backup" {
			v = c.Measure
		}
	}
	if v.Probes != 3 || v.Timeouts != 0 || v.RTT <= 0 {
		t.Fatalf("measured %+v, want a round trip", v)
	}
	if r := m.Snapshot().Peers["B"].RTT; r <= 0 {
		t.Fatalf("peer B's round trip %v", r)
	}
}
