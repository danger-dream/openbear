package transport

import (
	"context"
	"encoding/binary"
	"errors"
	"net"
	"net/netip"
	"openbear/node/mesh/bypass"
	"openbear/node/mesh/clock"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"golang.zx2c4.com/wireguard/conn"
)

type udpSocket struct {
	*net.UDPConn
	buffers int
	// local is LocalAddr().String(), which a path's stats take on every
	// datagram sent.
	local  string
	closed atomic.Bool
	done   chan struct{}
	// steer keeps it past a TUN proxy on Windows and macOS; nil elsewhere.
	steer *bypass.UDP
}

func (u *udpSocket) Close() error { u.closed.Store(true); return u.UDPConn.Close() }

// WriteToUDPAddrPort takes IPv4 in either form: without IPv6 the host gives
// an IPv4-only socket, which refuses the IPv4-mapped form.
func (u *udpSocket) WriteToUDPAddrPort(b []byte, to netip.AddrPort) (int, error) {
	u.steer.Sent()
	return u.UDPConn.WriteToUDPAddrPort(b, netip.AddrPortFrom(to.Addr().Unmap(), to.Port()))
}
func openUDP(port int) (*udpSocket, error) {
	if err := checkIPv4Free("udp", port); err != nil {
		return nil, err
	}
	lc := net.ListenConfig{Control: bypass.Control}
	pc, err := lc.ListenPacket(context.Background(), "udp", net.JoinHostPort("", strconv.Itoa(port)))
	if err != nil {
		return nil, err
	}
	c := pc.(*net.UDPConn)
	fail := func(err error) (*udpSocket, error) { c.Close(); return nil, err }
	if err = c.SetReadBuffer(udpReadBufferSize); err != nil {
		return fail(err)
	}
	if err = c.SetWriteBuffer(socketBufferSize); err != nil {
		return fail(err)
	}
	raw, err := c.SyscallConn()
	if err != nil {
		return fail(err)
	}
	buffers, err := configureSocket(raw, false)
	if err != nil {
		return fail(err)
	}
	steer := bypass.NewUDP(raw)
	steer.Check()
	return &udpSocket{UDPConn: c, buffers: buffers, local: c.LocalAddr().String(), done: make(chan struct{}), steer: steer}, nil
}

// udpReadBufferSize holds what arrives at full speed while the reader is
// between reads; as much as the kernel allows of it.
const udpReadBufferSize = 1 << 20

// datagrams are the buffers a datagram is framed in for the socket call.
var datagrams = sync.Pool{New: func() any { return new([]byte) }}

func (b *Bind) startUDP(r *bindRun, u *udpSocket) {
	r.udp = u
	r.socketBytes += u.buffers
	r.wg.Add(1)
	go func() { defer r.wg.Done(); defer close(u.done); b.readUDP(r, u) }()
	if u.steer != nil {
		r.wg.Add(1)
		go func() {
			defer r.wg.Done()
			tick := time.NewTicker(bypass.TunCheckEvery)
			defer tick.Stop()
			for {
				select {
				case <-u.done:
					return
				case <-tick.C:
					u.steer.Check()
				}
			}
		}()
	}
}
func (b *Bind) readUDP(r *bindRun, u *udpSocket) {
	reader := newUDPBatchReader(u)
	failures := 0
	for {
		batch, err := reader.read()
		if err != nil {
			if u.closed.Load() || errors.Is(err, net.ErrClosed) {
				return
			}
			// An error about one datagram, not the socket: keep reading.
			if failures++; failures > 16 {
				time.Sleep(10 * time.Millisecond)
			}
			continue
		}
		failures = 0
		for _, d := range batch {
			if !b.readDatagram(r, u, d.data, d.addr) {
				return
			}
		}
	}
}

