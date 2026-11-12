package transport

import (
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"reflect"
	"sync"
	"testing"
	"time"

	"golang.zx2c4.com/wireguard/conn"

	"openbear/node/mesh/wire"
)

// largeBind is an open bind with the large queue, and its receive functions.
func largeBind(t *testing.T) (*Bind, *bindRun, []conn.ReceiveFunc) {
	t.Helper()
	b := newBind(t, Options{MTU: 1420, LargeFrames: true})
	fns, _, err := b.Open(0)
	if err != nil || len(fns) != 3 || b.LargeSwapReceive() == nil {
		t.Fatalf("open: %d receive functions, %v", len(fns), err)
	}
	return b, b.run, fns
}

// streamInto runs readStream on a session of the transport given, over a
// pipe: what is written to the other end arrives as from a stream.
func streamInto(t *testing.T, b *Bind, r *bindRun, transport string) (*session, net.Conn, <-chan error) {
	t.Helper()
	local, remote := net.Pipe()
	s := &session{conn: local, ep: &endpoint{b: b, transport: transport}, version: wire.LargeFrames, done: make(chan struct{})}
	done := make(chan error, 1)
	go func() { done <- b.readStream(r, s); close(s.done) }()
	t.Cleanup(func() { remote.Close(); local.Close() })
	return s, remote, done
}

func streamFrame(payload []byte) []byte {
	return append(binary.BigEndian.AppendUint16(nil, uint16(len(payload)+1)), append([]byte{0}, payload...)...)
}

// message is a data message of n bytes, its bytes following from seed.
func message(n int, seed byte) []byte {
	p := make([]byte, n)
	for i := range p {
		p[i] = seed + byte(i*7)
	}
	binary.LittleEndian.PutUint32(p, 4)
	return p
}

