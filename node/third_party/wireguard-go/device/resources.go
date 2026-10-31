package device

import (
	"sync/atomic"
	"time"

	"golang.zx2c4.com/wireguard/conn"
)

type messagePool struct {
	free chan *[MaxMessageSize]byte
}

func newMessagePool(count int, owners map[*[MaxMessageSize]byte]*messagePool) *messagePool {
	p := &messagePool{free: make(chan *[MaxMessageSize]byte, count)}
	for i := 0; i < count; i++ {
		b := new([MaxMessageSize]byte)
		owners[b] = p
		p.free <- b
	}
	return p
}

func (p *messagePool) get(stop <-chan struct{}) *[MaxMessageSize]byte {
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

func (p *messagePool) tryGet() *[MaxMessageSize]byte {
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
	owners            map[*[MaxMessageSize]byte]*messagePool
	pending           atomic.Int32
	peerLimit         atomic.Int32
}

func newDeviceResources(bind conn.Bind) *deviceResources {
	provider, ok := bind.(conn.ResourceBound)
	if !ok {
		return nil
	}
	limits := provider.DeviceLimits()
	r := &deviceResources{limits: limits, stop: make(chan struct{}), owners: make(map[*[MaxMessageSize]byte]*messagePool)}
	r.peerLimit.Store(int32(limits.MaxPeers))
	r.tx = newMessagePool(limits.TxBuffers, r.owners)
	r.rx = newMessagePool(limits.RxBuffers, r.owners)
	r.keepalive = newMessagePool(limits.KeepaliveBuffers, r.owners)
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

func (device *Device) nextMessageBuffer() *[MaxMessageSize]byte {
	if device.resources != nil {
		return device.resources.rx.tryGet()
	}
	return device.GetMessageBuffer()
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