// readDatagram takes one datagram read from u; false: u is no longer the
// bind's.
func (b *Bind) readDatagram(r *bindRun, u *udpSocket, buf []byte, addr netip.AddrPort) bool {
	u.steer.Received()
	n := len(buf)
	addr = netip.AddrPortFrom(addr.Addr().Unmap(), addr.Port())
	if isReflect(buf[:n]) {
		b.reflect(u, buf[:n], addr)
		return true
	}
	if isSTUN(buf[:n]) && b.stunReply(buf[:n], addr) {
		return true
	}
	b.mu.Lock()
	limit := receiveLimit(b.opt.MTU)
	local := b.opt.Protocol
	b.mu.Unlock()
	payload, plane, version, err := unwrapDatagram(buf[:n], limit)
	if err != nil || !knownPlane(plane) || !local.Has(version) {
		// A plane or version of a later protocol: its sender did not
		// know this node's range yet.
		return true
	}
	key := "udp://" + addr.String()
	b.mu.Lock()
	if b.run != r || r.udp != u {
		b.mu.Unlock()
		return false
	}
	ep := b.paths[key]
	if ep == nil {
		for _, configured := range b.paths {
			if configured.outbound && configured.transport == "udp" && configured.remote == addr {
				ep = configured
				break
			}
		}
	}
	if ep == nil {
		ep = &endpoint{b: b, key: key, transport: "udp", address: addr.String(), remote: addr}
		ep.stats = PathStats{Endpoint: key, Transport: "udp", RemoteAddr: addr.String(), LocalAddr: u.local, State: "unconfirmed"}
	}
	if ep.id == "" {
		b.initPath(ep)
		ep.generation = 1
	}
	ep.rxVersion = version
	b.mu.Unlock()
	b.enqueue(r, received{ep: ep, udp: u, plane: plane}, payload)
	return true
}

// Receive queue limits: what one path may have queued, more for a datagram
// path, which has no flow control to slow its sender; the room data leaves
// for control, of all paths' and of one path's; and one path's control and
// companion handshakes.
const (
	pathQueueBytes   = 256 << 10
	pathQueueUDP     = 384 << 10
	dataReserve      = 64 << 10
	pathDataReserve  = 32 << 10
	controlPathBytes = 32 << 10
)

func (b *Bind) enqueue(r *bindRun, p received, data []byte) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.enqueueLocked(r, p, data, false)
}

// enqueueOwned queues data, which the caller gives up, without a copy.
func (b *Bind) enqueueOwned(r *bindRun, p received, data []byte) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.enqueueLocked(r, p, data, true)
}

// enqueueLocked queues data or drops it when it does not fit; owned: data
// is the queue's to keep, else it is copied. A large buffer (p.pooled) a
// drop leaves becomes a spare. Called with b.mu held.
func (b *Bind) enqueueLocked(r *bindRun, p received, data []byte, owned bool) {
	p.pooled = p.pooled && owned
	p.data = data
	if b.run != r || p.ep.retired || r.ctx.Err() != nil {
		b.dropFrame(r, p)
		return
	}
	queue := r.dataQueue(len(data))
	if p.urgent {
		queue = r.urgent
	}
	if p.plane == 1 {
		if r.control == nil {
			return
		}
		queue = r.control.rx
	}
	control := p.plane == 1 || p.urgent
	if !b.fits(r, p, len(data)) {
		p.ep.stats.Dropped++
		b.rxDrops.queue.Add(1)
		b.dropFrame(r, p)
		return
	}
	if !owned {
		data = append([]byte(nil), data...)
	}
	p.data = data
	p.at = time.Now()
	p.ref = PathRef{p.ep.id, p.ep.generation}
	select {
	case queue <- p:
		r.queuedBytes += len(data)
		if control {
			r.controlBytes += len(data)
			p.ep.controlRxBytes += len(data)
		}
		p.ep.stats.RxQueueBytes += uint64(len(data))
		p.ep.stats.RxBytes += uint64(len(data))
	default:
		p.ep.stats.Dropped++
		b.rxDrops.queue.Add(1)
		b.dropFrame(r, p)
	}
}

// fits says whether n bytes for p are within the receive limits. Called
// with b.mu held.
func (b *Bind) fits(r *bindRun, p received, n int) bool {
	total, path := b.opt.limits().rxBytes, pathQueueBytes
	if p.udp != nil {
		path = pathQueueUDP
	}
	if p.plane == 1 || p.urgent {
		if p.ep.controlRxBytes+n > controlPathBytes {
			return false
		}
	} else {
		total -= dataReserve
		path -= pathDataReserve
	}
	return r.queuedBytes+n <= total && int(p.ep.stats.RxQueueBytes)+n <= path
}

// dataQueue is the queue of a data frame of n bytes: the large one when it
// does not fit the device's other buffers.
func (r *bindRun) dataQueue(n int) chan received {
	if r.large != nil && n > smallMessageBuffer {
		return r.large
	}
	return r.rx
}

func (b *Bind) releasePacket(r *bindRun, p received) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.releaseLocked(r, p)
}