// receiveOne receives one message with f, which reads into buf or hands
// over a buffer in its place, and returns that buffer and the length.
func receiveOne(t *testing.T, f conn.ReceiveFunc, buf []byte) ([]byte, int) {
	t.Helper()
	packets, sizes, eps := [][]byte{buf}, []int{0}, []conn.Endpoint{nil}
	type result struct {
		n   int
		err error
	}
	done := make(chan result, 1)
	go func() {
		n, err := f(packets, sizes, eps)
		done <- result{n, err}
	}()
	select {
	case r := <-done:
		if r.err != nil || r.n != 1 || eps[0] == nil {
			t.Fatalf("receive: %d, %v", r.n, r.err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("nothing received")
	}
	return packets[0], sizes[0]
}

// A data frame long enough is read into a large buffer, which WireGuard
// takes over; the buffer WireGuard read into is the next frame's. One
// handed over is WireGuard's alone: the next frame never lands in it.
func TestLargeReceiveSwapsAndReusesBuffers(t *testing.T) {
	b, r, _ := largeBind(t)
	_, remote, _ := streamInto(t, b, r, "tcp")
	swap := b.LargeSwapReceive()
	first, second := message(9000, 1), message(spareFrameMin+1, 2)
	go remote.Write(streamFrame(first))
	wg1 := make([]byte, largeMessageBuffer)
	got1, n1 := receiveOne(t, swap, wg1)
	if &got1[0] == &wg1[0] || len(got1) != largeMessageBuffer || cap(got1) != largeMessageBuffer || !bytes.Equal(got1[:n1], first) {
		t.Fatal("frame not handed over in a large buffer of its own")
	}
	go remote.Write(streamFrame(second))
	wg2 := make([]byte, largeMessageBuffer)
	got2, n2 := receiveOne(t, swap, wg2)
	if &got2[0] != &wg1[0] || !bytes.Equal(got2[:n2], second) {
		t.Fatal("the next frame was not read into the buffer WireGuard gave back")
	}
	if !bytes.Equal(got1[:n1], first) {
		t.Fatal("reading the next frame changed the one handed over")
	}
	if c := b.LargeReceive(); c != (LargeReceiveCounts{Swapped: 2, Reused: 1, Allocated: 1}) {
		t.Fatalf("counts %+v", c)
	}
	if len(r.spares) != 1 || &(<-r.spares)[0] != &wg2[0] {
		t.Fatal("the buffer given back last is not the spare")
	}
}

// Where WireGuard does not take a buffer over, the frame is copied into
// its own and the large buffer kept for the next frame. Frames of
// spareFrameMin bytes or fewer, and from TLS, keep their own allocation.
func TestLargeReceiveFallsBackToCopying(t *testing.T) {
	for _, c := range []struct {
		name      string
		transport string
		size      int
		copying   bool // the receive function of a device that adopts nothing
		buf       []byte
		pooled    bool
	}{
		{"device that adopts nothing", "tcp", 9000, true, make([]byte, largeMessageBuffer), true},
		{"shorter buffer", "tcp", 9000, false, make([]byte, largeMessageBuffer)[:9200], true},
		{"other capacity", "tcp", 9000, false, make([]byte, largeMessageBuffer, 2*largeMessageBuffer), true},
		{"frame of spareFrameMin bytes", "tcp", spareFrameMin, false, make([]byte, largeMessageBuffer), false},
		{"tls", "tls", 9000, false, make([]byte, largeMessageBuffer), false},
	} {
		t.Run(c.name, func(t *testing.T) {
			b, r, fns := largeBind(t)
			_, remote, _ := streamInto(t, b, r, c.transport)
			f := b.LargeSwapReceive()
			if c.copying {
				f = fns[2]
			}
			p := message(c.size, 9)
			go remote.Write(streamFrame(p))
			got, n := receiveOne(t, f, c.buf)
			if &got[0] != &c.buf[0] || len(got) != len(c.buf) || cap(got) != cap(c.buf) || !bytes.Equal(got[:n], p) {
				t.Fatal("not copied into the buffer given")
			}
			want, spares := LargeReceiveCounts{Copied: 1}, 0
			if c.pooled {
				want.Allocated, spares = 1, 1
			}
			if got := b.LargeReceive(); got != want || len(r.spares) != spares {
				t.Fatalf("counts %+v, %d spares", got, len(r.spares))
			}
		})
	}
}

// Only data frames from TCP longer than spareFrameMin go into large
// buffers, and those hold no more memory than the frame's own allocation
// would: Go allocates both in the size class of largeMessageBuffer. So the
// receive limits, which count frames by length, hold what they did before,
// in entries no larger than before.
func TestLargeReceiveBuffersOnlyForLargeFrames(t *testing.T) {
	_, r, _ := largeBind(t)
	for _, c := range []struct {
		transport string
		plane     uint8
		n         int
		want      bool
	}{
		{"tcp", 0, spareFrameMin + 1, true}, {"tcp", 0, largeLimit, true}, {"tcp", 0, largeMessageBuffer, true},
		{"tcp", 0, spareFrameMin, false}, {"tcp", 0, 1400, false}, {"tcp", 0, largeMessageBuffer + 1, false},
		{"tcp", 1, 9000, false}, {"tcp", 2, 9000, false}, {"tls", 0, 9000, false}, {"udp", 0, 9000, false},
	} {
		if got := pooledFrame(r, &session{ep: &endpoint{transport: c.transport}}, c.plane, c.n); got != c.want {
			t.Errorf("%s plane %d, %d bytes: %v", c.transport, c.plane, c.n, got)
		}
	}
	for _, n := range []int{spareFrameMin + 1, 9000, largeLimit, largeMessageBuffer} {
		if c := cap(append([]byte(nil), make([]byte, n)...)); c != largeMessageBuffer {
			t.Errorf("a frame of %d bytes takes %d", n, c)
		}
	}
	if c := cap(append([]byte(nil), make([]byte, spareFrameMin)...)); c >= largeMessageBuffer {
		t.Errorf("a frame of %d bytes takes %d", spareFrameMin, c)
	}
	plain := newBind(t, Options{MTU: 1420})
	openBind(t, plain)
	if plain.run.spares != nil || plain.LargeSwapReceive() != nil || pooledFrame(plain.run, &session{ep: &endpoint{transport: "tcp"}}, 0, 9000) {
		t.Fatal("large buffers without the large queue")
	}
	type original struct {
		data    []byte
		plane   uint8
		urgent  bool
		ref     PathRef
		at      time.Time
		ep      *endpoint
		session *session
		udp     *udpSocket
	}
	if reflect.TypeOf(received{}).Size() != reflect.TypeOf(original{}).Size() {
		t.Fatal("queue entries grew")
	}
}

// The spares hold at most LargeSpares buffers, each of largeMessageBuffer
// bytes; more go to the GC.
func TestLargeReceiveSparesBounded(t *testing.T) {
	b, r, _ := largeBind(t)
	for range LargeSpares + 3 {
		b.putSpare(r, make([]byte, largeMessageBuffer))
	}
	if len(r.spares) != LargeSpares || cap(r.spares) != LargeSpares || b.LargeReceive().Discarded != 3 {
		t.Fatalf("%d spares, %+v", len(r.spares), b.LargeReceive())
	}
	<-r.spares
	b.putSpare(r, make([]byte, largeMessageBuffer-1))
	b.putSpare(r, make([]byte, largeMessageBuffer, largeMessageBuffer+1))
	if len(r.spares) != LargeSpares-1 || b.LargeReceive().Discarded != 3 {
		t.Fatal("kept a buffer of another size")
	}
	if buf := b.largeBuffer(r); len(buf) != largeMessageBuffer || b.LargeReceive().Reused != 1 {
		t.Fatal("a spare not handed out whole")
	}
}

// Every way a frame in a large buffer can be dropped gives the buffer back,
// once: the bind's large buffers are its spares, what it queued and what
// its readers hold (none here), as many as it allocated and did not leave
// to the GC.
func TestLargeReceiveBuffersGivenBackOnDrops(t *testing.T) {
	b, r, _ := largeBind(t)
	swap := b.LargeSwapReceive()
	eps := []*endpoint{{b: b, transport: "tcp"}, {b: b, transport: "tcp"}, {b: b, transport: "tcp"}}
	queue := func(ep *endpoint, s *session) {
		b.enqueueOwned(r, received{ep: ep, session: s, pooled: true}, b.largeBuffer(r)[:9000])
	}
	accounted := func(what string) {
		t.Helper()
		c := b.LargeReceive()
		if len(r.spares)+len(r.large) != int(c.Allocated-c.Discarded) {
			t.Fatalf("%s: %d spares, %d queued, %+v", what, len(r.spares), len(r.large), c)
		}
	}
	drain := func() {
		for len(r.large) > 0 {
			receiveOne(t, swap, make([]byte, largeMessageBuffer))
		}
	}

	eps[0].retired = true
	queue(eps[0], nil)
	eps[0].retired = false
	if len(r.spares) != 1 {
		t.Fatal("a retired path's frame not given back")
	}
	accounted("retired path")

	before := b.rxDrops.queue.Load()
	for range 30 {
		queue(eps[0], nil)
	}
	if b.rxDrops.queue.Load() == before || len(r.large) == 30 {
		t.Fatal("nothing dropped past the path's limit")
	}
	accounted("past the path's limit")
	drain()
	accounted("drained")

	before = b.rxDrops.queue.Load()
	for i := range cap(r.large) + 2 {
		queue(eps[i%3], nil)
	}
	if len(r.large) != cap(r.large) || b.rxDrops.queue.Load() != before+2 {
		t.Fatalf("%d queued, %d dropped", len(r.large), b.rxDrops.queue.Load()-before)
	}
	accounted("queue full")
	drain()
	accounted("drained again")

	// Queued before the path reconnected, or from a stream that closed.
	stale := b.rxDrops.stale.Load()
	queue(eps[0], nil)
	eps[0].generation++
	queue(eps[0], nil)
	closed := &session{ep: eps[1], done: make(chan struct{})}
	queue(eps[1], closed)
	closed.closed.Store(true)
	queue(eps[1], nil)
	receiveOne(t, swap, make([]byte, largeMessageBuffer))
	receiveOne(t, swap, make([]byte, largeMessageBuffer))
	if b.rxDrops.stale.Load() != stale+2 || len(r.large) != 0 {
		t.Fatalf("stale %d, %d queued", b.rxDrops.stale.Load()-stale, len(r.large))
	}
	accounted("stale")

	tooLong := b.rxDrops.tooLong.Load()
	queue(eps[0], nil)
	b.enqueueOwned(r, received{ep: eps[0]}, make([]byte, 4000))
	if _, n := receiveOne(t, swap, make([]byte, 4096)); n != 4000 || b.rxDrops.tooLong.Load() != tooLong+1 {
		t.Fatalf("received %d, too long %d", n, b.rxDrops.tooLong.Load()-tooLong)
	}
	accounted("too long")

	for range 3 {
		queue(eps[2], nil)
	}
	b.Close()
	if len(r.large) != 0 {
		t.Fatal("queue not drained")
	}
	accounted("closed")
}

// A frame that does not arrive whole, or that its stream can no longer
// queue, gives its large buffer back as the stream ends.
func TestLargeReceiveBufferGivenBackWhenAStreamEnds(t *testing.T) {
	t.Run("half a frame", func(t *testing.T) {
		b, r, _ := largeBind(t)
		_, remote, done := streamInto(t, b, r, "tcp")
		f := streamFrame(message(9000, 3))
		go remote.Write(f[:4000])
		// The rest never comes: the stream ends while the frame is being read
		// into its large buffer. (Closed sooner, the pipe would fail the
		// reader's deadline before it took one.)
		waitUntil(t, 2*time.Second, "a large buffer", func() bool { return b.LargeReceive().Allocated == 1 })
		remote.Close()
		if err := pathResult(t, done, 2*time.Second); !errors.Is(err, io.ErrUnexpectedEOF) {
			t.Fatalf("error %v", err)
		}
		if c := b.LargeReceive(); c.Allocated != 1 || len(r.spares) != 1 || len(r.large) != 0 {
			t.Fatalf("%+v, %d spares", c, len(r.spares))
		}
	})
	t.Run("stream closed", func(t *testing.T) {
		b, r, _ := largeBind(t)
		s, remote, done := streamInto(t, b, r, "tcp")
		s.closed.Store(true)
		go remote.Write(streamFrame(message(9000, 4)))
		if err := pathResult(t, done, 2*time.Second); !errors.Is(err, net.ErrClosed) {
			t.Fatalf("error %v", err)
		}
		if c := b.LargeReceive(); c.Allocated != 1 || len(r.spares) != 1 || len(r.large) != 0 {
			t.Fatalf("%+v, %d spares", c, len(r.spares))
		}
	})
}

// tinyLargePool gives the data device two large receive buffers, taken one
// at a time, so that every buffer goes round between the bind and the
// device all the while.
type tinyLargePool struct{ *Bind }

func (b tinyLargePool) DeviceLimits() conn.DeviceLimits {
	l := b.Bind.DeviceLimits()
	l.LargeRxBuffers, l.LargeBatchSize = 2, 1
	return l
}

// innerPacket is a packet of n bytes from one to the other, numbered seq,
// its bytes following from seq.
func innerPacket(from, to *testWG, seq, n int) []byte {
	payload := make([]byte, n-20)
	binary.BigEndian.PutUint32(payload, uint32(seq))
	for i := 4; i < len(payload); i++ {
		payload[i] = byte(seq*13 + i)
	}
	return dataPacket(from, to, payload)
}

// Through real WireGuard over a TCP path, the receiving device's large
// buffers go round between it and the bind, and every large packet arrives
// whole and in order: one at a time, in a burst with traffic the other way,
// after the stream reconnects, after the bind closes and opens again, and
// after the peers are removed and added again.
func TestLargeReceiveBuffersThroughWireGuard(t *testing.T) {
	port := freePort(t, "tcp")
	server := newBind(t, Options{ListenTCP: port, LargeFrames: true})
	// Over a network MTU of 9000 the sender sends the same frames without
	// its own large pool or asking whether the receiver takes them.
	client := newBind(t, Options{MTU: 9000})
	a := wg(t, client, "10.99.0.1")
	z := wg(t, tinyLargePool{server}, "10.99.0.2")
	endpoint := fmt.Sprintf("tcp://127.0.0.1:%d", port)
	addPeer(t, z, a, "")
	addPeer(t, a, z, endpoint)
	if err := z.dev.Up(); err != nil {
		t.Fatal(err)
	}
	if err := a.dev.Up(); err != nil {
		t.Fatal(err)
	}
	transfer(t, a, z, "request")
	transfer(t, z, a, "reply")
	stop := make(chan struct{})
	var running sync.WaitGroup
	defer func() { close(stop); running.Wait() }()
	running.Add(1)
	go func() { // what arrives the other way
		defer running.Done()
		for {
			select {
			case <-a.tun.Inbound:
			case <-stop:
				return
			}
		}
	}()

	sizes := []int{spareFrameMin, 8800, 9000}
	// next is the next large packet z delivers, checked whole.
	next := func(within time.Duration) (int, bool) {
		t.Helper()
		deadline := time.After(within)
		for {
			select {
			case p := <-z.tun.Inbound:
				if len(p) < spareFrameMin {
					continue // transfer sends more than once
				}
				seq := int(binary.BigEndian.Uint32(p[20:]))
				if !bytes.Equal(p, innerPacket(a, z, seq, len(p))) {
					t.Fatalf("packet %d changed", seq)
				}
				return seq, true
			case <-deadline:
				return 0, false
			}
		}
	}
	seq, delivered := 0, 0
	for range 24 {
		a.tun.Outbound <- innerPacket(a, z, seq, sizes[seq%len(sizes)])
		if got, ok := next(5 * time.Second); !ok || got != seq {
			t.Fatalf("packet %d: got %d, %v", seq, got, ok)
		}
		seq++
		delivered++
	}
	if c := server.LargeReceive(); c != (LargeReceiveCounts{Swapped: 24, Reused: 23, Allocated: 1}) {
		t.Fatalf("one at a time: %+v", c)
	}

	// A burst, with small packets the other way; what the bind drops for
	// want of room is gone, the rest arrives in order. Its end is sent until
	// it arrives.
	first, burst := seq, 200
	end := first + burst
	burstStop := make(chan struct{})
	running.Add(2)
	go func() {
		defer running.Done()
		for i := 0; ; i++ {
			p := innerPacket(a, z, end, 9000)
			if i < burst {
				p = innerPacket(a, z, first+i, sizes[i%len(sizes)])
			} else {
				time.Sleep(20 * time.Millisecond)
			}
			select {
			case a.tun.Outbound <- p:
			case <-burstStop:
				return
			case <-stop:
				return
			}
		}
	}()
	go func() {
		defer running.Done()
		for i := 0; ; i++ {
			select {
			case z.tun.Outbound <- dataPacket(z, a, []byte(fmt.Sprintf("reply %d", i))):
			case <-burstStop:
				return
			case <-stop:
				return
			}
			time.Sleep(time.Millisecond)
		}
	}()
	for last := first - 1; ; {
		got, ok := next(20 * time.Second)
		if !ok {
			t.Fatalf("burst stuck after %d", last)
		}
		if got == end {
			break
		}
		if got <= last || got > end {
			t.Fatalf("burst: %d after %d", got, last)
		}
		last = got
		delivered++
	}
	close(burstStop)
	seq = end + 1
	delivered++

	// deliver sends n packets one at a time, each until it arrives.
	deliver := func(what string, n int) {
		t.Helper()
		for range n {
			deadline := time.Now().Add(10 * time.Second)
			for {
				a.tun.Outbound <- innerPacket(a, z, seq, sizes[seq%len(sizes)])
				got, ok := next(200 * time.Millisecond)
				for ok && got != seq {
					got, ok = next(200 * time.Millisecond)
				}
				if ok {
					break
				}
				if time.Now().After(deadline) {
					t.Fatalf("%s: packet %d not delivered", what, seq)
				}
			}
			seq++
			delivered++
		}
	}
	client.mu.Lock()
	ep := client.paths[endpoint]
	old := ep.current
	client.mu.Unlock()
	old.conn.Close()
	waitUntil(t, 5*time.Second, "reconnect", func() bool {
		client.mu.Lock()
		defer client.mu.Unlock()
		return ep.current != nil && ep.current != old
	})
	deliver("after reconnecting", 8)

	server.mu.Lock()
	run := server.run
	server.mu.Unlock()
	if err := z.dev.BindUpdate(); err != nil {
		t.Fatal(err)
	}
	server.mu.Lock()
	reopened := server.run != run && server.run != nil
	server.mu.Unlock()
	if !reopened {
		t.Fatal("bind not opened again")
	}
	swapped := server.LargeReceive().Swapped
	deliver("after the bind opened again", 8)
	if server.LargeReceive().Swapped < swapped+8 {
		t.Fatal("not handed over after the bind opened again")
	}

	for _, p := range []struct{ from, to *testWG }{{z, a}, {a, z}} {
		if err := p.from.dev.IpcSet("public_key=" + p.to.public + "\nremove=true\n"); err != nil {
			t.Fatal(err)
		}
	}
	addPeer(t, z, a, "")
	addPeer(t, a, z, endpoint)
	deliver("after the peers were added again", 8)

	c, drops := server.LargeReceive(), z.dev.ReceiveDrops()
	if c.Copied != 0 || c.Swapped < uint64(delivered) || c.Reused <= c.Allocated || drops.Decrypt != 0 || drops.Replay != 0 {
		t.Fatalf("%d delivered: %+v, %+v", delivered, c, drops)
	}
	t.Logf("%d large packets delivered: %+v", delivered, c)
}
