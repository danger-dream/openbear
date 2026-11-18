package proxy

import (
	"cmp"
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/netip"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// Port forwards. A node, the entry, takes clients on a port of its own and
// hands each through a tunnel, as an exit's entries do, to another node, the
// target, which dials what the forward names from its machine, loopback
// included. A target dials only what its forwards name, for the entries they
// come from. The tunnel goes to the target's forward port over the mesh, or
// to its fast forward port, whose connections the fast relay carries: those
// arrive from the target's own address, once the relay has checked who sent
// them, or from the entry when the relay is not ready and the mesh takes them.

const (
	fwdMax      = 256
	fwdAllowMax = 200000 // client networks, in all sets and forwards
	fwdUDPMax   = 1024   // UDP clients one forward relays at once
)

var fwdID = regexp.MustCompile(`^[A-Za-z0-9_-]{1,32}$`)
var fwdHost = regexp.MustCompile(`^(?i)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$`)

// ForwardConfig is a node's part of the port forwards (Config.Forward).
type ForwardConfig struct {
	// Port and FastPort are the tunnel ports on every target's mesh address.
	Port     int `json:"port"`
	FastPort int `json:"fastPort,omitempty"`
	// Forwards are the ports this node takes clients on.
	Forwards []Forward `json:"forwards,omitempty"`
	// Targets are what this node dials for forwards that end here.
	Targets []ForwardTarget `json:"targets,omitempty"`
	// Sets are named lists of client networks (a province's) forwards allow by name.
	Sets map[string][]netip.Prefix `json:"sets,omitempty"`
}

// Forward is one port this node takes clients on.
type Forward struct {
	ID     string `json:"id"`
	Listen int    `json:"listen"`
	Proto  string `json:"proto"` // tcp, udp or any
	// Node is the target's mesh address; Fast sends the tunnel to its fast port.
	Node netip.Addr `json:"node"`
	Fast bool       `json:"fast,omitempty"`
	// Host and Port are what the target dials: an address or a name.
	Host string `json:"host"`
	Port int    `json:"port"`
	// ProxyProtocol 1 or 2 puts a PROXY protocol header with the client's
	// address ahead of its data (TCP).
	ProxyProtocol int `json:"proxyProtocol,omitempty"`
	// Allow and AllowSets are the clients it takes; neither: every one.
	Allow     []netip.Prefix `json:"allow,omitempty"`
	AllowSets []string       `json:"allowSets,omitempty"`
}

// ForwardTarget is what this node dials for an entry's forward.
type ForwardTarget struct {
	From  netip.Addr `json:"from"` // the entry's mesh address
	Fast  bool       `json:"fast,omitempty"`
	Host  string     `json:"host"`
	Port  int        `json:"port"`
	Proto string     `json:"proto"`
}

func validProto(p string) bool { return p == "tcp" || p == "udp" || p == "any" }

func validHost(h string) bool {
	if _, err := netip.ParseAddr(h); err == nil {
		return true
	}
	return len(h) <= 253 && fwdHost.MatchString(h)
}

func takes(proto, kind string) bool { return proto == "any" || proto == kind }

// Check rejects a config the forwards cannot run.
func (c *ForwardConfig) Check() error {
	if len(c.Forwards) == 0 && len(c.Targets) == 0 {
		return nil
	}
	if c.Port < 1 || c.Port > 65535 || c.FastPort < 0 || c.FastPort > 65535 || c.FastPort == c.Port {
		return fmt.Errorf("forward.port %d and fastPort %d: two different ports", c.Port, c.FastPort)
	}
	if len(c.Forwards) > fwdMax || len(c.Targets) > fwdMax {
		return fmt.Errorf("forward: at most %d forwards and %d targets", fwdMax, fwdMax)
	}
	total := 0
	for name, set := range c.Sets {
		if name == "" || len(name) > 64 {
			return fmt.Errorf("forward.sets: invalid name %q", name)
		}
		total += len(set)
	}
	ids := map[string]bool{}
	type bound struct {
		port int
		kind string
	}
	used := map[bound]string{}
	for i := range c.Forwards {
		f := &c.Forwards[i]
		p := "forward.forwards " + f.ID
		if !fwdID.MatchString(f.ID) || ids[f.ID] {
			return fmt.Errorf("forward.forwards: invalid or repeated id %q", f.ID)
		}
		ids[f.ID] = true
		if f.Listen < 1 || f.Listen > 65535 || f.Port < 1 || f.Port > 65535 {
			return fmt.Errorf("%s: listen %d and port %d need 1 to 65535", p, f.Listen, f.Port)
		}
		if !validProto(f.Proto) || !f.Node.Is4() || !validHost(f.Host) {
			return fmt.Errorf("%s: proto, node or host is invalid", p)
		}
		if f.Fast && c.FastPort == 0 {
			return fmt.Errorf("%s: fast without forward.fastPort", p)
		}
		if f.ProxyProtocol != 0 && (f.ProxyProtocol != 1 && f.ProxyProtocol != 2 || f.Proto == "udp") {
			return fmt.Errorf("%s: proxyProtocol is 1 or 2, for TCP", p)
		}
		for _, kind := range []string{"tcp", "udp"} {
			if !takes(f.Proto, kind) {
				continue
			}
			if other := used[bound{f.Listen, kind}]; other != "" {
				return fmt.Errorf("%s: %s port %d is %s's too", p, kind, f.Listen, other)
			}
			used[bound{f.Listen, kind}] = f.ID
		}
		for _, name := range f.AllowSets {
			if _, ok := c.Sets[name]; !ok {
				return fmt.Errorf("%s: no set %q", p, name)
			}
		}
		for _, a := range f.Allow {
			if !a.IsValid() {
				return fmt.Errorf("%s: an allowed network is invalid", p)
			}
		}
		total += len(f.Allow)
	}
	if total > fwdAllowMax {
		return fmt.Errorf("forward: at most %d allowed networks", fwdAllowMax)
	}
	for i, t := range c.Targets {
		if !t.From.IsValid() || !validHost(t.Host) || t.Port < 1 || t.Port > 65535 || !validProto(t.Proto) || t.Fast && c.FastPort == 0 {
			return fmt.Errorf("forward.targets[%d] is invalid", i)
		}
	}
	return nil
}

func addrOf(host string, port int) Addr {
	if ip, err := netip.ParseAddr(host); err == nil {
		return Addr{IP: ip.Unmap(), Port: uint16(port)}
	}
	return Addr{Host: strings.ToLower(host), Port: uint16(port)}
}

// ----- the clients a forward takes -----

type allowList struct {
	all bool
	v4  [][2]uint32 // merged ranges, in order
	v6  []netip.Prefix
}

func newAllowList(f *Forward, sets map[string][]netip.Prefix) *allowList {
	l := &allowList{all: len(f.Allow) == 0 && len(f.AllowSets) == 0}
	var nets []netip.Prefix
	nets = append(nets, f.Allow...)
	for _, name := range f.AllowSets {
		nets = append(nets, sets[name]...)
	}
	for _, n := range nets {
		n = n.Masked()
		if !n.Addr().Is4() {
			l.v6 = append(l.v6, n)
			continue
		}
		a := n.Addr().As4()
		lo := uint32(a[0])<<24 | uint32(a[1])<<16 | uint32(a[2])<<8 | uint32(a[3])
		hi := lo | uint32(uint64(1)<<(32-n.Bits())-1)
		l.v4 = append(l.v4, [2]uint32{lo, hi})
	}
	slices.SortFunc(l.v4, func(x, y [2]uint32) int { return cmp.Compare(x[0], y[0]) })
	merged := l.v4[:0]
	for _, r := range l.v4 {
		if n := len(merged); n > 0 && uint64(r[0]) <= uint64(merged[n-1][1])+1 {
			merged[n-1][1] = max(merged[n-1][1], r[1])
			continue
		}
		merged = append(merged, r)
	}
	l.v4 = merged
	return l
}

func (l *allowList) has(ip netip.Addr) bool {
	if l.all {
		return true
	}
	ip = ip.Unmap()
	if ip.Is4() {
		a := ip.As4()
		v := uint32(a[0])<<24 | uint32(a[1])<<16 | uint32(a[2])<<8 | uint32(a[3])
		i, _ := slices.BinarySearchFunc(l.v4, v, func(r [2]uint32, v uint32) int { return cmp.Compare(r[0], v) })
		if i < len(l.v4) && l.v4[i][0] == v {
			return true
		}
		return i > 0 && v <= l.v4[i-1][1]
	}
	for _, n := range l.v6 {
		if n.Contains(ip) {
			return true
		}
	}
	return false
}

// ----- PROXY protocol -----

var proxyV2Sig = []byte("\r\n\r\n\x00\r\nQUIT\n")

// proxyHeader is the PROXY protocol header of version v for a client from
// src that reached dst; nil without one.
func proxyHeader(v int, src, dst netip.AddrPort) []byte {
	src = netip.AddrPortFrom(src.Addr().Unmap(), src.Port())
	dst = netip.AddrPortFrom(dst.Addr().Unmap(), dst.Port())
	if src.Addr().Is4() != dst.Addr().Is4() {
		// One family on both sides: the client's decides.
		if src.Addr().Is4() {
			dst = netip.AddrPortFrom(netip.IPv4Unspecified(), dst.Port())
		} else {
			dst = netip.AddrPortFrom(netip.IPv6Unspecified(), dst.Port())
		}
	}
	switch v {
	case 1:
		fam := "TCP6"
		if src.Addr().Is4() {
			fam = "TCP4"
		}
		return fmt.Appendf(nil, "PROXY %s %s %s %d %d\r\n", fam, src.Addr(), dst.Addr(), src.Port(), dst.Port())
	case 2:
		b := append([]byte{}, proxyV2Sig...)
		if src.Addr().Is4() {
			b = append(b, 0x21, 0x11, 0, 12)
			s, d := src.Addr().As4(), dst.Addr().As4()
			b = append(append(b, s[:]...), d[:]...)
		} else {
			b = append(b, 0x21, 0x21, 0, 36)
			s, d := src.Addr().As16(), dst.Addr().As16()
			b = append(append(b, s[:]...), d[:]...)
		}
		return append(b, byte(src.Port()>>8), byte(src.Port()), byte(dst.Port()>>8), byte(dst.Port()))
	}
	return nil
}

// ----- counters -----

type fwdStats struct {
	Active, Opened, Refused, Failed, BytesIn, BytesOut, Sessions atomic.Int64
	mu                                                           sync.Mutex
	lastErr                                                      string
	lastAt                                                       time.Time
}

func (s *fwdStats) fail(reason string) {
	s.Failed.Add(1)
	s.mu.Lock()
	s.lastErr, s.lastAt = reason, time.Now()
	s.mu.Unlock()
}

func (s *fwdStats) last() (string, int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.lastErr == "" {
		return "", 0
	}
	return s.lastErr, s.lastAt.UnixMilli()
}

// statusReason names what a target answered, for a forward's last error.
func statusReason(b byte) string {
	switch b {
	case statusNotAllowed:
		return "target_not_allowed"
	case statusRefused:
		return "target_refused"
	case statusTimeout:
		return "target_timeout"
	case statusHost, statusNetwork:
		return "target_unreachable"
	}
	return "target_failed"
}

// ----- the entry: a forward's listeners -----

type fwdEntry struct {
	journal   *forwardLog
	match     string
	cfg       Forward
	key       string
	allowKey  string
	to        Addr
	allow     atomic.Pointer[allowList]
	pool      *pool
	stats     *fwdStats
	open      conns
	tcp       net.Listener
	udp       *net.UDPConn
	errs      []string
	wg        sync.WaitGroup
	sessionMu sync.Mutex
	sessions  map[netip.AddrPort]*fwdSession
	closed    atomic.Bool
	stop      chan struct{}
}

func startForward(f Forward, key string, sets map[string][]netip.Prefix, p *pool, stats *fwdStats, journal *forwardLog, self netip.Addr) *fwdEntry {
	e := &fwdEntry{journal: journal, match: forwardMatch(self, f.Fast, addrOf(f.Host, f.Port)), cfg: f, key: key, allowKey: allowKey(f, sets), to: addrOf(f.Host, f.Port), pool: p, stats: stats,
		sessions: map[netip.AddrPort]*fwdSession{}, stop: make(chan struct{})}
	e.allow.Store(newAllowList(&f, sets))
	e.open.setEntry(e.allow.Load(), false)
	e.listen()
	return e
}

// listen opens the listeners the forward lacks: all at the start, those a
// port another program held on a retry; the others keep their clients.
func (e *fwdEntry) listen() {
	e.errs = nil
	at := ":" + strconv.Itoa(e.cfg.Listen)
	if takes(e.cfg.Proto, "tcp") && e.tcp == nil {
		if ln, err := net.Listen("tcp", at); err != nil {
			e.errs = append(e.errs, "tcp: "+err.Error())
		} else {
			e.tcp = ln
			e.wg.Add(1)
			go e.acceptTCP()
		}
	}
	if takes(e.cfg.Proto, "udp") && e.udp == nil {
		if pc, err := net.ListenUDP("udp", &net.UDPAddr{Port: e.cfg.Listen}); err != nil {
			e.errs = append(e.errs, "udp: "+err.Error())
		} else {
			e.udp = pc
			e.wg.Add(2)
			go e.readUDP()
			go e.reapUDP()
		}
	}
}

func allowKey(f Forward, sets map[string][]netip.Prefix) string {
	named := map[string][]netip.Prefix{}
	for _, name := range f.AllowSets {
		named[name] = sets[name]
	}
	return key(f.Allow, named)
}

// entryKey is what restarts a forward's listeners when it changes: all of
// it but the clients it takes, which change in place.
func entryKey(f Forward, port int) string {
	f.Allow, f.AllowSets = nil, nil
	return key(f, port)
}

// setAllow takes the clients l allows from now on and cuts those it does not.
func (e *fwdEntry) setAllow(l *allowList) {
	e.allow.Store(l)
	e.open.setEntry(l, false)
	e.sessionMu.Lock()
	for from, s := range e.sessions {
		if !l.has(from.Addr()) {
			s.t.Close()
		}
	}
	e.sessionMu.Unlock()
}

// close stops taking clients and cuts those it has.
func (e *fwdEntry) close() {
	e.closed.Store(true)
	close(e.stop)
	if e.tcp != nil {
		e.tcp.Close()
	}
	if e.udp != nil {
		e.udp.Close()
	}
	e.open.setEntry(nil, true)
	e.sessionMu.Lock()
	for _, s := range e.sessions {
		s.t.Close()
	}
	e.sessionMu.Unlock()
	e.wg.Wait()
}

func (e *fwdEntry) acceptTCP() {
	defer e.wg.Done()
	for {
		c, err := e.tcp.Accept()
		if err != nil {
			if errors.Is(err, net.ErrClosed) {
				return
			}
			time.Sleep(50 * time.Millisecond)
			continue
		}
		tc := c.(*net.TCPConn)
		from, _ := netip.ParseAddrPort(c.RemoteAddr().String())
		if !e.allow.Load().has(from.Addr()) {
			e.stats.Refused.Add(1)
			tc.SetLinger(0)
			c.Close()
			continue
		}
		if !e.open.add(tc, from.Addr().Unmap()) {
			e.stats.fail("too_many_connections")
			e.attempt("tcp").fail("admit_client", "too_many_connections")
			c.Close()
			continue
		}
		e.wg.Add(1)
		go func() { defer e.wg.Done(); e.serveTCP(tc, from) }()
	}
}

// tunnel gives a tunnel to the target with the header (and what follows it) sent.
func (e *fwdEntry) tunnel(head []byte, attempt *forwardAttempt) (*net.TCPConn, error) {
	var err error
	stage := "open_tunnel"
	for range 2 {
		var t *net.TCPConn
		if t, err = e.pool.get(e.cfg.Node); err != nil {
			stage = "open_tunnel"
			continue
		}
		if _, err = t.Write(head); err != nil {
			stage = "write_tunnel_header"
			t.Close()
			continue
		}
		return t, nil
	}
	attempt.fail(stage, socketReason(err))
	return nil, err
}

func (e *fwdEntry) serveTCP(c *net.TCPConn, from netip.AddrPort) {
	defer e.open.done(c)
	defer c.Close()
	head := tunnelHeader(cmdConnect, e.to)
	if e.cfg.ProxyProtocol != 0 {
		local, _ := netip.ParseAddrPort(c.LocalAddr().String())
		head = append(head, proxyHeader(e.cfg.ProxyProtocol, from, local)...)
	}
	attempt := e.attempt("tcp")
	t, err := e.tunnel(head, attempt)
	if err != nil {
		e.stats.fail("tunnel: " + err.Error())
		return
	}
	defer t.Close()
	if !e.open.pair(c, t) {
		return
	}
	e.stats.Opened.Add(1)
	e.stats.Active.Add(1)
	defer e.stats.Active.Add(-1)
	down := make(chan int64, 1)
	go func() {
		if !attempt.confirmed(t, e.stats) {
			c.Close()
			t.Close()
			down <- 0
			return
		}
		n, err := io.Copy(c, t)
		if err != nil {
			attempt.streamError("tunnel_to_client", err)
			abortForwardConn(c)
			abortForwardConn(t)
		} else {
			c.CloseWrite()
		}
		down <- n
	}()
	up, err := io.Copy(t, c)
	if err != nil {
		attempt.streamError("client_to_tunnel", err)
		abortForwardConn(c)
		abortForwardConn(t)
	} else {
		t.CloseWrite()
	}
	e.stats.BytesIn.Add(up)
	e.stats.BytesOut.Add(<-down)
}

// fwdSession is one UDP client's datagrams, in frames through one tunnel.
type fwdSession struct {
	attempt *forwardAttempt
	t       *net.TCPConn
	wmu     sync.Mutex
	last    atomic.Int64
}

func (e *fwdEntry) readUDP() {
	defer e.wg.Done()
	buf := make([]byte, 0x10000)
	for {
		n, from, err := e.udp.ReadFromUDPAddrPort(buf)
		if err != nil {
			if errors.Is(err, net.ErrClosed) {
				return
			}
			continue
		}
		if !e.allow.Load().has(from.Addr()) {
			e.stats.Refused.Add(1)
			continue
		}
		s := e.session(from)
		if s == nil {
			continue
		}
		s.last.Store(time.Now().UnixNano())
		s.wmu.Lock()
		err = writeFrame(s.t, e.to, buf[:n])
		s.wmu.Unlock()
		if err != nil {
			s.attempt.streamError("client_to_tunnel", err)
			s.t.Close()
			continue
		}
		e.stats.BytesIn.Add(int64(n))
	}
}

// session is the client's, opened now for its first datagram; nil when none can be.
func (e *fwdEntry) session(from netip.AddrPort) *fwdSession {
	e.sessionMu.Lock()
	s := e.sessions[from]
	full := len(e.sessions) >= fwdUDPMax
	e.sessionMu.Unlock()
	if s != nil {
		return s
	}
	attempt := e.attempt("udp")
	if full {
		attempt.fail("admit_client", "too_many_udp_clients")
		e.stats.fail("too_many_udp_clients")
		return nil
	}
	t, err := e.tunnel(tunnelHeader(cmdUDP, e.to), attempt)
	if err != nil {
		e.stats.fail("tunnel: " + err.Error())
		return nil
	}
	s = &fwdSession{t: t, attempt: attempt}
	s.last.Store(time.Now().UnixNano())
	e.sessionMu.Lock()
	if e.closed.Load() {
		e.sessionMu.Unlock()
		t.Close()
		return nil
	}
	e.sessions[from] = s
	e.sessionMu.Unlock()
	e.stats.Opened.Add(1)
	e.stats.Sessions.Add(1)
	e.wg.Add(1)
	go e.answerUDP(from, s)
	return s
}

// answerUDP sends a session's frames back to its client until its tunnel ends.
func (e *fwdEntry) answerUDP(to netip.AddrPort, s *fwdSession) {
	defer e.wg.Done()
	defer func() {
		e.sessionMu.Lock()
		if e.sessions[to] == s {
			delete(e.sessions, to)
		}
		e.sessionMu.Unlock()
		e.stats.Sessions.Add(-1)
		s.t.Close()
	}()
	if !s.attempt.confirmed(s.t, e.stats) {
		return
	}
	buf := make([]byte, 0x10000)
	for {
		_, payload, err := readFrame(s.t, buf)
		if err != nil {
			s.attempt.streamError("read_tunnel_frame", err)
			return
		}
		s.last.Store(time.Now().UnixNano())
		if n, err := e.udp.WriteToUDPAddrPort(payload, to); err == nil {
			e.stats.BytesOut.Add(int64(n))
		} else {
			s.attempt.streamError("write_client_datagram", err)
		}
	}
}

// reapUDP ends the sessions nothing went through for udpIdle.
func (e *fwdEntry) reapUDP() {
	defer e.wg.Done()
	t := time.NewTicker(10 * time.Second)
	defer t.Stop()
	for {
		select {
		case <-e.stop:
			return
		case <-t.C:
		}
		e.sessionMu.Lock()
		for _, s := range e.sessions {
			if time.Since(time.Unix(0, s.last.Load())) > udpIdle {
				s.t.Close()
			}
		}
		e.sessionMu.Unlock()
	}
}

// ----- the target: what it dials for whom -----

type fwdTargetServer struct {
	journal  *forwardLog
	self     netip.Addr
	port     int
	fastPort int
	targets  []ForwardTarget
	lns      []net.Listener
	fast     net.Listener
	errs     []string
	stats    *fwdStats
	open     *conns
	wg       sync.WaitGroup
}

func startTargets(self netip.Addr, c *ForwardConfig, stats *fwdStats, open *conns, journal *forwardLog) *fwdTargetServer {
	open.updateForward(c.Targets)
	s := &fwdTargetServer{journal: journal, self: self, port: c.Port, fastPort: c.FastPort, targets: slices.Clone(c.Targets), stats: stats, open: open}
	ports := []int{c.Port}
	if c.FastPort != 0 && slices.ContainsFunc(c.Targets, func(t ForwardTarget) bool { return t.Fast }) {
		ports = append(ports, c.FastPort)
	}
	for i, port := range ports {
		ln, err := net.Listen("tcp", netip.AddrPortFrom(self, uint16(port)).String())
		if err != nil {
			s.errs = append(s.errs, err.Error())
			continue
		}
		s.lns = append(s.lns, ln)
		if i == 1 {
			s.fast = ln
		}
		s.wg.Add(1)
		go s.accept(ln, i == 1)
	}
	return s
}

func (s *fwdTargetServer) close() {
	for _, ln := range s.lns {
		ln.Close()
	}
	s.wg.Wait()
}

// knows is whether a tunnel from from on the fast port (fast) or the forward
// port may come in at all.
func (s *fwdTargetServer) knows(from netip.Addr, fast bool) bool {
	return slices.ContainsFunc(s.targets, func(t ForwardTarget) bool { return t.serves(from, fast, s.self) })
}

// serves: a tunnel from from on the fast port (fast) or the forward port may
// ask for t. Each forward's tunnels go to one of the two ports.
func (t *ForwardTarget) serves(from netip.Addr, fast bool, self netip.Addr) bool {
	if t.Fast != fast {
		return false
	}
	return t.From == from
}

// rule is the target a tunnel's header names for it; nil when none.
func (s *fwdTargetServer) rule(from netip.Addr, fast bool, cmd byte, a Addr) *ForwardTarget {
	kind := "tcp"
	if cmd == cmdUDP {
		kind = "udp"
	}
	for i := range s.targets {
		t := &s.targets[i]
		if takes(t.Proto, kind) && addrOf(t.Host, t.Port) == a && t.serves(from, fast, s.self) {
			return t
		}
	}
	return nil
}

func (s *fwdTargetServer) accept(ln net.Listener, fast bool) {
	defer s.wg.Done()
	for {
		c, err := ln.Accept()
		if err != nil {
			if errors.Is(err, net.ErrClosed) {
				return
			}
			time.Sleep(50 * time.Millisecond)
			continue
		}
		tc := c.(*net.TCPConn)
		from, _ := netip.ParseAddrPort(c.RemoteAddr().String())
		if !s.open.addForward(tc, from.Addr().Unmap(), fast) {
			s.stats.Refused.Add(1)
			c.Close()
			continue
		}
		go s.serve(tc, from.Addr().Unmap(), fast)
	}
}

func (s *fwdTargetServer) serve(c *net.TCPConn, from netip.Addr, fast bool) (bytesIn, bytesOut int64) {
	defer s.open.done(c)
	defer c.Close()
	c.SetReadDeadline(time.Now().Add(headerWait))
	hdr := &headerReader{Reader: c}
	cmd, a, err := readHeader(hdr)
	if err != nil {
		// Unused warm tunnels normally close without a single request byte.
		if hdr.n != 0 {
			s.journal.record(forwardFailure{Side: "target"}, "read_tunnel_header", socketReason(err))
		}
		return
	}
	c.SetReadDeadline(time.Time{})
	bytesIn = int64(len(tunnelHeader(cmd, a)))
	proto := "tcp"
	if cmd == cmdUDP {
		proto = "udp"
	}
	attempt := &forwardAttempt{journal: s.journal, fields: forwardFailure{Side: "target", Proto: proto}}
	if !s.open.authorizeForward(c, cmd, a) {
		attempt.fail("authorize_header", "target_not_allowed")
		s.stats.Refused.Add(1)
		c.Write([]byte{statusNotAllowed})
		bytesOut++
		return
	}
	attempt.fields.Match, attempt.fields.Port = forwardMatch(from, fast, a), a.Port
	s.stats.Opened.Add(1)
	s.stats.Active.Add(1)
	defer s.stats.Active.Add(-1)
	if cmd == cmdUDP {
		u, d := s.udp(c, a, attempt)
		return bytesIn + u, bytesOut + d
	}
	out, status := s.dialTCP(a, attempt)
	n, err := c.Write([]byte{status})
	bytesOut += int64(n)
	if err != nil || status != statusOK {
		if err != nil {
			attempt.fail("write_target_ack", socketReason(err))
		}
		if out != nil {
			out.Close()
		}
		return
	}
	defer out.Close()
	if !s.open.pair(c, out) {
		return
	}
	in, outBytes := relayObserved(c, out, attempt)
	s.stats.BytesIn.Add(in)
	s.stats.BytesOut.Add(outBytes)
	return bytesIn + in, bytesOut + outBytes
}

func resolveTarget(ctx context.Context, a Addr) ([]netip.Addr, byte, error) {
	if a.Host == "" {
		return []netip.Addr{a.IP}, statusOK, nil
	}
	found, err := net.DefaultResolver.LookupNetIP(ctx, "ip", a.Host)
	if err != nil || len(found) == 0 {
		return nil, statusHost, err
	}
	for i := range found {
		found[i] = found[i].Unmap()
	}
	return interleave(found), statusOK, nil
}

func (s *fwdTargetServer) dialTCP(a Addr, attempt *forwardAttempt) (*net.TCPConn, byte) {
	ctx, cancel := context.WithTimeout(context.Background(), dialTimeout)
	defer cancel()
	ips, status, err := resolveTarget(ctx, a)
	stage := "resolve_backend"
	var d net.Dialer
	for _, ip := range ips {
		stage = "dial_backend"
		var c net.Conn
		c, err = d.DialContext(ctx, "tcp", netip.AddrPortFrom(ip, a.Port).String())
		if err == nil {
			return c.(*net.TCPConn), statusOK
		}
		status = dialStatus(err)
		if ctx.Err() != nil {
			break
		}
	}
	s.stats.fail(statusReason(status))
	attempt.fail(stage, socketReason(err))
	return nil, status
}

// udp relays one client's datagrams to the target the forward names and its
// answers back; nothing else is sent or taken.
func (s *fwdTargetServer) udp(c *net.TCPConn, a Addr, attempt *forwardAttempt) (bytesIn, bytesOut int64) {
	var received atomic.Int64
	defer func() { bytesIn = received.Load() }()
	frameOverhead := int64(2 + len(appendAddr(nil, a)))
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	ips, status, err := resolveTarget(ctx, a)
	cancel()
	if status != statusOK {
		attempt.fail("resolve_backend", socketReason(err))
		s.stats.fail(statusReason(status))
		c.Write([]byte{status})
		return
	}
	to := netip.AddrPortFrom(ips[0], a.Port)
	pc, err := net.ListenUDP("udp", nil)
	if err != nil {
		attempt.fail("open_udp_socket", socketReason(err))
		s.stats.fail("target_failed")
		c.Write([]byte{statusFailure})
		return
	}
	defer pc.Close()
	if !s.open.pair(c, pc) {
		return
	}
	nstatus, statusErr := c.Write([]byte{statusOK})
	bytesOut += int64(nstatus)
	if statusErr != nil {
		attempt.fail("write_target_ack", socketReason(statusErr))
		return
	}
	var last atomic.Int64
	last.Store(time.Now().UnixNano())
	go func() {
		defer pc.Close()
		buf := make([]byte, 0x10000)
		for {
			fa, payload, err := readFrame(c, buf)
			if err != nil {
				attempt.streamError("read_tunnel_frame", err)
				return
			}
			received.Add(frameOverhead + int64(len(payload)))
			if fa != a {
				continue
			}
			last.Store(time.Now().UnixNano())
			if n, err := pc.WriteToUDPAddrPort(payload, to); err == nil {
				s.stats.BytesIn.Add(int64(n))
			} else {
				attempt.streamError("write_backend_datagram", err)
			}
		}
	}()
	buf := make([]byte, 0x10000)
	for {
		pc.SetReadDeadline(time.Now().Add(30 * time.Second))
		n, from, err := pc.ReadFromUDPAddrPort(buf)
		if err != nil {
			var ne net.Error
			if errors.As(err, &ne) && ne.Timeout() {
				if time.Since(time.Unix(0, last.Load())) < udpIdle {
					continue
				}
			} else {
				attempt.streamError("read_backend_datagram", err)
			}
			c.Close()
			return
		}
		if netip.AddrPortFrom(from.Addr().Unmap(), from.Port()) != to {
			continue
		}
		last.Store(time.Now().UnixNano())
		s.stats.BytesOut.Add(int64(n))
		if err := writeFrame(c, a, buf[:n]); err != nil {
			attempt.streamError("write_tunnel_frame", err)
			return
		}
		bytesOut += frameOverhead + int64(n)
	}
}

// ----- the service -----

// Forwarder runs a node's forwards and its part as their target.
type Forwarder struct {
	journal *forwardLog
	// FastDial and FastRTT, set before the first Apply, are the fast relay's:
	// a fast forward's tunnels go up its paths, without capture.
	FastDial    func(ctx context.Context, target netip.AddrPort) (*net.TCPConn, error)
	FastRTT     func(target netip.AddrPort) (time.Duration, bool)
	mu          sync.Mutex
	entries     map[string]*fwdEntry // by forward id
	stats       map[string]*fwdStats
	pools       map[int]*pool // by tunnel port
	target      *fwdTargetServer
	targetKey   string
	targetStats fwdStats
	targetOpen  conns
	last        func()
}

// Apply runs c (nil: nothing) with self, this node's mesh address. What did
// not change keeps running, its clients included.
func (s *Forwarder) Apply(c *ForwardConfig, self netip.Addr) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.last = func() { s.apply(c, self) }
	s.apply(c, self)
}

