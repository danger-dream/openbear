// Package probe measures round trips, loss and jitter to candidate peers
// without opening a tunnel or a session: reflector requests on UDP where a
// public relay listens on it, TCP connects otherwise.
package probe

import (
	"context"
	"errors"
	"fmt"
	"math"
	mrand "math/rand/v2"
	"net"
	"net/netip"
	"slices"
	"strconv"
	"sync"
	"time"

	"openbear/node/mesh/transport"
)

const (
	MaxTargets      = 64
	DefaultInterval = 5 * time.Minute
	MinInterval     = 5 * time.Second
	MaxInterval     = time.Hour
	SampleCount     = 5
	DefaultTimeout  = 2 * time.Second
	MaxParallel     = 4
	MinSpacing      = 50 * time.Millisecond
)

const (
	MethodUDP = "udp"
	MethodTCP = "tcp"
)

// Config is the probe section of config.json.
type Config struct {
	Targets   []Target `json:"targets,omitempty"`
	IntervalS int      `json:"intervalS,omitempty"`
}

// Target is one candidate peer to measure.
type Target struct {
	Peer string `json:"peer"`
	Host string `json:"host"`
	UDP  int    `json:"udp,omitempty"`
	TCP  int    `json:"tcp,omitempty"`
	TLS  int    `json:"tls,omitempty"`
}

// Result is one entry of status.json's probes list.
type Result struct {
	Peer     string   `json:"peer"`
	RTTMs    *float64 `json:"rttMs"`
	Loss     float64  `json:"loss"`
	JitterMs *float64 `json:"jitterMs"`
	Method   string   `json:"method"`
	At       int64    `json:"at"`
}

// Check rejects a probe config the runner cannot execute.
func (c *Config) Check() error {
	if c == nil {
		return nil
	}
	if len(c.Targets) > MaxTargets {
		return fmt.Errorf("probe.targets: at most %d", MaxTargets)
	}
	if c.IntervalS < 0 || c.IntervalS > int(MaxInterval/time.Second) {
		return errors.New("probe.intervalS is out of range")
	}
	seen := map[string]bool{}
	for i, t := range c.Targets {
		if t.Peer == "" || seen[t.Peer] {
			return fmt.Errorf("probe.targets[%d].peer is empty or repeated", i)
		}
		seen[t.Peer] = true
		if t.Host == "" {
			return fmt.Errorf("probe.targets[%d].host is empty", i)
		}
		for name, p := range map[string]int{"udp": t.UDP, "tcp": t.TCP, "tls": t.TLS} {
			if p < 0 || p > 65535 {
				return fmt.Errorf("probe.targets[%d].%s is not a port", i, name)
			}
		}
		if t.UDP == 0 && t.TCP == 0 && t.TLS == 0 {
			return fmt.Errorf("probe.targets[%d] has no port", i)
		}
	}
	return nil
}

func (c *Config) interval() time.Duration {
	if c == nil || c.IntervalS <= 0 {
		return DefaultInterval
	}
	d := time.Duration(c.IntervalS) * time.Second
	return min(max(d, MinInterval), MaxInterval)
}

// Service runs periodic lightweight probes in the background.
type Service struct {
	mu      sync.Mutex
	key     string
	cancel  context.CancelFunc
	done    chan struct{}
	results map[string]Result
	order   []string

	// Hooks for tests; nil uses the real network and clock.
	ProbeUDP func(ctx context.Context, addr netip.AddrPort, count int, timeout time.Duration) ([]time.Duration, string)
	ProbeTCP func(ctx context.Context, addr string, timeout time.Duration) (time.Duration, string)
	Resolve  func(ctx context.Context, host string, timeout time.Duration) (netip.Addr, error)
	Now      func() time.Time
	Jitter   func(d time.Duration) time.Duration
	Spacing  time.Duration
	Timeout  time.Duration
	Samples  int
}

