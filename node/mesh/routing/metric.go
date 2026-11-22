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
	// any rttMedian, leave both as they were.
	rttMedian = 5
	// Loss combines a long 100-probe window (first single loss ignored) with
	// a short 10-probe window that triggers immediately once at least 3 of
	// the last 10 probes are lost, and a slow rate that counts every lost
	// probe with a half-life of slowLossHalfLife, so a link that drops a
	// packet now and then keeps that cost through its clean spells.
	lossShort        = 10
	lossShortTrigger = 3
	lossWindow       = 100
	slowLossHalfLife = 15 * time.Minute
	// slowLossMaxStep caps the weight one probe gets after a pause.
	slowLossMaxStep = 5 * time.Second
	downAfter       = 3 // consecutive lost probes that make a link down
	// A new link is up after warmSamples answered probes; until then its
	// round trip is the fastest of them, so that one slow first answer, common
	// while the path starts, does not weigh on it.
	warmSamples = 3
	penaltyStep = 1000.0
	penaltyMax  = 4000.0
	suppressAt  = 2000.0
	reuseBelow  = 750.0
	halfLife    = 30 * time.Second
	minCost     = 0.1 // ms; a negative bias cannot make a link free

	// Advertise a pure cost change once it moves by at least 2% of the larger
	// value and at least 2 ms.
	advertChangePct   = 0.02
	advertChangeMinMs = 2.0
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
	window       [lossWindow]bool
	winPos       int
	winLen       int
	lost100      int
	consec       int
	slowLoss     float64   // every lost probe, outages left out, halving each slowLossHalfLife
	slowKept     float64   // slowLoss as of the last answer: what an outage's run of losses goes back to
	slowAt       time.Time // the probe slowLoss last took
	down         bool
	penalty      float64
	penaltyAt    time.Time
	suppressed   bool
	kept         bool // suppressed, yet in use: a member's best link when every one that works is suppressed
	bias         float64
	narrow       float64 // narrowCost while the link carries less than the network MTU from this end, else 0
	tune         *Tuning // the router's

	// What this relay advertises for the link.
	advState string
	advCost  float64
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
	if !lost && m.down {
		m.forgetOutage()
	}
	m.trackSlowLoss(lost, now)
	if m.winLen == lossWindow {
		if m.window[m.winPos] {
			m.lost100--
		}
	} else {
		m.winLen++
	}
	m.window[m.winPos] = lost
	if lost {
		m.lost100++
	}
	m.winPos++
	if m.winPos == lossWindow {
		m.winPos = 0
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

// forgetOutage takes the probes a link lost while down, the run before its
// first answer, out of its loss rate: the outage has its own cost, the flap
// penalty and the start penalty it weighs on routes with, and counted as
// loss as well it held a recovered link at 10 or 20 times its cost for a
// minute and more.
func (m *metric) forgetOutage() {
	for i := 1; i <= m.winLen; i++ {
		idx := (m.winPos - i + lossWindow) % lossWindow
		if !m.window[idx] {
			return
		}
		m.window[idx] = false
		m.lost100--
	}
}

// gone is the link's path closing: down at once.
func (m *metric) gone(now time.Time) {
	m.decay(now)
	m.consec = max(m.consec, downAfter)
	m.enterDown(true)
}

// trackSlowLoss moves slowLoss toward this probe, weighted by the time since
// the one before. The probes of an outage are not loss: enterDown takes the
// run that made it back out, and none counts while the link is down, nor
// does the time it was.
func (m *metric) trackSlowLoss(lost bool, now time.Time) {
	if m.down {
		if lost {
			return
		}
		m.slowAt = now
	}
	if !m.slowAt.IsZero() {
		if dt := min(now.Sub(m.slowAt), slowLossMaxStep); dt > 0 {
			w := 1 - math.Pow(0.5, dt.Seconds()/slowLossHalfLife.Seconds())
			x := 0.0
			if lost {
				x = 1
			}
			m.slowLoss += w * (x - m.slowLoss)
		}
	}
	m.slowAt = now
	if !lost {
		m.slowKept = m.slowLoss
	}
}

func (m *metric) enterDown(down bool) {
	if down && !m.down {
		m.penalty = min(penaltyMax, m.penalty+penaltyStep)
		m.slowLoss = m.slowKept
	}
	m.down = down
	m.suppress()
}

func (m *metric) loss() float64 {
	return max(m.windowLoss(), m.slowLoss)
}

// windowLoss is the loss rate of the last 100 and 10 probes.
func (m *metric) windowLoss() float64 {
	longLoss := float64(max(0, m.lost100-1)) / float64(lossWindow)
	shortN := min(m.winLen, lossShort)
	lost10 := 0
	for i := 1; i <= shortN; i++ {
		idx := m.winPos - i
		if idx < 0 {
			idx += lossWindow
		}
		if m.window[idx] {
			lost10++
		}
	}
	shortLoss := 0.0
	if lost10 >= lossShortTrigger {
		shortLoss = float64(lost10) / float64(lossShort)
	}
	return max(longLoss, shortLoss)
}

func last(v []float64) []float64 { return v[max(0, len(v)-rttMedian):] }

// median is the middle value of v, the upper one of the two for an even count.
func median(v []float64) float64 {
	s := slices.Clone(v)
	slices.Sort(s)
	return s[len(s)/2]
}

func (m *metric) state() string {
	switch {
	case m.suppressed && !m.kept:
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
	return m.measured()
}

// measured is the cost the link's probes give, whatever its state.
func (m *metric) measured() float64 {
	return max(minCost, m.rtt+m.tune.JitterWeight*m.jitter+m.tune.LossWeight*m.loss()+m.bias+m.narrow+m.tune.HopCostMs)
}

// working says whether the link answers: warmed up and not down, suppressed or not.
func (m *metric) working() bool {
	return !m.down && m.samples >= warmSamples
}

// costDiffers says whether measured moved far enough from advertised to
// publish a new cost: at least max(2% of the larger, 2 ms).
func costDiffers(measured, advertised float64) bool {
	d := math.Abs(measured - advertised)
	thresh := max(advertChangePct*max(measured, advertised), advertChangeMinMs)
	return d >= thresh-1e-9
}

// settle brings the advertised state and cost up to date. changed reports any
// update; stateChanged distinguishes a state transition (advertised promptly
// after advertGap) from a pure cost update (rate-limited to costAdvertGap).
func (m *metric) settle() (changed, stateChanged bool) {
	st := m.state()
	if st != m.advState {
		m.advState, m.advCost = st, 0
		if st == StateUp {
			m.advCost = round1(m.cost())
		}
		return true, true
	}
	if st != StateUp {
		return false, false
	}
	c := m.cost()
	if !costDiffers(c, m.advCost) {
		return false, false
	}
	m.advCost = round1(c)
	return true, false
}

func round1(v float64) float64 { return math.Round(v*10) / 10 }
