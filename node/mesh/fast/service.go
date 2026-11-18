package fast

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/netip"
	"reflect"
	"slices"
	"strconv"
	"sync"
	"sync/atomic"
	"time"
)

const maxFastConns = 4096

var (
	errNotAllowed  = errors.New("rejected by access rules")
	errNoFastRoute = errors.New("no fast route to destination")
	errUnknownDest = errors.New("not a fast port")
	errTooMany     = errors.New("too many open fast connections")
)

// Hooks connects Service to the mesh engine's live routing and ownership state.
type Hooks struct {
	Dir string
	// Paths are up to k ways to dest in the order the mesh takes them: its
	// fixed paths from this node first, then its route and the next best.
	Paths func(dest string, k int, allow func(string) bool) []Plan
	// Route is the path after this node to dest of a stream planned no
	// further, over its alt-th path (0: the route): ending at dest when
	// planned, else at a relay that will plan the rest.
	Route func(dest string, alt int, allow func(string) bool, avoid map[string]bool) (path []string, planned bool, ok bool)
	// LinkUp reports whether peer is currently reachable from this node.
	LinkUp func(peer string) bool
	// SourceAllowed reports whether originID may carry traffic with source IP
	// (for gateway subnets without SNAT).
	SourceAllowed func(originID string, source netip.Addr) bool
	// LocalSubnets are this node's non-SNAT gateway subnets whose LAN clients
	// preserve their real source IP in mesh access rules.
	LocalSubnets func() []netip.Prefix
	// Member names the mesh member at a mesh address, "" for none.
	Member func(addr netip.Addr) string
	// ToOS gives the machine a packet as if from the mesh: the answers of
	// capture in the data plane.
	ToOS    func(pkt []byte)
	ToMesh  func(pkt []byte) // owned bytes, ordinary mesh route (not capture)
	MeshMTU func() uint32
	// LocalTCP consumes a TCP exit in-process when target is a forward tunnel.
	// Origin is metadata from the opened fast header, never from payload bytes.
	LocalTCP func(c *net.TCPConn, origin netip.Addr, target netip.AddrPort) (handled bool, bytesIn, bytesOut int64)
}

type roleCounters struct {
	active, opened, failed, in, out atomic.Int64
}

func (r *roleCounters) status() RoleStatus {
	return RoleStatus{
		Active:   r.active.Load(),
		Opened:   r.opened.Load(),
		Failed:   r.failed.Load(),
		BytesIn:  r.in.Load(),
		BytesOut: r.out.Load(),
	}
}

// targetKey is a fast port as connections name it: proto 0 for both.
type targetKey struct {
	addr  netip.Addr
	port  uint16
	proto byte
}

type runtimeState struct {
	// These references belong to this enabled lifetime, never to a later Apply.
	pool       *pool
	chans      *channels
	cfg        *Config
	selfID     string
	selfAddr   netip.Addr
	meshPrefix netip.Prefix
	idByAddr   map[netip.Addr]string
	dialEdges  map[[2]string]bool
	sealer     *sealer
	inbound    []Rule
	targets    map[targetKey]*Target
	dests      map[string]bool // the nodes some target is on
	// warmed is when packets to each of dests last reminded its channel.
	warmed map[string]*atomic.Int64
}

// target is the fast port proto connections to addr:port take, if any.
func (st *runtimeState) target(addr netip.Addr, port uint16, proto byte) (*Target, bool) {
	addr = addr.Unmap()
	if t := st.targets[targetKey{addr, port, proto}]; t != nil {
		return t, true
	}
	t := st.targets[targetKey{addr, port, 0}]
	return t, t != nil
}

func (st *runtimeState) knows(dest string) bool { return st.dests[dest] }

func targetsOf(c *Config, selfID string) (map[targetKey]*Target, map[string]bool) {
	out := map[targetKey]*Target{}
	dests := map[string]bool{}
	for i := range c.Targets {
		t := &c.Targets[i]
		addr, ok := c.Nodes[t.Node]
		if !ok || t.Node == selfID {
			continue
		}
		var proto byte
		switch t.Proto {
		case "tcp":
			proto = protoTCP
		case "udp":
			proto = protoUDP
		}
		out[targetKey{addr, t.Port, proto}] = t
		dests[t.Node] = true
	}
	return out, dests
}