// Retry applies the last config again where a listener failed: a port
// another program held.
func (s *Forwarder) Retry() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.last == nil {
		return
	}
	for _, e := range s.entries {
		if len(e.errs) > 0 {
			e.listen()
		}
	}
	if s.target != nil && len(s.target.errs) > 0 {
		s.targetKey = ""
		s.last()
	}
}

// Close stops every forward.
func (s *Forwarder) Close() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.apply(nil, netip.Addr{})
	s.last = nil
}

func (s *Forwarder) apply(c *ForwardConfig, self netip.Addr) {
	if c == nil {
		c = &ForwardConfig{}
	}
	tk := ""
	if len(c.Targets) > 0 {
		tk = key(self, c.Port, c.FastPort, c.Targets)
	}
	if tk != s.targetKey {
		if s.target != nil {
			s.target.close()
			s.target = nil
		}
		s.targetKey = tk
		// Shared current policy covers old/prewarmed/in-flight tunnels too;
		// unrelated config updates preserve still-authorized streams.
		s.targetOpen.updateForward(c.Targets)
		if tk != "" {
			s.target = startTargets(self, c, &s.targetStats, &s.targetOpen, s.journal)
		}
	}
	want := map[string]Forward{}
	for _, f := range c.Forwards {
		want[f.ID] = f
	}
	ports := map[int]bool{}
	for _, f := range c.Forwards {
		ports[s.portOf(c, f)] = true
	}
	for port, p := range s.pools {
		if !ports[port] {
			p.close()
			delete(s.pools, port)
		}
	}
	for id, e := range s.entries {
		f, ok := want[id]
		if ok && e.key == entryKey(f, s.portOf(c, f)) {
			if k := allowKey(f, c.Sets); k != e.allowKey {
				e.allowKey = k
				e.setAllow(newAllowList(&f, c.Sets))
			}
			continue
		}
		e.close()
		delete(s.entries, id)
		if !ok {
			delete(s.stats, id)
		}
	}
	if len(c.Forwards) == 0 {
		return
	}
	if s.entries == nil {
		s.entries, s.stats, s.pools = map[string]*fwdEntry{}, map[string]*fwdStats{}, map[int]*pool{}
	}
	warm := map[int][]netip.Addr{}
	for _, f := range c.Forwards {
		port := s.portOf(c, f)
		if s.pools[port] == nil {
			s.pools[port] = newDialPool(port, s.dialer(f, port))
		}
		if !slices.Contains(warm[port], f.Node) {
			warm[port] = append(warm[port], f.Node)
		}
		if s.entries[f.ID] != nil {
			continue
		}
		st := s.stats[f.ID]
		if st == nil {
			st = &fwdStats{}
			s.stats[f.ID] = st
		}
		s.entries[f.ID] = startForward(f, entryKey(f, port), c.Sets, s.pools[port], st, s.journal, self)
	}
	for port, nodes := range warm {
		s.pools[port].warm(nodes)
	}
}

