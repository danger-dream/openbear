package fast

import (
	"bufio"
	"context"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"net/netip"
	"sync"
	"sync/atomic"
	"time"

	"openbear/node/mesh/bypass"
)

var (
	udpIdleDefault = 60 * time.Second
	udpIdleDNS     = 10 * time.Second
	udpSweepEvery  = time.Second
)

func udpIdleFor(port uint16) time.Duration {
	if port == 53 {
		return udpIdleDNS
	}
	return udpIdleDefault
}

const (
	atypIPv4   = 1
	atypDomain = 3
	atypIPv6   = 4
)

func appendSocksAddr(b []byte, ap netip.AddrPort) []byte {
	ip := ap.Addr().Unmap()
	if ip.Is4() {
		v := ip.As4()
		b = append(append(b, atypIPv4), v[:]...)
	} else {
		v := ip.As16()
		b = append(append(b, atypIPv6), v[:]...)
	}
	return binary.BigEndian.AppendUint16(b, ap.Port())
}

func readSocksAddr(r io.Reader) (netip.AddrPort, string, error) {
	var t [1]byte
	if _, err := io.ReadFull(r, t[:]); err != nil {
		return netip.AddrPort{}, "", err
	}
	var ip netip.Addr
	var host string
	switch t[0] {
	case atypIPv4:
		var b [4]byte
		if _, err := io.ReadFull(r, b[:]); err != nil {
			return netip.AddrPort{}, "", err
		}
		ip = netip.AddrFrom4(b)
	case atypIPv6:
		var b [16]byte
		if _, err := io.ReadFull(r, b[:]); err != nil {
			return netip.AddrPort{}, "", err
		}
		ip = netip.AddrFrom16(b).Unmap()
	case atypDomain:
		var n [1]byte
		if _, err := io.ReadFull(r, n[:]); err != nil {
			return netip.AddrPort{}, "", err
		}
		if n[0] == 0 {
			return netip.AddrPort{}, "", errors.New("empty domain")
		}
		d := make([]byte, n[0])
		if _, err := io.ReadFull(r, d); err != nil {
			return netip.AddrPort{}, "", err
		}
		if parsed, err := netip.ParseAddr(string(d)); err == nil {
			ip = parsed.Unmap()
		} else {
			host = string(d)
		}
	default:
		return netip.AddrPort{}, "", errors.New("unsupported address type")
	}
	var p [2]byte
	if _, err := io.ReadFull(r, p[:]); err != nil {
		return netip.AddrPort{}, "", err
	}
	port := binary.BigEndian.Uint16(p[:])
	if host != "" {
		return netip.AddrPortFrom(netip.IPv4Unspecified(), port), host, nil
	}
	return netip.AddrPortFrom(ip, port), "", nil
}

func parseSocksAddrSlice(b []byte) (netip.AddrPort, string, []byte, error) {
	sr := &byteReader{b: b}
	ap, host, err := readSocksAddr(sr)
	if err != nil {
		return netip.AddrPort{}, "", nil, err
	}
	return ap, host, b[sr.n:], nil
}

type byteReader struct {
	b []byte
	n int
}

func (r *byteReader) Read(p []byte) (int, error) {
	if r.n >= len(r.b) {
		return 0, io.ErrUnexpectedEOF
	}
	n := copy(p, r.b[r.n:])
	r.n += n
	return n, nil
}

// writeUDPFrame writes length (2) | socks-address | payload, matching mesh/proxy.
func writeUDPFrame(w io.Writer, ap netip.AddrPort, payload []byte) error {
	b := make([]byte, 2, 2+22+len(payload))
	b = append(appendSocksAddr(b, ap), payload...)
	if len(b)-2 > 0xffff {
		return errors.New("datagram too long")
	}
	binary.BigEndian.PutUint16(b[:2], uint16(len(b)-2))
	_, err := w.Write(b)
	return err
}

func readUDPFrame(r io.Reader, buf []byte) (netip.AddrPort, []byte, error) {
	var n [2]byte
	if _, err := io.ReadFull(r, n[:]); err != nil {
		return netip.AddrPort{}, nil, err
	}
	size := int(binary.BigEndian.Uint16(n[:]))
	if size == 0 || size > len(buf) {
		return netip.AddrPort{}, nil, errors.New("frame size invalid")
	}
	if _, err := io.ReadFull(r, buf[:size]); err != nil {
		return netip.AddrPort{}, nil, err
	}
	ap, host, rest, err := parseSocksAddrSlice(buf[:size])
	if err != nil || host != "" {
		return netip.AddrPort{}, nil, errors.New("invalid frame address")
	}
	return ap, rest, nil
}