// Service manages a node's fast relay port, connection pool, channels,
// capture and kernel tuning across config reloads.
type Service struct {
	mu    sync.Mutex
	hooks Hooks
	state atomic.Pointer[runtimeState]
	now   func() time.Time
	pool  *pool
	chans *channels
	// live is chans for the data plane, which never waits on mu.
	live    atomic.Pointer[channels]
	tcpmem  tcpMemManager
	relayLn net.Listener
	relayAt int
	lnErr   string
	relayWG sync.WaitGroup

	capture *captureRunner
	// tun is the capture in the data plane Outbound hands packets to.
	tun         atomic.Pointer[tunCapture]
	fallback    atomic.Pointer[sourceFallback]
	udpSessions *udpSessionTable
	// readyDests are the destinations a new connection finds a path to.
	readyDests atomic.Pointer[map[string]bool]

	// dialLn takes the connections Dial makes, which write dialKey first.
	dialLn  net.Listener
	dialKey [16]byte

	openMu  sync.Mutex
	open    map[io.Closer]trackedConn
	pending map[*net.TCPConn]struct{} // pre-authentication only, separate from business capacity
	// watchStop stops watchStalls.
	watchStop chan struct{}

	entryRole, transitRole, exitRole        roleCounters
	headerErrors, refusedEntry, refusedExit atomic.Int64
	lastApply                               func()
	closed                                  bool
}

// connRole is what this node is to a connection it carries.
type connRole byte

const (
	roleEntry connRole = iota
	roleTransit
	roleExit
)

type trackedConn struct {
	src, dst netip.Addr
	proto    byte
	port     uint16
	role     connRole
	// dest and path are an entry stream's destination node and the key of
	// the path it took.
	dest, path string
	// socks are its sockets to other nodes and to the client, watched for a
	// peer gone silent.
	socks []*net.TCPConn
}

func (s *Service) clock() time.Time {
	if s.now != nil {
		return s.now()
	}
	return time.Now()
}

// SetHooks sets engine callbacks before the first Apply.
func (s *Service) SetHooks(h Hooks) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.hooks = h
	s.tcpmem.init(h.Dir)
}

// Apply starts, updates or stops fast relay according to c (nil: disabled).
func (s *Service) Apply(c *Config, selfID string, selfAddr netip.Addr, meshPrefix netip.Prefix, inbound []Rule) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return
	}
	inboundCopy := slices.Clone(inbound)
	var cfgCopy *Config
	if c != nil {
		raw, _ := json.Marshal(c)
		cfgCopy = &Config{}
		_ = json.Unmarshal(raw, cfgCopy)
	}
	s.lastApply = func() { s.applyLocked(cfgCopy, selfID, selfAddr, meshPrefix, inboundCopy) }
	s.applyLocked(cfgCopy, selfID, selfAddr, meshPrefix, inboundCopy)
}

// Retry retries a listener or capture that failed earlier.
func (s *Service) Retry() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.closed {
		if st := s.state.Load(); st != nil && st.cfg.Port > 0 {
			s.tcpmem.apply()
		} else {
			s.tcpmem.restore()
		}
	}
	if !s.closed && s.lastApply != nil && (s.lnErr != "" || (s.capture != nil && s.capture.failed())) {
		s.lastApply()
	}
}

// Heal restores capture rules if removed externally.
func (s *Service) Heal() {
	s.mu.Lock()
	cr := s.capture
	s.mu.Unlock()
	if cr != nil {
		cr.heal()
	}
}

// Close stops listeners, pools, channels, capture and restores tcp_mem.
func (s *Service) Close() {
	s.mu.Lock()
	s.closed = true
	s.lastApply = nil
	if s.dialLn != nil {
		s.dialLn.Close()
		s.dialLn = nil
	}
	s.stopAllLocked()
	s.tcpmem.restore()
	chans := s.chans
	s.chans = nil
	s.live.Store(nil)
	s.mu.Unlock()
	if chans != nil {
		chans.close()
	}
}

func (s *Service) stopAllLocked() {
	s.openMu.Lock()
	s.state.Store(nil)
	for c := range s.pending {
		c.Close()
		delete(s.pending, c)
	}
	s.openMu.Unlock()
	if f := s.fallback.Swap(nil); f != nil {
		f.close()
	}
	if s.capture != nil {
		s.capture.close()
		s.capture = nil
	}
	if s.udpSessions != nil {
		s.udpSessions.close()
		s.udpSessions = nil
	}
	s.closeRelayLocked()
	if s.pool != nil {
		s.pool.close()
		s.pool = nil
	}
	s.lnErr = ""
	s.readyDests.Store(nil)
	if s.watchStop != nil {
		close(s.watchStop)
		s.watchStop = nil
	}
	s.closeAllConns(nil)
}

