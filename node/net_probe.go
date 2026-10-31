//go:build linux || darwin || windows

package node

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"math/rand/v2"
	"net"
	"net/netip"
	"os"
	"slices"
	"strings"
	"sync"
	"time"
)

// Network tests run in the background: the controller starts one, polls its
// status and stops it. A test the controller stops polling is stopped for it.
const (
	probeMaxRunning = 4
	probeMaxRun     = 10 * time.Minute
	probeLease      = 30 * time.Second
	probeKeep       = 2 * time.Minute
	probeStagger    = time.Millisecond
)

var (
	errProbeEnded   = errors.New("probe ended")
	probePayload    = []byte("openbear-node network probe 0123")
	errInvalidProbe = errors.New("invalid_params")
)

type replyKind int

const (
	replyTimeout replyKind = iota
	replyEcho
	replyExceeded
	replyUnreachable
	replyFailed
)

type echoReply struct {
	kind replyKind
	from netip.Addr
	rtt  time.Duration
	ttl  int
	at   time.Time
	err  error // replyFailed
}

// echoConn sends ICMP echoes to one address. A ttl of 0 leaves the system default.
type echoConn interface {
	echo(ctx context.Context, seq, ttl int, timeout time.Duration) echoReply
	Close() error
}

type probeParams struct {
	Mode     string
	Target   string
	Port     int
	Family   int
	Count    int
	Interval time.Duration
	Timeout  time.Duration
	MaxHops  int
}

func parseProbeParams(raw json.RawMessage) (probeParams, error) {
	var in struct {
		Mode       string `json:"mode"`
		Target     string `json:"target"`
		Port       *int   `json:"port"`
		Family     *int   `json:"family"`
		Count      *int   `json:"count"`
		IntervalMs *int   `json:"intervalMs"`
		TimeoutMs  *int   `json:"timeoutMs"`
		MaxHops    *int   `json:"maxHops"`
	}
	if json.Unmarshal(raw, &in) != nil {
		return probeParams{}, errInvalidProbe
	}
	p := probeParams{Mode: in.Mode, Target: in.Target}
	value := func(v *int, def, lo, hi int) (int, bool) {
		if v == nil {
			return def, true
		}
		return *v, *v >= lo && *v <= hi
	}
	ok := true
	check := func(v int, good bool) int {
		ok = ok && good
		return v
	}
	switch p.Mode {
	case "ping", "tcping":
		p.Count = check(value(in.Count, 10, 1, 100))
	case "trace":
		p.Count = check(value(in.Count, 3, 1, 10))
		p.MaxHops = check(value(in.MaxHops, 30, 1, 64))
	default:
		return p, errInvalidProbe
	}
	if p.Mode == "tcping" {
		p.Port = check(value(in.Port, 0, 1, 65535))
		ok = ok && in.Port != nil
	}
	p.Family = check(value(in.Family, 0, 0, 6))
	interval := check(value(in.IntervalMs, 1000, 200, 10000))
	timeout := check(value(in.TimeoutMs, 2000, 200, 10000))
	p.Interval, p.Timeout = time.Duration(interval)*time.Millisecond, time.Duration(timeout)*time.Millisecond
	if !ok || p.Family != 0 && p.Family != 4 && p.Family != 6 || !validProbeTarget(p.Target, p.Family) {
		return p, errInvalidProbe
	}
	return p, nil
}

// validProbeTarget takes a host name or an address literal of the requested
// family; no scheme, port, brackets or blanks.
func validProbeTarget(s string, family int) bool {
	if s == "" || len(s) > 253 {
		return false
	}
	if ip, err := netip.ParseAddr(s); err == nil {
		ip = ip.Unmap()
		return family == 0 || family == 4 && ip.Is4() || family == 6 && ip.Is6()
	}
	for _, c := range s {
		if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '-' || c == '.' || c == '_') {
			return false
		}
	}
	return true
}