// dialer is how f's tunnels to port open: up the fast relay's paths for a
// fast forward, else nil, dialing them over the mesh.
func (s *Forwarder) dialer(f Forward, port int) func(context.Context, netip.Addr) (*net.TCPConn, error) {
	if !f.Fast || s.FastDial == nil {
		return nil
	}
	dial := s.FastDial
	return func(ctx context.Context, a netip.Addr) (*net.TCPConn, error) {
		return dial(ctx, netip.AddrPortFrom(a, uint16(port)))
	}
}

func (s *Forwarder) portOf(c *ForwardConfig, f Forward) int {
	if f.Fast {
		return c.FastPort
	}
	return c.Port
}

// ForwardStatus is how a node's forwards are doing.
type ForwardStatus struct {
	Forwards []ForwardEntryStatus `json:"forwards,omitempty"`
	Target   *ForwardTargetStatus `json:"target,omitempty"`
}

type ForwardEntryStatus struct {
	ID string `json:"id"`
	// Listen: what listens now, as tcp :443 or udp :443; Errors: what could not.
	Listen   []string `json:"listen"`
	Errors   []string `json:"errors,omitempty"`
	Active   int64    `json:"active"`
	Sessions int64    `json:"sessions"` // UDP clients now
	Opened   int64    `json:"opened"`
	Refused  int64    `json:"refused"` // clients the allow list turned away
	Failed   int64    `json:"failed"`
	BytesIn  int64    `json:"bytesIn"` // from clients
	BytesOut int64    `json:"bytesOut"`
	// LastError and LastErrorAt (Unix ms): why the last client went unserved.
	LastError   string `json:"lastError,omitempty"`
	LastErrorAt int64  `json:"lastErrorAt,omitempty"`
	// Ready, Down, TunnelError and RTTMs: the tunnels to the target.
	Ready       int      `json:"ready"`
	Down        bool     `json:"down,omitempty"`
	TunnelError string   `json:"tunnelError,omitempty"`
	RTTMs       *float64 `json:"rttMs,omitempty"`
	// Via, for a fast forward: fast while a fast path answers (RTTMs is
	// its), mesh while new connections go over the mesh.
	Via string `json:"via,omitempty"`
}