func (s *Service) closeRelayLocked() {
	if s.relayLn != nil {
		_ = s.relayLn.Close()
		s.relayLn = nil
		s.relayAt = 0
		s.relayWG.Wait()
	}
}

func (s *Service) applyLocked(c *Config, selfID string, selfAddr netip.Addr, meshPrefix netip.Prefix, inbound []Rule) {
	if c == nil || !selfAddr.IsValid() {
		s.stopAllLocked()
		s.tcpmem.restore()
		return
	}
	seal, err := newSealer(c.Keys)
	if err != nil {
		s.lnErr = err.Error()
		return
	}
	// Preserve replay cache across config reloads when keys overlap.
	prev := s.state.Load()
	if prev != nil && prev.sealer != nil {
		seal.seen = prev.sealer.seen
	}
	idByAddr := make(map[netip.Addr]string, len(c.Nodes))
	for id, a := range c.Nodes {
		idByAddr[a] = id
	}
	idByAddr[selfAddr] = selfID
	edges := make(map[[2]string]bool, len(c.Dial))
	for _, d := range c.Dial {
		edges[d] = true
	}
	targets, dests := targetsOf(c, selfID)
	st := &runtimeState{
		cfg:        c,
		selfID:     selfID,
		selfAddr:   selfAddr,
		meshPrefix: meshPrefix,
		idByAddr:   idByAddr,
		dialEdges:  edges,
		sealer:     seal,
		inbound:    inbound,
		targets:    targets,
		dests:      dests,
		warmed:     warmTimes(dests),
	}
	if c.Port > 0 {
		s.tcpmem.apply()
	} else {
		s.tcpmem.restore()
	}

	if s.pool == nil {
		s.pool = newPool()
	}
	s.pool.configure(c, selfID)
	if s.chans == nil {
		s.chans = newChannels(s)
		s.live.Store(s.chans)
	}
	if s.udpSessions == nil {
		s.udpSessions = newUDPSessionTable(s)
	}
	st.pool, st.chans = s.pool, s.chans
	// Admission checks and policy publication share this linearization point.
	s.openMu.Lock()
	s.state.Store(st)
	s.openMu.Unlock()
	if s.watchStop == nil {
		s.watchStop = make(chan struct{})
		go s.watchStalls(s.watchStop, stallCheck, stallAfter)
	}

	if s.relayLn != nil && s.relayAt != c.Port {
		s.closeRelayLocked()
	}
	if c.Port == 0 {
		s.lnErr = ""
	} else if s.relayLn == nil {
		ln, err := listenFixedTCP("tcp", net.JoinHostPort("0.0.0.0", strconv.Itoa(c.Port)), 0)
		if err != nil {
			s.lnErr = err.Error()
		} else {
			s.relayLn = ln
			s.relayAt = c.Port
			s.lnErr = ""
			s.relayWG.Add(1)
			go s.acceptRelay(ln)
		}
	}

	s.applyCaptureLocked(c)
	s.publishReady(st, s.chans, s.capture)

	// Revoke open connections that changed rules no longer permit. Rules are
	// checked where a stream enters and where it leaves; a node it only
	// passes through has no say over it.
	if prev != nil && rulesChanged(prev, st) {
		s.closeAllConns(func(t trackedConn) bool { return !st.allows(t) })
	}
}

func (st *runtimeState) allows(t trackedConn) bool {
	switch t.role {
	case roleEntry:
		return st.entryAllowed(t.proto, t.src, t.dst, t.port)
	case roleExit:
		return t.dst == st.selfAddr && ruleAllows(st.inbound, t.proto, t.src, st.selfAddr, t.port)
	default:
		return true
	}
}

func rulesChanged(prev, next *runtimeState) bool {
	return prev.selfAddr != next.selfAddr || !reflect.DeepEqual(prev.cfg.Targets, next.cfg.Targets) || !reflect.DeepEqual(prev.cfg.Nodes, next.cfg.Nodes) || !reflect.DeepEqual(prev.inbound, next.inbound)
}