// Apply starts, updates or stops the probe loop for c. Targets that stay in c
// keep their last measured result until the next round updates them.
func (s *Service) Apply(c *Config) {
	s.mu.Lock()
	var targets []Target
	interval := DefaultInterval
	if c != nil {
		targets = slices.Clone(c.Targets)
		if len(targets) > MaxTargets {
			targets = targets[:MaxTargets]
		}
		interval = c.interval()
	}
	k := configKey(targets, interval)
	if k == s.key {
		s.mu.Unlock()
		return
	}
	prevCancel, prevDone := s.cancel, s.done
	s.key = k
	s.cancel, s.done = nil, nil
	keep := map[string]Result{}
	order := make([]string, 0, len(targets))
	for _, t := range targets {
		order = append(order, t.Peer)
		if r, ok := s.results[t.Peer]; ok {
			keep[t.Peer] = r
		}
	}
	s.results = keep
	s.order = order
	var ctx context.Context
	var done chan struct{}
	if len(targets) > 0 {
		ctx, s.cancel = context.WithCancel(context.Background())
		done = make(chan struct{})
		s.done = done
	}
	s.mu.Unlock()

	if prevCancel != nil {
		prevCancel()
		<-prevDone
	}
	if len(targets) > 0 {
		go s.loop(ctx, done, targets, interval)
	}
}

// Close stops the background loop and clears results.
func (s *Service) Close() {
	s.mu.Lock()
	cancel, done := s.cancel, s.done
	s.key = ""
	s.cancel, s.done = nil, nil
	s.results = nil
	s.order = nil
	s.mu.Unlock()
	if cancel != nil {
		cancel()
		<-done
	}
}

// Status returns the latest probe results in target order.
func (s *Service) Status() []Result {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.results) == 0 {
		return nil
	}
	out := make([]Result, 0, len(s.results))
	for _, peer := range s.order {
		if r, ok := s.results[peer]; ok {
			out = append(out, r)
		}
	}
	return out
}

// RunRound runs one round synchronously for targets and stores the results.
func (s *Service) RunRound(ctx context.Context, targets []Target) []Result {
	if len(targets) > MaxTargets {
		targets = targets[:MaxTargets]
	}
	results := s.measureAll(ctx, targets)
	s.mu.Lock()
	if s.results == nil {
		s.results = map[string]Result{}
	}
	if len(s.order) == 0 {
		for _, t := range targets {
			s.order = append(s.order, t.Peer)
		}
	}
	for _, r := range results {
		s.results[r.Peer] = r
	}
	s.mu.Unlock()
	return results
}

func (s *Service) loop(ctx context.Context, done chan struct{}, targets []Target, interval time.Duration) {
	defer close(done)
	initial := s.jitter(min(interval/10, 5*time.Second))
	if initial > 0 {
		select {
		case <-ctx.Done():
			return
		case <-time.After(initial):
		}
	}
	for {
		if ctx.Err() != nil {
			return
		}
		results := s.measureAll(ctx, targets)
		if ctx.Err() != nil {
			return
		}
		s.mu.Lock()
		for _, r := range results {
			if slices.Contains(s.order, r.Peer) {
				s.results[r.Peer] = r
			}
		}
		s.mu.Unlock()
		wait := interval + s.jitter(interval/5)
		select {
		case <-ctx.Done():
			return
		case <-time.After(wait):
		}
	}
}

func (s *Service) jitter(span time.Duration) time.Duration {
	if s.Jitter != nil {
		return s.Jitter(span)
	}
	if span <= 0 {
		return 0
	}
	return time.Duration(mrand.Int64N(int64(span)))
}

func (s *Service) now() time.Time {
	if s.Now != nil {
		return s.Now()
	}
	return time.Now()
}

func (s *Service) measureAll(ctx context.Context, targets []Target) []Result {
	out := make([]Result, len(targets))
	slots := make(chan struct{}, MaxParallel)
	spacing := s.Spacing
	if spacing == 0 {
		spacing = MinSpacing
	}
	var rateMu sync.Mutex
	var lastStart time.Time
	var wg sync.WaitGroup
	for i, t := range targets {
		wg.Add(1)
		go func(i int, t Target) {
			defer wg.Done()
			select {
			case slots <- struct{}{}:
			case <-ctx.Done():
				return
			}
			defer func() { <-slots }()
			if spacing > 0 {
				rateMu.Lock()
				now := time.Now()
				wait := time.Duration(0)
				if !lastStart.IsZero() && now.Sub(lastStart) < spacing {
					wait = spacing - now.Sub(lastStart)
				}
				lastStart = now.Add(wait)
				rateMu.Unlock()
				if wait > 0 {
					select {
					case <-ctx.Done():
						return
					case <-time.After(wait):
					}
				}
			}
			out[i] = s.measureOne(ctx, t)
		}(i, t)
	}
	wg.Wait()
	return out
}