type probeResult struct {
	Seq   int      `json:"seq"`
	At    int64    `json:"at"`
	OK    bool     `json:"ok"`
	RTT   *float64 `json:"rttMs"`
	TTL   int      `json:"ttl"`
	Error string   `json:"error"`
	rtt   time.Duration
}

type traceHop struct {
	addrs               []netip.Addr
	sent, received      int
	last, min, max, sum time.Duration
}

type probe struct {
	id      string
	p       probeParams
	echoID  int
	cancel  context.CancelCauseFunc
	state   string
	err     string
	ip      netip.Addr
	started time.Time
	ended   time.Time
	queried time.Time
	results []probeResult
	hops    []traceHop
	reach   int // the ttl at which the target answered, 0 until it has
	seen    int // the highest ttl with an outcome
}

type probeManager struct {
	mu     sync.Mutex
	probes map[string]*probe
	names  map[netip.Addr]string // reverse lookups, "" while pending or without a name

	maxRun, lease, keep time.Duration
	openEcho            func(dst netip.Addr, id int) (echoConn, error)
	lookupIP            func(ctx context.Context, network, host string) ([]netip.Addr, error)
	dial                func(ctx context.Context, network, address string) (net.Conn, error)
	lookupAddr          func(ctx context.Context, addr string) ([]string, error)
	route               func(dst netip.Addr) error
}

func newProbeManager() *probeManager {
	var d net.Dialer
	return &probeManager{probes: map[string]*probe{}, names: map[netip.Addr]string{},
		maxRun: probeMaxRun, lease: probeLease, keep: probeKeep, openEcho: openEcho,
		lookupIP: net.DefaultResolver.LookupNetIP, dial: d.DialContext, lookupAddr: net.DefaultResolver.LookupAddr, route: checkRoute}
}

func (a *Agent) netProbes() *probeManager {
	a.probeOnce.Do(func() {
		if a.probes == nil {
			a.probes = newProbeManager()
		}
	})
	return a.probes
}

// netCall serves the net.* methods, which need neither command execution
// nor a busy slot.
func (a *Agent) netCall(ctx context.Context, m envelope) (any, error) {
	if m.Method == "net.addrs" {
		return a.netAddrs(), nil
	}
	if m.Method == "net.ports" {
		return a.netPorts(ctx, m.Params)
	}
	pm := a.netProbes()
	if m.Method == "net.probe.start" {
		return pm.start(m.Params)
	}
	var p struct {
		ID string `json:"id"`
	}
	if m.Method != "net.probe.status" && m.Method != "net.probe.stop" {
		return nil, errors.New("unsupported_method")
	}
	if json.Unmarshal(m.Params, &p) != nil || p.ID == "" {
		return nil, errInvalidProbe
	}
	if m.Method == "net.probe.status" {
		return pm.status(p.ID)
	}
	return pm.stop(p.ID)
}

