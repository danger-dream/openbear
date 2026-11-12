package device

import (
	"sync/atomic"
	"time"

	"golang.zx2c4.com/wireguard/conn"
)

// messageBuffer holds one message; Data is as long as its pool's buffers.
type messageBuffer struct{ Data []byte }

func newMessageBuffer(size int) *messageBuffer { return &messageBuffer{Data: make([]byte, size)} }

type messagePool struct {
	free chan *messageBuffer
}

func newMessagePool(count, size int, owners map[*messageBuffer]*messagePool) *messagePool {
	p := &messagePool{free: make(chan *messageBuffer, count)}
	for i := 0; i < count; i++ {
		b := newMessageBuffer(size)
		owners[b] = p
		p.free <- b
	}
	return p
}

func (p *messagePool) get(stop <-chan struct{}) *messageBuffer {
	select {
	case <-stop:
		return nil
	default:
	}
	select {
	case b := <-p.free:
		return b
	case <-stop:
		return nil
	}
}

func (p *messagePool) tryGet() *messageBuffer {
	select {
	case b := <-p.free:
		return b
	default:
		return nil
	}
}

type deviceResources struct {
	limits            conn.DeviceLimits
	stop              chan struct{}
	tx, rx, keepalive *messagePool
	priorityRx        *messagePool
	largeTx, largeRx  *messagePool
	owners            map[*messageBuffer]*messagePool
	pending           atomic.Int32
	peerLimit         atomic.Int32
}

func newDeviceResources(bind conn.Bind) *deviceResources {
	provider, ok := bind.(conn.ResourceBound)
	if !ok {
		return nil
	}
	limits := provider.DeviceLimits()
	r := &deviceResources{limits: limits, stop: make(chan struct{}), owners: make(map[*messageBuffer]*messagePool)}
	r.peerLimit.Store(int32(limits.MaxPeers))
	size := MaxMessageSize
	if limits.MessageBufferSize > 0 && limits.MessageBufferSize < size {
		size = limits.MessageBufferSize
	}
	priority := min(max(limits.PriorityRxBuffers, 0), limits.RxBuffers)
	r.tx = newMessagePool(limits.TxBuffers, size, r.owners)
	r.rx = newMessagePool(limits.RxBuffers-priority, size, r.owners)
	if priority > 0 {
		r.priorityRx = newMessagePool(priority, size, r.owners)
	}
	r.keepalive = newMessagePool(limits.KeepaliveBuffers, MaxMessageSize, r.owners)
	if l := limits; l.LargeBufferSize > 0 && l.LargeBufferSize <= MaxMessageSize && l.LargeTxBuffers > 0 && l.LargeRxBuffers > 1 {
		r.largeTx = newMessagePool(l.LargeTxBuffers, l.LargeBufferSize, r.owners)
		r.largeRx = newMessagePool(l.LargeRxBuffers, l.LargeBufferSize, r.owners)
	}
	return r
}

// SetPeerLimit changes admission only; buffers, queues and workers stay fixed.
func (device *Device) SetPeerLimit(limit int) {
	if device.resources != nil && limit > 0 && limit <= MaxPeers {
		device.resources.peerLimit.Store(int32(limit))
	}
}

func (device *Device) releasePending(elem *QueueOutboundElement) {
	if elem.pending {
		elem.pending = false
		device.resources.pending.Add(-1)
	}
}

func (peer *Peer) boundPending(c *QueueOutboundElementsContainer) {
	r := peer.device.resources
	if r == nil {
		return
	}
	limit := int32(max(0, (r.limits.TxBuffers-r.limits.BatchSize)/2))
	if r.limits.PendingLimit > 0 {
		limit = min(limit, int32(r.limits.PendingLimit))
	}
	n := 0
	for _, elem := range c.elems {
		if !elem.pending {
			for used := r.pending.Load(); used < limit; used = r.pending.Load() {
				if r.pending.CompareAndSwap(used, used+1) {
					elem.pending = true
					break
				}
			}
		}
		if elem.pending {
			c.elems[n] = elem
			n++
		} else {
			peer.device.PutMessageBuffer(elem.buffer)
			peer.device.PutOutboundElement(elem)
		}
	}
	clear(c.elems[n:])
	c.elems = c.elems[:n]
}

func (device *Device) queueSize(fallback int) int {
	if device.resources != nil {
		return device.resources.limits.QueueDepth
	}
	return fallback
}

// handshakeQueueSize and stagedQueueSize follow QueueDepth unless the
// limits size them on their own.
func (device *Device) handshakeQueueSize() int {
	if device.resources != nil && device.resources.limits.HandshakeQueueDepth > 0 {
		return device.resources.limits.HandshakeQueueDepth
	}
	return device.queueSize(QueueHandshakeSize)
}

func (device *Device) stagedQueueSize() int {
	if device.resources != nil && device.resources.limits.StagedDepth > 0 {
		return device.resources.limits.StagedDepth
	}
	return device.queueSize(QueueStagedSize)
}

func (device *Device) nextMessageBuffer() *messageBuffer {
	if device.resources != nil {
		return device.resources.rx.tryGet()
	}
	return device.GetMessageBuffer()
}