// entryAllowed is what an entry checks before opening a stream: that dst:port
// is a fast port src may use.
func (st *runtimeState) entryAllowed(proto byte, src, dst netip.Addr, port uint16) bool {
	t, ok := st.target(dst, port, proto)
	return ok && prefixMatches(t.Src, src)
}

// readyChanged follows the channels: what is ready to take connections now.
func (s *Service) readyChanged() {
	s.mu.Lock()
	defer s.mu.Unlock()
	chans, cr := s.chans, s.capture
	if st := s.state.Load(); st != nil {
		s.publishReady(st, chans, cr)
	}
}

func (s *Service) publishReady(st *runtimeState, chans *channels, cr *captureRunner) {
	var ready map[string]bool
	if chans != nil {
		ready = chans.ready()
	}
	s.readyDests.Store(&ready)
	if cr != nil {
		var keys []targetKey
		for k, t := range st.targets {
			if ready[t.Node] {
				keys = append(keys, k)
			}
		}
		cr.update(keys)
	}
}

func (s *Service) isReady(dest string) bool {
	m := s.readyDests.Load()
	return m != nil && (*m)[dest]
}

func (s *Service) trackConn(down *net.TCPConn, src, dst netip.Addr, proto byte, port uint16, role connRole) bool {
	return s.track(down, trackedConn{src: src, dst: dst, proto: proto, port: port, role: role, socks: []*net.TCPConn{down}})
}

// trackStream tracks c, an entry stream opened as o. Its socket over the
// mesh, where no path took it, is left to the mesh's own failover.
func (s *Service) trackStream(c io.Closer, o opened, proto byte, port uint16) bool {
	t := trackedConn{src: o.src, dst: o.dst, proto: proto, port: port, role: roleEntry, dest: o.dest, path: o.plan.key()}
	if o.up != nil {
		t.socks = append(t.socks, o.up)
	}
	if es, ok := c.(*entryStream); ok {
		if client, ok := es.client.(*net.TCPConn); ok {
			t.socks = append(t.socks, client)
		}
	}
	return s.track(c, t)
}

func (s *Service) track(c io.Closer, t trackedConn) bool {
	s.openMu.Lock()
	defer s.openMu.Unlock()
	st := s.state.Load()
	if st == nil || !st.allows(t) || len(s.open) >= maxFastConns {
		return false
	}
	if s.open == nil {
		s.open = map[io.Closer]trackedConn{}
	}
	s.open[c] = t
	return true
}

// entryStream is an entry's client connection and the stream it took up its
// path, closed together: a client that already left would leave the stream
// waiting on a path that no longer answers.
type entryStream struct {
	client io.Closer
	up     io.Closer
}

func (e *entryStream) Close() error {
	closeTracked(e.up)
	if tc, ok := e.client.(*net.TCPConn); ok {
		abortTCP(tc)
		return nil
	}
	return e.client.Close()
}

// cutPath closes the entry streams to dest that took the path keyed path,
// which stopped answering: their clients reconnect, and the new connections
// take a path that answers.
func (s *Service) cutPath(dest, path string) {
	s.closeAllConns(func(t trackedConn) bool { return t.role == roleEntry && t.dest == dest && t.path == path })
}

// streamsTo reports whether an entry stream to dest is open.
func (s *Service) streamsTo(dest string) bool {
	s.openMu.Lock()
	defer s.openMu.Unlock()
	for _, t := range s.open {
		if t.role == roleEntry && t.dest == dest {
			return true
		}
	}
	return false
}

func (s *Service) untrackConn(c io.Closer) {
	s.openMu.Lock()
	delete(s.open, c)
	s.openMu.Unlock()
}

func (s *Service) closeAllConns(drop func(trackedConn) bool) {
	s.closeConns(func(_ io.Closer, t trackedConn) bool { return drop == nil || drop(t) })
}

// closeConns aborts the streams pick picks, each socket of theirs: one blocked
// on the socket of a peer gone silent wakes only so.
func (s *Service) closeConns(pick func(io.Closer, trackedConn) bool) {
	s.openMu.Lock()
	var list []io.Closer
	for c, t := range s.open {
		if pick(c, t) {
			delete(s.open, c) // late socket registration must not resurrect a cut stream
			list = append(list, c)
			for _, sock := range t.socks {
				list = append(list, sock)
			}
		}
	}
	s.openMu.Unlock()
	for _, c := range list {
		closeTracked(c)
	}
}