type udpSessionKey struct {
	scope       uint64
	client      netip.AddrPort
	target      netip.AddrPort
	preserveLAN bool
}

// udpQueueLen bounds the datagrams waiting for one session's stream. A stream
// that is still opening or is slow drops its own datagrams, as a congested
// network would, and never holds up the reader every session shares.
const udpQueueLen = 128

// udpSocketBuf is the receive buffer of the UDP sockets fast relay reads: the
// kernel default holds a few milliseconds of a fast flow's bursts.
const udpSocketBuf = 2 << 20

var (
	errNoSession = errors.New("no such UDP session")
	errQueueFull = errors.New("UDP session queue is full; datagram dropped")
)

type udpSession struct {
	key      udpSessionKey
	idle     time.Duration
	lastNano atomic.Int64
	queue    chan []byte
	done     chan struct{}

	mu      sync.Mutex
	closed  bool
	release []func()
}

func (u *udpSession) touch() {
	u.lastNano.Store(time.Now().UnixNano())
}

// keep registers what closing the session releases. A session already closed
// releases it at once and reports false.
func (u *udpSession) keep(release func()) bool {
	u.mu.Lock()
	if u.closed {
		u.mu.Unlock()
		release()
		return false
	}
	u.release = append(u.release, release)
	u.mu.Unlock()
	return true
}

func (u *udpSession) Close() error { u.close(); return nil }

func (u *udpSession) close() {
	u.mu.Lock()
	if u.closed {
		u.mu.Unlock()
		return
	}
	u.closed = true
	close(u.done)
	release := u.release
	u.release = nil
	u.mu.Unlock()
	for _, f := range release {
		f()
	}
}

type udpSessionTable struct {
	svc      *Service
	mu       sync.Mutex
	sessions map[udpSessionKey]*udpSession
	stop     chan struct{}
	wg       sync.WaitGroup
	closed   bool
	nextID   atomic.Uint64
}

func newUDPSessionTable(svc *Service) *udpSessionTable {
	t := &udpSessionTable{
		svc:      svc,
		sessions: map[udpSessionKey]*udpSession{},
		stop:     make(chan struct{}),
	}
	t.wg.Add(1)
	go t.sweepLoop(udpSweepEvery)
	return t
}

func (t *udpSessionTable) newScope() uint64 {
	return t.nextID.Add(1)
}

func (t *udpSessionTable) close() {
	t.mu.Lock()
	if t.closed {
		t.mu.Unlock()
		return
	}
	t.closed = true
	close(t.stop)
	list := make([]*udpSession, 0, len(t.sessions))
	for k, s := range t.sessions {
		list = append(list, s)
		delete(t.sessions, k)
	}
	t.mu.Unlock()
	for _, s := range list {
		s.close()
	}
	t.wg.Wait()
}

func (t *udpSessionTable) dropScope(scope uint64) {
	t.mu.Lock()
	var list []*udpSession
	for k, s := range t.sessions {
		if k.scope == scope {
			list = append(list, s)
			delete(t.sessions, k)
		}
	}
	t.mu.Unlock()
	for _, s := range list {
		s.close()
	}
}

func (t *udpSessionTable) sweepLoop(every time.Duration) {
	defer t.wg.Done()
	tick := time.NewTicker(every)
	defer tick.Stop()
	for {
		select {
		case <-t.stop:
			return
		case <-tick.C:
		}
		t.sweep(time.Now())
	}
}

func (t *udpSessionTable) sweep(now time.Time) {
	nowNano := now.UnixNano()
	t.mu.Lock()
	var expired []*udpSession
	for k, s := range t.sessions {
		if nowNano-s.lastNano.Load() > s.idle.Nanoseconds() {
			expired = append(expired, s)
			delete(t.sessions, k)
		}
	}
	t.mu.Unlock()
	for _, s := range expired {
		s.close()
	}
}

func (t *udpSessionTable) count() int {
	t.mu.Lock()
	defer t.mu.Unlock()
	return len(t.sessions)
}

