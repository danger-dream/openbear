package transport

import (
	"bufio"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net"
	"slices"
	"time"

	"openbear/node/mesh/bypass"
)

// A stream of companionVersion or later has a companion: a second
// connection the dialing end opens to the same listener once the stream
// has authenticated a peer, which carries the control plane and the data
// plane's handshakes while the stream carries data. A full receive queue
// then holds the stream's reader, and TCP slows the sender, instead of
// dropping what arrived and leaving the flow inside to retransmit it:
// control and handshakes, which must not wait behind data, keep moving
// over the companion. Either end of a pair works with the other's frames
// on either connection; without a companion, or with an end of an older
// protocol, a stream carries everything and a full queue drops data, as
// before.
//
// After the preambles the dialing end of such a stream sends helloStream,
// and the accepting end answers with a token naming the stream; a
// companion sends helloCompanion and the token, and gets back 1 when it
// joined, 0 when not. A companion only joins a stream that has
// authenticated a peer, over the same listener, and replaces the one
// before. It ends when its stream does; when it ends alone the stream
// goes back to carrying everything, and the dialing end opens another.
const (
	companionVersion = 3
	helloStream      = 'P'
	helloCompanion   = 'C'
)

var errCompanionRefused = errors.New("companion connection refused")

// errPathsFull refuses a new stream let in past the path limit (accept).
var errPathsFull = errors.New("no room for another path")

// receiveStall is the longest a stream's reader waits for room in the
// receive queue: a device taking nothing for that long is stuck.
const receiveStall = 10 * time.Second

// readBufferSize is what a TCP stream's reader takes from the socket at
// once, held within its path's receive limit.
const readBufferSize = 16 << 10

// isHandshake: p is a WireGuard handshake initiation, response or cookie
// reply.
func isHandshake(p []byte) bool {
	if len(p) < 4 {
		return false
	}
	switch binary.LittleEndian.Uint32(p[:4]) {
	case 1:
		return len(p) == 148
	case 2:
		return len(p) == 92
	case 3:
		return len(p) == 64
	}
	return false
}

// closeConn closes c without the TLS alert, whose write could block.
func closeConn(c net.Conn) {
	if tc, ok := c.(*tls.Conn); ok {
		tc.NetConn().Close()
		return
	}
	c.Close()
}

// dialHello opens a dialed stream and returns the token naming it.
func dialHello(c net.Conn) ([32]byte, error) {
	var token [32]byte
	if err := c.SetDeadline(time.Now().Add(5 * time.Second)); err != nil {
		return token, err
	}
	if err := writeAll(c, []byte{helloStream}); err != nil {
		return token, err
	}
	if _, err := io.ReadFull(c, token[:]); err != nil {
		return token, err
	}
	return token, c.SetDeadline(time.Time{})
}

// acceptHello reads what an accepted connection of companionVersion or
// later is: a stream, which gets its token, or a companion, which joins its
// stream. joined: s is a companion now, its errors none of its path's.
func (b *Bind) acceptHello(r *bindRun, s *session) (joined bool, err error) {
	if err := s.conn.SetDeadline(time.Now().Add(5 * time.Second)); err != nil {
		return false, err
	}
	var hello [33]byte
	if _, err := io.ReadFull(s.conn, hello[:1]); err != nil {
		return false, err
	}
	switch hello[0] {
	case helloStream:
		var token [32]byte
		rand.Read(token[:])
		b.mu.Lock()
		if b.run != r {
			b.mu.Unlock()
			return false, net.ErrClosed
		}
		if s.companionOnly {
			b.mu.Unlock()
			return false, errPathsFull
		}
		s.token = token
		r.tokens[token] = s
		b.mu.Unlock()
		if err := writeAll(s.conn, token[:]); err != nil {
			return false, err
		}
		return false, s.conn.SetDeadline(time.Time{})
	case helloCompanion:
	default:
		return false, ErrFrame
	}
	if _, err := io.ReadFull(s.conn, hello[1:]); err != nil {
		return false, err
	}
	b.mu.Lock()
	p := r.tokens[[32]byte(hello[1:])]
	if b.run != r || p == nil || p.closed.Load() || !p.authenticated || p.listener != s.listener || p.ep.retired {
		b.mu.Unlock()
		writeAll(s.conn, []byte{0})
		return false, errCompanionRefused
	}
	if old := p.companion; old != nil {
		// The dialing end opens another only once it has given up on the
		// one it had, which the network may have stopped carrying without
		// either end seeing it close.
		old.closed.Store(true)
		closeConn(old.conn)
	}
	// No path of its own: it is its stream's.
	delete(r.sessions, s)
	if !s.authenticated {
		r.unauth--
	}
	if s.companionOnly {
		s.companionOnly = false
		r.overPaths--
	}
	s.authenticated = true
	s.ep.retired = true
	s.ep, s.parent = p.ep, p
	s.version = p.version
	p.companion = s
	r.companions++
	b.mu.Unlock()
	// Not ready to send on until the answer is out ahead of any frame.
	if err := writeAll(s.conn, []byte{1}); err != nil {
		return true, err
	}
	if err := s.conn.SetDeadline(time.Time{}); err != nil {
		return true, err
	}
	b.mu.Lock()
	s.ready = true
	if p.ep.current == p {
		p.ep.stats.Companion = true
	}
	b.mu.Unlock()
	return true, nil
}

