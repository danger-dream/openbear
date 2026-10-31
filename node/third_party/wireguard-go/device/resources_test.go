package device

import (
	"testing"
	"time"
	"unsafe"

	"golang.zx2c4.com/wireguard/conn"
	"golang.zx2c4.com/wireguard/conn/bindtest"
	"golang.zx2c4.com/wireguard/tun/tuntest"
)

type resourceTestBind struct {
	conn.Bind
	limits conn.DeviceLimits
}

func (b resourceTestBind) DeviceLimits() conn.DeviceLimits { return b.limits }

func TestMeshResourcesHavePeerIndependentCaps(t *testing.T) {
	limits := conn.DeviceLimits{MaxPeers: 16, Workers: 2, BatchSize: 1, QueueDepth: 4, TxBuffers: 32, RxBuffers: 64, KeepaliveBuffers: 4}
	tun := tuntest.NewChannelTUN()
	b := resourceTestBind{Bind: bindtest.NewChannelBinds()[0], limits: limits}
	d := NewDevice(tun.TUN(), b, NewLogger(LogLevelError, "bounded pools: "))
	t.Cleanup(d.Close)
	r := d.resources
	total := limits.TxBuffers + limits.RxBuffers + limits.KeepaliveBuffers
	if len(r.owners) != total || d.BatchSize() != 1 {
		t.Fatal("incorrect initial pool/batch bounds")
	}
	for i := 0; i < limits.MaxPeers; i++ {
		p, err := d.NewPeer(NoisePublicKey{byte(i + 1)})
		if err != nil {
			t.Fatal(err)
		}
		if cap(p.queue.outbound.c) != 4 || cap(p.queue.inbound.c) != 4 || cap(p.queue.staged) != 4 {
			t.Fatal("peer queues not bounded")
		}
	}
	if _, err := d.NewPeer(NoisePublicKey{200}); err == nil {
		t.Fatal("accepted peer beyond existing mesh bound")
	}
	if len(r.owners) != total {
		t.Fatal("buffers grew with peer count")
	}
	if cap(d.queue.encryption.c) != 4 || cap(d.queue.decryption.c) != 4 || cap(d.queue.handshake.c) != 4 {
		t.Fatal("device queues not bounded")
	}
	var held []*[MaxMessageSize]byte
	for {
		msg := r.tx.tryGet()
		if msg == nil {
			break
		}
		held = append(held, msg)
	}
	defer func() {
		for _, msg := range held {
			d.PutMessageBuffer(msg)
		}
	}()
	if r.tx.tryGet() != nil {
		t.Fatal("allocated past hard TX budget")
	}
	keep := d.newKeepaliveElement()
	if keep == nil {
		t.Fatal("TX exhaustion consumed keepalive reserve")
	}
	d.PutMessageBuffer(keep.buffer)
	d.PutOutboundElement(keep)
	rx := d.nextMessageBuffer()
	if rx == nil {
		t.Fatal("TX exhaustion consumed receive reserve")
	}
	d.PutMessageBuffer(rx)
	done := make(chan *QueueOutboundElement, 1)
	go func() { done <- d.NewOutboundElement() }()
	select {
	case <-done:
		t.Fatal("TX allocation ignored hard cap")
	case <-time.After(30 * time.Millisecond):
	}
	d.Close()
	select {
	case elem := <-done:
		if elem != nil {
			d.PutMessageBuffer(elem.buffer)
			d.PutOutboundElement(elem)
		}
	case <-time.After(time.Second):
		t.Fatal("Close did not cancel exhausted TX pool wait")
	}
	if len(r.owners) != total {
		t.Fatal("pool escaped cap during close")
	}
	meta := uintptr(cap(d.pool.outboundElements.fixed))*unsafe.Sizeof(QueueOutboundElement{}) + uintptr(cap(d.pool.inboundElements.fixed))*unsafe.Sizeof(QueueInboundElement{}) + uintptr(cap(d.pool.outboundElementsContainer.fixed))*unsafe.Sizeof(QueueOutboundElementsContainer{}) + uintptr(cap(d.pool.inboundElementsContainer.fixed))*unsafe.Sizeof(QueueInboundElementsContainer{})
	t.Logf("data plane hard message storage=%d bytes; preallocated metadata structs=%d bytes (excluding channel/map headers)", total*MaxMessageSize, meta)
}

