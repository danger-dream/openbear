package routing

import (
	"math"
	"slices"
	"time"
)

// Link metric parameters (README, "Link metrics").
const (
	// A link's round trip is the median of its last rttMedian answered
	// probes, and its jitter the median of their distances from the round
	// trip they made: on a TCP link a lost segment holds the probes behind
	// it for a retransmission timeout, and such late answers, up to two in
	// any rttMedian, leave both as they were. (An average of every answer
	// rose by several times the round trip on one of them and stayed up
	// longer than switchHold.) Round trips that vary all the time still
	// show as jitter, and a change shows from its third probe.
	rttMedian = 5
	// The loss rate is the lower of the shares lost in the last lossShort
	// and the last lossWindow probes: a few losses close together count
	// by their share of lossWindow probes until losses persist, and
	// lossShort good probes clear the rate either way.
	lossShort  = 10
	lossWindow = 30
	downAfter  = 3 // consecutive lost probes that make a link down
	// A new link is up after warmSamples answered probes; until then its
	// round trip is the fastest of them, so that one slow first answer, common
	// while the path starts, does not weigh on it for half a minute.
	warmSamples = 3
	penaltyStep = 1000.0
	penaltyMax  = 4000.0
	suppressAt  = 2000.0
	reuseBelow  = 750.0
	halfLife    = 30 * time.Second
	minCost     = 0.1 // ms; a negative bias cannot make a link free

	// A link's advertised cost follows its measured cost only after the two
	// differ by more than switchRatio and switchMargin for switchHold.
	switchRatio  = 0.8
	switchMargin = 10.0 // ms
	switchHold   = 5 * time.Second
)

const (
	StateUp         = "up"
	StateDown       = "down"
	StateSuppressed = "suppressed"
)

// metric is one relay neighbor as this relay measures it.
type metric struct {
	samples      int
	recent, devs []float64 // the last rttMedian answered round trips and their distances from rtt, ms
	rtt, jitter  float64   // ms
	window       []bool    // the last lossWindow probes, true when lost
	consec       int
	down         bool
	penalty      float64
	penaltyAt    time.Time
	suppressed   bool
	bias         float64

	// What this relay advertises for the link, and since when the measured
	// cost has differed from it enough to replace it.
	advState string
	advCost  float64
	pending  time.Time
}

func newMetric(bias float64) *metric { return &metric{bias: bias, advState: StateDown} }

// decay lets the penalty halve every halfLife and applies suppression.
func (m *metric) decay(now time.Time) {
	if m.penalty > 0 && !m.penaltyAt.IsZero() && now.After(m.penaltyAt) {
		m.penalty *= math.Pow(0.5, now.Sub(m.penaltyAt).Seconds()/halfLife.Seconds())
		if m.penalty < 1e-3 {
			m.penalty = 0
		}
	}
	m.penaltyAt = now
	m.suppress()
}

func (m *metric) suppress() {
	if m.penalty >= suppressAt {
		m.suppressed = true
	} else if m.penalty < reuseBelow {
		m.suppressed = false
	}
}

// sample takes one probe: its round trip, or lost.
func (m *metric) sample(rtt time.Duration, lost bool, now time.Time) {
	m.decay(now)
	m.window = append(m.window, lost)
	if len(m.window) > lossWindow {
		m.window = m.window[len(m.window)-lossWindow:]
	}
	if lost {
		m.consec++
	} else {
		ms := float64(rtt) / float64(time.Millisecond)
		m.consec = 0
		m.recent = last(append(m.recent, ms))
		switch {
		case m.samples == 0:
			m.rtt = ms
		case m.samples < warmSamples:
			m.rtt = min(m.rtt, ms)
		default:
			m.rtt = median(m.recent)
			m.devs = last(append(m.devs, math.Abs(ms-m.rtt)))
			m.jitter = median(m.devs)
		}
		m.samples++
	}
	m.enterDown(m.consec >= downAfter)
}

// gone is the link's path closing: down at once.
func (m *metric) gone(now time.Time) {
	m.decay(now)
	m.consec = max(m.consec, downAfter)
	m.enterDown(true)
}

func (m *metric) enterDown(down bool) {
	if down && !m.down {
		m.penalty = min(penaltyMax, m.penalty+penaltyStep)
	}
	m.down = down
	m.suppress()
}

func (m *metric) loss() float64 {
	return min(lostShare(m.window[max(0, len(m.window)-lossShort):]), lostShare(m.window))
}

func last(v []float64) []float64 { return v[max(0, len(v)-rttMedian):] }

// median is the middle value of v, the upper one of the two for an even count.
func median(v []float64) float64 {
	s := slices.Clone(v)
	slices.Sort(s)
	return s[len(s)/2]
}

func lostShare(w []bool) float64 {
	if len(w) == 0 {
		return 0
	}
	n := 0
	for _, l := range w {
		if l {
			n++
		}
	}
	return float64(n) / float64(len(w))
}

func (m *metric) state() string {
	switch {
	case m.suppressed:
		return StateSuppressed
	case m.down || m.samples < warmSamples:
		return StateDown
	}
	return StateUp
}

// cost is the measured cost in ms, +Inf unless the link is up.
func (m *metric) cost() float64 {
	if m.state() != StateUp {
		return math.Inf(1)
	}
	return max(minCost, m.rtt+2*m.jitter+1000*m.loss()+m.bias)
}

// differs says whether a cost moved far enough from the advertised one
// to replace it: by more than switchMargin and by the switchRatio factor
// either way.
func differs(measured, advertised float64) bool {
	return math.Abs(measured-advertised) > switchMargin && (measured < switchRatio*advertised || advertised < switchRatio*measured)
}

// settle brings the advertised state and cost up to date and says whether
// they changed. A state change is advertised at once; a cost change only
// after it has lasted switchHold.
func (m *metric) settle(now time.Time) bool {
	st := m.state()
	if st != m.advState {
		m.advState, m.advCost, m.pending = st, 0, time.Time{}
		if st == StateUp {
			m.advCost = round1(m.cost())
		}
		return true
	}
	if st != StateUp {
		return false
	}
	c := m.cost()
	if !differs(c, m.advCost) {
		m.pending = time.Time{}
		return false
	}
	if m.pending.IsZero() {
		m.pending = now
	}
	if now.Sub(m.pending) < switchHold {
		return false
	}
	m.advCost, m.pending = round1(c), time.Time{}
	return true
}

func round1(v float64) float64 { return math.Round(v*10) / 10 }
