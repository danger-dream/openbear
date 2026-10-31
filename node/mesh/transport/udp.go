package transport

import (
	"context"
	"encoding/binary"
	"errors"
	"net"
	"net/netip"
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
}

func (u *udpSocket) Close() error { u.closed.Store(true); return u.UDPConn.Close() }

// WriteToUDPAddrPort takes IPv4 in either form: without IPv6 the host gives
// an IPv4-only socket, which refuses the IPv4-mapped form.
func (u *udpSocket) WriteToUDPAddrPort(b []byte, to netip.AddrPort) (int, error) {
	return u.UDPConn.WriteToUDPAddrPort(b, netip.AddrPortFrom(to.Addr().Unmap(), to.Port()))
}
func openUDP(port int) (*udpSocket, error) {
	if err := checkIPv4Free("udp", port); err != nil {
		return nil, err
	}
	c, err := net.ListenUDP("udp", &net.UDPAddr{Port: port})
	if err != nil {
		return nil, err
	}
	fail := func(err error) (*udpSocket, error) { c.Close(); return nil, err }
	if err = c.SetReadBuffer(socketBufferSize); err != nil {
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
	return &udpSocket{UDPConn: c, buffers: buffers, local: c.LocalAddr().String(), done: make(chan struct{})}, nil
}

// datagrams are the buffers a datagram is framed in for the socket call.
var datagrams = sync.Pool{New: func() any { return new([]byte) }}

func (b *Bind) startUDP(r *bindRun, u *udpSocket) {
	r.udp = u
	r.socketBytes += u.buffers
	r.wg.Add(1)
	go func() { defer r.wg.Done(); defer close(u.done); b.readUDP(r, u) }()
}
func (b *Bind) readUDP(r *bindRun, u *udpSocket) {
	buf := make([]byte, 65540)
	failures := 0
	for {
		n, addr, err := u.ReadFromUDPAddrPort(buf)
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
		addr = netip.AddrPortFrom(addr.Addr().Unmap(), addr.Port())
		if isReflect(buf[:n]) {
			b.reflect(u, buf[:n], addr)
			continue
		}
		if isSTUN(buf[:n]) && b.stunReply(buf[:n], addr) {
			continue
		}
		b.mu.Lock()
		limit := receiveLimit(b.opt.MTU)
		local := b.opt.Protocol
		b.mu.Unlock()
		payload, plane, version, err := unwrapDatagram(buf[:n], limit)
		if err != nil || !knownPlane(plane) || !local.Has(version) {
			// A plane or version of a later protocol: its sender did not
			// know this node's range yet.
			continue
		}
		key := "udp://" + addr.String()
		b.mu.Lock()
		if b.run != r || r.udp != u {
			b.mu.Unlock()
			return
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
	}
}

func (b *Bind) enqueue(r *bindRun, p received, data []byte) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.run != r || p.ep.retired || r.ctx.Err() != nil {
		return
	}
	queue := r.rx
	if p.plane == 1 {
		if r.control == nil {
			return
		}
		queue = r.control.rx
	}
	totalLimit := b.opt.limits().rxBytes
	pathBytes := int(p.ep.stats.RxQueueBytes)
	pathLimit := 256 << 10
	if p.plane == 0 {
		totalLimit -= 64 << 10
		pathLimit -= 32 << 10
	} else if p.ep.controlRxBytes+len(data) > 32<<10 {
		p.ep.stats.Dropped++
		return
	}
	if r.queuedBytes+len(data) > totalLimit || pathBytes+len(data) > pathLimit {
		p.ep.stats.Dropped++
		return
	}
	p.data = append([]byte(nil), data...)
	p.at = time.Now()
	p.ref = PathRef{p.ep.id, p.ep.generation}
	select {
	case queue <- p:
		r.queuedBytes += len(data)
		if p.plane == 1 {
			r.controlBytes += len(data)
			p.ep.controlRxBytes += len(data)
		}
		p.ep.stats.RxQueueBytes += uint64(len(data))
		p.ep.stats.RxBytes += uint64(len(data))
	default:
		p.ep.stats.Dropped++
	}
}
func (b *Bind) releasePacket(r *bindRun, p received) {
	b.mu.Lock()
	defer b.mu.Unlock()
	r.queuedBytes -= len(p.data)
	if p.plane == 1 {
		r.controlBytes -= len(p.data)
		p.ep.controlRxBytes -= len(p.data)
	}
	p.ep.stats.RxQueueBytes -= uint64(len(p.data))
}
func (b *Bind) receive(r *bindRun, packets [][]byte, sizes []int, eps []conn.Endpoint) (int, error) {
	return b.receiveFrom(r, r.ctx, r.rx, packets, sizes, eps)
}
func (b *Bind) receiveFrom(r *bindRun, ctx context.Context, queue chan received, packets [][]byte, sizes []int, eps []conn.Endpoint) (int, error) {
	if len(packets) == 0 || len(sizes) == 0 || len(eps) == 0 {
		return 0, errors.New("empty receive batch")
	}
	for {
		if ctx.Err() != nil {
			return 0, net.ErrClosed
		}
		select {
		case <-ctx.Done():
			return 0, net.ErrClosed
		case p := <-queue:
			b.releasePacket(r, p)
			b.mu.Lock()
			valid := b.run == r && !p.ep.retired && p.ep.generation == p.ref.Generation
			b.mu.Unlock()
			if ctx.Err() != nil {
				return 0, net.ErrClosed
			}
			if !valid || (p.session != nil && p.session.closed.Load()) || (p.udp != nil && p.udp.closed.Load()) {
				continue
			}
			if len(p.data) > len(packets[0]) {
				continue
			}
			sizes[0] = copy(packets[0], p.data)
			b.mu.Lock()
			eps[0] = &pathEndpoint{endpoint: p.ep, ref: p.ref, plane: p.plane, receivedAt: p.at, session: p.session}
			b.mu.Unlock()
			return 1, nil
		}
	}
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
	limit := maxDatagram(b.opt.MTU)
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
	ss := ep.current
	if ss == nil || ss.closed.Load() {
		b.mu.Unlock()
		return net.ErrClosed
	}
	if err := b.acquireSend(r, ss, plane, deadline); err != nil {
		ep.stats.Dropped++
		b.mu.Unlock()
		return err
	}
	ep.stats.TxBufferedBytes = uint64(len(data) + 3)
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
