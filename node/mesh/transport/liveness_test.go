package transport

import (
	"errors"
	"fmt"
	"io"
	"net"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"golang.zx2c4.com/wireguard/conn"

	"openbear/node/mesh/wire"
)

// fastLiveness shortens keepalives and the silence limit for a test.
func fastLiveness(t *testing.T, every, silence time.Duration) {
	e, s := keepaliveEvery.Load(), silenceLimit.Load()
	keepaliveEvery.Store(int64(every))
	silenceLimit.Store(int64(silence))
	t.Cleanup(func() { keepaliveEvery.Store(e); silenceLimit.Store(s) })
}

// blackHole forwards TCP connections to target until told to stop carrying
// one: from then on what either end writes is taken and goes nowhere, and
// neither end sees the connection close, as with a network line that
// stops carrying one connection's packets.
type blackHole struct {
	ln    net.Listener
	mu    sync.Mutex
	stuck []*atomic.Bool
}

func newBlackHole(t *testing.T, target string) *blackHole {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	h := &blackHole{ln: ln}
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			d, err := net.Dial("tcp", target)
			if err != nil {
				c.Close()
				continue
			}
			stuck := new(atomic.Bool)
			h.mu.Lock()
			h.stuck = append(h.stuck, stuck)
			h.mu.Unlock()
			pipe := func(dst, src net.Conn) {
				buf := make([]byte, 32<<10)
				for {
					n, err := src.Read(buf)
					if n > 0 && !stuck.Load() {
						if _, werr := dst.Write(buf[:n]); werr != nil {
							err = werr
						}
					}
					if err != nil {
						if !stuck.Load() {
							dst.Close()
							src.Close()
						}
						return
					}
				}
			}
			go pipe(d, c)
			go pipe(c, d)
		}
	}()
	return h
}

func (h *blackHole) uri() string { return "tcp://" + h.ln.Addr().String() }

// stop stops carrying the i-th connection made through h.
func (h *blackHole) stop(i int) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.stuck[i].Store(true)
}

// The network stops carrying a busy path's companion, as it did under a
// 1.3 Gbit/s transfer: neither end sees it close. Both ends give it up
// once nothing arrived for silenceLimit, the dialing end opens another
// and the accepting end takes it in place of the old one; control moves
// on, and the stream, which keepalives kept open while idle, is the same.
func TestDeadCompanionReplacedStreamStays(t *testing.T) {
	fastLiveness(t, 100*time.Millisecond, 800*time.Millisecond)
	l := newPathLab(t)
	h := newBlackHole(t, fmt.Sprintf("127.0.0.1:%d", l.z.opt.ListenTCP))
	active, remote := l.prepare(t, h.uri())
	ac := waitCompanion(t, l.a, active)
	zc := waitCompanion(t, l.z, remote)
	l.commit(t, active, remote)
	as, _ := streamOf(l.a, active)
	zs, _ := streamOf(l.z, remote)
	time.Sleep(3 * time.Second)
	if s, c := streamOf(l.a, active); s != as || c != ac {
		t.Fatal("an idle connection was closed at the dialing end")
	}
	if s, c := streamOf(l.z, remote); s != zs || c != zc {
		t.Fatal("an idle connection was closed at the accepting end")
	}
	h.stop(1)
	var ac2, zc2 *session
	waitUntil(t, 5*time.Second, "new companions", func() bool {
		_, ac2 = streamOf(l.a, active)
		_, zc2 = streamOf(l.z, remote)
		return ac2 != nil && ac2 != ac && zc2 != nil && zc2 != zc
	})
	if !ac.closed.Load() || !zc.closed.Load() {
		t.Fatal("the dead companion is still open")
	}
	sendControl(t, l.ac, l.zc, l.a, active, "over the new companion", 50)
	sendControl(t, l.zc, l.ac, l.z, remote, "back over the new companion", 51)
	transfer(t, l.ad, l.zd, "data after")
	if s, _ := streamOf(l.a, active); s != as || as.closed.Load() {
		t.Fatal("the stream was replaced")
	}
	if s, _ := streamOf(l.z, remote); s != zs || zs.closed.Load() {
		t.Fatal("the stream was replaced at the accepting end")
	}
	l.a.mu.Lock()
	still := l.a.peers["z"].active == active
	l.a.mu.Unlock()
	if !still {
		t.Fatal("the path stopped being active")
	}
}

// A stream the network stops carrying ends at both ends, and the dialing
// end connects again.
func TestDeadStreamRedialed(t *testing.T) {
	fastLiveness(t, 100*time.Millisecond, 800*time.Millisecond)
	l := newPathLab(t)
	h := newBlackHole(t, fmt.Sprintf("127.0.0.1:%d", l.z.opt.ListenTCP))
	active, remote := l.prepare(t, h.uri())
	waitCompanion(t, l.a, active)
	l.commit(t, active, remote)
	as, _ := streamOf(l.a, active)
	zs, _ := streamOf(l.z, remote)
	h.stop(0)
	waitUntil(t, 5*time.Second, "both ends give the stream up", func() bool { return as.closed.Load() && zs.closed.Load() })
	waitUntil(t, 5*time.Second, "connected again", func() bool {
		l.a.mu.Lock()
		defer l.a.mu.Unlock()
		s := l.a.ids[active.ID].current
		return s != nil && s != as && s.ready
	})
}

