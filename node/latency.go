package node

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"io"
	"math"
	"math/rand/v2"
	"net"
	"net/http"
	"net/http/httptrace"
	"net/netip"
	"net/url"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// Latency watch (延迟监测): the controller names targets and an interval; each target is probed on
// its own schedule and every finished minute goes back as {t: latency, minutes: [...]}. The probes
// run as long as the node does, not only while it is connected: the minutes of a time without a
// session wait in memory and go with the next one, so a server cut off shows as lost probes rather
// than as nothing. The controller gives the targets again on every hello; the same ones keep running.

const (
	latencyMinInterval = 3 * time.Second
	latencyMaxInterval = 10 * time.Minute
	latencyMaxTargets  = 32
	latencyMaxTimeout  = 3 * time.Second
	// A TCPing is one SYN. The kernel sends it again after its first retransmission timeout, a
	// second, and a connection made only then is a SYN lost, not a second of latency.
	latencyTCPTimeout = time.Second
	// An HTTP probe may need a connection and a TLS handshake before its request goes.
	latencyHTTPTimeout = 5 * time.Second
	latencyMaxURL      = 2048
	// Finished minutes kept for the next session (about a day of five targets), and how many go in one message.
	latencyQueueMax = 8192
	latencyBatch    = 256
	// A name is looked up again after this; a lookup that fails counts as a lost probe.
	latencyResolveEvery = 5 * time.Minute
)

var errInvalidLatency = errors.New("invalid_latency")

// Mode tcp needs Port, http its URL (whose host is Host).
type latencyTarget struct {
	ID   string `json:"id"`
	Host string `json:"host"`
	Port int    `json:"port,omitempty"`
	Mode string `json:"mode"`
	URL  string `json:"url,omitempty"`
}

type latencyMinute struct {
	TS     int64    `json:"ts"`
	Target string   `json:"target"`
	Sent   int      `json:"sent"`
	Lost   int      `json:"lost"`
	Avg    *float64 `json:"avg"`
	Min    *float64 `json:"min"`
	Max    *float64 `json:"max"`
}

type latencyWatch struct {
	Targets  []latencyTarget
	Interval time.Duration
}

type latencyWatcher struct {
	mu   sync.Mutex
	stop context.CancelFunc
	spec latencyWatch
	// queue: finished minutes not yet delivered, oldest first; seq numbers them.
	queue []queuedMinute
	seq   uint64
	// send delivers to the current session; gen counts the sessions given, so a failed send only
	// forgets the one it was.
	send func([]latencyMinute) error
	gen  uint64
	wake chan struct{}
	// Swapped in tests.
	dial     func(ctx context.Context, network, address string) (net.Conn, error)
	lookupIP func(ctx context.Context, network, host string) ([]netip.Addr, error)
	openEcho func(dst netip.Addr, id int) (echoConn, error)
	now      func() time.Time
}

func newLatencyWatcher() *latencyWatcher {
	return &latencyWatcher{dial: bypassDial, lookupIP: bypassLookup, openEcho: openEcho, now: time.Now}
}

// parseLatencyWatch reads {targets, intervalMs}. allowed, when set, names the only hosts a target may
// be (a front's exits).
func parseLatencyWatch(raw json.RawMessage, allowed func(host string) bool) (latencyWatch, error) {
	var in struct {
		Targets    []latencyTarget `json:"targets"`
		IntervalMs int             `json:"intervalMs"`
	}
	if json.Unmarshal(raw, &in) != nil || len(in.Targets) > latencyMaxTargets {
		return latencyWatch{}, errInvalidLatency
	}
	w := latencyWatch{Interval: time.Duration(in.IntervalMs) * time.Millisecond}
	if w.Interval < latencyMinInterval || w.Interval > latencyMaxInterval {
		return latencyWatch{}, errInvalidLatency
	}
	seen := map[string]bool{}
	for _, t := range in.Targets {
		if t.ID == "" || len(t.ID) > 64 || seen[t.ID] || t.Host == "" || len(t.Host) > 253 || strings.ContainsAny(t.Host, " /:") && !isIP(t.Host) {
			return latencyWatch{}, errInvalidLatency
		}
		switch t.Mode {
		case "tcp":
			if t.Port < 1 || t.Port > 65535 {
				return latencyWatch{}, errInvalidLatency
			}
		case "icmp":
		case "http":
			u, err := url.Parse(t.URL)
			if err != nil || len(t.URL) > latencyMaxURL || u.Scheme != "http" && u.Scheme != "https" || !strings.EqualFold(u.Hostname(), t.Host) {
				return latencyWatch{}, errInvalidLatency
			}
		default:
			return latencyWatch{}, errInvalidLatency
		}
		if allowed != nil && !allowed(t.Host) {
			return latencyWatch{}, errors.New("front_target_refused")
		}
		seen[t.ID] = true
		w.Targets = append(w.Targets, t)
	}
	return w, nil
}

