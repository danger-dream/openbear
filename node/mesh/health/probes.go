package health

import (
	"errors"
	"sort"
	"time"
)

type Parameters struct {
	ActiveInterval, StandbyInterval, ColdRetry  time.Duration
	InitialTimeout, MinTimeout, MaxTimeout      time.Duration
	ConsecutiveFailures, ReadySuccesses         int
	HandshakeGrace, ReturnHealthy, MinResidence time.Duration
	Lease, LeaseRefresh                         time.Duration
}

func Defaults() Parameters {
	return Parameters{time.Second, 2 * time.Second, 30 * time.Second, 3 * time.Second, 2 * time.Second, 10 * time.Second, 3, 3, 6 * time.Second, 60 * time.Second, 60 * time.Second, 30 * time.Second, 5 * time.Second}
}

func (p Parameters) Validate() error {
	for _, d := range []time.Duration{p.ActiveInterval, p.StandbyInterval, p.ColdRetry, p.InitialTimeout, p.MinTimeout, p.MaxTimeout, p.HandshakeGrace, p.ReturnHealthy, p.MinResidence, p.Lease, p.LeaseRefresh} {
		if d <= 0 {
			return errors.New("health durations must be positive")
		}
	}
	if p.MinTimeout > p.InitialTimeout || p.InitialTimeout > p.MaxTimeout {
		return errors.New("health requires minTimeout <= initialTimeout <= maxTimeout")
	}
	if p.LeaseRefresh >= p.Lease {
		return errors.New("health lease refresh must be shorter than lease")
	}
	if p.ColdRetry > 300*time.Second {
		return errors.New("health cold retry exceeds 300 seconds")
	}
	if p.ConsecutiveFailures < 1 || p.ConsecutiveFailures > 16 || p.ReadySuccesses < 1 || p.ReadySuccesses > 16 {
		return errors.New("health failure and ready counts must be between 1 and 16")
	}
	if p.ActiveInterval > 10*time.Second/time.Duration(p.ReadySuccesses) {
		return errors.New("health ready probes exceed the ten-second preparation allowance")
	}
	return nil
}

type ProbeStats struct {
	Sent, Acked, TimedOut, Late, LocalExpired uint64
	RTT, SRTT, RTTVAR, Queue                  time.Duration
	Samples                                   int
	TimeoutRate                               float64
	Failures, Successes                       int
	LastSuccess                               time.Time
}

type probe struct {
	id                                 uint64
	queued, started, written, deadline time.Time
}

// minRTT is the least round trip a probe measures.
const minRTT = time.Microsecond

type sample struct {
	at     time.Time
	failed bool
}

type tracker struct {
	params       Parameters
	pending      map[uint64]probe
	expired      map[uint64]time.Time
	samples      []sample
	stats        ProbeStats
	windowStart  time.Time
	badWindows   int
	healthySince time.Time
}

func newTracker(p Parameters, now time.Time) *tracker {
	return &tracker{params: p, pending: map[uint64]probe{}, expired: map[uint64]time.Time{}, windowStart: now}
}

func (t *tracker) timeout() time.Duration {
	if t.stats.SRTT == 0 {
		return t.params.InitialTimeout
	}
	d := 4*t.stats.SRTT + 4*t.stats.RTTVAR
	return max(t.params.MinTimeout, min(t.params.MaxTimeout, d))
}

func (t *tracker) queue(id uint64, now time.Time) bool {
	if len(t.pending) >= 16 {
		return false
	}
	t.pending[id] = probe{id: id, queued: now, deadline: now.Add(t.timeout())}
	return true
}

func (t *tracker) written(id uint64, started, completed time.Time, err error) {
	p, ok := t.pending[id]
	if !ok {
		return
	}
	if err != nil {
		delete(t.pending, id)
		t.stats.LocalExpired++
		return
	}
	p.started, p.written = started, completed
	if started.Before(p.queued) {
		started = p.queued
	}
	p.deadline = completed.Add(t.timeout())
	t.stats.Queue = started.Sub(p.queued)
	t.pending[id] = p
	t.stats.Sent++
}

