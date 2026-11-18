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
	// longer than Tuning.Hold.) Round trips that vary all the time still
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
	// A link between relays back from down stays down for probationBase,
	// doubled for each further down within flapWindow, up to probationMax:
	// one that answers again for a moment would draw routes to it and drop
	// them, again and again.
	probationBase = 30 * time.Second
	probationMax  = 8 * time.Minute
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
	kept         bool // suppressed, yet in use: a member's best link when every one that works is suppressed
	bias         float64
	narrow       float64 // narrowCost while the link carries less than the network MTU from this end, else 0
	tune         *Tuning // the router's

	// probation is set on a link between relays: back from down, it is held
	// down until heldUntil, after the downs it had within flapWindow;
	// heldFor is the hold just begun, for the router's event.
	probation bool
	downs     []time.Time
	heldUntil time.Time
	held      bool
	heldFor   time.Duration

	// What this relay advertises for the link, and since when the measured
	// cost has differed from it enough to replace it.
	advState string
	advCost  float64
	pending  time.Time
}

func newMetric(bias float64, t *Tuning) *metric {
	return &metric{bias: bias, tune: t, advState: StateDown}
}

// decay lets the penalty halve every halfLife and applies suppression.
func (m *metric) decay(now time.Time) {
	if m.penalty > 0 && !m.penaltyAt.IsZero() && now.After(m.penaltyAt) {
		m.penalty *= math.Pow(0.5, now.Sub(m.penaltyAt).Seconds()/halfLife.Seconds())
		if m.penalty < 1e-3 {
			m.penalty = 0
		}
	}
	m.penaltyAt = now
	m.held = now.Before(m.heldUntil)
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
	m.enterDown(m.consec >= downAfter, now)
}

// gone is the link's path closing: down at once.
func (m *metric) gone(now time.Time) {
	m.decay(now)
	m.consec = max(m.consec, downAfter)
	m.enterDown(true, now)
}

func (m *metric) enterDown(down bool, now time.Time) {
	if down && !m.down {
		m.penalty = min(penaltyMax, m.penalty+penaltyStep)
		m.downs = append(slices.DeleteFunc(m.downs, func(t time.Time) bool { return now.Sub(t) >= flapWindow }), now)
	}
	if !down && m.down && m.probation {
		m.heldFor = min(probationMax, probationBase<<min(max(len(m.downs)-1, 0), 8))
		m.heldUntil = now.Add(m.heldFor)
		m.held = true
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
	case m.suppressed && !m.kept:
		return StateSuppressed
	case m.down || m.held || m.samples < warmSamples:
		return StateDown
	}
	return StateUp
}

// cost is the measured cost in ms, +Inf unless the link is up.
func (m *metric) cost() float64 {
	if m.state() != StateUp {
		return math.Inf(1)
	}
	return m.measured()
}

// measured is the cost the link's probes give, whatever its state.
func (m *metric) measured() float64 {
	return max(minCost, m.rtt+m.tune.JitterWeight*m.jitter+m.tune.LossWeight*m.loss()+m.bias+m.narrow)
}

// working says whether the link answers: warmed up and not down, suppressed or not.
func (m *metric) working() bool {
	return !m.down && m.samples >= warmSamples
}

// differs says whether a cost moved far enough from the advertised one
// to replace it: by more than AdvertMs and, with AdvertPct, by more than
// that share of the larger of the two.
func (t *Tuning) differs(measured, advertised float64) bool {
	d := math.Abs(measured - advertised)
	return d > t.AdvertMs && (t.AdvertPct == 0 || d > max(measured, advertised)*t.AdvertPct/100)
}

// settle brings the advertised state and cost up to date and says whether
// they changed. A state change is advertised at once; a cost change only
// after it has lasted Tuning.Hold.
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
	if !m.tune.differs(c, m.advCost) {
		m.pending = time.Time{}
		return false
	}
	if m.pending.IsZero() {
		m.pending = now
	}
	if now.Sub(m.pending) < m.tune.Hold {
		return false
	}
	m.advCost, m.pending = round1(c), time.Time{}
	return true
}

func round1(v float64) float64 { return math.Round(v*10) / 10 }
