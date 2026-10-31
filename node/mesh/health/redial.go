package health

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"slices"
	"strings"
	"sync"
	"time"
)

// Redialing a degraded TCP or TLS path (README.md, "Redial"). Some networks
// put each connection on one of several lines by its addresses and ports;
// a connection on a line that loses or delays stays slow for as long as it
// lasts, while a new one from another source port may well get a good
// line. The end that dials a relay or direct link, and a member over its
// entry candidates in use, judges every minute of its probes against the
// link's own baseline, and after three bad minutes in a row moves the path
// to a new connection (transport.Bind.Redial), no more often than its
// backoff allows.

// RedialParams are the time constants and sample sizes of redialing.
type RedialParams struct {
	// Period is one judgement: a period with MinOutcomes probes answered or
	// timed out, MinAnswers of them answered, is judged; others are not
	// and break both runs below.
	Period                  time.Duration
	MinOutcomes, MinAnswers int
	// The baseline is the least median of a judged period within Window,
	// once Window holds BaselinePeriods judged periods. Each Bucket keeps
	// its least median and count, also in state.json, so the baseline
	// survives a restart.
	Bucket, Window  time.Duration
	BaselinePeriods int
	// BadPeriods bad periods in a row redial; GoodPeriods healthy ones in
	// a row end the backoff.
	BadPeriods, GoodPeriods int
	// Backoff[i] is the least time from a redial to the next after i+1
	// redials without the link becoming healthy; the last one repeats. The
	// first is also the least time between any two redials.
	Backoff []time.Duration
	// SaveEvery: changed baselines are written at most this often.
	SaveEvery time.Duration
}

func DefaultRedialParams() RedialParams {
	return RedialParams{
		Period: time.Minute, MinOutcomes: 20, MinAnswers: 10,
		Bucket: time.Hour, Window: 6 * time.Hour, BaselinePeriods: 30,
		BadPeriods: 3, GoodPeriods: 10,
		Backoff:   []time.Duration{10 * time.Minute, 20 * time.Minute, 40 * time.Minute, 60 * time.Minute},
		SaveEvery: 10 * time.Minute,
	}
}

var (
	redialMu     sync.Mutex
	redialParams = DefaultRedialParams()
)

// SetRedialParams replaces the redial parameters of managers created
// afterwards; tests shorten them. It returns a function restoring them.
func SetRedialParams(p RedialParams) (restore func()) {
	redialMu.Lock()
	defer redialMu.Unlock()
	old := redialParams
	redialParams = p
	return func() {
		redialMu.Lock()
		defer redialMu.Unlock()
		redialParams = old
	}
}

func currentRedialParams() RedialParams {
	redialMu.Lock()
	defer redialMu.Unlock()
	return redialParams
}

// Thresholds against the baseline b (or, without one, the period's own
// median for slowness):
//   - slow answer: above max(1.5*b, b+30 ms);
//   - bad period: median above max(1.5*b, b+30 ms), or at least 10% of its
//     probes slow or timed out;
//   - healthy period: not bad, its median at most max(1.25*b, b+15 ms).
//     Healthy ones in a row end the backoff; a period neither bad nor
//     healthy breaks both runs.
const (
	slowFactor, slowMargin       = 1.5, 30 * time.Millisecond
	healthyFactor, healthyMargin = 1.25, 15 * time.Millisecond
	badShare                     = .10
	// redialTimeout bounds one redial: dial, TLS and preamble.
	redialTimeout = 15 * time.Second
	// swapWait is how long after a redial the other end may still answer
	// over, and be retired from, the old connection (transport's
	// redialGrace).
	swapWait = 30 * time.Second
)

func above(b time.Duration, f float64, margin time.Duration) time.Duration {
	return max(time.Duration(float64(b)*f), b+margin)
}

// redialBackend is a backend that can redial a dialed stream path.
type redialBackend interface {
	// Redial moves path to a new connection and returns the local
	// addresses it moved from and to.
	Redial(ctx context.Context, peer string, path Path) (from, to string, err error)
	// Retire closes the connection the last redial of path moved from.
	Retire(path Path)
}

type bucket struct {
	at      time.Time // start
	min     time.Duration
	periods int
}