func (t *tracker) reply(id uint64, now time.Time) bool {
	p, ok := t.pending[id]
	if !ok {
		if _, late := t.expired[id]; late {
			t.stats.Late++
			delete(t.expired, id)
		}
		return false
	}
	if p.written.IsZero() {
		return false
	}
	if !now.Before(p.deadline) {
		t.expire(p, now)
		t.stats.Late++
		delete(t.expired, id)
		return false
	}
	// An answer stamped before the write returned, its writer held up
	// after writing, came back within the write.
	sent := p.written
	if now.Before(sent) {
		sent = p.started
	}
	delete(t.pending, id)
	// An answer is never instant: one within the clock's resolution is
	// not taken for no measurement.
	rtt := max(now.Sub(sent), minRTT)
	if t.stats.SRTT == 0 {
		t.stats.SRTT = rtt
		t.stats.RTTVAR = rtt / 2
	} else {
		delta := t.stats.SRTT - rtt
		if delta < 0 {
			delta = -delta
		}
		t.stats.RTTVAR = (3*t.stats.RTTVAR + delta) / 4
		t.stats.SRTT = (7*t.stats.SRTT + rtt) / 8
	}
	t.stats.RTT = rtt
	t.stats.Acked++
	t.stats.Failures = 0
	t.stats.Successes++
	t.stats.LastSuccess = now
	if t.healthySince.IsZero() {
		t.healthySince = now
	}
	t.samples = append(t.samples, sample{at: now})
	return true
}

func (t *tracker) expire(p probe, now time.Time) {
	delete(t.pending, p.id)
	if p.written.IsZero() {
		t.stats.LocalExpired++
		return
	}
	t.expired[p.id] = now
	t.stats.TimedOut++
	t.stats.Failures++
	t.stats.Successes = 0
	t.healthySince = time.Time{}
	t.samples = append(t.samples, sample{at: p.deadline, failed: true})
}

func (t *tracker) tick(now time.Time) {
	var due []probe
	for _, p := range t.pending {
		if !now.Before(p.deadline) {
			due = append(due, p)
		}
	}
	sort.Slice(due, func(i, j int) bool { return due[i].deadline.Before(due[j].deadline) })
	for _, p := range due {
		t.expire(p, now)
	}
	for id, at := range t.expired {
		if now.Sub(at) > time.Minute {
			delete(t.expired, id)
		}
	}
	for !now.Before(t.windowStart.Add(time.Minute)) {
		end := t.windowStart.Add(time.Minute)
		n, bad := 0, 0
		for _, s := range t.samples {
			if !s.at.Before(t.windowStart) && s.at.Before(end) {
				n++
				if s.failed {
					bad++
				}
			}
		}
		if n >= 20 && bad*2 >= n {
			t.badWindows++
		} else {
			t.badWindows = 0
		}
		t.windowStart = end
	}
	keep := t.samples[:0]
	for _, s := range t.samples {
		if !s.at.Before(now.Add(-2 * time.Minute)) {
			keep = append(keep, s)
		}
	}
	t.samples = keep
	t.stats.Samples, t.stats.TimeoutRate = t.quality(now)
}

func (t *tracker) quality(now time.Time) (int, float64) {
	n, bad := 0, 0
	for _, s := range t.samples {
		if !s.at.Before(now.Add(-time.Minute)) {
			n++
			if s.failed {
				bad++
			}
		}
	}
	if n == 0 {
		return 0, 0
	}
	return n, float64(bad) / float64(n)
}

func (t *tracker) stable(now time.Time) bool {
	n, rate := t.quality(now)
	return n >= 20 && rate <= .1 && !t.healthySince.IsZero() && now.Sub(t.healthySince) >= t.params.ReturnHealthy
}