func closeTracked(c io.Closer) {
	if tc, ok := c.(*net.TCPConn); ok {
		abortTCP(tc)
	} else {
		_ = c.Close()
	}
}

// RelayAddr returns the active relay listener's local address (for tests).
func (s *Service) RelayAddr() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.relayLn == nil {
		return ""
	}
	return s.relayLn.Addr().String()
}

func (s *Service) acceptRelay(ln net.Listener) {
	defer s.relayWG.Done()
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
		_ = tc.SetNoDelay(true)
		_ = tc.SetKeepAliveConfig(peerKeepAlive)
		limitRetransmit(tc)
		if !s.admitHeader(tc) {
			tc.Close()
			continue
		}
		go s.serveRelay(tc)
	}
}

// maxPendingHeaders bounds unauthenticated sockets only. It does not consume
// business slots or impose per-IP limits on nodes behind a shared egress.
const maxPendingHeaders = 4096

func (s *Service) admitHeader(c *net.TCPConn) bool {
	s.openMu.Lock()
	defer s.openMu.Unlock()
	if s.state.Load() == nil || len(s.pending) >= pendingHeaderLimit() {
		return false
	}
	if s.pending == nil {
		s.pending = make(map[*net.TCPConn]struct{})
	}
	s.pending[c] = struct{}{}
	return true
}
func (s *Service) headerDone(c *net.TCPConn) {
	s.openMu.Lock()
	delete(s.pending, c)
	s.openMu.Unlock()
}

func (s *Service) serveRelay(c *net.TCPConn) {
	defer s.headerDone(c)
	defer c.Close()
	st := s.state.Load()
	if st == nil {
		return
	}
	_ = c.SetReadDeadline(time.Now().Add(headerWait))
	var buf [sealedLen]byte
	if _, err := io.ReadFull(c, buf[:]); err != nil {
		return
	}
	_ = c.SetReadDeadline(time.Time{})
	st = s.state.Load()
	if st == nil {
		return
	}
	h, err := st.sealer.open(buf[:], s.clock())
	if err != nil {
		s.headerErrors.Add(1)
		return
	}
	s.headerDone(c)
	if h.dest == st.selfAddr {
		s.serveExit(st, c, h)
		return
	}
	s.serveTransit(st, c, h)
}

func (s *Service) serveTransit(st *runtimeState, down *net.TCPConn, h header) {
	destID := st.idByAddr[h.dest]
	if destID == "" || slices.Contains(h.visited, st.selfAddr) || len(h.visited) >= maxVisited {
		s.headerErrors.Add(1)
		return
	}
	if len(h.hops) > 0 && h.hops[0] == st.selfAddr {
		h.hops = h.hops[1:]
	}
	h.visited = append(slices.Clone(h.visited), st.selfAddr)
	avoid := make(map[string]bool, len(h.visited))
	for _, v := range h.visited {
		if id := st.idByAddr[v]; id != "" {
			avoid[id] = true
		}
	}
	var nextID string
	if len(h.hops) > 0 {
		if cand := st.idByAddr[h.hops[0]]; cand != "" && !avoid[cand] && s.isLinkUp(st, cand) {
			nextID = cand
		}
	}
	if nextID == "" {
		hops, planned, ok := s.planHops(st, destID, int(h.alt), avoid)
		if !ok || len(hops) == 0 {
			if !h.probe {
				s.transitRole.failed.Add(1)
			}
			abortTCP(down)
			return
		}
		nextID = hops[0]
		h.planned = planned
		h.hops = addrsForIDs(st.cfg.Nodes, hops)
	}
	wire, err := st.sealer.seal(h)
	if err != nil {
		abortTCP(down)
		return
	}
	if h.probe {
		up, err := st.pool.get(nextID)
		if err != nil {
			abortTCP(down)
			return
		}
		defer up.Close()
		// No one waits on a probe longer.
		deadline := time.Now().Add(probeTimeout)
		_ = down.SetDeadline(deadline)
		_ = up.SetDeadline(deadline)
		if _, err := up.Write(wire); err != nil {
			abortTCP(up)
			abortTCP(down)
			return
		}
		relayTCP(down, up, nil, nil)
		return
	}
	if !s.trackConn(down, h.source, h.dest, h.proto, h.port, roleTransit) {
		s.transitRole.failed.Add(1)
		abortTCP(down)
		return
	}
	defer s.untrackConn(down)

	s.transitRole.opened.Add(1)
	up, err := st.pool.get(nextID)
	if err != nil {
		s.transitRole.failed.Add(1)
		abortTCP(down)
		return
	}
	defer up.Close()
	s.watchToo(down, up)
	if _, err := up.Write(wire); err != nil {
		s.transitRole.failed.Add(1)
		abortTCP(up)
		abortTCP(down)
		return
	}
	s.transitRole.active.Add(1)
	defer s.transitRole.active.Add(-1)
	inBytes, outBytes, _ := relayTCP(down, up, nil, nil)
	s.transitRole.in.Add(inBytes)
	s.transitRole.out.Add(outBytes)
}

