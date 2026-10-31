package transport

import (
	"context"
	"crypto/sha256"
	"crypto/tls"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"slices"
	"time"
)

// A redial moves a dialed TCP or TLS path to a new connection, from
// another source port, without closing it first: networks that spread
// connections over their lines by address and port can put one connection
// on a line that loses or delays, and another on a good one. The path keeps
// its PathRef; sends go over the new connection once it is up (TCP, TLS
// with the pins, the preamble), the old one still delivers what arrives on
// it until Retire, or redialGrace, closes it without telling anyone the
// path closed. The other end sees a new connection from the same peer.
// If the new connection authenticates nothing within its first
// redialAuthWait while the old one is open, the path goes back to the old
// one.
const (
	redialGrace    = 30 * time.Second
	redialAuthWait = 10 * time.Second
	retireDrain    = time.Second
)

// ErrRedialBusy: a redial of the path is under way, or its last one still
// has the connection it replaced open.
var ErrRedialBusy = errors.New("redial in progress")

// Redialed names the local addresses of the connections a redial moved a
// path from and to.
type Redialed struct{ From, To string }

// Redial moves the dialed stream path ref to a new connection; see above.
func (b *Bind) Redial(ctx context.Context, ref PathRef) (Redialed, error) {
	b.mu.Lock()
	ep, err := b.path(ref)
	if err != nil {
		b.mu.Unlock()
		return Redialed{}, err
	}
	old := ep.current
	r := b.run
	switch {
	case !ep.outbound || ep.transport == "udp" || ep.ctx == nil:
		err = errors.New("not a dialed TCP or TLS path")
	case r == nil || old == nil || !old.ready || old.closed.Load():
		err = net.ErrClosed
	case ep.redialing || old.replaced:
		err = ErrRedialBusy
	}
	if err == nil {
		for s := range r.sessions {
			if s.ep == ep && s.replaced {
				err = ErrRedialBusy
			}
		}
	}
	if err != nil {
		b.mu.Unlock()
		return Redialed{}, err
	}
	ep.redialing = true
	pins := b.pins[ep.key]
	epCtx := ep.ctx
	b.mu.Unlock()
	defer func() {
		b.mu.Lock()
		ep.redialing = false
		b.mu.Unlock()
	}()
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	stop := context.AfterFunc(epCtx, cancel)
	defer stop()

	d := net.Dialer{Timeout: 5 * time.Second, KeepAlive: 15 * time.Second}
	c, err := d.DialContext(ctx, "tcp", ep.remoteAddr().String())
	if err != nil {
		return Redialed{}, err
	}
	cost, err := configureTCP(c.(*net.TCPConn))
	if err != nil {
		c.Close()
		return Redialed{}, err
	}
	b.mu.Lock()
	if b.run != r || ep.retired || len(r.sessions) >= b.opt.limits().paths || r.socketBytes+cost > b.opt.limits().socketBytes {
		b.mu.Unlock()
		c.Close()
		return Redialed{}, ErrBackpressure
	}
	if ep.transport == "tls" {
		c = tls.Client(c, &tls.Config{MinVersion: tls.VersionTLS13, InsecureSkipVerify: true, SessionTicketsDisabled: true, VerifyConnection: func(cs tls.ConnectionState) error { return verifyPins(cs, pins) }})
	}
	s := &session{conn: c, ep: ep, cost: cost, done: make(chan struct{})}
	r.sessions[s] = true
	r.socketBytes += cost
	// Until it carries the path it is one that ends quietly.
	s.replaced = true
	b.mu.Unlock()
	fail := func(err error) (Redialed, error) {
		b.finish(r, s)
		return Redialed{}, err
	}
	closeOnCancel := context.AfterFunc(ctx, func() { c.Close() })
	fingerprint := ""
	if tc, ok := c.(*tls.Conn); ok {
		hctx, hcancel := context.WithTimeout(ctx, 5*time.Second)
		err = tc.HandshakeContext(hctx)
		hcancel()
		if err != nil {
			closeOnCancel()
			return fail(err)
		}
		h := sha256.Sum256(tc.ConnectionState().PeerCertificates[0].Raw)
		fingerprint = hex.EncodeToString(h[:])
	}
	v, err := exchangeVersions(c, b.protocol())
	if !closeOnCancel() || err != nil {
		if err == nil {
			err = context.Canceled
		}
		return fail(err)
	}
	b.mu.Lock()
	current := b.pins[ep.key]
	switch {
	case b.run != r || ep.retired || epCtx.Err() != nil || ep.generation != ref.Generation || ep.current != old || old.closed.Load():
		err = ErrPathChanged
	case ep.transport == "tls" && (current.err != nil || !slices.Contains(current.values, fingerprint)):
		err = fmt.Errorf("%w: pins changed during redial", ErrSecurity)
	}
	if err != nil {
		b.mu.Unlock()
		return fail(err)
	}
	s.version = v
	s.ready = true
	s.replaced = false
	old.replaced = true
	old.next = s
	ep.current = s
	if fingerprint != "" {
		ep.stats.FingerprintSHA256 = fingerprint
	}
	out := Redialed{From: old.conn.LocalAddr().String(), To: c.LocalAddr().String()}
	ep.stats.LocalAddr = out.To
	ep.stats.RemoteAddr = c.RemoteAddr().String()
	ep.stats.SocketBufferBytes = cost
	ep.stats.TxBufferedBytes = 0
	ep.stats.Redials++
	r.wg.Add(1)
	b.mu.Unlock()
	go func() { defer r.wg.Done(); b.carry(epCtx, r, s, old) }()
	time.AfterFunc(redialGrace, func() { b.closeReplaced(old) })
	if logf := b.opt.Logf; logf != nil {
		logf("mesh transport %s: redialed from %s to %s", ep.key, out.From, out.To)
	}
	return out, nil
}