func (s *Service) measureOne(ctx context.Context, t Target) Result {
	count := s.Samples
	if count <= 0 {
		count = SampleCount
	}
	timeout := s.Timeout
	if timeout <= 0 {
		timeout = DefaultTimeout
	}
	method := MethodUDP
	tcpPort := t.TCP
	if tcpPort == 0 {
		tcpPort = t.TLS
	}
	if t.UDP == 0 {
		method = MethodTCP
	}
	resolve := s.Resolve
	if resolve == nil {
		resolve = defaultResolve
	}
	addr, err := resolve(ctx, t.Host, timeout)
	if err != nil {
		return Result{Peer: t.Peer, Loss: 1, Method: method, At: s.now().UnixMilli()}
	}
	var rtts []time.Duration
	if t.UDP > 0 {
		probeUDP := s.ProbeUDP
		if probeUDP == nil {
			probeUDP = transport.ProbeUDP
		}
		rtts, _ = probeUDP(ctx, netip.AddrPortFrom(addr, uint16(t.UDP)), count, timeout)
		if len(rtts) == 0 && tcpPort > 0 {
			method = MethodTCP
			rtts = s.probeTCPMany(ctx, net.JoinHostPort(addr.String(), strconv.Itoa(tcpPort)), count, timeout)
		}
	} else if tcpPort > 0 {
		rtts = s.probeTCPMany(ctx, net.JoinHostPort(addr.String(), strconv.Itoa(tcpPort)), count, timeout)
	}
	return Summarize(t.Peer, method, rtts, count, s.now())
}

func (s *Service) probeTCPMany(ctx context.Context, addr string, count int, timeout time.Duration) []time.Duration {
	probeTCP := s.ProbeTCP
	if probeTCP == nil {
		probeTCP = transport.ProbeTCP
	}
	var rtts []time.Duration
	for i := 0; i < count && ctx.Err() == nil; i++ {
		if took, state := probeTCP(ctx, addr, timeout); state == transport.ProbeOK {
			rtts = append(rtts, took)
		}
	}
	return rtts
}

// Summarize computes the median RTT, median-deviation jitter and loss rate
// for one target's samples.
func Summarize(peer, method string, rtts []time.Duration, sent int, at time.Time) Result {
	if sent <= 0 {
		sent = SampleCount
	}
	loss := math.Round(float64(max(0, sent-len(rtts)))/float64(sent)*1000) / 1000
	out := Result{Peer: peer, Loss: loss, Method: method, At: at.UnixMilli()}
	if len(rtts) == 0 {
		return out
	}
	ms := make([]float64, len(rtts))
	for i, d := range rtts {
		ms[i] = float64(d.Microseconds()) / 1000
	}
	rtt := median(ms)
	devs := make([]float64, len(ms))
	for i, v := range ms {
		devs[i] = math.Abs(v - rtt)
	}
	jitter := median(devs)
	rttRound := round1(rtt)
	jitterRound := round1(jitter)
	out.RTTMs = &rttRound
	out.JitterMs = &jitterRound
	return out
}

func median(v []float64) float64 {
	s := slices.Clone(v)
	slices.Sort(s)
	return s[len(s)/2]
}

func round1(v float64) float64 { return math.Round(v*10) / 10 }

func defaultResolve(ctx context.Context, host string, timeout time.Duration) (netip.Addr, error) {
	if a, err := netip.ParseAddr(host); err == nil {
		return a.Unmap(), nil
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	addrs, err := net.DefaultResolver.LookupNetIP(ctx, "ip", host)
	if err != nil || len(addrs) == 0 {
		return netip.Addr{}, errors.New("unresolved")
	}
	for _, a := range addrs {
		if a.Unmap().Is4() {
			return a.Unmap(), nil
		}
	}
	return addrs[0], nil
}

func configKey(targets []Target, interval time.Duration) string {
	var b []byte
	b = strconv.AppendInt(b, int64(interval), 10)
	for _, t := range targets {
		b = append(b, '|')
		b = append(b, t.Peer...)
		b = append(b, '@')
		b = append(b, t.Host...)
		b = append(b, ':')
		b = strconv.AppendInt(b, int64(t.UDP), 10)
		b = append(b, ',')
		b = strconv.AppendInt(b, int64(t.TCP), 10)
		b = append(b, ',')
		b = strconv.AppendInt(b, int64(t.TLS), 10)
	}
	return string(b)
}