func (m *probeManager) start(raw json.RawMessage) (any, error) {
	params, err := parseProbeParams(raw)
	if err != nil {
		return nil, err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	now := time.Now()
	m.sweep(now)
	// Raw ICMP sockets see every echo reply; each test keeps its own echo ID.
	inUse := map[int]bool{0: true}
	running := 0
	for _, p := range m.probes {
		if p.state == "running" {
			inUse[p.echoID] = true
			running++
		}
	}
	if running >= probeMaxRunning {
		return nil, errors.New("probe_busy")
	}
	p := &probe{id: randomHex(8), p: params, state: "running", started: now, queried: now}
	for inUse[p.echoID] {
		p.echoID = rand.IntN(1 << 16)
	}
	if params.Mode == "trace" {
		p.hops = make([]traceHop, params.MaxHops)
	}
	ctx, cancel := context.WithCancelCause(context.Background())
	ctx, stopTimer := context.WithTimeout(ctx, m.maxRun)
	p.cancel = cancel
	m.probes[p.id] = p
	go m.run(ctx, stopTimer, p)
	go m.watch(ctx, p)
	return map[string]any{"id": p.id}, nil
}

func (m *probeManager) status(id string) (any, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	now := time.Now()
	m.sweep(now)
	p := m.probes[id]
	if p == nil {
		return nil, errors.New("not_found")
	}
	p.queried = now
	return m.view(p), nil
}

func (m *probeManager) stop(id string) (any, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	now := time.Now()
	m.sweep(now)
	p := m.probes[id]
	if p == nil {
		return nil, errors.New("not_found")
	}
	p.queried = now
	m.endLocked(p, "stopped", "")
	return map[string]any{}, nil
}

// sweep drops tests that ended and have not been looked at for a while.
func (m *probeManager) sweep(now time.Time) {
	for id, p := range m.probes {
		if p.state != "running" && now.Sub(later(p.ended, p.queried)) > m.keep {
			delete(m.probes, id)
		}
	}
	if len(m.probes) == 0 {
		clear(m.names)
	}
}

func later(a, b time.Time) time.Time {
	if a.After(b) {
		return a
	}
	return b
}

func (m *probeManager) end(p *probe, state, msg string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.endLocked(p, state, msg)
}

// endLocked settles a running test once; results still in flight are dropped.
func (m *probeManager) endLocked(p *probe, state, msg string) {
	if p.state != "running" {
		return
	}
	p.state, p.err, p.ended = state, msg, time.Now()
	p.cancel(errProbeEnded)
}

// watch stops a running test nobody has asked about within the lease.
func (m *probeManager) watch(ctx context.Context, p *probe) {
	t := time.NewTimer(m.lease)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
		m.mu.Lock()
		idle := time.Since(p.queried)
		if idle >= m.lease {
			m.endLocked(p, "stopped", "")
			m.mu.Unlock()
			return
		}
		m.mu.Unlock()
		t.Reset(m.lease - idle)
	}
}

func (m *probeManager) run(ctx context.Context, stopTimer context.CancelFunc, p *probe) {
	defer stopTimer()
	addrs, err := m.resolve(ctx, p.p.Target, p.p.Family)
	if err != nil {
		if ctx.Err() != nil {
			m.end(p, "done", "")
		} else {
			m.end(p, "error", "dns_failed: "+err.Error())
		}
		return
	}
	ip, err := m.routable(addrs)
	m.mu.Lock()
	p.ip = ip
	if err != nil {
		m.endLocked(p, "error", err.Error())
		m.mu.Unlock()
		return
	}
	id := p.echoID
	m.mu.Unlock()
	if p.p.Mode == "tcping" {
		m.repeat(ctx, p, func(int) probeResult { return m.tcping(ctx, p, ip) })
		m.end(p, "done", "")
		return
	}
	conn, err := m.openEcho(ip, id)
	if err != nil {
		m.end(p, "error", "icmp_unavailable: "+shortError(err))
		return
	}
	defer conn.Close()
	if p.p.Mode == "ping" {
		m.repeat(ctx, p, func(seq int) probeResult { return pingResult(conn.echo(ctx, seq, 0, p.p.Timeout)) })
	} else {
		m.trace(ctx, p, conn)
	}
	m.end(p, "done", "")
}

// resolve lists the target's addresses of the wanted family, the one to
// try first first: IPv4 before IPv6 when either will do.
func (m *probeManager) resolve(ctx context.Context, host string, family int) ([]netip.Addr, error) {
	if ip, err := netip.ParseAddr(host); err == nil {
		return []netip.Addr{ip.Unmap()}, nil
	}
	network := map[int]string{0: "ip", 4: "ip4", 6: "ip6"}[family]
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	addrs, err := m.lookupIP(ctx, network, host)
	if err != nil {
		var dnsErr *net.DNSError
		if errors.As(err, &dnsErr) {
			return nil, errors.New(dnsErr.Err)
		}
		return nil, err
	}
	for i := range addrs {
		addrs[i] = addrs[i].Unmap()
	}
	var out []netip.Addr
	if i := slices.IndexFunc(addrs, netip.Addr.Is4); i >= 0 && family != 6 {
		out = append(out, addrs[i])
	}
	if i := slices.IndexFunc(addrs, netip.Addr.Is6); i >= 0 && family != 4 {
		out = append(out, addrs[i])
	}
	if len(out) == 0 {
		return nil, errors.New("no address")
	}
	return out, nil
}

