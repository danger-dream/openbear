package transport

import (
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"golang.zx2c4.com/wireguard/conn"
	"openbear/node/mesh/wire"
)

type Options struct {
	ListenUDP, ListenTCP, ListenTLS int
	TLSDir                          string
	MTU                             int
	ResourceProfile                 string
	// Reflector answers reflector requests on the UDP listener.
	Reflector bool
	// UDP keeps a UDP socket, an ephemeral one without ListenUDP, even
	// with no UDP path: for reachability tests and punched paths.
	UDP bool
	// Protocol is the mesh protocol range this node speaks (package wire);
	// zero is this build's.
	Protocol wire.Range
	Logf     func(format string, args ...any)
}

type PathStats struct {
	Endpoint, Transport, LocalAddr, RemoteAddr, State string
	Outbound, Authenticated                           bool
	CertificateID, FingerprintSHA256, LastError       string
	DialAttempts, Reconnects, RxBytes, TxBytes        uint64
	Dropped, RxQueueBytes, TxBufferedBytes            uint64
	// Redials counts the connections Redial moved the path to.
	Redials           uint64
	SocketBufferBytes int
	ObservedAt        time.Time
}

type limits struct{ paths, unauth, rxBytes, socketBytes int }

func (o Options) limits() limits {
	if o.ResourceProfile == "relay" {
		return limits{128, 16, 16 << 20, 64 << 20}
	}
	return limits{16, 8, 8 << 20, 16 << 20}
}
func options(o Options) (Options, error) {
	if o.MTU == 0 {
		o.MTU = 1400
	}
	o.Protocol = o.Protocol.Or()
	if o.Protocol.Min < 1 || o.Protocol.Min > o.Protocol.Max || o.Protocol.Max > 8 {
		return o, errors.New("invalid protocol range")
	}
	if o.MTU < 576 || maxDatagram(o.MTU) > 65534 {
		return o, errors.New("MTU must be between 576 and 65488")
	}
	if o.ResourceProfile != "" && o.ResourceProfile != "member" && o.ResourceProfile != "relay" {
		return o, errors.New("invalid resource profile")
	}
	for _, p := range []int{o.ListenUDP, o.ListenTCP, o.ListenTLS} {
		if p < 0 || p > 65535 {
			return o, errors.New("invalid listener port")
		}
	}
	if o.ListenTLS != 0 && o.TLSDir == "" {
		return o, errors.New("TLSDir is required for a TLS listener")
	}
	return o, nil
}

var (
	ErrBackpressure = errors.New("transport buffer limit reached")
	ErrSecurity     = errors.New("TLS peer verification failed")
	// ErrNoPath: a peer's own endpoint with no path behind it, as a
	// WireGuard peer with no endpoint; a closed path is net.ErrClosed alone.
	ErrNoPath = fmt.Errorf("no path to peer: %w", net.ErrClosed)
)

type Bind struct {
	op            sync.Mutex
	mu            sync.Mutex
	opt           Options
	run           *bindRun
	paths         map[string]*endpoint
	pins          map[string]pinSet
	peers         map[string]*peerPaths
	ids           map[string]*endpoint
	managed       bool
	nextID        uint64
	events        chan PathEvent
	droppedEvents uint64
	reflectLimit  reflectLimiter
	reflectWait   map[[8]byte]*reflectWaiter
	reflectIDs    map[[8]byte]reflectSeen
	reflectFrom   map[netip.Addr]time.Time
	tests         map[[8]byte]byte
	stunWait      map[[12]byte]*stunWaiter
}

type bindRun struct {
	ctx                              context.Context
	cancel                           context.CancelFunc
	wg                               sync.WaitGroup
	rx                               chan received
	control                          *controlRun
	controlBytes                     int
	listeners                        map[string]*listener
	udp                              *udpSocket
	sessions                         map[*session]bool
	unauth, queuedBytes, socketBytes int
}

type listener struct {
	net.Listener
	transport     string
	port          int
	cert          *tls.Certificate
	certificateID string
	done          chan struct{}
}