func (s *Service) serveExit(st *runtimeState, down *net.TCPConn, h header) {
	originID := st.idByAddr[h.origin]
	if originID == "" {
		s.mu.Lock()
		member := s.hooks.Member
		s.mu.Unlock()
		if member != nil {
			originID = member(h.origin)
		}
	}
	if originID == "" || !s.validSource(originID, h.origin, h.source) {
		s.refusedExit.Add(1)
		abortTCP(down)
		return
	}
	if h.probe {
		_ = down.SetWriteDeadline(time.Now().Add(probeTimeout))
		_, _ = down.Write(probeAnswer(append(slices.Clone(h.visited), st.selfAddr)))
		return
	}
	if !ruleAllows(st.inbound, h.proto, h.source, st.selfAddr, h.port) {
		s.refusedExit.Add(1)
		abortTCP(down)
		return
	}
	if !s.trackConn(down, h.source, st.selfAddr, h.proto, h.port, roleExit) {
		s.exitRole.failed.Add(1)
		abortTCP(down)
		return
	}
	defer s.untrackConn(down)

	s.exitRole.opened.Add(1)
	if h.proto == protoUDP {
		s.serveExitUDP(st, down, h)
		return
	}
	s.mu.Lock()
	localTCP := s.hooks.LocalTCP
	s.mu.Unlock()
	if localTCP != nil {
		s.exitRole.active.Add(1)
		handled, in, out := localTCP(down, h.origin, netip.AddrPortFrom(st.selfAddr, h.port))
		s.exitRole.active.Add(-1)
		if handled {
			s.exitRole.in.Add(in)
			s.exitRole.out.Add(out)
			return
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), localDialTimeout)
	local, err := dialAddrTCP(ctx, netip.AddrPortFrom(st.selfAddr, h.port), fixedRcvBuf)
	cancel()
	if err != nil {
		s.exitRole.failed.Add(1)
		abortTCP(down)
		return
	}
	defer local.Close()
	s.watchToo(down, local)
	s.exitRole.active.Add(1)
	defer s.exitRole.active.Add(-1)
	inBytes, outBytes, _ := relayTCP(down, local, nil, nil)
	s.exitRole.in.Add(inBytes)
	s.exitRole.out.Add(outBytes)
}

// Shared fast keys authenticate possession, not an independently proved origin.
// Participants trust other key holders to report origin/source honestly. These
// checks constrain ordinary traffic, not a malicious key holder impersonating
// another mesh node; possession grants no OS privileges or arbitrary destinations.
func (s *Service) validSource(originID string, origin, source netip.Addr) bool {
	if source == origin {
		return true
	}
	s.mu.Lock()
	fn := s.hooks.SourceAllowed
	s.mu.Unlock()
	return fn != nil && fn(originID, source)
}

// clientSource returns the source IP access rules see for clientAddr: a LAN
// client in a non-SNAT gateway subnet keeps its IP; everything else (local
// processes, SNAT gateways) uses this node's mesh address.
func (s *Service) clientSource(st *runtimeState, clientAddr netip.Addr, preserveLAN bool) netip.Addr {
	if preserveLAN && clientAddr.IsValid() {
		ip := clientAddr.Unmap()
		s.mu.Lock()
		fn := s.hooks.LocalSubnets
		s.mu.Unlock()
		if fn != nil {
			for _, sub := range fn() {
				if sub.Contains(ip) {
					return ip
				}
			}
		}
	}
	return st.selfAddr
}

// opened is a stream a connection to a fast port takes: up its first hop,
// or nil when it is to go over the mesh instead (no path answers).
type opened struct {
	up             *net.TCPConn
	src, dst       netip.Addr
	dest           string
	plan           Plan
	preserveSource bool
}