func isIP(s string) bool {
	_, err := netip.ParseAddr(s)
	return err == nil
}

// frontHosts are the hosts a front's addresses point at: its exits, the only places it may reach.
func frontHosts(c Config) func(string) bool {
	if !c.Front {
		return nil
	}
	hosts := map[string]bool{}
	for _, raw := range []string{c.URL, c.Backup} {
		if u, err := url.Parse(raw); err == nil && u.Hostname() != "" {
			hosts[strings.ToLower(u.Hostname())] = true
		}
	}
	return func(h string) bool { return hosts[strings.ToLower(h)] }
}

type queuedMinute struct {
	seq uint64
	m   latencyMinute
}

// watch makes send the way minutes go from now on and spec what is probed: the same targets and
// interval keep running, others replace them (no targets stops it). ctx is the node's, not the session's.
func (w *latencyWatcher) watch(ctx context.Context, spec latencyWatch, send func([]latencyMinute) error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.wake == nil {
		w.wake = make(chan struct{}, 1)
	}
	w.send, w.gen = send, w.gen+1
	if w.stop == nil || !sameLatencyWatch(w.spec, spec) {
		if w.stop != nil {
			w.stop()
			w.stop = nil
		}
		w.spec = spec
		kept := map[string]bool{}
		for _, t := range spec.Targets {
			kept[t.ID] = true
		}
		w.queue = slices.DeleteFunc(w.queue, func(q queuedMinute) bool { return !kept[q.m.Target] })
		if len(spec.Targets) > 0 {
			ctx, w.stop = context.WithCancel(ctx)
			go w.deliver(ctx)
			for _, t := range spec.Targets {
				go w.run(ctx, t, spec.Interval, w.finished)
			}
		}
	}
	w.poke()
}

func sameLatencyWatch(a, b latencyWatch) bool {
	return a.Interval == b.Interval && slices.Equal(a.Targets, b.Targets)
}

func (w *latencyWatcher) poke() {
	select {
	case w.wake <- struct{}{}:
	default:
	}
}

// finished queues a minute; past the limit the oldest go.
func (w *latencyWatcher) finished(m latencyMinute) {
	w.mu.Lock()
	w.seq++
	w.queue = append(w.queue, queuedMinute{w.seq, m})
	if over := len(w.queue) - latencyQueueMax; over > 0 {
		w.queue = slices.Delete(w.queue, 0, over)
	}
	w.mu.Unlock()
	w.poke()
}

// deliver sends what is queued while a session takes it, oldest first and several together; a send
// that fails keeps them for the next session.
func (w *latencyWatcher) deliver(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case <-w.wake:
		}
		for ctx.Err() == nil {
			w.mu.Lock()
			send, gen, n := w.send, w.gen, min(len(w.queue), latencyBatch)
			if send == nil || n == 0 {
				w.mu.Unlock()
				break
			}
			batch, last := make([]latencyMinute, n), w.queue[n-1].seq
			for i, q := range w.queue[:n] {
				batch[i] = q.m
			}
			w.mu.Unlock()
			err := send(batch)
			w.mu.Lock()
			if err != nil {
				if w.gen == gen {
					w.send = nil
				}
				w.mu.Unlock()
				break
			}
			w.queue = slices.DeleteFunc(w.queue, func(q queuedMinute) bool { return q.seq <= last })
			w.mu.Unlock()
		}
	}
}