type endpoint struct {
	b                       *Bind
	id, peer                string
	generation              uint64
	auth                    [2]bool
	lastAuthenticated       [2]time.Time
	authenticatedPackets    [2]uint64
	controlRxBytes          int
	preparing               int
	prepared                bool
	key, transport, address string
	remote                  netip.AddrPort
	outbound                bool
	retired                 bool
	stats                   PathStats
	udpWriters              [2]int // datagrams being written, by plane
	rxVersion               int    // protocol of the last datagram from it
	current                 *session
	// ctx is a dialed stream path's: cancelled when it is released.
	ctx       context.Context
	redialing bool
	cancel    context.CancelFunc
	done      chan struct{}
	wake      chan struct{}
}

type received struct {
	data    []byte
	plane   uint8
	ref     PathRef
	at      time.Time
	ep      *endpoint
	session *session
	udp     *udpSocket
}

func New(opt Options) (*Bind, error) {
	opt, err := options(opt)
	if err != nil {
		return nil, err
	}
	return &Bind{opt: opt, paths: map[string]*endpoint{}, pins: map[string]pinSet{}, peers: map[string]*peerPaths{}, ids: map[string]*endpoint{}, events: make(chan PathEvent, 256), reflectWait: map[[8]byte]*reflectWaiter{},
		reflectIDs: map[[8]byte]reflectSeen{}, reflectFrom: map[netip.Addr]time.Time{}, tests: map[[8]byte]byte{}, stunWait: map[[12]byte]*stunWaiter{}}, nil
}

func endpointKey(s string) (string, string, string, error) {
	u, err := url.Parse(s)
	if err != nil || (u.Scheme != "udp" && u.Scheme != "tcp" && u.Scheme != "tls") || u.User != nil || u.Path != "" || u.RawQuery != "" || u.Fragment != "" || u.Opaque != "" {
		return "", "", "", errors.New("endpoint must be udp://, tcp:// or tls://host:port")
	}
	h, p, err := net.SplitHostPort(u.Host)
	port, e := strconv.Atoi(p)
	if err != nil || e != nil || h == "" || port < 1 || port > 65535 {
		return "", "", "", errors.New("invalid endpoint host or port")
	}
	address := net.JoinHostPort(strings.ToLower(h), strconv.Itoa(port))
	return u.Scheme + "://" + address, u.Scheme, address, nil
}