// receivePool is the pool of the priority or the large receive function,
// nil for one that uses the RX pool.
func (device *Device) receivePool(priority, large bool) *messagePool {
	switch r := device.resources; {
	case r == nil:
		return nil
	case priority:
		return r.priorityRx
	case large:
		return r.largeRx
	}
	return nil
}

// receiveBuffer and nextReceiveBuffer serve a receive function its buffers:
// from its own pool, if it has one.
func (device *Device) receiveBuffer(pool *messagePool) *messageBuffer {
	if pool != nil {
		return pool.get(device.resources.stop)
	}
	return device.GetMessageBuffer()
}

func (device *Device) nextReceiveBuffer(pool *messagePool) *messageBuffer {
	if pool != nil {
		return pool.tryGet()
	}
	return device.nextMessageBuffer()
}

// waitReceiveBuffer is nextReceiveBuffer waiting for a buffer to come back;
// nil once the device closes or its bind is being closed or replaced. The
// sequential receiver that returns buffers may itself be waiting for the net
// lock the closing holds (to send a handshake), so the wait must not outlast it.
func (device *Device) waitReceiveBuffer(pool *messagePool, unbinding <-chan struct{}) *messageBuffer {
	if pool == nil && device.resources != nil {
		pool = device.resources.rx
	}
	if pool == nil {
		return device.GetMessageBuffer()
	}
	select {
	case <-device.resources.stop:
		return nil
	case <-unbinding:
		return nil
	default:
	}
	select {
	case b := <-pool.free:
		return b
	case <-device.resources.stop:
		return nil
	case <-unbinding:
		return nil
	}
}

// ReceiveDrops counts the messages the receive path dropped where nothing
// else counts them.
type ReceiveDrops struct {
	// NoBuffer: no free buffer to read the next message into after a
	// handshake message, so that one went; NoLargeBuffer in the large pool.
	// A transport message waits for one instead.
	NoBuffer, NoLargeBuffer uint64
	// Decrypt: failed authentication; Replay: a counter already seen or
	// behind the window; StaleKey: no keypair for the index, or expired.
	Decrypt, Replay, StaleKey uint64
}

func (device *Device) ReceiveDrops() ReceiveDrops {
	d := &device.rxDrops
	return ReceiveDrops{NoBuffer: d.noBuffer.Load(), NoLargeBuffer: d.noLargeBuffer.Load(),
		Decrypt: d.decrypt.Load(), Replay: d.replay.Load(), StaleKey: d.staleKey.Load()}
}

func (device *Device) dropNoBuffer(pool *messagePool) {
	if pool != nil && device.resources != nil && pool == device.resources.largeRx {
		device.rxDrops.noLargeBuffer.Add(1)
	} else {
		device.rxDrops.noBuffer.Add(1)
	}
}

// newLargeOutboundElement is NewOutboundElement for the large pool.
func (device *Device) newLargeOutboundElement() *QueueOutboundElement {
	buffer := device.resources.largeTx.get(device.resources.stop)
	if buffer == nil {
		return nil
	}
	elem := device.GetOutboundElement()
	elem.buffer, elem.nonce = buffer, 0
	return elem
}

func (device *Device) newKeepaliveElement() *QueueOutboundElement {
	if device.resources == nil {
		return device.NewOutboundElement()
	}
	buffer := device.resources.keepalive.tryGet()
	if buffer == nil {
		return nil
	}
	elem := device.GetOutboundElement()
	elem.buffer, elem.nonce = buffer, 0
	return elem
}

// handshakeLoad is the handshake queue length from which the device counts
// as under load: an eighth of the queue, as upstream's 128 of 1024, and at
// least one waiting, so that the few slots of fixed resource mode still
// turn on cookies and the per-IP limit.
func (device *Device) handshakeLoad() int {
	return max(1, cap(device.queue.handshake.c)/8)
}

// outboundStallWait bounds how long, in fixed resource mode, a packet waits
// for room in its peer's sequential queue. A path whose writes block, as a
// stream that takes nothing until its send deadline, must not hold the TUN
// reader and with it every other peer's packets. A busy stream that still
// drains can block a few hundred milliseconds, as TCP recovers from loss;
// at 100 or 250 ms that dropped 0.3 s of a 20 Mbit/s fragmented UDP flow
// in mesh's TestLoad, at 500 ms nothing.
const outboundStallWait = 500 * time.Millisecond

// queueOutbound hands c to peer's sequential sender; false: c is dropped.
// After one wait runs out, packets for the peer are dropped at once until
// its queue has room again.
func (peer *Peer) queueOutbound(c *QueueOutboundElementsContainer) bool {
	q := &peer.queue
	if peer.device.resources == nil {
		q.outbound.c <- c
		return true
	}
	select {
	case q.outbound.c <- c:
		q.outboundStalled.Store(false)
		return true
	default:
	}
	if q.outboundStalled.Load() {
		return false
	}
	t := time.NewTimer(outboundStallWait)
	defer t.Stop()
	select {
	case q.outbound.c <- c:
		return true
	case <-t.C:
		q.outboundStalled.Store(true)
		return false
	}
}