// routable picks the first address this machine has a route and a source
// address for. Without one every attempt would fail the same way, or a
// trace would wait out each hop, so the test ends at once and says why.
func (m *probeManager) routable(addrs []netip.Addr) (netip.Addr, error) {
	v4, v6 := false, false
	for _, ip := range addrs {
		if m.route(ip) == nil {
			return ip, nil
		}
		v4, v6 = v4 || ip.Is4(), v6 || ip.Is6()
	}
	family := "IPv4"
	switch {
	case v4 && v6:
		family = "IPv4 或 IPv6"
	case v6:
		family = "IPv6"
	}
	return addrs[0], errors.New("本机没有可用的 " + family + " 地址或路由")
}

// errNoSource: the only source address for the target is link-local, which
// nothing beyond the link answers.
var errNoSource = errors.New("no usable source address")

// checkRoute asks the system which source address it would send from, as a
// UDP connect does without sending anything. Only the answers that mean
// there is no way out count; anything else is left to the test itself.
func checkRoute(dst netip.Addr) error {
	network := "udp4"
	if dst.Is6() {
		network = "udp6"
	}
	c, err := net.DialUDP(network, nil, net.UDPAddrFromAddrPort(netip.AddrPortFrom(dst, 9)))
	if err != nil {
		if noRoute(err) {
			return err
		}
		return nil
	}
	defer c.Close()
	src := c.LocalAddr().(*net.UDPAddr).AddrPort().Addr().Unmap()
	if !dst.IsLinkLocalUnicast() && !dst.IsLoopback() && !dst.IsMulticast() && (src.IsLinkLocalUnicast() || src.IsUnspecified()) {
		return errNoSource
	}
	return nil
}

// repeat runs count attempts intervalMs apart; a slow attempt does not hold
// back the next one.
func (m *probeManager) repeat(ctx context.Context, p *probe, once func(seq int) probeResult) {
	var wg sync.WaitGroup
	start := time.Now()
	for seq := 1; seq <= p.p.Count; seq++ {
		if seq > 1 && !sleepUntil(ctx, start.Add(time.Duration(seq-1)*p.p.Interval)) {
			break
		}
		at := time.Now()
		wg.Add(1)
		go func() {
			defer wg.Done()
			r := once(seq)
			r.Seq, r.At = seq, at.UnixMilli()
			if r.OK {
				r.RTT = msPtr(r.rtt)
			}
			m.mu.Lock()
			defer m.mu.Unlock()
			if p.state == "running" {
				i, _ := slices.BinarySearchFunc(p.results, seq, func(r probeResult, seq int) int { return r.Seq - seq })
				p.results = slices.Insert(p.results, i, r)
			}
		}()
	}
	done := make(chan struct{})
	go func() {
		wg.Wait()
		close(done)
	}()
	select {
	case <-done:
	case <-ctx.Done():
	}
}

func sleepUntil(ctx context.Context, t time.Time) bool {
	timer := time.NewTimer(time.Until(t))
	defer timer.Stop()
	select {
	case <-timer.C:
		return true
	case <-ctx.Done():
		return false
	}
}

func pingResult(r echoReply) probeResult {
	switch r.kind {
	case replyEcho:
		return probeResult{OK: true, rtt: r.rtt, TTL: r.ttl}
	case replyTimeout:
		return probeResult{Error: "timeout"}
	case replyUnreachable:
		return probeResult{Error: "unreachable"}
	case replyExceeded:
		return probeResult{Error: "ttl exceeded"}
	}
	if dialErrorKind(r.err) == "unreachable" {
		return probeResult{Error: "unreachable"}
	}
	return probeResult{Error: shortError(r.err)}
}