// send queues one UDP datagram on key's session and returns at once. The first
// datagram opens the session, whose stream is dialed in the background with
// makeReply's reply state; makeReply nil feeds only a session already there.
func (t *udpSessionTable) send(
	key udpSessionKey,
	payload []byte,
	makeReply func() (reply func([]byte) int, cleanup func(), err error),
	onUp func(int64),
	onDown func(int64),
	onFail func(),
) error {
	t.mu.Lock()
	if t.closed {
		t.mu.Unlock()
		return net.ErrClosed
	}
	sess := t.sessions[key]
	if sess == nil {
		if makeReply == nil {
			t.mu.Unlock()
			return errNoSession
		}
		sess = &udpSession{key: key, idle: udpIdleFor(key.target.Port()), queue: make(chan []byte, udpQueueLen), done: make(chan struct{})}
		sess.touch()
		t.sessions[key] = sess
		go t.run(sess, makeReply, onUp, onDown, onFail)
	}
	t.mu.Unlock()
	sess.touch()
	select {
	case <-sess.done:
		return net.ErrClosed
	case sess.queue <- payload:
		return nil
	default:
		return errQueueFull
	}
}

func (t *udpSessionTable) remove(key udpSessionKey, sess *udpSession) {
	t.mu.Lock()
	if t.sessions[key] == sess {
		delete(t.sessions, key)
	}
	t.mu.Unlock()
}

// run opens sess's stream and carries its datagrams until the session closes.
func (t *udpSessionTable) run(
	sess *udpSession,
	makeReply func() (func([]byte) int, func(), error),
	onUp func(int64),
	onDown func(int64),
	onFail func(),
) {
	defer func() {
		t.remove(sess.key, sess)
		sess.close()
	}()
	fail := func(count bool) {
		if count {
			t.svc.entryRole.failed.Add(1)
		}
		if onFail != nil {
			onFail()
		}
	}
	reply, cleanup, err := makeReply()
	if err != nil {
		fail(false)
		return
	}
	if cleanup != nil && !sess.keep(cleanup) {
		return
	}
	key := sess.key
	t.svc.entryRole.opened.Add(1)
	o, err := t.svc.openStream(protoUDP, key.client, key.preserveLAN, key.target)
	if errors.Is(err, errNoFastRoute) {
		if !t.svc.trackStream(sess, o, protoUDP, key.target.Port()) {
			fail(true)
			return
		}
		defer t.svc.untrackConn(sess)
		t.meshUDP(sess, key, o, reply, onUp, onDown)
		return
	}
	if err != nil {
		fail(!errors.Is(err, errNotAllowed))
		return
	}
	stream := o.up
	if !t.svc.trackStream(stream, o, protoUDP, key.target.Port()) {
		_ = stream.Close()
		fail(true)
		return
	}
	defer t.svc.untrackConn(stream)
	if !sess.keep(func() { _ = stream.Close() }) {
		return
	}
	t.svc.entryRole.active.Add(1)
	defer t.svc.entryRole.active.Add(-1)
	go func() {
		defer sess.close()
		r := bufio.NewReaderSize(stream, 0x10000)
		buf := make([]byte, 0x10000)
		for {
			_, payload, err := readUDPFrame(r, buf)
			if err != nil {
				return
			}
			sess.touch()
			if n := reply(payload); n > 0 {
				t.svc.entryRole.in.Add(int64(len(payload)))
				if onDown != nil {
					onDown(int64(n))
				}
			}
		}
	}()
	w := bufio.NewWriterSize(stream, 0x10000)
	for {
		select {
		case p := <-sess.queue:
			if !t.writeQueued(w, sess, p, onUp) || w.Flush() != nil {
				return
			}
		case <-sess.done:
			return
		}
	}
}

// writeQueued frames p and whatever else is already queued, for one flush.
func (t *udpSessionTable) writeQueued(w *bufio.Writer, sess *udpSession, p []byte, onUp func(int64)) bool {
	for {
		if writeUDPFrame(w, sess.key.target, p) != nil {
			return false
		}
		t.svc.entryRole.out.Add(int64(len(p)))
		if onUp != nil {
			onUp(int64(len(p)))
		}
		select {
		case p = <-sess.queue:
		default:
			return true
		}
	}
}