// trend is what a redialable path's probes have shown, period by period.
type trend struct {
	endpoint string
	start    time.Time // of the period being filled; zero before an outcome
	rtts     []time.Duration
	lost     int
	buckets  []bucket // oldest first
	saved    bool     // buckets unchanged since written

	bad, good int // judged periods in a row
	// The last judged period.
	median   time.Duration
	share    float64
	rttBad   bool
	baseline time.Duration // as it stood when that period was judged

	level, redials int
	last           time.Time
	inflight       bool
	// After a redial: when it moved the path, and whether the other end has
	// asked for it to be prepared again since.
	swapped    time.Time
	sawReclaim bool
}

func redialable(endpoint string) bool {
	return strings.HasPrefix(endpoint, "tcp://") || strings.HasPrefix(endpoint, "tls://")
}

// trendOf returns p's trend if p is a path this node may redial: a dialed
// TCP or TLS link or entry candidate.
func (m *Manager) trendOf(p *candidateState) *trend {
	if !redialable(p.config.Endpoint) || p.config.Punch || (m.links[p.config.Peer] != p && m.candidates[p.config.ID] != p) {
		return nil
	}
	if p.trend == nil || p.trend.endpoint != p.config.Endpoint {
		p.trend = &trend{endpoint: p.config.Endpoint}
		if s, ok := m.baselines[p.config.ID]; ok && s.Endpoint == p.config.Endpoint {
			for _, h := range s.Buckets {
				p.trend.buckets = append(p.trend.buckets, bucket{at: time.Unix(h.At, 0), min: time.Duration(h.MinUs) * time.Microsecond, periods: h.Periods})
			}
			p.trend.saved = true
		}
	}
	return p.trend
}

// observe takes one outcome of p's probes: an answer after rtt, or a
// timeout.
func (m *Manager) observe(p *candidateState, at time.Time, rtt time.Duration, lost bool) {
	t := m.trendOf(p)
	if t == nil {
		return
	}
	m.rollTrend(p, t, at)
	if lost {
		t.lost++
	} else {
		t.rtts = append(t.rtts, rtt)
	}
}

// expire counts p's timed-out probes, in its meter and its trend.
func (m *Manager) expire(p *candidateState, now time.Time) {
	before := p.meter.timeouts
	p.meter.expired(p.track, now)
	for i := before; i < p.meter.timeouts; i++ {
		m.observe(p, now, 0, true)
	}
}

// rollTrend judges t's period once it is over, and redials p if due.
func (m *Manager) rollTrend(p *candidateState, t *trend, now time.Time) {
	rp := m.redial
	if rp.Period <= 0 {
		return // not configured: a manager built in a test
	}
	if t.start.IsZero() {
		t.start = now
		return
	}
	if now.Sub(t.start) < rp.Period {
		return
	}
	t.judge(rp)
	if periods := int(now.Sub(t.start) / rp.Period); periods > 1 {
		// Whole periods without an outcome.
		t.bad, t.good = 0, 0
		t.start = t.start.Add(time.Duration(periods) * rp.Period)
	} else {
		t.start = t.start.Add(rp.Period)
	}
	t.rtts, t.lost = t.rtts[:0], 0
	if t.level > 0 && t.good >= rp.GoodPeriods {
		t.level = 0
	}
	if t.bad >= rp.BadPeriods && !t.inflight && t.swapped.IsZero() && p.path.ID != "" && p.ready && (t.last.IsZero() || now.Sub(t.last) >= t.wait(rp)) {
		m.startRedial(p, t)
	}
}

// wait is the least time from t's last redial to the next.
func (t *trend) wait(rp RedialParams) time.Duration {
	if len(rp.Backoff) == 0 {
		return 0
	}
	return rp.Backoff[min(max(t.level, 1), len(rp.Backoff))-1]
}