type ForwardTargetStatus struct {
	Listen      []string `json:"listen"`
	Errors      []string `json:"errors,omitempty"`
	Active      int64    `json:"active"`
	Opened      int64    `json:"opened"`
	Refused     int64    `json:"refused"`
	Failed      int64    `json:"failed"`
	BytesIn     int64    `json:"bytesIn"`
	BytesOut    int64    `json:"bytesOut"`
	LastError   string   `json:"lastError,omitempty"`
	LastErrorAt int64    `json:"lastErrorAt,omitempty"`
}

// Status is nil while the node has no forward and is no target.
func (s *Forwarder) Status() *ForwardStatus {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.entries) == 0 && s.target == nil {
		return nil
	}
	out := &ForwardStatus{}
	ids := make([]string, 0, len(s.entries))
	for id := range s.entries {
		ids = append(ids, id)
	}
	slices.Sort(ids)
	for _, id := range ids {
		e, st := s.entries[id], s.stats[id]
		es := ForwardEntryStatus{ID: id, Listen: []string{}, Errors: slices.Clone(e.errs), Active: st.Active.Load(), Sessions: st.Sessions.Load(),
			Opened: st.Opened.Load(), Refused: st.Refused.Load(), Failed: st.Failed.Load(), BytesIn: st.BytesIn.Load(), BytesOut: st.BytesOut.Load()}
		if e.tcp != nil {
			es.Listen = append(es.Listen, "tcp "+e.tcp.Addr().String())
		}
		if e.udp != nil {
			es.Listen = append(es.Listen, "udp "+e.udp.LocalAddr().String())
		}
		es.LastError, es.LastErrorAt = st.last()
		p := e.pool.status(e.cfg.Node)
		es.Ready, es.Down, es.TunnelError = p.Ready, p.Down, p.Error
		rtt := p.RTT
		if e.cfg.Fast && s.FastRTT != nil {
			es.Via, rtt = "mesh", 0
			if d, ok := s.FastRTT(netip.AddrPortFrom(e.cfg.Node, uint16(e.pool.port))); ok {
				es.Via, rtt = "fast", d
			}
		}
		if rtt > 0 {
			ms := float64(rtt.Microseconds()) / 1000
			es.RTTMs = &ms
		}
		out.Forwards = append(out.Forwards, es)
	}
	if t := s.target; t != nil {
		ts := &ForwardTargetStatus{Listen: []string{}, Errors: slices.Clone(t.errs), Active: s.targetStats.Active.Load(), Opened: s.targetStats.Opened.Load(),
			Refused: s.targetStats.Refused.Load(), Failed: s.targetStats.Failed.Load(), BytesIn: s.targetStats.BytesIn.Load(), BytesOut: s.targetStats.BytesOut.Load()}
		for _, ln := range t.lns {
			ts.Listen = append(ts.Listen, ln.Addr().String())
		}
		ts.LastError, ts.LastErrorAt = s.targetStats.last()
		out.Target = ts
	}
	return out
}

// ListenPorts are the TCP and UDP ports c's forwards take clients on, for
// the host's firewall.
func (c *ForwardConfig) ListenPorts() (tcp, udp []int) {
	if c == nil {
		return nil, nil
	}
	for _, f := range c.Forwards {
		if takes(f.Proto, "tcp") {
			tcp = append(tcp, f.Listen)
		}
		if takes(f.Proto, "udp") {
			udp = append(udp, f.Listen)
		}
	}
	slices.Sort(tcp)
	slices.Sort(udp)
	return tcp, udp
}
