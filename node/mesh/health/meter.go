package health

import (
	"slices"
	"time"
)

// What status reports of an entry candidate's probes (Measurement). A
// candidate's tracker starts again with every path it gets, a cold one's
// at each check, so the meter keeps its own count beside it. It adds no
// probes: a cold candidate is measured by its checks only.
const (
	// The recent outcomes are those within meterWindow up to the latest.
	meterWindow = time.Minute
	meterKeep   = 128
	// The round trip is the median of the last meterRTTs answers among
	// them, as a relay link's (routing.rttMedian).
	meterRTTs = 5
)

// Measurement is what status shows of a candidate's probes. Probes and
// Timeouts count since start: an answered or timed-out probe, and a
// failed attempt to connect, which is one of each. CheckedAt is the last
// such outcome. RTT is zero, and Recent too, without one within the
// window.
type Measurement struct {
	Probes, Timeouts uint64
	CheckedAt        time.Time
	RTT              time.Duration
	Loss             float64
	Recent           int
}

type outcome struct {
	at     time.Time
	rtt    time.Duration
	failed bool
}

type meter struct {
	probes, timeouts uint64
	checked          time.Time
	recent           []outcome
	// counted is how many of track's timeouts are in.
	track   *tracker
	counted uint64
}

func (m *meter) add(o outcome) {
	m.probes++
	m.checked = o.at
	if o.failed {
		m.timeouts++
	}
	m.recent = append(m.recent, o)
	if len(m.recent) > meterKeep {
		m.recent = slices.Delete(m.recent, 0, len(m.recent)-meterKeep)
	}
}

// answered counts a probe answered after rtt.
func (m *meter) answered(at time.Time, rtt time.Duration) { m.add(outcome{at: at, rtt: rtt}) }

// unreachable counts a failed attempt to connect.
func (m *meter) unreachable(at time.Time) { m.add(outcome{at: at, failed: true}) }

// expired counts the probes t timed out since the last call.
func (m *meter) expired(t *tracker, now time.Time) {
	if m.track != t {
		m.track, m.counted = t, 0
	}
	for ; m.counted < t.stats.TimedOut; m.counted++ {
		m.add(outcome{at: now, failed: true})
	}
}

// restart keeps the counts and forgets what was measured: the candidate
// is another path now.
func (m *meter) restart() { m.recent = nil }

func (m *meter) measurement() Measurement {
	out := Measurement{Probes: m.probes, Timeouts: m.timeouts, CheckedAt: m.checked}
	if len(m.recent) == 0 {
		return out
	}
	last := m.recent[len(m.recent)-1].at
	var rtts []time.Duration
	failed := 0
	for _, o := range m.recent {
		if o.at.Before(last.Add(-meterWindow)) {
			continue
		}
		out.Recent++
		if o.failed {
			failed++
		} else {
			rtts = append(rtts, o.rtt)
		}
	}
	out.Loss = float64(failed) / float64(out.Recent)
	if len(rtts) > 0 {
		rtts = rtts[max(0, len(rtts)-meterRTTs):]
		slices.Sort(rtts)
		out.RTT = rtts[len(rtts)/2]
	}
	return out
}