// keepCompanion keeps a companion beside s, an authenticated dialed stream,
// while s carries its path.
func (b *Bind) keepCompanion(r *bindRun, s *session) {
	defer r.wg.Done()
	for attempt := 0; ; attempt++ {
		started := time.Now()
		c, err := b.openCompanion(r, s)
		if err == nil {
			err = b.readCompanion(r, c)
			b.finish(r, c)
		}
		b.mu.Lock()
		over := b.run != r || s.closed.Load() || s.replaced || s.ep.retired
		logf := b.opt.Logf
		b.mu.Unlock()
		if over {
			return
		}
		if logf != nil && err != nil {
			logf("mesh transport %s: companion: %v", s.ep.key, err)
		}
		if time.Since(started) >= 10*time.Second {
			attempt = 0
		}
		t := time.NewTimer(retryDelay(attempt))
		select {
		case <-t.C:
		case <-s.done:
			t.Stop()
			return
		case <-r.ctx.Done():
			t.Stop()
			return
		}
	}
}

// openCompanion dials a companion for s and joins it.
func (b *Bind) openCompanion(r *bindRun, s *session) (*session, error) {
	b.mu.Lock()
	ep, token, version := s.ep, s.token, s.version
	ctx, pins := ep.ctx, b.pins[ep.key]
	b.mu.Unlock()
	if ctx == nil {
		return nil, net.ErrClosed
	}
	d := net.Dialer{Timeout: 5 * time.Second, KeepAlive: 15 * time.Second}
	c, err := bypass.DialMesh(ctx, d, ep.remoteAddr().String())
	if err != nil {
		return nil, err
	}
	cost, err := configureTCP(c.(*net.TCPConn))
	if err != nil {
		c.Close()
		return nil, err
	}
	b.mu.Lock()
	if b.run != r || s.closed.Load() || s.replaced || ep.retired || s.companion != nil || r.companions >= b.opt.limits().paths || r.socketBytes+cost > b.opt.limits().socketBytes {
		b.mu.Unlock()
		c.Close()
		return nil, ErrBackpressure
	}
	if ep.transport == "tls" {
		c = tls.Client(c, &tls.Config{MinVersion: tls.VersionTLS13, InsecureSkipVerify: true, SessionTicketsDisabled: true, VerifyConnection: func(cs tls.ConnectionState) error { return verifyPins(cs, pins) }})
	}
	cs := &session{conn: c, ep: ep, parent: s, cost: cost, version: version, authenticated: true, done: make(chan struct{})}
	r.companions++
	r.socketBytes += cost
	b.mu.Unlock()
	fail := func(err error) (*session, error) {
		b.finish(r, cs)
		return nil, err
	}
	stop := context.AfterFunc(ctx, func() { c.Close() })
	defer stop()
	if tc, ok := c.(*tls.Conn); ok {
		hctx, cancel := context.WithTimeout(ctx, 5*time.Second)
		err = tc.HandshakeContext(hctx)
		cancel()
		if err != nil {
			return fail(err)
		}
		h := sha256.Sum256(tc.ConnectionState().PeerCertificates[0].Raw)
		b.mu.Lock()
		current := b.pins[ep.key]
		b.mu.Unlock()
		if current.err != nil || !slices.Contains(current.values, hex.EncodeToString(h[:])) {
			return fail(fmt.Errorf("%w: pins changed during handshake", ErrSecurity))
		}
	}
	v, err := exchangeVersions(c, b.protocol())
	if err != nil {
		return fail(err)
	}
	if v < companionVersion {
		return fail(fmt.Errorf("%w: the other end now speaks mesh protocol %d", errCompanionRefused, v))
	}
	if err = c.SetDeadline(time.Now().Add(5 * time.Second)); err != nil {
		return fail(err)
	}
	if err = writeAll(c, append([]byte{helloCompanion}, token[:]...)); err != nil {
		return fail(err)
	}
	var answer [1]byte
	if _, err = io.ReadFull(c, answer[:]); err != nil {
		return fail(err)
	}
	if answer[0] != 1 {
		return fail(errCompanionRefused)
	}
	if err = c.SetDeadline(time.Time{}); err != nil {
		return fail(err)
	}
	b.mu.Lock()
	if b.run != r || s.closed.Load() || s.companion != nil {
		b.mu.Unlock()
		return fail(net.ErrClosed)
	}
	s.companion = cs
	cs.ready = true
	if ep.current == s {
		ep.stats.Companion = true
	}
	b.mu.Unlock()
	return cs, nil
}