// releaseLocked returns what p held of the receive limits, and wakes a
// stream reader waiting for room. Called with b.mu held.
func (b *Bind) releaseLocked(r *bindRun, p received) {
	r.queuedBytes -= len(p.data)
	if p.plane == 1 || p.urgent {
		r.controlBytes -= len(p.data)
		p.ep.controlRxBytes -= len(p.data)
	}
	p.ep.stats.RxQueueBytes -= uint64(len(p.data))
	r.wakeReaders()
}

// wakeReaders wakes a stream reader waiting for room. Called with b.mu held.
func (r *bindRun) wakeReaders() {
	if r.rxWaiters > 0 {
		select {
		case r.rxWake <- struct{}{}:
		default:
		}
	}
}
func (b *Bind) receive(r *bindRun, packets [][]byte, sizes []int, eps []conn.Endpoint) (int, error) {
	return b.receiveFrom(r, r.ctx, r.rx, packets, sizes, eps)
}

// receiveFrom waits for a packet, then takes as many more as are queued, up
// to the batch.
func (b *Bind) receiveFrom(r *bindRun, ctx context.Context, queue chan received, packets [][]byte, sizes []int, eps []conn.Endpoint) (int, error) {
	return b.receiveQueue(r, ctx, queue, packets, sizes, eps, false)
}

// receiveQueue is receiveFrom; swap: the caller adopts the buffers it is
// handed in packets (LargeSwapReceive), so a frame in a large buffer of
// exactly the length and capacity of packets[n] goes in it, and the one it
// displaces becomes a spare. Every other large buffer is copied from and
// becomes a spare, also one of a frame dropped here.
func (b *Bind) receiveQueue(r *bindRun, ctx context.Context, queue chan received, packets [][]byte, sizes []int, eps []conn.Endpoint, swap bool) (int, error) {
	batch := min(len(packets), len(sizes), len(eps))
	if batch == 0 {
		return 0, errors.New("empty receive batch")
	}
	n := 0
	for n < batch {
		if ctx.Err() != nil {
			break
		}
		var p received
		if n == 0 {
			select {
			case <-ctx.Done():
				return 0, net.ErrClosed
			case p = <-queue:
			}
		} else {
			select {
			case p = <-queue:
			default:
				return n, nil
			}
		}
		b.mu.Lock()
		b.releaseLocked(r, p)
		valid := b.run == r && !p.ep.retired && p.ep.generation == p.ref.Generation
		var ep *pathEndpoint
		if valid {
			ep = &pathEndpoint{endpoint: p.ep, ref: p.ref, plane: p.plane, receivedAt: p.at, session: p.session}
		}
		b.mu.Unlock()
		if !valid || (p.session != nil && p.session.closed.Load()) || (p.udp != nil && p.udp.closed.Load()) {
			b.rxDrops.stale.Add(1)
			b.dropFrame(r, p)
			continue
		}
		if len(p.data) > len(packets[n]) {
			b.rxDrops.tooLong.Add(1)
			b.dropFrame(r, p)
			continue
		}
		if swap && p.pooled && cap(p.data) == largeMessageBuffer && len(packets[n]) == largeMessageBuffer && cap(packets[n]) == largeMessageBuffer {
			displaced := packets[n]
			packets[n], sizes[n] = p.data[:largeMessageBuffer], len(p.data)
			b.putSpare(r, displaced)
			b.largeRx.swapped.Add(1)
		} else {
			sizes[n] = copy(packets[n], p.data)
			b.dropFrame(r, p)
			if queue == r.large {
				b.largeRx.copied.Add(1)
			}
		}
		eps[n] = ep
		n++
	}
	if n == 0 {
		return 0, net.ErrClosed
	}
	return n, nil
}

func (b *Bind) Send(bufs [][]byte, target conn.Endpoint) error {
	cookie := len(bufs) == 1 && len(bufs[0]) == 64 && binary.LittleEndian.Uint32(bufs[0]) == 3
	return b.send(bufs, target, 0, cookie)
}

type sendTiming struct{ start, end time.Time }

// udpWriters is how many datagrams of one plane may be in the socket call
// at once on one UDP path, as many as a stream's writer and waiters.
const udpWriters = 1 + maxSendWaiters