func (t *trend) judge(rp RedialParams) {
	n := len(t.rtts) + t.lost
	if n < rp.MinOutcomes || len(t.rtts) < rp.MinAnswers {
		t.bad, t.good = 0, 0
		return
	}
	sorted := slices.Clone(t.rtts)
	slices.Sort(sorted)
	med := sorted[len(sorted)/2]
	base := t.base(t.start, rp)
	ref := base
	if ref == 0 {
		ref = med
	}
	limit := above(ref, slowFactor, slowMargin)
	slow := t.lost
	for _, r := range t.rtts {
		if r > limit {
			slow++
		}
	}
	share := float64(slow) / float64(n)
	rttBad := base > 0 && med > above(base, slowFactor, slowMargin)
	bad := rttBad || share >= badShare
	healthy := !bad && (base == 0 || med <= above(base, healthyFactor, healthyMargin))
	switch {
	case bad:
		t.bad++
		t.good = 0
	case healthy:
		t.bad = 0
		t.good++
	default:
		t.bad, t.good = 0, 0
	}
	t.median, t.share, t.rttBad, t.baseline = med, share, rttBad, base
	t.add(t.start, med, rp)
}

// base is the baseline at at: zero until Window holds BaselinePeriods
// judged periods.
func (t *trend) base(at time.Time, rp RedialParams) time.Duration {
	var least time.Duration
	periods := 0
	for _, b := range t.buckets {
		if at.Sub(b.at) >= rp.Window {
			continue
		}
		periods += b.periods
		if least == 0 || b.min < least {
			least = b.min
		}
	}
	if periods < rp.BaselinePeriods {
		return 0
	}
	return least
}

func (t *trend) add(at time.Time, med time.Duration, rp RedialParams) {
	start := at.Truncate(rp.Bucket)
	if n := len(t.buckets); n > 0 && t.buckets[n-1].at.Equal(start) {
		b := &t.buckets[n-1]
		b.periods++
		b.min = min(b.min, med)
	} else {
		t.buckets = append(t.buckets, bucket{at: start, min: med, periods: 1})
	}
	t.buckets = slices.DeleteFunc(t.buckets, func(b bucket) bool { return at.Sub(b.at) >= rp.Window+rp.Bucket })
	t.saved = false
}

type redialDone struct {
	candidate string
	path      Path
	from, to  string
	err       error
}

func (m *Manager) startRedial(p *candidateState, t *trend) {
	rb, ok := m.backend.(redialBackend)
	if !ok {
		return
	}
	t.inflight = true
	id, peer, path := p.config.ID, p.config.Peer, p.path
	go func() {
		ctx, cancel := context.WithTimeout(m.ctx, redialTimeout)
		from, to, err := rb.Redial(ctx, peer, path)
		cancel()
		select {
		case m.input <- redialDone{id, path, from, to, err}:
		case <-m.ctx.Done():
		}
	}()
}

// redialDetail is the event detail of a redial of p (README.md).
func redialDetail(p *candidateState, t *trend, m *Manager) string {
	var b strings.Builder
	b.WriteString(p.config.ID)
	if m.candidates[p.config.ID] == p {
		b.WriteString(" relay=" + p.config.Peer)
	}
	reason := "slow"
	if t.rttBad {
		reason = "rtt"
	}
	fmt.Fprintf(&b, " reason=%s rtt=%sms baseline=", reason, msText(t.median))
	if t.baseline > 0 {
		b.WriteString(msText(t.baseline) + "ms")
	} else {
		b.WriteString("none")
	}
	fmt.Fprintf(&b, " slow=%.0f%%", t.share*100)
	return b.String()
}

func msText(d time.Duration) string {
	return fmt.Sprintf("%.1f", float64(d)/float64(time.Millisecond))
}

func port(addr string) string {
	if i := strings.LastIndexByte(addr, ':'); i >= 0 {
		return addr[i+1:]
	}
	return addr
}

func (m *Manager) redialed(v redialDone, now time.Time) {
	p := m.find(v.candidate)
	if p == nil || p.trend == nil {
		return
	}
	t := p.trend
	t.inflight = false
	t.last = now
	t.redials++
	t.level = min(t.level+1, max(len(m.redial.Backoff), 1))
	t.bad = 0
	detail := redialDetail(p, t, m)
	if v.err != nil {
		m.emit("link.redial.failed", detail+" error="+v.err.Error())
		return
	}
	m.emit("link.redial", detail+" port="+port(v.from)+"->"+port(v.to))
	if p.path == v.path {
		// What the next periods show is the new connection.
		t.start, t.rtts, t.lost = now, t.rtts[:0], 0
		t.swapped, t.sawReclaim = now, false
	}
}