// openStream checks that target is a fast port client may use and sends the
// connection's header up the first path of its channel that takes it. With
// none ready, it returns errNoFastRoute: the connection goes over the mesh.
func (s *Service) openStream(proto byte, client netip.AddrPort, preserveLAN bool, target netip.AddrPort) (opened, error) {
	st := s.state.Load()
	if st == nil || st.pool == nil || st.chans == nil {
		return opened{}, net.ErrClosed
	}
	t, ok := st.target(target.Addr(), target.Port(), proto)
	if !ok {
		return opened{}, errUnknownDest
	}
	o := opened{src: s.clientSource(st, client.Addr(), preserveLAN), dst: target.Addr().Unmap(), dest: t.Node}
	o.preserveSource = o.src != st.selfAddr
	if !prefixMatches(t.Src, o.src) {
		s.refusedEntry.Add(1)
		return o, errNotAllowed
	}
	plans := st.chans.pick(t.Node)
	for i, plan := range plans {
		if i == 2 {
			break
		}
		up, err := s.sendHeader(st, plan, header{proto: proto, dest: o.dst, port: target.Port(), source: o.src, sourcePort: client.Port()})
		if err == nil {
			o.up, o.plan = up, plan
			return o, nil
		}
		st.chans.failed(t.Node, plan, err)
	}
	if len(plans) > 0 {
		st.chans.fellBack(t.Node)
	}
	return o, errNoFastRoute
}

// entryTCP carries c, a connection captured on its way from client to
// target, along target's channel, or over the mesh when no path takes it.
// opened, when set, is told once the stream is up, before anything goes
// through; its error drops the connection.
func (s *Service) entryTCP(c *net.TCPConn, client netip.AddrPort, preserveLAN bool, target netip.AddrPort, opened func() error) {
	s.entryRole.opened.Add(1)
	o, err := s.openStream(protoTCP, client, preserveLAN, target)
	var up net.Conn = o.up
	if errors.Is(err, errNoFastRoute) {
		if o.preserveSource {
			ctx, cancel := context.WithTimeout(context.Background(), localDialTimeout)
			up, err = s.sourceDial(ctx, protoTCP, netip.AddrPortFrom(o.src, client.Port()), target)
			cancel()
		} else {
			up, err = dialMesh(target)
		}
	}
	if err != nil {
		if !errors.Is(err, errNotAllowed) {
			s.entryRole.failed.Add(1)
		}
		abortTCP(c)
		return
	}
	defer up.Close()
	es := &entryStream{client: c, up: up}
	if !s.trackStream(es, o, protoTCP, target.Port()) {
		s.entryRole.failed.Add(1)
		up.Close()
		abortTCP(c)
		return
	}
	defer s.untrackConn(es)
	if opened != nil && opened() != nil {
		s.entryRole.failed.Add(1)
		up.Close()
		abortTCP(c)
		return
	}

	s.entryRole.active.Add(1)
	defer s.entryRole.active.Add(-1)
	var toUp, toClient int64
	var aborted bool
	if tcp, ok := up.(*net.TCPConn); ok {
		toUp, toClient, aborted = relayTCP(c, tcp, nil, nil)
	} else {
		toUp, toClient = relaySource(c, up)
	}
	s.entryRole.out.Add(toUp)
	s.entryRole.in.Add(toClient)
	if chans := s.live.Load(); aborted && o.up != nil && chans != nil {
		chans.recheck(o.dest, o.plan)
	}
}

// sendHeader dials plan's first hop from the pool and writes h, planned by
// plan, on it.
func (s *Service) sendHeader(st *runtimeState, plan Plan, h header) (*net.TCPConn, error) {
	if len(plan.Hops) == 0 {
		return nil, errNoFastRoute
	}
	h.planned, h.alt = plan.Planned, plan.Alt
	h.at = s.clock()
	h.origin = st.selfAddr
	h.visited = []netip.Addr{st.selfAddr}
	h.hops = addrsForIDs(st.cfg.Nodes, plan.Hops)
	if len(h.hops) != len(plan.Hops) {
		return nil, errNoFastRoute
	}
	wire, err := st.sealer.seal(h)
	if err != nil {
		return nil, err
	}
	up, err := st.pool.get(plan.Hops[0])
	if err != nil {
		return nil, err
	}
	if _, err := up.Write(wire); err != nil {
		abortTCP(up)
		return nil, err
	}
	return up, nil
}

