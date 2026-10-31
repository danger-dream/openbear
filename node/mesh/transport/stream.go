package transport

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"crypto/tls"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"math/rand/v2"
	"net"
	"net/netip"
	"slices"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"openbear/node/mesh/wire"
)

type pinSet struct {
	values   []string
	err      error
	revision uint64
}

type session struct {
	conn          net.Conn
	ep            *endpoint
	listener      *listener
	writing       bool
	waiters       [2][]*sendWaiter
	done          chan struct{}
	closed        atomic.Bool
	authenticated bool
	ready         bool
	cost          int
	// opened: when an inbound connection was accepted; evicted: closed to
	// give its slot to another source's (accept).
	opened  time.Time
	evicted bool
	// version is the mesh protocol the frames are in: the newest both
	// ends' preambles allow.
	version int
	// A redial (redial.go): replaced is a connection the path no longer
	// sends on, next the one that took its place; it ends without telling
	// anyone the path closed. err is why a connection ended, set before
	// done is closed.
	replaced bool
	next     *session
	err      error
}

func newListener(transport string, port int, dir string) (*listener, error) {
	var cert *tls.Certificate
	var info Certificate
	var err error
	if transport == "tls" {
		cert, info, err = activeCertificate(dir)
		if err != nil {
			return nil, err
		}
	}
	if err := checkIPv4Free("tcp", port); err != nil {
		return nil, err
	}
	l, err := net.Listen("tcp", net.JoinHostPort("", strconv.Itoa(port)))
	if err != nil {
		return nil, err
	}
	return &listener{Listener: l, transport: transport, port: port, cert: cert, certificateID: info.ID, done: make(chan struct{})}, nil
}