// swapping says whether p was redialed within swapWait: the other end
// asking for it to be prepared again then means a new connection, not a
// restart, and the routes stay on it meanwhile.
func (p *candidateState) swapping(now time.Time) bool {
	t := p.trend
	if t == nil || t.swapped.IsZero() {
		return false
	}
	if now.Sub(t.swapped) >= swapWait {
		t.swapped, t.sawReclaim = time.Time{}, false
		p.reprepare = false
		return false
	}
	return true
}

// reclaimAfterRedial: the other end asked for p, just redialed, to be
// prepared again, as for any new connection of a peer.
func (m *Manager) reclaimAfterRedial(p *candidateState) {
	p.trend.sawReclaim = true
	p.reprepare = true
}

// settledAfterRedial: the other end answers over the new connection
// without asking for it to be prepared: it sends over it now, and the old
// one can go.
func (m *Manager) settledAfterRedial(p *candidateState) {
	if rb, ok := m.backend.(redialBackend); ok {
		rb.Retire(p.path)
	}
	p.trend.swapped, p.trend.sawReclaim = time.Time{}, false
}

// redialTick judges the periods of paths without outcomes and writes
// changed baselines.
func (m *Manager) redialTick(now time.Time) {
	for _, p := range m.allPaths() {
		if t := m.trendOf(p); t != nil {
			m.rollTrend(p, t, now)
			p.swapping(now)
		}
	}
	if m.store.path == "" || now.Sub(m.baselineSaved) < m.redial.SaveEvery {
		return
	}
	m.baselineSaved = now
	changed := false
	all := map[string]savedTrend{}
	for _, p := range m.allPaths() {
		t := p.trend
		if t == nil || m.trendOf(p) != t || len(t.buckets) == 0 {
			continue
		}
		changed = changed || !t.saved
		s := savedTrend{Endpoint: t.endpoint}
		for _, b := range t.buckets {
			s.Buckets = append(s.Buckets, savedBucket{At: b.at.Unix(), MinUs: b.min.Microseconds(), Periods: b.periods})
		}
		all[p.config.ID] = s
	}
	if !changed && len(all) == len(m.baselines) {
		return
	}
	if err := UpdateState(m.store.path, baselineField, all); err != nil {
		m.emit("state.error", "baselines: "+err.Error())
		return
	}
	for _, p := range m.allPaths() {
		if p.trend != nil {
			p.trend.saved = true
		}
	}
	m.baselines = all
}

// What state.json keeps of each redialable path's baseline, by candidate
// ID ("link:<peer>" for a link).
const baselineField = "linkBaselines"

type savedTrend struct {
	Endpoint string        `json:"endpoint"`
	Buckets  []savedBucket `json:"buckets"`
}
type savedBucket struct {
	At      int64 `json:"at"` // Unix seconds
	MinUs   int64 `json:"minUs"`
	Periods int   `json:"periods"`
}

func loadBaselines(path string) map[string]savedTrend {
	out := map[string]savedTrend{}
	if path == "" {
		return out
	}
	stateMu.Lock()
	raw, err := os.ReadFile(path)
	stateMu.Unlock()
	if err != nil {
		return out
	}
	var all map[string]json.RawMessage
	if json.Unmarshal(raw, &all) != nil {
		return out
	}
	json.Unmarshal(all[baselineField], &out)
	if out == nil {
		out = map[string]savedTrend{}
	}
	return out
}

// redialStatus is what status shows of p's redials; nil for a path this
// node does not redial.
func (m *Manager) redialStatus(p *candidateState, now time.Time) *RedialStatus {
	t := m.trendOf(p)
	if t == nil {
		return nil
	}
	s := &RedialStatus{Baseline: t.base(now, m.redial), Level: t.level, Redials: t.redials, Degraded: t.bad, Last: t.last}
	if !t.last.IsZero() {
		s.Next = t.last.Add(t.wait(m.redial))
	}
	return s
}