func (b *Bind) send(bufs [][]byte, target conn.Endpoint, plane uint8, explicit bool) error {
	return b.sendMeasured(bufs, target, plane, explicit, nil)
}
func (b *Bind) sendMeasured(bufs [][]byte, target conn.Endpoint, plane uint8, explicit bool, timing *sendTiming) error {
	deadline := time.Now().Add(sendTimeout)
	if plane == 0 && len(bufs) > 1 {
		if handled, err := b.sendUDPBatch(bufs, target, explicit, timing, deadline); handled {
			return err
		}
		if handled, err := b.sendStreamBatch(bufs, target, explicit, timing, deadline); handled {
			return err
		}
	}
	var firstErr error
	for _, data := range bufs {
		var packetTiming sendTiming
		var measured *sendTiming
		if timing != nil {
			measured = &packetTiming
		}
		err := b.sendDatagram(data, target, plane, explicit, measured, deadline)
		if firstErr == nil && err != nil {
			firstErr = err
		}
		if timing != nil && !packetTiming.start.IsZero() {
			if timing.start.IsZero() {
				timing.start = packetTiming.start
			}
			timing.end = packetTiming.end
		}
	}
	return firstErr
}

func (b *Bind) sendDatagram(data []byte, target conn.Endpoint, plane uint8, explicit bool, timing *sendTiming, deadline time.Time) error {
	b.mu.Lock()
	r := b.run
	ep, resolveErr := b.resolve(target, plane, explicit)
	if resolveErr != nil {
		b.mu.Unlock()
		return resolveErr
	}
	if r == nil || ep.retired {
		b.mu.Unlock()
		return net.ErrClosed
	}
	ss := ep.current
	limit := b.sendLimit(ep, ss)
	if plane == 0 && len(data) > limit && len(data) <= largeLimit {
		// Sealed for a stream that took large frames, the path has moved
		// on since: dropped as a full queue drops, and the path stays.
		ep.stats.Dropped++
		b.mu.Unlock()
		return nil
	}
	if len(data) == 0 || len(data) > limit || (plane == 1 && len(data) > maxDatagram(1044)) {
		b.mu.Unlock()
		return ErrFrame
	}
	if ep.transport == "udp" {
		u := r.udp
		if u == nil {
			b.mu.Unlock()
			return net.ErrClosed
		}
		// Datagrams need no single writer: a probe or handshake written while
		// a data batch is going out goes out beside it. Writers are bounded
		// per plane, so a busy data plane cannot take the control plane's.
		if ep.udpWriters[plane] >= udpWriters {
			ep.stats.Dropped++
			b.mu.Unlock()
			return ErrBackpressure
		}
		ep.udpWriters[plane]++
		ep.stats.TxBufferedBytes += uint64(len(data) + 5)
		remote := ep.remote
		head := preambleFor(b.datagramVersion(ep.peer, ep.rxVersion))
		b.mu.Unlock()
		buf := datagrams.Get().(*[]byte)
		packet := append(append(append((*buf)[:0], head[:]...), plane), data...)
		if timing != nil {
			timing.start = clock.Now()
		}
		_, err := u.WriteToUDPAddrPort(packet, remote)
		if timing != nil {
			timing.end = clock.Now()
		}
		*buf = packet
		datagrams.Put(buf)
		b.mu.Lock()
		ep.udpWriters[plane]--
		ep.stats.TxBufferedBytes -= uint64(len(data) + 5)
		if err == nil {
			ep.stats.TxBytes += uint64(len(data))
			ep.stats.LocalAddr = u.local
		}
		b.mu.Unlock()
		return err
	}
	if ss == nil || ss.closed.Load() {
		b.mu.Unlock()
		return net.ErrClosed
	}
	// Control and handshakes take the companion, where data cannot hold
	// them up.
	if c := ss.companion; c != nil && c.ready && !c.closed.Load() && (plane == 1 || isHandshake(data)) {
		ss = c
	}
	if err := b.acquireSend(r, ss, plane, deadline); err != nil {
		ep.stats.Dropped++
		b.mu.Unlock()
		return err
	}
	if ss.parent == nil {
		ep.stats.TxBufferedBytes = uint64(len(data) + 3)
	}
	b.mu.Unlock()
	if timing != nil {
		timing.start = clock.Now()
	}
	err := ss.conn.SetWriteDeadline(deadline)
	if err == nil {
		err = writePlaneFrame(ss.conn, data, limit, plane)
	}
	if timing != nil {
		timing.end = clock.Now()
	}
	if err != nil {
		ss.closed.Store(true)
		ss.conn.Close()
	}
	b.mu.Lock()
	if ep.current == ss {
		ep.stats.TxBufferedBytes = 0
	}
	if err == nil {
		ep.stats.TxBytes += uint64(len(data))
	}
	ss.releaseSend()
	b.mu.Unlock()
	return err
}

func udpPort(u *udpSocket) int {
	if u == nil {
		return 0
	}
	_, p, _ := net.SplitHostPort(u.LocalAddr().String())
	v, _ := strconv.Atoi(p)
	return v
}