type latencyAcc struct {
	ts         int64
	sent, lost int
	sum        float64
	min, max   float64
}

func (a *latencyAcc) add(rtt time.Duration, ok bool) {
	a.sent++
	if !ok {
		a.lost++
		return
	}
	ms := float64(rtt.Microseconds()) / 1000
	if a.sent-a.lost == 1 || ms < a.min {
		a.min = ms
	}
	if ms > a.max {
		a.max = ms
	}
	a.sum += ms
}

func (a *latencyAcc) minute(target string) latencyMinute {
	m := latencyMinute{TS: a.ts, Target: target, Sent: a.sent, Lost: a.lost}
	if got := a.sent - a.lost; got > 0 {
		round := func(v float64) *float64 { r := math.Round(v*100) / 100; return &r }
		m.Avg, m.Min, m.Max = round(a.sum/float64(got)), round(a.min), round(a.max)
	}
	return m
}

func (w *latencyWatcher) run(ctx context.Context, t latencyTarget, interval time.Duration, done func(latencyMinute)) {
	timeout := min(interval, latencyMaxTimeout)
	if t.Mode == "http" {
		timeout = min(interval, latencyHTTPTimeout)
	}
	p := &latencyProbe{w: w, t: t}
	defer p.close()
	var acc latencyAcc
	// Targets start spread over the interval, not all at once.
	timer := time.NewTimer(time.Duration(rand.Int64N(int64(interval))))
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-timer.C:
		}
		timer.Reset(interval)
		rtt, ok := p.probe(ctx, timeout)
		if ctx.Err() != nil {
			return
		}
		ts := w.now().Unix() / 60 * 60
		if acc.sent > 0 && acc.ts != ts {
			done(acc.minute(t.ID))
			acc = latencyAcc{}
		}
		acc.ts = ts
		acc.add(rtt, ok)
	}
}

// latencyProbe keeps one target's address and, for ICMP, its echo socket between probes; for HTTP its
// client, whose connections go to httpIP.
type latencyProbe struct {
	w        *latencyWatcher
	t        latencyTarget
	ip       netip.Addr
	resolved time.Time
	echo     echoConn
	echoIP   netip.Addr
	seq      int
	client   *http.Client
	httpIP   netip.Addr
}

func (p *latencyProbe) close() {
	if p.echo != nil {
		p.echo.Close()
		p.echo = nil
	}
	if p.client != nil {
		p.client.CloseIdleConnections()
	}
}