// Keepalives and the silence limit are protocol 4's: with an older end a
// stream waits for frames as long as before.
func TestNoSilenceLimitWithAnOlderEnd(t *testing.T) {
	if idleLimit(&session{version: 3}) != 180*time.Second || idleLimit(&session{version: keepaliveVersion}) != time.Duration(silenceLimit.Load()) {
		t.Fatal("idle limits")
	}
	if !knownPlane(0) || knownPlane(planeKeepalive) {
		t.Fatal("a keepalive is not skipped by readers")
	}
	h, plane, err := readFrameHeader(&fixedReader{b: keepaliveFrame}, 0)
	if err != nil || h != 1 || plane != planeKeepalive {
		t.Fatal("keepalive frame", h, plane, err)
	}
}

type fixedReader struct{ b []byte }

func (r *fixedReader) Read(p []byte) (int, error) {
	if len(r.b) == 0 {
		return 0, io.EOF
	}
	n := copy(p, r.b)
	r.b = r.b[n:]
	return n, nil
}

// Large frames under load: the peer's word withdrawn mid-transfer, as by
// control held up long enough for its range to be forgotten, neither
// fails a send nor stops what was sealed for the stream from going over
// it; LargeLinks drops the peer at once and has it back once it says so
// again. A new stream takes large frames only after the peer says so on
// it; until then they are dropped, without an error or another
// connection.
func TestLargeKeptThroughLoadAndRegained(t *testing.T) {
	port := freePort(t, "tcp")
	server := newBind(t, Options{ListenTCP: port, LargeFrames: true})
	fns, _, err := server.Open(0)
	if err != nil {
		t.Fatal(err)
	}
	var arrived atomic.Int64
	for _, f := range fns {
		go func(f conn.ReceiveFunc) {
			packets, sizes, eps := [][]byte{make([]byte, 65536)}, []int{0}, []conn.Endpoint{nil}
			for {
				n, err := f(packets, sizes, eps)
				if err != nil {
					return
				}
				if n == 1 && sizes[0] > maxDatagram(1420) {
					arrived.Add(1)
				}
			}
		}(f)
	}
	client := newBind(t, Options{LargeFrames: true, SendLarge: true})
	openBind(t, client)
	key := fmt.Sprintf("tcp://127.0.0.1:%d", port)
	target, err := client.ParseEndpoint(key)
	if err != nil {
		t.Fatal(err)
	}
	ep := pathReady(t, client, key)
	client.mu.Lock()
	first := ep.current
	ep.peer = "z"
	client.peers["z"] = &peerPaths{active: PathRef{ep.id, ep.generation}}
	client.mu.Unlock()
	client.SetPeerLarge("z", true)
	authenticateStreams(server)

	large := append(packet4(), make([]byte, 8960)...)
	var refused atomic.Int64
	stop := make(chan struct{})
	done := make(chan struct{})
	go func() {
		defer close(done)
		batch := [][]byte{large, large, large, large}
		for {
			select {
			case <-stop:
				return
			default:
			}
			if err := client.Send(batch, target); errors.Is(err, ErrFrame) {
				refused.Add(1)
			}
		}
	}()
	defer func() { close(stop); <-done }()
	flowing := func(what string) {
		t.Helper()
		from := arrived.Load()
		waitUntil(t, 3*time.Second, what, func() bool { return arrived.Load() > from+20 })
	}
	links := func() bool { return client.LargeLinks()["z"] }
	dropped := func() uint64 { client.mu.Lock(); defer client.mu.Unlock(); return ep.stats.Dropped }

	flowing("large frames")
	if !links() {
		t.Fatal("not a large link")
	}
	for len(client.LargeChanges()) > 0 {
		<-client.LargeChanges()
	}
	client.SetPeerLarge("z", false)
	select {
	case <-client.LargeChanges():
	default:
		t.Fatal("no change signalled")
	}
	if links() {
		t.Fatal("still asked for large frames")
	}
	flowing("frames sealed for the stream")
	client.SetPeerLarge("z", true)
	if !links() {
		t.Fatal("large link not back")
	}
	if n := refused.Load(); n != 0 {
		t.Fatalf("%d sends refused", n)
	}

	first.conn.Close()
	var second *session
	waitUntil(t, 5*time.Second, "a new stream", func() bool {
		client.mu.Lock()
		defer client.mu.Unlock()
		second = ep.current
		return second != nil && second != first && second.ready
	})
	// Committed again, as health does on a new connection.
	client.mu.Lock()
	client.peers["z"].active = PathRef{ep.id, ep.generation}
	client.mu.Unlock()
	authenticateStreams(server)
	before := dropped()
	waitUntil(t, 3*time.Second, "dropped on the new stream", func() bool { return dropped() > before+20 })
	if links() {
		t.Fatal("a large link before the peer said so on the new stream")
	}
	client.SetPeerLarge("z", true)
	flowing("large frames on the new stream")
	client.mu.Lock()
	same := ep.current == second
	client.mu.Unlock()
	if !links() || !same || second.version != wire.LargeFrames {
		t.Fatal("not large again on the same new stream")
	}
	if n := refused.Load(); n != 0 {
		t.Fatalf("%d sends refused", n)
	}
}