func (b *Bind) ParseEndpoint(s string) (conn.Endpoint, error) {
	if strings.HasPrefix(s, "mesh-peer://") {
		return b.parseLogical(s)
	}
	key, transport, address, err := endpointKey(s)
	if err != nil {
		return nil, err
	}
	// A path that exists keeps its address; refreshNames follows a name.
	b.mu.Lock()
	ep := b.paths[key]
	b.mu.Unlock()
	if ep != nil && ep.outbound {
		return ep, nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	remote, err := resolveRemote(ctx, address, netip.AddrPort{})
	cancel()
	if err != nil {
		return nil, err
	}
	b.op.Lock()
	defer b.op.Unlock()
	b.mu.Lock()
	defer b.mu.Unlock()
	if ep := b.paths[key]; ep != nil && ep.outbound {
		return ep, nil
	}
	if len(b.paths) >= b.opt.limits().paths {
		return nil, ErrBackpressure
	}
	if transport == "tls" {
		if p := b.pins[key]; p.err != nil || len(p.values) == 0 {
			return nil, fmt.Errorf("%w: missing or invalid pins", ErrSecurity)
		}
	}
	ep = &endpoint{b: b, key: key, transport: transport, address: address, remote: remote, outbound: true, wake: make(chan struct{}, 1)}
	ep.stats = PathStats{Endpoint: key, Transport: transport, RemoteAddr: ep.remote.String(), Outbound: true, State: "closed"}
	b.paths[key] = ep
	b.initPath(ep)
	if b.run != nil {
		if transport == "udp" && b.run.udp == nil {
			u, err := openUDP(0)
			if err != nil {
				delete(b.paths, key)
				return nil, err
			}
			if b.run.socketBytes+u.buffers > b.opt.limits().socketBytes {
				u.Close()
				delete(b.paths, key)
				return nil, ErrBackpressure
			}
			b.startUDP(b.run, u)
		}
		b.startEndpoint(b.run, ep)
	}
	return ep, nil
}

// ListenUDP controls the public socket; zero permits only an ephemeral socket for configured outbound UDP paths.
func (b *Bind) Open(port uint16) ([]conn.ReceiveFunc, uint16, error) {
	b.op.Lock()
	defer b.op.Unlock()
	b.mu.Lock()
	if b.run != nil {
		b.mu.Unlock()
		return nil, 0, conn.ErrBindAlreadyOpen
	}
	opt := b.opt
	if port != 0 && opt.ListenUDP != 0 && int(port) != opt.ListenUDP {
		b.mu.Unlock()
		return nil, 0, errors.New("Bind.Open UDP port differs from Options")
	}
	needUDP := opt.ListenUDP != 0 || opt.UDP
	for _, ep := range b.paths {
		needUDP = needUDP || ep.transport == "udp"
	}
	b.mu.Unlock()
	ctx, cancel := context.WithCancel(context.Background())
	r := &bindRun{ctx: ctx, cancel: cancel, rx: make(chan received, 256), listeners: map[string]*listener{}, sessions: map[*session]bool{}}
	abort := func(err error) ([]conn.ReceiveFunc, uint16, error) {
		cancel()
		if r.udp != nil {
			r.udp.Close()
		}
		for _, l := range r.listeners {
			l.Close()
		}
		return nil, 0, err
	}
	if needUDP {
		u, err := openUDP(opt.ListenUDP)
		if err != nil {
			return abort(err)
		}
		r.udp = u
		r.socketBytes = u.buffers
	}
	for _, t := range []string{"tcp", "tls"} {
		p := opt.ListenTCP
		if t == "tls" {
			p = opt.ListenTLS
		}
		if p == 0 {
			continue
		}
		l, err := newListener(t, p, opt.TLSDir)
		if err != nil {
			return abort(err)
		}
		r.listeners[t] = l
	}
	b.mu.Lock()
	b.run = r
	if r.udp != nil {
		u := r.udp
		r.udp = nil
		r.socketBytes = 0
		b.startUDP(r, u)
	}
	for _, l := range r.listeners {
		r.wg.Add(1)
		go b.accept(r, l)
	}
	for _, ep := range b.paths {
		b.startEndpoint(r, ep)
	}
	r.wg.Add(1)
	go b.refreshNames(r)
	actualPort := udpPort(r.udp)
	b.mu.Unlock()
	return []conn.ReceiveFunc{func(p [][]byte, s []int, e []conn.Endpoint) (int, error) { return b.receive(r, p, s, e) }}, uint16(actualPort), nil
}

func (b *Bind) startEndpoint(r *bindRun, ep *endpoint) {
	if ep.transport == "udp" {
		b.newGeneration(ep)
		ep.stats.State = "ready"
		if r.udp != nil {
			ep.stats.LocalAddr = r.udp.local
		}
		return
	}
	ctx, cancel := context.WithCancel(r.ctx)
	ep.ctx = ctx
	ep.cancel = cancel
	ep.done = make(chan struct{})
	ep.stats.State = "connecting"
	r.wg.Add(1)
	go func() { defer r.wg.Done(); defer close(ep.done); b.dial(ctx, r, ep) }()
}

func (b *Bind) Close() error {
	b.op.Lock()
	defer b.op.Unlock()
	b.mu.Lock()
	r := b.run
	b.run = nil
	if r == nil {
		b.mu.Unlock()
		return nil
	}
	r.cancel()
	if r.udp != nil {
		r.udp.Close()
	}
	for _, l := range r.listeners {
		l.Close()
	}
	for s := range r.sessions {
		s.conn.Close()
	}
	for key, ep := range b.paths {
		ep.stats.State = "closed"
		ep.current = nil
		if !ep.outbound {
			ep.retired = true
			delete(b.ids, ep.id)
			delete(b.paths, key)
		}
	}
	b.mu.Unlock()
	r.wg.Wait()
	if r.control != nil {
		r.control.cancel()
		for {
			select {
			case p := <-r.control.rx:
				b.releasePacket(r, p)
			default:
				goto drainedControl
			}
		}
	}
drainedControl:
	for {
		select {
		case p := <-r.rx:
			b.releasePacket(r, p)
		default:
			return nil
		}
	}
}

func (b *Bind) Release(s string) {
	b.op.Lock()
	defer b.op.Unlock()
	b.release(s)
}
func (b *Bind) release(s string) {
	key, _, _, err := endpointKey(s)
	if err != nil {
		return
	}
	b.mu.Lock()
	delete(b.pins, key)
	targets := map[*endpoint]bool{}
	if ep := b.paths[key]; ep != nil {
		targets[ep] = true
	}
	if b.run != nil {
		for ss := range b.run.sessions {
			if ss.ep.key == key {
				targets[ss.ep] = true
			}
		}
	}
	for ep := range targets {
		ep.retired = true
	}
	b.mu.Unlock()
	b.releaseTargets(targets)
}
func (b *Bind) releaseTargets(targets map[*endpoint]bool) {
	b.mu.Lock()
	var waits []<-chan struct{}
	var idleUDP *udpSocket
	for ep := range targets {
		ep.retired = true
		delete(b.ids, ep.id)
		if b.paths[ep.key] == ep {
			delete(b.paths, ep.key)
		}
		if ep.cancel != nil {
			ep.cancel()
			waits = append(waits, ep.done)
		}
		if ep.current != nil {
			ep.current.conn.Close()
			waits = append(waits, ep.current.done)
		}
	}
	if r := b.run; r != nil && b.opt.ListenUDP == 0 && !b.opt.UDP && r.udp != nil {
		needed := false
		for _, ep := range b.paths {
			needed = needed || (ep.outbound && ep.transport == "udp")
		}
		if !needed {
			idleUDP = r.udp
			r.udp = nil
			r.socketBytes -= idleUDP.buffers
			idleUDP.Close()
			for k, ep := range b.paths {
				if ep.transport == "udp" {
					ep.retired = true
					delete(b.ids, ep.id)
					delete(b.paths, k)
				}
			}
		}
	}
	b.mu.Unlock()
	if idleUDP != nil {
		<-idleUDP.done
	}
	for _, done := range waits {
		if done != nil {
			<-done
		}
	}
}

func (b *Bind) Stats() []PathStats {
	b.mu.Lock()
	defer b.mu.Unlock()
	out := make([]PathStats, 0, len(b.paths))
	for _, ep := range b.paths {
		s := ep.stats
		s.ObservedAt = time.Now()
		out = append(out, s)
	}
	if b.run != nil {
		for s := range b.run.sessions {
			if !s.ep.outbound {
				v := s.ep.stats
				v.ObservedAt = time.Now()
				out = append(out, v)
			}
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Endpoint < out[j].Endpoint })
	return out
}

func (b *Bind) BatchSize() int { return 1 }
func (b *Bind) SetMark(mark uint32) error {
	if mark != 0 {
		return errors.New("mesh transport does not support socket marks")
	}
	return nil
}

func (e *endpoint) ClearSrc() {}
func (e *endpoint) SrcToString() string {
	e.b.mu.Lock()
	defer e.b.mu.Unlock()
	return e.stats.LocalAddr
}
func (e *endpoint) DstToString() string { return e.key }
func (e *endpoint) DstToBytes() []byte  { return []byte(e.remoteAddr().String()) }
func (e *endpoint) DstIP() netip.Addr   { return e.remoteAddr().Addr() }

// remoteAddr is for callers without b.mu: a named endpoint's address can change.
func (e *endpoint) remoteAddr() netip.AddrPort {
	e.b.mu.Lock()
	defer e.b.mu.Unlock()
	return e.remote
}
func (e *endpoint) SrcIP() netip.Addr {
	a, err := netip.ParseAddrPort(e.SrcToString())
	if err != nil {
		return netip.Addr{}
	}
	return a.Addr()
}

var _ conn.Bind = (*Bind)(nil)
