package health

import (
	"context"
	"encoding/json"
	"math/rand/v2"
	"openbear/node/mesh/wire"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

type redialFake struct {
	policyBackend
	redials []time.Time
	retired int
	fail    error
	now     *time.Time
}

func (b *redialFake) Redial(_ context.Context, _ string, _ Path) (string, string, error) {
	b.redials = append(b.redials, *b.now)
	if b.fail != nil {
		return "", "", b.fail
	}
	n := len(b.redials)
	return "192.0.2.10:" + itoa(49177+n), "192.0.2.10:" + itoa(49178+n), nil
}
func (b *redialFake) Retire(Path) { b.retired++ }

func itoa(n int) string { return strconv.Itoa(n) }

func encodeFor(t *testing.T, v message) []byte {
	t.Helper()
	v.Revision = 1
	raw, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

// redialLab is a relay with a dialed TCP link to S, its probes fed by hand
// on a fake clock, one a second.
type redialLab struct {
	t      *testing.T
	m      *Manager
	p      *candidateState
	b      *redialFake
	now    time.Time
	rng    *rand.Rand
	events []string
}

func newRedialLab(t *testing.T, rp RedialParams, dir string) *redialLab {
	t.Helper()
	s, err := openStore(dir, "n", "r")
	if err != nil {
		t.Fatal(err)
	}
	l := &redialLab{t: t, now: time.Date(2026, 10, 28, 12, 0, 0, 0, time.UTC), rng: rand.New(rand.NewPCG(1, 2))}
	l.b = &redialFake{now: &l.now}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	cfg := withKeys(Config{Network: "n", Self: "r", Role: "relay", Revision: 1, Parameters: Defaults(), Links: []Link{{Peer: "S", Endpoint: "tcp://192.0.2.1:52000", Dial: true}}})
	l.m = &Manager{cfg: cfg, signKeys: keysOf(cfg), backend: l.b, store: s, owners: newOwners(s, cfg.Parameters.Lease), candidates: map[string]*candidateState{}, links: map[string]*candidateState{}, inbound: map[Path]*inbound{},
		changes: make(chan struct{}, 1), input: make(chan any, 16), ctx: ctx, notices: map[string]outboundNotice{}, lastRefresh: map[string]time.Time{},
		redial: rp, baselines: loadBaselines(s.path), baselineSaved: l.now, hooks: Hooks{Event: func(kind, detail string) { l.events = append(l.events, kind+" "+detail) }}}
	l.p = &candidateState{config: Candidate{ID: "link:S", Peer: "S", Endpoint: "tcp://192.0.2.1:52000"}, path: Path{"p1", 1}, ready: true, routeReady: true, track: newTracker(cfg.Parameters, l.now), pending: map[uint64]string{}, early: map[uint64]time.Time{}}
	l.m.links["S"] = l.p
	return l
}

// second feeds one probe's outcome, rtt or lost, and moves on a second.
func (l *redialLab) second(rtt time.Duration, lost bool) {
	l.m.observe(l.p, l.now, rtt, lost)
	l.m.redialTick(l.now)
	if l.p.trend != nil && l.p.trend.inflight {
		v := (<-l.m.input).(redialDone)
		l.m.redialed(v, l.now)
	}
	l.now = l.now.Add(time.Second)
}

// minutes runs n minutes of probes from f.
func (l *redialLab) minutes(n int, f func(*rand.Rand) (time.Duration, bool)) {
	for i := 0; i < n*60; i++ {
		rtt, lost := f(l.rng)
		l.second(rtt, lost)
	}
}

func ms(f float64) time.Duration { return time.Duration(f * float64(time.Millisecond)) }

// The link at its usual 75 ms: a few ms either way, one or two probes a
// minute timed out.
func usual(r *rand.Rand) (time.Duration, bool) {
	if r.IntN(60) < 2 {
		return 0, true
	}
	return ms(72 + 6*r.Float64()), false
}

// Tonight's connection: 147-166 ms, now and then up to 526 ms.
func tonight(r *rand.Rand) (time.Duration, bool) {
	rtt := 147 + 19*r.Float64()
	if r.IntN(10) == 0 {
		rtt += 360 * r.Float64()
	}
	return ms(rtt), false
}

// The link losing 15% of its segments one way: most probes answer as
// usual, the rest a retransmission later.
func lossy(r *rand.Rand) (time.Duration, bool) {
	if r.IntN(100) < 15 {
		return ms(75 + 200 + 100*r.Float64()), false
	}
	return ms(72 + 6*r.Float64()), false
}

func (l *redialLab) status() RedialStatus {
	return *l.m.redialStatus(l.p, l.now)
}

func TestRedialTonight(t *testing.T) {
	l := newRedialLab(t, DefaultRedialParams(), "")
	l.minutes(40, usual)
	if s := l.status(); s.Baseline < ms(72) || s.Baseline > ms(76) || len(l.b.redials) != 0 {
		t.Fatalf("after 40 usual minutes: %+v, %d redials", s, len(l.b.redials))
	}
	degraded := l.now
	l.minutes(5, tonight)
	if len(l.b.redials) != 1 {
		t.Fatalf("%d redials", len(l.b.redials))
	}
	if d := l.b.redials[0].Sub(degraded); d < 3*time.Minute || d > 4*time.Minute {
		t.Fatalf("redialed %v after the link degraded", d)
	}
	want := "link.redial link:S reason=rtt rtt=1"
	if len(l.events) != 1 || !strings.HasPrefix(l.events[0], want) || !strings.Contains(l.events[0], "baseline=7") || !strings.HasSuffix(l.events[0], " port=49178->49179") {
		t.Fatalf("events %q", l.events)
	}
	t.Log(l.events[0])
}

// The same link losing 15% one way: its median stays, its slow answers
// redial it.
func TestRedialLossy(t *testing.T) {
	l := newRedialLab(t, DefaultRedialParams(), "")
	l.minutes(40, usual)
	l.minutes(6, lossy)
	if len(l.b.redials) != 1 || !strings.Contains(l.events[0], "reason=slow") {
		t.Fatalf("%d redials, events %q", len(l.b.redials), l.events)
	}
	t.Log(l.events[0])
}

// Usual jitter and an occasional timeout, for six hours, redial nothing;
// neither does a short spike.
func TestRedialNotOnNoise(t *testing.T) {
	l := newRedialLab(t, DefaultRedialParams(), "")
	l.minutes(6*60, func(r *rand.Rand) (time.Duration, bool) {
		if r.IntN(1000) < 15 {
			return 0, true // 1.5%: a burst of up to several a minute
		}
		return ms(70 + 10*r.Float64()), false
	})
	l.minutes(2, tonight)
	l.minutes(10, usual)
	if len(l.b.redials) != 0 || l.status().Level != 0 {
		t.Fatalf("redialed at %v: %q", l.b.redials, l.events)
	}
}

// Without enough samples nothing redials: a link that has been slow from
// the start has no baseline to be slower than; one probed too rarely has
// no judged minutes.
func TestRedialNeedsSamples(t *testing.T) {
	l := newRedialLab(t, DefaultRedialParams(), "")
	l.minutes(25, func(r *rand.Rand) (time.Duration, bool) { return ms(150 + 16*r.Float64()), false })
	if s := l.status(); s.Baseline != 0 || len(l.b.redials) != 0 {
		t.Fatalf("no baseline yet: %+v, %d redials", s, len(l.b.redials))
	}
	l = newRedialLab(t, DefaultRedialParams(), "")
	l.minutes(40, usual)
	// A probe every four seconds: 15 a minute.
	for i := 0; i < 10*15; i++ {
		rtt, _ := tonight(l.rng)
		l.m.observe(l.p, l.now, rtt, false)
		l.m.redialTick(l.now)
		l.now = l.now.Add(4 * time.Second)
	}
	if len(l.b.redials) != 0 {
		t.Fatalf("redialed on sparse probes: %v", l.b.redials)
	}
}

// A link that stays bad is redialed after 10, 20, 40, 60 and again 60
// minutes; healthy for ten minutes, it starts over, and two redials are
// still ten minutes apart at least.
func TestRedialBackoff(t *testing.T) {
	l := newRedialLab(t, DefaultRedialParams(), "")
	l.minutes(40, usual)
	l.minutes(4+10+20+40+60+60, tonight)
	if len(l.b.redials) != 6 {
		t.Fatalf("%d redials: %v", len(l.b.redials), l.b.redials)
	}
	for i, want := range []time.Duration{10, 20, 40, 60, 60} {
		gap := l.b.redials[i+1].Sub(l.b.redials[i])
		// The new connection needs three bad minutes of its own, judged
		// at minute boundaries.
		if gap < want*time.Minute || gap > want*time.Minute+time.Minute {
			t.Fatalf("gap %d: %v, want %d min", i, gap, want)
		}
	}
	if s := l.status(); s.Level != 4 || s.Redials != 6 || s.Next != s.Last.Add(time.Hour) {
		t.Fatalf("status %+v", s)
	}
	l.minutes(11, usual)
	if s := l.status(); s.Level != 0 || len(l.b.redials) != 6 {
		t.Fatalf("after ten healthy minutes: %+v", s)
	}
	l.minutes(5, tonight)
	if len(l.b.redials) != 7 {
		t.Fatalf("not redialed again: %d", len(l.b.redials))
	}
	// Healthy sooner (two minutes), a new degradation still waits for ten
	// minutes from the last redial.
	rp := DefaultRedialParams()
	rp.GoodPeriods = 2
	l = newRedialLab(t, rp, "")
	l.minutes(40, usual)
	l.minutes(4, tonight)
	first := l.b.redials[0]
	l.minutes(3, usual)
	if l.status().Level != 0 {
		t.Fatal("not healthy")
	}
	l.minutes(10, tonight)
	if len(l.b.redials) != 2 || l.b.redials[1].Sub(first) < 10*time.Minute {
		t.Fatalf("redials %v", l.b.redials)
	}
	if l.events[1] == "" || len(l.events) != 2 {
		t.Fatalf("events %q", l.events)
	}
}

// A failed redial is an event and counts toward the backoff; skipped
// redials are no events.
func TestRedialFailure(t *testing.T) {
	l := newRedialLab(t, DefaultRedialParams(), "")
	l.b.fail = context.DeadlineExceeded
	l.minutes(40, usual)
	l.minutes(15, tonight)
	if len(l.b.redials) != 2 || len(l.events) != 2 || !strings.HasPrefix(l.events[0], "link.redial.failed link:S reason=rtt") || !strings.HasSuffix(l.events[0], "error=context deadline exceeded") {
		t.Fatalf("redials %v events %q", l.b.redials, l.events)
	}
}

// The baseline is kept in state.json: after a restart a connection slow
// from the start is still slower than the link was.
func TestRedialBaselineSurvivesRestart(t *testing.T) {
	dir := t.TempDir()
	rp := DefaultRedialParams()
	l := newRedialLab(t, rp, dir)
	l.minutes(40, usual)
	if l.m.store.path != filepath.Join(dir, "state.json") || len(l.m.baselines) != 1 {
		t.Fatalf("not saved: %+v", l.m.baselines)
	}
	base := l.status().Baseline
	later := l.now.Add(5 * time.Minute)
	l = newRedialLab(t, rp, dir)
	l.now = later
	if s := l.status(); (s.Baseline - base).Abs() > time.Microsecond {
		t.Fatalf("baseline %v after restart, was %v", s.Baseline, base)
	}
	l.minutes(5, tonight)
	if len(l.b.redials) != 1 {
		t.Fatalf("%d redials after restart", len(l.b.redials))
	}
	// Another endpoint starts over.
	l = newRedialLab(t, rp, dir)
	l.p.config.Endpoint = "tcp://192.0.2.2:52000"
	if s := l.status(); s.Baseline != 0 {
		t.Fatalf("baseline of another endpoint: %v", s.Baseline)
	}
}

// After a redial the other end asks for the path to be prepared again;
// the routes stay on it, and once it answers without asking, the old
// connection is retired.
func TestRedialPreparesAgain(t *testing.T) {
	l := newRedialLab(t, DefaultRedialParams(), "")
	l.minutes(40, usual)
	for deadline := l.now.Add(10 * time.Minute); len(l.b.redials) == 0 && l.now.Before(deadline); {
		rtt, _ := tonight(l.rng)
		l.second(rtt, false)
	}
	if len(l.b.redials) != 1 || !l.p.swapping(l.now) {
		t.Fatal("not redialed")
	}
	p := l.p
	pong := func(reclaim bool) {
		l.m.nextID++
		id := l.m.nextID
		p.track.queue(id, l.now)
		p.track.written(id, l.now, l.now, nil)
		p.pending[id] = strings.Repeat("a", 32)
		p.token = strings.Repeat("b", 32)
		raw := encodeFor(t, message{Version: wire.ProtocolMin, Network: "n", From: "S", To: "r", Type: "pong", Nonce: p.pending[id], Token: p.token, Generation: 1, Probe: id, Reclaim: reclaim})
		l.m.receive(Received{Peer: "S", Path: p.path, At: l.now.Add(75 * time.Millisecond), Payload: raw})
	}
	pong(false) // over the old connection
	if l.b.retired != 0 {
		t.Fatal("retired before the other end asked")
	}
	pong(true)
	if !p.routeReady || !p.reprepare || l.b.retired != 0 {
		t.Fatalf("routeReady %v reprepare %v", p.routeReady, p.reprepare)
	}
	raw := encodeFor(t, message{Version: wire.ProtocolMin, Network: "n", From: "S", To: "r", Type: "ready", Token: p.token, Generation: 1})
	l.m.receive(Received{Peer: "S", Path: p.path, At: l.now, Payload: raw})
	if p.reprepare {
		t.Fatal("still preparing")
	}
	pong(false)
	if l.b.retired != 1 || p.swapping(l.now) {
		t.Fatalf("retired %d", l.b.retired)
	}
}

// A member's TCP entry candidate redials as a link does; UDP and punched
// candidates, and a link this node does not dial, never do.
func TestRedialEntryCandidate(t *testing.T) {
	l := newRedialLab(t, DefaultRedialParams(), "")
	delete(l.m.links, "S")
	l.p.config.ID = "c2"
	l.m.candidates["c2"] = l.p
	l.minutes(40, usual)
	l.minutes(5, tonight)
	if len(l.b.redials) != 1 || !strings.HasPrefix(l.events[0], "link.redial c2 relay=S reason=rtt ") {
		t.Fatalf("redials %v events %q", l.b.redials, l.events)
	}
	for _, c := range []Candidate{{ID: "c1", Peer: "S", Endpoint: "udp://192.0.2.1:51820"}, {ID: "c3", Peer: "S", Endpoint: "tcp://192.0.2.1:52000", Punch: true}} {
		l := newRedialLab(t, DefaultRedialParams(), "")
		delete(l.m.links, "S")
		l.p.config = c
		l.m.candidates[c.ID] = l.p
		l.minutes(40, usual)
		l.minutes(15, tonight)
		if len(l.b.redials) != 0 || l.m.redialStatus(l.p, l.now) != nil {
			t.Fatalf("%s redialed", c.Endpoint)
		}
	}
	// The accepting end's link has no endpoint.
	l = newRedialLab(t, DefaultRedialParams(), "")
	l.p.config.Endpoint = ""
	l.minutes(40, usual)
	l.minutes(15, tonight)
	if len(l.b.redials) != 0 {
		t.Fatal("the accepting end redialed")
	}
}