// probe sends a probe to dest along plan and waits for its answer: the nodes
// it came through after this one.
func (s *Service) probe(dest string, plan Plan, timeout time.Duration) ([]string, error) {
	st := s.state.Load()
	if st == nil || st.pool == nil {
		return nil, errNoFastRoute
	}
	addr, ok := st.cfg.Nodes[dest]
	if !ok {
		return nil, errUnknownDest
	}
	up, err := s.sendHeader(st, plan, header{proto: protoTCP, probe: true, dest: addr, port: probePort, source: st.selfAddr})
	if err != nil {
		return nil, err
	}
	defer up.Close()
	_ = up.SetReadDeadline(time.Now().Add(timeout))
	visited, err := readProbeAnswer(up)
	if err != nil {
		return nil, err
	}
	if len(visited) == 0 || visited[len(visited)-1] != addr {
		return nil, errProbeAnswer
	}
	var path []string
	for _, a := range visited[1:] {
		path = append(path, st.idByAddr[a])
	}
	return path, nil
}

// paths are the ways to dest a channel tries, in the mesh's order.
func (s *Service) paths(st *runtimeState, dest string) []Plan {
	allow := func(id string) bool {
		_, ok := st.cfg.Nodes[id]
		return ok
	}
	s.mu.Lock()
	fn := s.hooks.Paths
	s.mu.Unlock()
	if fn != nil {
		if plans := fn(dest, st.cfg.pathCount(), allow); len(plans) > 0 {
			return plans
		}
	}
	return []Plan{{Hops: []string{dest}, Planned: true}}
}

func (s *Service) planHops(st *runtimeState, destID string, alt int, avoid map[string]bool) ([]string, bool, bool) {
	allow := func(id string) bool {
		_, ok := st.cfg.Nodes[id]
		return ok
	}
	s.mu.Lock()
	routeFn := s.hooks.Route
	s.mu.Unlock()
	if routeFn != nil {
		if path, planned, ok := routeFn(destID, alt, allow, avoid); ok && len(path) > 0 {
			return path, planned, true
		}
	}
	if !avoid[destID] && allow(destID) {
		return []string{destID}, true, true
	}
	return nil, false, false
}

func (s *Service) isLinkUp(st *runtimeState, peer string) bool {
	s.mu.Lock()
	fn := s.hooks.LinkUp
	s.mu.Unlock()
	if fn != nil {
		return fn(peer)
	}
	_, ok := st.cfg.Nodes[peer]
	return ok
}

func addrsForIDs(nodes map[string]netip.Addr, ids []string) []netip.Addr {
	out := make([]netip.Addr, 0, len(ids))
	for _, id := range ids {
		if a, ok := nodes[id]; ok {
			out = append(out, a)
		}
	}
	return out
}

// dialAddrTCP and dialAddrUDP reach mesh addresses: this node's own, where a
// stream leaves, and others over the mesh.
var (
	dialAddrTCP = dialTCP
	dialAddrUDP = dialUDP
)

// dialMesh is a connection's way when no path takes it: to target over the
// mesh, as it would have gone without fast relay.
func dialMesh(target netip.AddrPort) (*net.TCPConn, error) {
	ctx, cancel := context.WithTimeout(context.Background(), localDialTimeout)
	defer cancel()
	return dialAddrTCP(ctx, target, 0)
}

// Status snapshots fast relay state and counters for status.json.
func (s *Service) Status() *Status {
	st := s.state.Load()
	if st == nil {
		return nil
	}
	s.mu.Lock()
	port := 0
	if s.relayLn != nil {
		port = s.relayAt
	}
	lnErr := s.lnErr
	var peers []PeerPoolStatus
	if s.pool != nil {
		peers = s.pool.status()
	}
	var capture *CaptureStatus
	if s.capture != nil {
		capture = s.capture.status()
	}
	chans := s.chans
	tcpSt := s.tcpmem.status()
	s.mu.Unlock()
	var channels []ChannelStatus
	if chans != nil {
		channels = chans.status()
	}

	return &Status{
		Port:         port,
		ListenError:  lnErr,
		Capture:      capture,
		Channels:     channels,
		Entry:        s.entryRole.status(),
		Transit:      s.transitRole.status(),
		Exit:         s.exitRole.status(),
		Peers:        peers,
		HeaderErrors: s.headerErrors.Load(),
		RefusedEntry: s.refusedEntry.Load(),
		RefusedExit:  s.refusedExit.Load(),
		TCPMem:       tcpSt,
	}
}