// readCompanion reads companion c until it ends, queueing what arrives as
// its stream's: handshakes in the urgent queue.
func (b *Bind) readCompanion(r *bindRun, c *session) error {
	p := c.parent
	b.keepAlive(r, c)
	for {
		b.mu.Lock()
		limit := b.streamReceiveLimit(c)
		b.mu.Unlock()
		if err := c.conn.SetReadDeadline(time.Now().Add(idleLimit(c))); err != nil {
			return err
		}
		n, plane, err := readFrameHeader(c.conn, limit)
		if err != nil {
			return err
		}
		if err = c.conn.SetReadDeadline(time.Now().Add(5 * time.Second)); err != nil {
			return err
		}
		if !knownPlane(plane) {
			if _, err = io.CopyN(io.Discard, c.conn, int64(n)); err != nil {
				return err
			}
			continue
		}
		buf := make([]byte, n)
		if _, err = io.ReadFull(c.conn, buf); err != nil {
			return err
		}
		b.enqueueOwned(r, received{ep: p.ep, session: p, plane: plane, urgent: plane == 0 && isHandshake(buf)}, buf)
	}
}

// leaveStream undoes what companion c held, once it has ended.
func (b *Bind) leaveStream(r *bindRun, c *session) {
	b.mu.Lock()
	defer b.mu.Unlock()
	p := c.parent
	if p.companion == c {
		p.companion = nil
		if p.ep.current == p {
			p.ep.stats.Companion = false
		}
	}
	r.companions--
	r.socketBytes -= c.cost
}

// streamReader is what readStream reads s through: on TCP a buffer that
// takes several frames from the socket at once, when its path's receive
// limit has room for it. release gives that room back.
func (b *Bind) streamReader(r *bindRun, s *session) (io.Reader, func()) {
	if s.ep.transport != "tcp" {
		return s.conn, func() {}
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if !b.fits(r, received{ep: s.ep, session: s}, readBufferSize) {
		return s.conn, func() {}
	}
	r.queuedBytes += readBufferSize
	s.ep.stats.RxQueueBytes += readBufferSize
	return bufio.NewReaderSize(s.conn, readBufferSize), func() {
		b.mu.Lock()
		defer b.mu.Unlock()
		r.queuedBytes -= readBufferSize
		s.ep.stats.RxQueueBytes -= readBufferSize
		r.wakeReaders()
	}
}

// enqueueData queues a data frame read from stream s. While a companion
// carries control, a full queue holds s's reader until there is room.
func (b *Bind) enqueueData(r *bindRun, s *session, p received, buf []byte) error {
	var since time.Time
	for {
		b.mu.Lock()
		if b.run != r || r.ctx.Err() != nil || s.closed.Load() || p.ep.retired {
			b.mu.Unlock()
			return net.ErrClosed
		}
		c := s.companion
		queue := r.dataQueue(len(buf))
		if c == nil || !c.ready || c.closed.Load() || (b.fits(r, p, len(buf)) && len(queue) < cap(queue)) {
			b.enqueueLocked(r, p, buf, true)
			b.mu.Unlock()
			return nil
		}
		if since.IsZero() {
			since = time.Now()
		} else if time.Since(since) > receiveStall {
			b.mu.Unlock()
			return errors.New("receive queue stalled")
		}
		r.rxWaiters++
		select {
		case <-r.rxWake:
		default:
		}
		b.mu.Unlock()
		t := time.NewTimer(5 * time.Millisecond)
		select {
		case <-r.rxWake:
		case <-t.C:
		case <-r.ctx.Done():
		}
		t.Stop()
		b.mu.Lock()
		r.rxWaiters--
		b.mu.Unlock()
	}
}