func (m *probeManager) tcping(ctx context.Context, p *probe, ip netip.Addr) probeResult {
	network := "tcp4"
	if ip.Is6() {
		network = "tcp6"
	}
	ctx, cancel := context.WithTimeout(ctx, p.p.Timeout)
	defer cancel()
	start := time.Now()
	c, err := m.dial(ctx, network, netip.AddrPortFrom(ip, uint16(p.p.Port)).String())
	rtt := time.Since(start)
	if err == nil {
		c.Close()
		return probeResult{OK: true, rtt: rtt}
	}
	var ne net.Error
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, os.ErrDeadlineExceeded) || errors.As(err, &ne) && ne.Timeout() {
		return probeResult{Error: "timeout"}
	}
	if kind := dialErrorKind(err); kind != "" {
		return probeResult{Error: kind}
	}
	return probeResult{Error: shortError(err)}
}

// shortError keeps the cause without the operation and addresses around it.
func shortError(err error) string {
	var se *os.SyscallError
	var oe *net.OpError
	switch {
	case errors.As(err, &se):
		err = se.Err
	case errors.As(err, &oe):
		err = oe.Err
	}
	s := err.Error()
	if len(s) > 120 {
		s = strings.ToValidUTF8(s[:120], "")
	}
	return s
}

// trace sends every ttl of a round at once and waits for the round's
// answers. Once the target answers at some ttl, nothing beyond it is sent or kept.
func (m *probeManager) trace(ctx context.Context, p *probe, conn echoConn) {
	for round := 0; round < p.p.Count; round++ {
		if round > 0 && !sleepUntil(ctx, time.Now().Add(p.p.Interval)) {
			return
		}
		var wg sync.WaitGroup
		for ttl := 1; ttl <= p.p.MaxHops; ttl++ {
			if ttl > 1 && !sleepUntil(ctx, time.Now().Add(probeStagger)) {
				break
			}
			m.mu.Lock()
			beyond := p.reach > 0 && ttl > p.reach
			m.mu.Unlock()
			if beyond {
				break
			}
			wg.Add(1)
			go func() {
				defer wg.Done()
				m.addHop(p, ttl, conn.echo(ctx, round<<8|ttl, ttl, p.p.Timeout))
			}()
		}
		wg.Wait()
	}
}

func (m *probeManager) addHop(p *probe, ttl int, r echoReply) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if p.state != "running" || p.reach > 0 && ttl > p.reach {
		return
	}
	h := &p.hops[ttl-1]
	h.sent++
	p.seen = max(p.seen, ttl)
	switch r.kind {
	case replyEcho, replyExceeded, replyUnreachable:
	default:
		return
	}
	if h.received == 0 || r.rtt < h.min {
		h.min = r.rtt
	}
	h.received++
	h.last, h.max, h.sum = r.rtt, max(h.max, r.rtt), h.sum+r.rtt
	if r.from.IsValid() && !slices.Contains(h.addrs, r.from) {
		h.addrs = append(h.addrs, r.from)
		m.lookupName(r.from)
	}
	if r.kind == replyEcho && (p.reach == 0 || ttl < p.reach) {
		p.reach = ttl
	}
}

// lookupName resolves an address once, in the background.
func (m *probeManager) lookupName(ip netip.Addr) {
	if _, ok := m.names[ip]; ok {
		return
	}
	m.names[ip] = ""
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		names, err := m.lookupAddr(ctx, ip.String())
		if err != nil || len(names) == 0 {
			return
		}
		m.mu.Lock()
		m.names[ip] = strings.TrimSuffix(names[0], ".")
		m.mu.Unlock()
	}()
}

type hopAddr struct {
	IP   string `json:"ip"`
	Name string `json:"name"`
}

