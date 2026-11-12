package device

import (
	"fmt"
	"net/netip"
	"os"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
	"unsafe"

	"golang.zx2c4.com/wireguard/conn"
	"golang.zx2c4.com/wireguard/conn/bindtest"
	"golang.zx2c4.com/wireguard/tun"
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
	var held []*messageBuffer
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

type largeTestTUN struct {
	tun.Device
	reads, closed chan struct{}
}

func (t largeTestTUN) ReadLarge(bufs [][]byte, sizes []int, offset int, _ []conn.SendMetadata) (int, error) {
	select {
	case t.reads <- struct{}{}:
	default:
	}
	<-t.closed
	return 0, os.ErrClosed
}

func (t largeTestTUN) Close() error {
	close(t.closed)
	return t.Device.Close()
}

// TestLargePoolApart: the large pool's buffers are of their own size and
// return to it, the RX and TX pools keep theirs, the large receive function
// takes from it at most LargeBatchSize, and a LargeReader TUN gets a second
// reader.
func TestLargePoolApart(t *testing.T) {
	limits := conn.DeviceLimits{MaxPeers: 16, Workers: 2, BatchSize: 32, QueueDepth: 4, TxBuffers: 224, RxBuffers: 384, KeepaliveBuffers: 4,
		MessageBufferSize: 2048, PriorityRxBuffers: 33, LargeBufferSize: 9472, LargeBatchSize: 8, LargeTxBuffers: 16, LargeRxBuffers: 24}
	reads := make(chan struct{}, 1)
	d := NewDevice(largeTestTUN{tuntest.NewChannelTUN().TUN(), reads, make(chan struct{})}, resourceTestBind{Bind: bindtest.NewChannelBinds()[0], limits: limits}, NewLogger(LogLevelError, ""))
	t.Cleanup(d.Close)
	r := d.resources
	if len(r.owners) != 224+384+4+16+24 || cap(r.largeTx.free) != 16 || cap(r.largeRx.free) != 24 {
		t.Fatalf("pools: %d buffers, large %d/%d", len(r.owners), cap(r.largeTx.free), cap(r.largeRx.free))
	}
	select {
	case <-reads:
	case <-time.After(time.Second):
		t.Fatal("no reader for the large packets")
	}
	if d.receivePool(false, true) != r.largeRx || d.receivePool(true, false) != r.priorityRx || d.receivePool(false, false) != nil {
		t.Fatal("receive functions given the wrong pools")
	}
	elem := d.newLargeOutboundElement()
	if elem == nil || len(elem.buffer.Data) != 9472 || r.owners[elem.buffer] != r.largeTx {
		t.Fatal("large element not from the large pool")
	}
	small := d.NewOutboundElement()
	if len(small.buffer.Data) != 2048 || r.owners[small.buffer] != r.tx {
		t.Fatal("TX buffers changed size")
	}
	d.PutMessageBuffer(elem.buffer)
	d.PutOutboundElement(elem)
	d.PutMessageBuffer(small.buffer)
	d.PutOutboundElement(small)
	// The reader holds its batch.
	if got := len(r.largeTx.free); got != 16-8 {
		t.Fatalf("large TX free %d, want %d", got, 16-8)
	}
	var held []*messageBuffer
	for b := r.largeRx.tryGet(); b != nil; b = r.largeRx.tryGet() {
		if len(b.Data) != 9472 {
			t.Fatal("large RX buffer of the wrong size")
		}
		held = append(held, b)
	}
	if len(held) != 24 || r.rx.tryGet() == nil {
		t.Fatal("large RX pool not apart from the RX pool")
	}
	for _, b := range held {
		d.PutMessageBuffer(b)
	}
	if len(r.largeRx.free) != 24 {
		t.Fatal("large RX buffers not back in their pool")
	}
	out := 2*(224+4+16) + 32 + 2*16 + 8
	if cap(d.pool.outboundElements.fixed) != out || cap(d.pool.inboundElements.fixed) != 2*(384+16)+32+2*24+8 {
		t.Fatal("elements not sized for the large pool")
	}
}

// TestNoLargePoolWithoutLimits: without large limits nothing changes.
func TestNoLargePoolWithoutLimits(t *testing.T) {
	limits := conn.DeviceLimits{MaxPeers: 16, Workers: 2, BatchSize: 32, QueueDepth: 4, TxBuffers: 224, RxBuffers: 384, KeepaliveBuffers: 4, MessageBufferSize: 2048}
	d := NewDevice(tuntest.NewChannelTUN().TUN(), resourceTestBind{Bind: bindtest.NewChannelBinds()[0], limits: limits}, NewLogger(LogLevelError, ""))
	t.Cleanup(d.Close)
	if d.resources.largeTx != nil || d.resources.largeRx != nil || d.receivePool(false, true) != nil || len(d.resources.owners) != 224+384+4 {
		t.Fatal("large pool without large limits")
	}
}

// TestReceiveWaitsForABuffer: with every RX buffer held downstream, the
// receive function waits for one to come back instead of dropping what the
// bind already handed it.
func TestReceiveWaitsForABuffer(t *testing.T) {
	limits := conn.DeviceLimits{MaxPeers: 4, Workers: 1, BatchSize: 4, QueueDepth: 64, TxBuffers: 64, RxBuffers: 16, KeepaliveBuffers: 4}
	cfg, endpointCfg := genConfigs(t)
	binds := bindtest.NewChannelBinds()
	var pair testPair
	for i := range pair {
		p := &pair[i]
		p.tun = tuntest.NewChannelTUN()
		p.ip = netip.AddrFrom4([4]byte{1, 0, 0, byte(i + 1)})
		p.dev = NewDevice(p.tun.TUN(), resourceTestBind{Bind: binds[i], limits: limits}, NewLogger(LogLevelError, ""))
		if err := p.dev.IpcSet(cfg[i]); err != nil {
			t.Fatal(err)
		}
		if err := p.dev.Up(); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(p.dev.Close)
		endpointCfg[i^1] = fmt.Sprintf(endpointCfg[i^1], p.dev.net.port)
	}
	for i := range pair {
		if err := pair[i].dev.IpcSet(endpointCfg[i]); err != nil {
			t.Fatal(err)
		}
	}
	pair.Send(t, Ping, nil)
	// Nothing reads the receiving TUN for now: its writes hold their
	// buffers, and the burst outnumbers the RX pool many times over.
	const burst = 60
	for range burst {
		pair[1].tun.Outbound <- tuntest.Ping(pair[0].ip, pair[1].ip)
	}
	time.Sleep(300 * time.Millisecond)
	for i := range burst {
		select {
		case <-pair[0].tun.Inbound:
		case <-time.After(5 * time.Second):
			t.Fatalf("%d of %d packets through, drops %+v", i, burst, pair[0].dev.ReceiveDrops())
		}
	}
	if d := pair[0].dev.ReceiveDrops(); d != (ReceiveDrops{}) {
		t.Fatalf("drops %+v", d)
	}
}

// gatedTUN holds every write while held is set, as a sequential receiver waiting for a lock would.
type gatedTUN struct {
	tun.Device
	held atomic.Bool
	open chan struct{}
}

func (g *gatedTUN) Write(bufs [][]byte, offset int) (int, error) {
	if g.held.Load() {
		<-g.open
	}
	return g.Device.Write(bufs, offset)
}

// oneDataReceiverBind leaves the data pool one receive routine, as a mesh bind does: bindtest
// delivers to the receive function of its port's family (ports 1 and 2 are IPv4), so the other one
// is named the priority receiver and takes its buffers from the priority pool. Otherwise it would
// give its buffers back on closing, and the routine waiting for one would have it.
type oneDataReceiverBind struct {
	resourceTestBind
	idle int
}

func (b *oneDataReceiverBind) Open(port uint16) ([]conn.ReceiveFunc, uint16, error) {
	fns, actual, err := b.resourceTestBind.Open(port)
	b.idle = 0
	if actual <= 2 {
		b.idle = 1
	}
	return fns, actual, err
}

func (b *oneDataReceiverBind) PriorityReceiveIndex() int { return b.idle }

// A receive routine waiting for a buffer stops waiting when its bind is closed. Whoever holds the
// buffers may itself wait for the net lock the closing holds (the sequential receiver, to send a
// handshake), so none would come back.
func TestBindCloseWhileWaitingForABuffer(t *testing.T) {
	limits := conn.DeviceLimits{MaxPeers: 4, Workers: 1, BatchSize: 4, QueueDepth: 64, TxBuffers: 64, RxBuffers: 16, PriorityRxBuffers: 2, KeepaliveBuffers: 4}
	cfg, endpointCfg := genConfigs(t)
	binds := bindtest.NewChannelBinds()
	var pair testPair
	gate := &gatedTUN{open: make(chan struct{})}
	for i := range pair {
		p := &pair[i]
		p.tun = tuntest.NewChannelTUN()
		p.ip = netip.AddrFrom4([4]byte{1, 0, 0, byte(i + 1)})
		dev := p.tun.TUN()
		if i == 0 {
			gate.Device = dev
			dev = gate
		}
		p.dev = NewDevice(dev, &oneDataReceiverBind{resourceTestBind: resourceTestBind{Bind: binds[i], limits: limits}}, NewLogger(LogLevelError, ""))
		if err := p.dev.IpcSet(cfg[i]); err != nil {
			t.Fatal(err)
		}
		if err := p.dev.Up(); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(p.dev.Close)
		endpointCfg[i^1] = fmt.Sprintf(endpointCfg[i^1], p.dev.net.port)
	}
	for i := range pair {
		if err := pair[i].dev.IpcSet(endpointCfg[i]); err != nil {
			t.Fatal(err)
		}
	}
	pair.Send(t, Ping, nil)
	gate.held.Store(true)
	release := sync.OnceFunc(func() { close(gate.open) })
	t.Cleanup(release)
	// More than the RX pool holds, until the receive routine waits for one to come back.
	waiting := func() bool {
		buf := make([]byte, 1<<20)
		return strings.Contains(string(buf[:runtime.Stack(buf, true)]), ").waitReceiveBuffer(")
	}
	for deadline := time.Now().Add(5 * time.Second); !waiting(); {
		if time.Now().After(deadline) {
			t.Fatal("the receive routine never waited for a buffer")
		}
		pair[1].tun.Outbound <- tuntest.Ping(pair[0].ip, pair[1].ip)
		time.Sleep(10 * time.Millisecond)
	}
	closed := make(chan error, 1)
	go func() { closed <- pair[0].dev.BindClose() }()
	select {
	case <-closed:
	case <-time.After(3 * time.Second):
		release()
		go func() {
			for range pair[0].tun.Inbound {
			}
		}()
		<-closed
		t.Fatal("BindClose waited for a receive routine waiting for a buffer")
	}
	release()
	for drained := false; !drained; {
		select {
		case <-pair[0].tun.Inbound:
		case <-time.After(200 * time.Millisecond):
			drained = true
		}
	}
	gate.held.Store(false)
	if err := pair[0].dev.BindUpdate(); err != nil {
		t.Fatal(err)
	}
	pair.Send(t, Ping, nil)
}