// meshUDP carries sess's datagrams over the mesh, as they would go without
// fast relay, when no path takes them.
func (t *udpSessionTable) meshUDP(sess *udpSession, key udpSessionKey, o opened, reply func([]byte) int, onUp, onDown func(int64)) {
	var conn net.Conn
	var err error
	if o.preserveSource {
		ctx, cancel := context.WithTimeout(context.Background(), localDialTimeout)
		conn, err = t.svc.sourceDial(ctx, protoUDP, netip.AddrPortFrom(o.src, key.client.Port()), key.target)
		cancel()
	} else {
		conn, err = dialAddrUDP(key.target)
	}
	if err != nil {
		t.svc.entryRole.failed.Add(1)
		return
	}
	if !sess.keep(func() { _ = conn.Close() }) {
		return
	}
	t.svc.entryRole.active.Add(1)
	defer t.svc.entryRole.active.Add(-1)
	go func() {
		defer sess.close()
		buf := make([]byte, 0x10000)
		for {
			n, err := conn.Read(buf)
			if err != nil {
				return
			}
			sess.touch()
			if m := reply(buf[:n]); m > 0 && onDown != nil {
				onDown(int64(m))
			}
		}
	}()
	for {
		select {
		case p := <-sess.queue:
			if _, err := conn.Write(p); err != nil {
				return
			}
			if onUp != nil {
				onUp(int64(len(p)))
			}
		case <-sess.done:
			return
		}
	}
}

// dialUDP is a UDP socket connected to ap past any capture of this node's own.
func dialUDP(ap netip.AddrPort) (net.Conn, error) {
	d := &net.Dialer{Timeout: localDialTimeout, Control: bypass.Control}
	ctx, cancel := context.WithTimeout(context.Background(), localDialTimeout)
	defer cancel()
	var conn net.Conn
	err := withFastSocket(func() error {
		var e error
		conn, e = d.DialContext(ctx, "udp4", ap.String())
		return e
	})
	if err != nil {
		return nil, err
	}
	setUDPRcvBuf(conn, udpSocketBuf)
	return conn, nil
}

// serveExitUDP relays framed UDP datagrams between down and a UDP socket
// connected to this node's mesh address on h.port, expiring after idle silence.
func (s *Service) serveExitUDP(st *runtimeState, down *net.TCPConn, h header) {
	local := netip.AddrPortFrom(st.selfAddr, h.port)
	conn, err := dialAddrUDP(local)
	if err != nil {
		s.exitRole.failed.Add(1)
		return
	}
	defer conn.Close()

	idle := udpIdleFor(h.port)
	replyAddr := local
	s.exitRole.active.Add(1)
	defer s.exitRole.active.Add(-1)

	// Reading the local socket never waits on the stream: a datagram that
	// finds the queue full is dropped here rather than in the socket buffer
	// while a write is stuck.
	queue := make(chan []byte, udpQueueLen)
	go func() {
		defer close(queue)
		// The producer has really ended (idle/error). Bound only its final
		// drain, not ordinary zero-window backpressure on an active session.
		defer func() { _ = down.SetWriteDeadline(time.Now().Add(idle)) }()
		buf := make([]byte, 0x10000)
		for {
			_ = conn.SetReadDeadline(time.Now().Add(idle))
			n, err := conn.Read(buf)
			if err != nil {
				return
			}
			_ = down.SetReadDeadline(time.Now().Add(idle))
			select {
			case queue <- append([]byte(nil), buf[:n]...):
			default:
			}
		}
	}()
	done := make(chan struct{})
	go func() {
		defer close(done)
		defer down.Close()
		w := bufio.NewWriterSize(down, 0x10000)
		for p := range queue {
			if writeUDPFrame(w, replyAddr, p) != nil {
				return
			}
			s.exitRole.out.Add(int64(len(p)))
			if len(queue) == 0 && w.Flush() != nil {
				return
			}
		}
	}()

	r := bufio.NewReaderSize(down, 0x10000)
	buf := make([]byte, 0x10000)
	halfClosed := false
	for {
		_ = down.SetReadDeadline(time.Now().Add(idle))
		_, payload, err := readUDPFrame(r, buf)
		if err != nil {
			halfClosed = errors.Is(err, io.EOF)
			break
		}
		_ = conn.SetReadDeadline(time.Now().Add(idle))
		if _, err := conn.Write(payload); err != nil {
			break
		}
		s.exitRole.in.Add(int64(len(payload)))
	}
	if !halfClosed {
		_ = conn.Close()
	}
	// FIN still permits UDP replies until the producer's idle expiry. Once
	// it ends the write deadline above wakes even a non-reading TCP peer.
	<-done
}