func (p *latencyProbe) address(ctx context.Context, timeout time.Duration) (netip.Addr, bool) {
	if ip, err := netip.ParseAddr(p.t.Host); err == nil {
		return ip.Unmap(), true
	}
	if p.ip.IsValid() && p.w.now().Sub(p.resolved) < latencyResolveEvery {
		return p.ip, true
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	// IPv4 first: the lines measured (三网 nodes, DNS) are IPv4 ones.
	for _, network := range []string{"ip4", "ip6"} {
		if ips, err := p.w.lookupIP(ctx, network, p.t.Host); err == nil && len(ips) > 0 {
			p.ip, p.resolved = ips[0].Unmap(), p.w.now()
			return p.ip, true
		}
	}
	return netip.Addr{}, false
}

func (p *latencyProbe) probe(ctx context.Context, timeout time.Duration) (time.Duration, bool) {
	ip, ok := p.address(ctx, timeout)
	if !ok {
		return 0, false
	}
	if p.t.Mode == "http" {
		return p.httpProbe(ctx, ip, timeout)
	}
	if p.t.Mode == "icmp" {
		if p.echo == nil || p.echoIP != ip {
			p.close()
			conn, err := p.w.openEcho(ip, rand.IntN(1<<16))
			if err != nil {
				return 0, false
			}
			p.echo, p.echoIP = conn, ip
		}
		p.seq = (p.seq + 1) % (1 << 16)
		r := p.echo.echo(ctx, p.seq, 0, timeout)
		return r.rtt, r.kind == replyEcho
	}
	network := "tcp4"
	if ip.Is6() {
		network = "tcp6"
	}
	ctx, cancel := context.WithTimeout(ctx, min(timeout, latencyTCPTimeout))
	defer cancel()
	start := time.Now()
	c, err := p.w.dial(ctx, network, netip.AddrPortFrom(ip, uint16(p.t.Port)).String())
	rtt := time.Since(start)
	if err != nil {
		return 0, false
	}
	c.Close()
	return rtt, true
}

// httpProbe times one GET from its request written to the first byte of the answer, so a connection
// or a TLS handshake it needed first does not count. Any answer counts, an error page being the server
// answering; a redirect is not followed. The certificate is not checked: nothing is sent but the
// request and nothing kept of the answer but its timing.
func (p *latencyProbe) httpProbe(ctx context.Context, ip netip.Addr, timeout time.Duration) (time.Duration, bool) {
	if p.client == nil {
		u, err := url.Parse(p.t.URL)
		if err != nil {
			return 0, false
		}
		port := u.Port()
		if port == "" {
			port = map[string]string{"http": "80", "https": "443"}[u.Scheme]
		}
		p.client = &http.Client{
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
			Transport: &http.Transport{
				Proxy: nil,
				// The transport dials apart from the request (its deadline does not reach here): a
				// target that never answers must not leave a connection behind for each probe.
				DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
					network := "tcp4"
					if p.httpIP.Is6() {
						network = "tcp6"
					}
					ctx, cancel := context.WithTimeout(ctx, latencyHTTPTimeout)
					defer cancel()
					return p.w.dial(ctx, network, net.JoinHostPort(p.httpIP.String(), port))
				},
				TLSHandshakeTimeout:   latencyHTTPTimeout,
				ResponseHeaderTimeout: latencyHTTPTimeout,
				TLSClientConfig:       &tls.Config{InsecureSkipVerify: true, ServerName: u.Hostname()},
				DisableCompression:    true,
				MaxIdleConnsPerHost:   1,
				IdleConnTimeout:       90 * time.Second,
			},
		}
	}
	if p.httpIP != ip {
		p.client.CloseIdleConnections()
		p.httpIP = ip
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	// The trace is called from the transport's own goroutines.
	var wrote, first atomic.Int64
	trace := &httptrace.ClientTrace{
		WroteRequest: func(httptrace.WroteRequestInfo) { wrote.Store(time.Now().UnixNano()) },
		GotFirstResponseByte: func() {
			if at := wrote.Load(); at > 0 {
				first.Store(time.Now().UnixNano() - at)
			}
		},
	}
	req, err := http.NewRequestWithContext(httptrace.WithClientTrace(ctx, trace), http.MethodGet, p.t.URL, nil)
	if err != nil {
		return 0, false
	}
	req.Header.Set("User-Agent", "openbear-node latency")
	resp, err := p.client.Do(req)
	if err != nil {
		return 0, false
	}
	// A short answer read to its end leaves the connection for the next probe.
	_, _ = io.CopyN(io.Discard, resp.Body, 16<<10)
	resp.Body.Close()
	got := time.Duration(first.Load())
	return got, got > 0
}

func (a *Agent) latencyCall(ctx context.Context, m envelope) (any, error) {
	if m.Method != "latency.watch" {
		return nil, errors.New("unsupported_method")
	}
	spec, err := parseLatencyWatch(m.Params, frontHosts(a.config()))
	if err != nil {
		return nil, err
	}
	ch := a.currentChannel()
	if ch == nil {
		return nil, errors.New("not_connected")
	}
	a.latencyOnce.Do(func() {
		if a.latency == nil {
			a.latency = newLatencyWatcher()
		}
	})
	// The probes go on without a session; this one takes the minutes while it lasts.
	a.latency.watch(a.lifetime(ctx), spec, func(minutes []latencyMinute) error {
		return ch.send(map[string]any{"t": "latency", "minutes": minutes})
	})
	return map[string]any{"accepted": len(spec.Targets)}, nil
}