type hopView struct {
	TTL      int       `json:"ttl"`
	Addrs    []hopAddr `json:"addrs"`
	Sent     int       `json:"sent"`
	Received int       `json:"received"`
	LossPct  float64   `json:"lossPct"`
	LastMs   *float64  `json:"lastMs"`
	MinMs    *float64  `json:"minMs"`
	AvgMs    *float64  `json:"avgMs"`
	MaxMs    *float64  `json:"maxMs"`
	Reached  bool      `json:"reached"`
}

func (m *probeManager) view(p *probe) map[string]any {
	ip, family, ended := "", p.p.Family, int64(0)
	if p.ip.IsValid() {
		ip, family = p.ip.String(), 4
		if p.ip.Is6() {
			family = 6
		}
	}
	if !p.ended.IsZero() {
		ended = p.ended.UnixMilli()
	}
	results, hops := []probeResult{}, []hopView{}
	var summary any
	if p.p.Mode == "trace" {
		n := p.seen
		if p.reach > 0 {
			n = p.reach
		}
		for i, h := range p.hops[:n] {
			v := hopView{TTL: i + 1, Addrs: []hopAddr{}, Sent: h.sent, Received: h.received, Reached: i+1 == p.reach}
			for _, a := range h.addrs {
				v.Addrs = append(v.Addrs, hopAddr{IP: a.String(), Name: m.names[a]})
			}
			if h.sent > 0 {
				v.LossPct = round1(float64(h.sent-h.received) * 100 / float64(h.sent))
			}
			if h.received > 0 {
				v.LastMs, v.MinMs, v.MaxMs = msPtr(h.last), msPtr(h.min), msPtr(h.max)
				v.AvgMs = msPtr(h.sum / time.Duration(h.received))
			}
			hops = append(hops, v)
		}
	} else {
		results = append(results, p.results...)
		if s := probeSummary(results); s != nil {
			summary = s
		}
	}
	return map[string]any{"id": p.id, "mode": p.p.Mode, "state": p.state, "error": p.err,
		"target": p.p.Target, "ip": ip, "family": family, "port": p.p.Port,
		"startedAt": p.started.UnixMilli(), "endedAt": ended,
		"results": results, "summary": summary, "hops": hops, "reached": p.reach > 0}
}

type probeStats struct {
	Sent     int      `json:"sent"`
	Received int      `json:"received"`
	LossPct  float64  `json:"lossPct"`
	MinMs    *float64 `json:"minMs"`
	AvgMs    *float64 `json:"avgMs"`
	MaxMs    *float64 `json:"maxMs"`
	JitterMs *float64 `json:"jitterMs"`
}

// probeSummary: jitter is the mean difference between consecutive successful
// round trips.
func probeSummary(results []probeResult) *probeStats {
	if len(results) == 0 {
		return nil
	}
	s := &probeStats{Sent: len(results)}
	var sum, jitter time.Duration
	var lo, hi, prev time.Duration
	for _, r := range results {
		if !r.OK {
			continue
		}
		if s.Received == 0 || r.rtt < lo {
			lo = r.rtt
		}
		if s.Received > 0 {
			jitter += (r.rtt - prev).Abs()
		}
		hi, prev, sum = max(hi, r.rtt), r.rtt, sum+r.rtt
		s.Received++
	}
	s.LossPct = round1(float64(s.Sent-s.Received) * 100 / float64(s.Sent))
	if s.Received > 0 {
		s.MinMs, s.MaxMs, s.AvgMs = msPtr(lo), msPtr(hi), msPtr(sum/time.Duration(s.Received))
	}
	if s.Received > 1 {
		s.JitterMs = msPtr(jitter / time.Duration(s.Received-1))
	}
	return s
}

func round1(v float64) float64 { return math.Round(v*10) / 10 }

func msPtr(d time.Duration) *float64 {
	v := round1(float64(d) / float64(time.Millisecond))
	return &v
}