// carry reads s, the connection a redial moved its path to, until it ends.
// Not authenticated within redialAuthWait, it hands the path back to old
// if that is still open.
func (b *Bind) carry(ctx context.Context, r *bindRun, s, old *session) {
	stop := context.AfterFunc(ctx, func() { s.conn.Close() })
	defer stop()
	timer := time.AfterFunc(redialAuthWait, func() {
		b.mu.Lock()
		defer b.mu.Unlock()
		if s.authenticated {
			return
		}
		if s.ep.current == s && old.next == s && !old.closed.Load() && b.run == r && !s.ep.retired {
			s.ep.current = old
			old.replaced = false
			old.next = nil
			s.replaced = true
			s.ep.stats.LocalAddr = old.conn.LocalAddr().String()
			s.ep.stats.RemoteAddr = old.conn.RemoteAddr().String()
			s.ep.stats.SocketBufferBytes = old.cost
		}
		s.conn.Close()
	})
	defer timer.Stop()
	err := b.readStream(r, s)
	if errors.Is(err, net.ErrClosed) && ctx.Err() == nil {
		err = errors.New("redialed connection closed")
	}
	s.err = err
	b.finish(r, s)
}

// Retire closes, after retireDrain, the connection the last redial of ref
// moved it from: the other end sends over the new one now.
func (b *Bind) Retire(ref PathRef) {
	b.mu.Lock()
	defer b.mu.Unlock()
	ep, err := b.path(ref)
	if err != nil || b.run == nil {
		return
	}
	for s := range b.run.sessions {
		if s.ep == ep && s.replaced && s.next != nil {
			old := s
			time.AfterFunc(retireDrain, func() { b.closeReplaced(old) })
		}
	}
}

func (b *Bind) closeReplaced(s *session) {
	b.mu.Lock()
	replaced := s.replaced
	b.mu.Unlock()
	if replaced {
		s.conn.Close()
	}
}