func TestHandshakeLoadFollowsQueueCapacity(t *testing.T) {
	limits := conn.DeviceLimits{MaxPeers: 16, Workers: 2, BatchSize: 1, QueueDepth: 4, TxBuffers: 32, RxBuffers: 64, KeepaliveBuffers: 4}
	bounded := NewDevice(tuntest.NewChannelTUN().TUN(), resourceTestBind{Bind: bindtest.NewChannelBinds()[0], limits: limits}, NewLogger(LogLevelError, ""))
	t.Cleanup(bounded.Close)
	plain := NewDevice(tuntest.NewChannelTUN().TUN(), bindtest.NewChannelBinds()[0], NewLogger(LogLevelError, ""))
	t.Cleanup(plain.Close)
	if got := bounded.handshakeLoad(); got != 1 {
		t.Fatalf("a queue of 4 is under load from %d waiting, want 1", got)
	}
	if got := plain.handshakeLoad(); got != QueueHandshakeSize/8 {
		t.Fatalf("upstream's queue is under load from %d waiting, want %d", got, QueueHandshakeSize/8)
	}
	if bounded.IsUnderLoad() {
		t.Fatal("an empty handshake queue is under load")
	}
}

// TestSeparateHandshakeAndStagedDepths: HandshakeQueueDepth and StagedDepth
// size their queues apart from QueueDepth, which the other queues keep, and
// the load threshold follows the handshake queue.
func TestSeparateHandshakeAndStagedDepths(t *testing.T) {
	limits := conn.DeviceLimits{MaxPeers: 16, Workers: 1, BatchSize: 1, QueueDepth: 4, TxBuffers: 8, RxBuffers: 8, KeepaliveBuffers: 4,
		HandshakeQueueDepth: 32, StagedDepth: 2}
	d := NewDevice(tuntest.NewChannelTUN().TUN(), resourceTestBind{Bind: bindtest.NewChannelBinds()[0], limits: limits}, NewLogger(LogLevelError, ""))
	t.Cleanup(d.Close)
	if got := cap(d.queue.handshake.c); got != 32 {
		t.Fatalf("handshake queue %d, want 32", got)
	}
	if got := d.handshakeLoad(); got != 4 {
		t.Fatalf("under load from %d waiting, want 4", got)
	}
	if got := cap(d.queue.encryption.c); got != 4 {
		t.Fatalf("encryption queue %d, want QueueDepth 4", got)
	}
	p, err := d.NewPeer(NoisePublicKey{2})
	if err != nil {
		t.Fatal(err)
	}
	if got := cap(p.queue.staged); got != 2 {
		t.Fatalf("staged queue %d, want 2", got)
	}
	if got := cap(p.queue.outbound.c); got != 4 {
		t.Fatalf("outbound queue %d, want QueueDepth 4", got)
	}
}

func TestStalledPeerQueueDropsInsteadOfBlocking(t *testing.T) {
	limits := conn.DeviceLimits{MaxPeers: 16, Workers: 2, BatchSize: 1, QueueDepth: 4, TxBuffers: 32, RxBuffers: 64, KeepaliveBuffers: 4}
	d := NewDevice(tuntest.NewChannelTUN().TUN(), resourceTestBind{Bind: bindtest.NewChannelBinds()[0], limits: limits}, NewLogger(LogLevelError, ""))
	t.Cleanup(d.Close)
	// Down, so that no sequential sender drains the queue.
	for deadline := time.Now().Add(time.Second); !d.isUp() && time.Now().Before(deadline); {
		time.Sleep(time.Millisecond)
	}
	d.Down()
	p, err := d.NewPeer(NoisePublicKey{1})
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < cap(p.queue.outbound.c); i++ {
		if !p.queueOutbound(d.GetOutboundElementsContainer()) {
			t.Fatal("dropped with room in the queue")
		}
	}
	start := time.Now()
	if p.queueOutbound(d.GetOutboundElementsContainer()) {
		t.Fatal("queued past the queue's capacity")
	}
	if took := time.Since(start); took < outboundStallWait || took > outboundStallWait+time.Second {
		t.Fatalf("the first packet for a full queue waited %v, want %v", took, outboundStallWait)
	}
	start = time.Now()
	if p.queueOutbound(d.GetOutboundElementsContainer()) || time.Since(start) > outboundStallWait/2 {
		t.Fatal("a stalled peer's next packet was waited for")
	}
	<-p.queue.outbound.c
	if !p.queueOutbound(d.GetOutboundElementsContainer()) || p.queue.outboundStalled.Load() {
		t.Fatal("room in the queue did not end the stall")
	}
	// The next full queue is waited for again before dropping.
	start = time.Now()
	if p.queueOutbound(d.GetOutboundElementsContainer()) || time.Since(start) < outboundStallWait {
		t.Fatal("a peer that drained again had its packets dropped without a wait")
	}
}