func (b *Bind) SetPins(s string, pins []string) {
	key, t, _, err := endpointKey(s)
	if err != nil || t != "tls" {
		return
	}
	set := pinSet{}
	if len(pins) > 2 {
		set.err = errors.New("at most two TLS pins are allowed")
	}
	for _, p := range pins {
		p = strings.ToLower(strings.ReplaceAll(p, ":", ""))
		h, err := hex.DecodeString(p)
		if err != nil || len(h) != 32 {
			set.err = errors.New("invalid SHA-256 TLS pin")
			break
		}
		if !slices.Contains(set.values, p) {
			set.values = append(set.values, p)
		}
	}
	slices.Sort(set.values)
	if len(set.values) == 0 {
		set.err = errors.New("TLS pins are required")
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if _, exists := b.pins[key]; !exists && len(b.pins) >= b.opt.limits().paths {
		return
	}
	old := b.pins[key]
	if slices.Equal(old.values, set.values) && fmt.Sprint(old.err) == fmt.Sprint(set.err) {
		return
	}
	set.revision = old.revision + 1
	b.pins[key] = set
	if ep := b.paths[key]; ep != nil {
		if ep.current != nil && (set.err != nil || !slices.Contains(set.values, ep.stats.FingerprintSHA256)) {
			ep.current.conn.Close()
		}
		if set.err != nil {
			ep.stats.LastError = set.err.Error()
			ep.stats.State = "security_error"
		}
		select {
		case ep.wake <- struct{}{}:
		default:
		}
	}
}

func (b *Bind) accept(r *bindRun, l *listener) {
	defer r.wg.Done()
	defer close(l.done)
	for {
		c, err := l.Accept()
		if err != nil {
			return
		}
		cost, err := configureTCP(c.(*net.TCPConn))
		if err != nil {
			c.Close()
			continue
		}
		remote, _ := netip.ParseAddrPort(c.RemoteAddr().String())
		remote = netip.AddrPortFrom(remote.Addr().Unmap(), remote.Port())
		key := l.transport + "://" + remote.String()
		ep := &endpoint{b: b, key: key, transport: l.transport, address: remote.String(), remote: remote}
		ep.stats = PathStats{Endpoint: key, Transport: l.transport, RemoteAddr: remote.String(), LocalAddr: c.LocalAddr().String(), State: "authenticating", SocketBufferBytes: cost}
		b.mu.Lock()
		bySource := map[netip.Prefix][]*session{}
		waiting := 0
		for ss := range r.sessions {
			if !ss.ep.outbound && !ss.authenticated && !ss.evicted {
				k := sourceOf(ss.ep.remote.Addr())
				bySource[k] = append(bySource[k], ss)
				waiting++
			}
		}
		from := len(bySource[sourceOf(remote.Addr())])
		lim := b.opt.limits()
		// With every slot or the socket budget taken, a source holding more
		// than one past this one's gives up its oldest: a few sources cannot
		// keep the others out by reconnecting as their slots time out.
		var victim *session
		freed := 0
		if waiting >= lim.unauth || r.socketBytes+cost > lim.socketBytes {
			most := 0
			for _, list := range bySource {
				most = max(most, len(list))
			}
			for _, list := range bySource {
				for _, ss := range list {
					if len(list) == most && most > from+1 && (victim == nil || ss.opened.Before(victim.opened)) {
						victim, freed = ss, ss.cost
					}
				}
			}
		}
		if b.run != r || r.listeners[l.transport] != l || len(r.sessions) >= lim.paths || (waiting >= lim.unauth && victim == nil) || from >= unauthPerSource || r.socketBytes-freed+cost > lim.socketBytes {
			b.mu.Unlock()
			c.Close()
			continue
		}
		if victim != nil {
			victim.evicted = true
			victim.conn.Close()
		}
		cert := l.cert
		ep.stats.CertificateID = l.certificateID
		if cert != nil {
			c = tls.Server(c, &tls.Config{Certificates: []tls.Certificate{*cert}, MinVersion: tls.VersionTLS13, SessionTicketsDisabled: true})
		}
		ss := &session{conn: c, ep: ep, listener: l, cost: cost, done: make(chan struct{}), opened: time.Now()}
		ep.current = ss
		b.newGeneration(ep)
		r.sessions[ss] = true
		r.unauth++
		r.socketBytes += cost
		r.wg.Add(1)
		b.mu.Unlock()
		go func() { defer r.wg.Done(); b.serve(r, ss) }()
	}
}

// unauthPerSource bounds the connections one source has waiting to
// authenticate.
const unauthPerSource = 4

// sourceOf is what those connections are counted by: the address, or an
// IPv6 address's /64, which one host may hold whole.
func sourceOf(a netip.Addr) netip.Prefix {
	if a.Is4() {
		return netip.PrefixFrom(a, 32)
	}
	p, _ := a.Prefix(64)
	return p
}

func (b *Bind) serve(r *bindRun, s *session) {
	timer := time.AfterFunc(10*time.Second, func() {
		b.mu.Lock()
		defer b.mu.Unlock()
		if !s.authenticated {
			s.conn.Close()
		}
	})
	defer timer.Stop()
	defer b.finish(r, s)
	if c, ok := s.conn.(*tls.Conn); ok {
		ctx, cancel := context.WithTimeout(r.ctx, 5*time.Second)
		err := c.HandshakeContext(ctx)
		cancel()
		if err != nil {
			b.pathError(s.ep, err)
			return
		}
	}
	v, err := exchangeVersions(s.conn, b.protocol())
	if err != nil {
		b.pathError(s.ep, err)
		return
	}
	b.mu.Lock()
	s.version = v
	s.ready = true
	b.event(PathEvent{Kind: "connected", Path: b.info(s.ep, 0, time.Time{})})
	b.mu.Unlock()
	if err := b.readStream(r, s); err != nil {
		b.pathError(s.ep, err)
	}
}

func exchangePreamble(c net.Conn) error {
	_, err := exchangeVersions(c, wire.Local())
	return err
}

// exchangeVersions sends the preamble naming the newest protocol of local
// and reads the other end's, then returns the protocol the stream's frames
// are in: the newer end speaks the older one's newest. Only an end whose
// newest is below local's oldest is refused.
func exchangeVersions(c net.Conn, local wire.Range) (int, error) {
	if err := c.SetDeadline(time.Now().Add(5 * time.Second)); err != nil {
		return 0, err
	}
	p := preambleFor(local.Max)
	if err := writeAll(c, p[:]); err != nil {
		return 0, err
	}
	theirs, err := readPreambleVersion(c)
	if err != nil {
		return 0, err
	}
	if theirs < local.Min {
		return 0, fmt.Errorf("%w: the other end speaks mesh protocol up to %d, this node %d..%d", ErrFrame, theirs, local.Min, local.Max)
	}
	return min(theirs, local.Max), c.SetDeadline(time.Time{})
}

func verifyPins(cs tls.ConnectionState, pins pinSet) error {
	if pins.err != nil || len(pins.values) == 0 || len(cs.PeerCertificates) == 0 {
		return fmt.Errorf("%w: missing certificate or pins", ErrSecurity)
	}
	leaf := cs.PeerCertificates[0]
	now := time.Now()
	if now.Before(leaf.NotBefore) || !now.Before(leaf.NotAfter) {
		return fmt.Errorf("%w: certificate is not currently valid", ErrSecurity)
	}
	sum := sha256.Sum256(leaf.Raw)
	for _, p := range pins.values {
		expected, _ := hex.DecodeString(p)
		if subtle.ConstantTimeCompare(sum[:], expected) == 1 {
			return nil
		}
	}
	return fmt.Errorf("%w: certificate fingerprint mismatch", ErrSecurity)
}

func retryDelay(attempt int) time.Duration {
	base := 500 * time.Millisecond * time.Duration(1<<min(attempt, 6))
	if base > 30*time.Second {
		base = 30 * time.Second
	}
	return min(30*time.Second, time.Duration(float64(base)*(0.8+rand.Float64()*0.4)))
}
func (b *Bind) dial(ctx context.Context, r *bindRun, ep *endpoint) {
	attempt := 0
	for ctx.Err() == nil {
		b.mu.Lock()
		pins := b.pins[ep.key]
		blocked := ep.transport == "tls" && (pins.err != nil || len(pins.values) == 0)
		if !blocked {
			ep.stats.State = "connecting"
			ep.stats.DialAttempts++
			if ep.stats.DialAttempts > 1 {
				ep.stats.Reconnects++
			}
		}
		b.mu.Unlock()
		if blocked {
			select {
			case <-ctx.Done():
				return
			case <-ep.wake:
				continue
			}
		}
		started := time.Now()
		s, err := b.connect(ctx, r, ep, pins)
		// A connection a redial replaced ends quietly; the path goes on
		// over its successor, and is dialed again once that one ends.
		for s != nil {
			b.mu.Lock()
			next := s.next
			if !s.replaced {
				next = nil
			}
			b.mu.Unlock()
			if next == nil {
				break
			}
			select {
			case <-next.done:
			case <-ctx.Done():
				return
			}
			s, err = next, next.err
		}
		if ctx.Err() != nil {
			return
		}
		b.pathError(ep, err)
		if errors.Is(err, ErrSecurity) {
			b.mu.Lock()
			ep.stats.State = "security_error"
			b.mu.Unlock()
			for {
				b.mu.Lock()
				changed := b.pins[ep.key].revision != pins.revision
				b.mu.Unlock()
				if changed {
					break
				}
				select {
				case <-ctx.Done():
					return
				case <-ep.wake:
				}
			}
			continue
		}
		if time.Since(started) >= 10*time.Second {
			attempt = 0
		}
		timer := time.NewTimer(retryDelay(attempt))
		attempt++
		select {
		case <-ctx.Done():
			timer.Stop()
			return
		case <-ep.wake:
			timer.Stop()
		case <-timer.C:
		}
	}
}

// connect returns the connection it made, if any, once that has ended.
func (b *Bind) connect(ctx context.Context, r *bindRun, ep *endpoint, pins pinSet) (*session, error) {
	d := net.Dialer{Timeout: 5 * time.Second, KeepAlive: 15 * time.Second}
	c, err := d.DialContext(ctx, "tcp", ep.remoteAddr().String())
	if err != nil {
		return nil, err
	}
	cost, err := configureTCP(c.(*net.TCPConn))
	if err != nil {
		c.Close()
		return nil, err
	}
	b.mu.Lock()
	if b.run != r || ep.retired || ctx.Err() != nil || len(r.sessions) >= b.opt.limits().paths || r.socketBytes+cost > b.opt.limits().socketBytes {
		b.mu.Unlock()
		c.Close()
		return nil, ErrBackpressure
	}
	if ep.transport == "tls" {
		c = tls.Client(c, &tls.Config{MinVersion: tls.VersionTLS13, InsecureSkipVerify: true, SessionTicketsDisabled: true, VerifyConnection: func(cs tls.ConnectionState) error { return verifyPins(cs, pins) }})
	}
	s := &session{conn: c, ep: ep, cost: cost, done: make(chan struct{})}
	r.sessions[s] = true
	r.socketBytes += cost
	ep.stats.LocalAddr = c.LocalAddr().String()
	ep.stats.RemoteAddr = c.RemoteAddr().String()
	ep.stats.SocketBufferBytes = cost
	ep.stats.State = "authenticating"
	ep.stats.Authenticated = false
	b.mu.Unlock()
	defer func() { s.err = err; b.finish(r, s) }()
	stop := context.AfterFunc(ctx, func() { c.Close() })
	defer stop()
	if tc, ok := c.(*tls.Conn); ok {
		hctx, cancel := context.WithTimeout(ctx, 5*time.Second)
		err = tc.HandshakeContext(hctx)
		cancel()
		if err != nil {
			return s, err
		}
		h := sha256.Sum256(tc.ConnectionState().PeerCertificates[0].Raw)
		b.mu.Lock()
		current := b.pins[ep.key]
		if current.err != nil || !slices.Contains(current.values, hex.EncodeToString(h[:])) {
			b.mu.Unlock()
			err = fmt.Errorf("%w: pins changed during handshake", ErrSecurity)
			return s, err
		}
		ep.stats.FingerprintSHA256 = hex.EncodeToString(h[:])
		b.mu.Unlock()
	}
	v, err := exchangeVersions(c, b.protocol())
	if err != nil {
		return s, err
	}
	b.mu.Lock()
	s.version = v
	if b.run != r || ep.retired {
		b.mu.Unlock()
		err = net.ErrClosed
		return s, err
	}
	if ep.transport == "tls" {
		current := b.pins[ep.key]
		if current.err != nil || !slices.Contains(current.values, ep.stats.FingerprintSHA256) {
			b.mu.Unlock()
			err = fmt.Errorf("%w: pins changed before path activation", ErrSecurity)
			return s, err
		}
	}
	ep.current = s
	s.ready = true
	b.newGeneration(ep)
	b.event(PathEvent{Kind: "connected", Path: b.info(ep, 0, time.Time{})})
	ep.stats.State = "connected"
	ep.stats.LastError = ""
	b.mu.Unlock()
	timer := time.AfterFunc(10*time.Second, func() {
		b.mu.Lock()
		defer b.mu.Unlock()
		if !s.authenticated {
			s.conn.Close()
		}
	})
	defer timer.Stop()
	err = b.readStream(r, s)
	return s, err
}

func (b *Bind) readStream(r *bindRun, s *session) error {
	for {
		b.mu.Lock()
		limit := receiveLimit(b.opt.MTU)
		b.mu.Unlock()
		if err := s.conn.SetReadDeadline(time.Now().Add(180 * time.Second)); err != nil {
			return err
		}
		n, plane, err := readFrameHeader(s.conn, limit)
		if err != nil {
			return err
		}
		if err = s.conn.SetReadDeadline(time.Now().Add(5 * time.Second)); err != nil {
			return err
		}
		if !knownPlane(plane) {
			// A plane of a later protocol: skipped, the stream goes on.
			if _, err = io.CopyN(io.Discard, s.conn, int64(n)); err != nil {
				return err
			}
			continue
		}
		buf := make([]byte, n)
		if _, err = io.ReadFull(s.conn, buf); err != nil {
			return err
		}
		b.enqueue(r, received{ep: s.ep, session: s, plane: plane}, buf)
	}
}

func (b *Bind) finish(r *bindRun, s *session) {
	s.closed.Store(true)
	s.conn.Close()
	b.mu.Lock()
	delete(r.sessions, s)
	r.socketBytes -= s.cost
	if s.replaced {
		// The path goes on over another connection.
		b.mu.Unlock()
		close(s.done)
		return
	}
	if !s.ep.outbound && !s.authenticated {
		r.unauth--
	}
	if s.ep.current == s {
		s.ep.current = nil
		s.ep.stats.TxBufferedBytes = 0
	}
	s.ep.auth = [2]bool{}
	b.event(PathEvent{Kind: "closed", Path: b.info(s.ep, 0, time.Time{})})
	if !s.ep.outbound {
		delete(b.ids, s.ep.id)
	}
	s.ep.stats.Authenticated = false
	s.ep.stats.State = "closed"
	s.ep.stats.SocketBufferBytes = 0
	b.mu.Unlock()
	close(s.done)
}
func (b *Bind) pathError(ep *endpoint, err error) {
	if err == nil {
		return
	}
	b.mu.Lock()
	ep.stats.LastError = err.Error()
	kind := "error"
	if errors.Is(err, ErrSecurity) {
		kind = "security_error"
	}
	b.event(PathEvent{Kind: kind, Path: b.info(ep, 0, time.Time{}), Err: err})
	logf := b.opt.Logf
	b.mu.Unlock()
	if logf != nil {
		logf("mesh transport %s: %v", ep.key, err)
	}
}
