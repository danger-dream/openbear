package transport

import (
	"bytes"
	"crypto/rand"
	"crypto/tls"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"os"
	"sync"
	"testing"
	"time"

	"golang.zx2c4.com/wireguard/conn"
	"openbear/node/mesh/wire"
)

// streamOf is ref's connection and its companion, nil when it has none
// ready.
func streamOf(b *Bind, ref PathRef) (*session, *session) {
	b.mu.Lock()
	defer b.mu.Unlock()
	ep, err := b.path(ref)
	if err != nil || ep.current == nil {
		return nil, nil
	}
	s := ep.current
	if c := s.companion; c != nil && c.ready && !c.closed.Load() {
		return s, c
	}
	return s, nil
}

func waitCompanion(t *testing.T, b *Bind, ref PathRef) *session {
	t.Helper()
	var c *session
	waitUntil(t, 5*time.Second, "companion", func() bool { _, c = streamOf(b, ref); return c != nil })
	return c
}

// Two nodes of this build give a TCP or TLS path a companion once it has
// authenticated a peer; data and control go on, and control does not wait
// behind a data write that holds the stream.
func TestCompanionJoinsAuthenticatedStream(t *testing.T) {
	for _, transport := range []string{"tcp", "tls"} {
		t.Run(transport, func(t *testing.T) {
			l := newPathLab(t)
			uri := l.tcp
			if transport == "tls" {
				uri = l.tls
			}
			active, remote := l.prepare(t, uri)
			waitCompanion(t, l.a, active)
			waitCompanion(t, l.z, remote)
			l.commit(t, active, remote)
			for _, b := range []*Bind{l.a, l.z} {
				if n := b.Resources().Connections; n != 2 {
					t.Fatalf("%d connections, want the stream and its companion", n)
				}
			}
			if len(l.z.Paths()) != 1 {
				t.Fatalf("the companion shows as a path: %+v", l.z.Paths())
			}
			for _, b := range []*Bind{l.a, l.z} {
				for _, s := range b.Stats() {
					if s.Transport == transport && !s.Companion {
						t.Fatalf("stats %+v", s)
					}
				}
			}
			s, _ := streamOf(l.a, active)
			l.a.mu.Lock()
			held := !s.writing
			s.writing = true
			l.a.mu.Unlock()
			if !held {
				t.Fatal("stream busy")
			}
			sendControl(t, l.ac, l.zc, l.a, active, "beside a data write", 40)
			l.a.mu.Lock()
			s.releaseSend()
			l.a.mu.Unlock()
			transfer(t, l.ad, l.zd, "data after")
		})
	}
}

// With an end of protocol 2 a stream carries everything, as before.
func TestNoCompanionWithAnOlderEnd(t *testing.T) {
	older := wire.Range{Min: 2, Max: 2}
	for _, c := range []struct {
		name   string
		zp, ap wire.Range
	}{{"older listener", older, wire.Range{}}, {"older dialer", wire.Range{}, older}} {
		t.Run(c.name, func(t *testing.T) {
			l := newPathLabSpeaking(t, c.zp, c.ap)
			active, remote := l.prepare(t, l.tcp)
			l.commit(t, active, remote)
			time.Sleep(300 * time.Millisecond)
			if _, comp := streamOf(l.a, active); comp != nil {
				t.Fatal("companion with an end of protocol 2")
			}
			for _, b := range []*Bind{l.a, l.z} {
				if n := b.Resources().Connections; n != 1 {
					t.Fatalf("%d connections", n)
				}
			}
			sendControl(t, l.ac, l.zc, l.a, active, "control over the stream", 40)
			transfer(t, l.zd, l.ad, "data over the stream")
		})
	}
}

// A companion that ends alone leaves its stream carrying everything, and
// the dialing end opens another.
func TestCompanionEndsAloneStreamGoesOn(t *testing.T) {
	l := newPathLab(t)
	active, remote := l.prepare(t, l.tcp)
	first := waitCompanion(t, l.a, active)
	zc := waitCompanion(t, l.z, remote)
	l.commit(t, active, remote)
	closeConn(zc.conn)
	waitUntil(t, 2*time.Second, "companion gone", func() bool { _, c := streamOf(l.a, active); return c != first })
	sendControl(t, l.ac, l.zc, l.a, active, "control without a companion", 40)
	transfer(t, l.ad, l.zd, "data without a companion")
	var again *session
	waitUntil(t, 5*time.Second, "new companion", func() bool { _, again = streamOf(l.a, active); return again != nil && again != first })
	sendControl(t, l.ac, l.zc, l.a, active, "over the new companion", 41)
	if n := l.z.Resources().Connections; n != 2 {
		t.Fatalf("%d connections on the listener", n)
	}
}

// rawStream opens a stream to address as a node of this build, and returns
// it with its token.
func rawStream(t *testing.T, address string) (net.Conn, [32]byte) {
	t.Helper()
	c, err := net.Dial("tcp", address)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { c.Close() })
	if _, err := exchangeVersions(c, wire.Local()); err != nil {
		t.Fatal(err)
	}
	token, err := dialHello(c)
	if err != nil {
		t.Fatal(err)
	}
	return c, token
}

// rawCompanion asks to join the stream token names, and returns the answer.
func rawCompanion(t *testing.T, address string, token [32]byte) (net.Conn, byte) {
	t.Helper()
	c, err := net.Dial("tcp", address)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { c.Close() })
	c.SetDeadline(time.Now().Add(2 * time.Second))
	if _, err := exchangeVersions(c, wire.Local()); err != nil {
		t.Fatal(err)
	}
	writeAll(c, append([]byte{helloCompanion}, token[:]...))
	var answer [1]byte
	if _, err := io.ReadFull(c, answer[:]); err != nil {
		return c, 0
	}
	c.SetDeadline(time.Time{})
	return c, answer[0]
}

// authenticateStreams marks b's accepted streams as having authenticated
// a peer, as the first WireGuard packet over them does.
func authenticateStreams(b *Bind) {
	b.mu.Lock()
	defer b.mu.Unlock()
	for s := range b.run.sessions {
		if s.ready && !s.authenticated {
			s.authenticated = true
			b.run.unauth--
		}
	}
}

func TestCompanionRefused(t *testing.T) {
	server := newBind(t, Options{ListenTCP: freePort(t, "tcp")})
	openBind(t, server)
	address := net.JoinHostPort("127.0.0.1", portString(server.opt.ListenTCP))
	_, token := rawStream(t, address)
	waitUntil(t, time.Second, "stream", func() bool { return len(server.Paths()) == 1 })
	if c, answer := rawCompanion(t, address, token); answer != 0 {
		t.Fatal("a companion joined a stream that authenticated nothing")
	} else if _, err := c.Read(make([]byte, 1)); err == nil {
		t.Fatal("a refused companion stayed open")
	}
	authenticateStreams(server)
	var other [32]byte
	rand.Read(other[:])
	if _, answer := rawCompanion(t, address, other); answer != 0 {
		t.Fatal("a companion joined with a token naming nothing")
	}
	first, answer := rawCompanion(t, address, token)
	if answer != 1 {
		t.Fatal("refused with the stream's token")
	}
	// The dialing end opens another once it gave up on the first, which
	// the network may still hold open at this end: it takes the place.
	if _, answer := rawCompanion(t, address, token); answer != 1 {
		t.Fatal("a new companion refused")
	}
	first.SetReadDeadline(time.Now().Add(2 * time.Second))
	if _, err := first.Read(make([]byte, 1)); err == nil || errors.Is(err, os.ErrDeadlineExceeded) {
		t.Fatalf("the companion it replaced stayed open: %v", err)
	}
	waitUntil(t, time.Second, "connections settle", func() bool {
		server.mu.Lock()
		defer server.mu.Unlock()
		return server.run.unauth == 0 && server.run.companions == 1 && len(server.run.sessions) == 1
	})
	if n := len(server.Paths()); n != 1 {
		t.Fatalf("%d paths", n)
	}
}

// With every path taken, a stream's companion still joins it, being no path of its own; another
// stream is still refused. Placeholders take the other paths: the socket budget, at this host's
// buffer sizes, would stop real connections first.
func TestCompanionJoinsWithEveryPathTaken(t *testing.T) {
	server := newBind(t, Options{ListenTCP: freePort(t, "tcp")})
	openBind(t, server)
	address := net.JoinHostPort("127.0.0.1", portString(server.opt.ListenTCP))
	_, token := rawStream(t, address)
	waitUntil(t, time.Second, "stream", func() bool { return len(server.Paths()) == 1 })
	authenticateStreams(server)
	paths := server.opt.limits().paths
	server.mu.Lock()
	for len(server.run.sessions) < paths {
		near, far := net.Pipe()
		t.Cleanup(func() { far.Close() })
		server.run.sessions[&session{conn: near, ep: &endpoint{outbound: true}, authenticated: true, done: make(chan struct{})}] = true
	}
	server.mu.Unlock()
	if _, answer := rawCompanion(t, address, token); answer != 1 {
		t.Fatal("a companion refused with every path taken")
	}
	c, err := net.Dial("tcp", address)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	if _, err := exchangeVersions(c, wire.Local()); err != nil {
		t.Fatal(err)
	}
	if _, err := dialHello(c); err == nil {
		t.Fatal("a stream let in past the path limit")
	}
	waitUntil(t, time.Second, "connections settle", func() bool {
		server.mu.Lock()
		defer server.mu.Unlock()
		return server.run.unauth == 0 && server.run.overPaths == 0 && server.run.companions == 1 && len(server.run.sessions) == paths
	})
}

// dataFrame is a data frame of n bytes numbered i.
func dataFrame(i, n int) []byte {
	p := make([]byte, n)
	binary.LittleEndian.PutUint32(p, 4)
	binary.LittleEndian.PutUint32(p[4:], uint32(i))
	return p
}

// flood writes count data frames to c and says when it is done.
func flood(c net.Conn, count int) chan error {
	done := make(chan error, 1)
	go func() {
		for i := 0; i < count; i++ {
			if err := writePlaneFrame(c, dataFrame(i, 1400), maxDatagram(1420), 0); err != nil {
				done <- err
				return
			}
		}
		done <- nil
	}()
	return done
}

// drain receives until count packets have arrived or none come for a
// while, and returns how many and the largest batch.
func drain(t *testing.T, receive conn.ReceiveFunc, count int) (int, int) {
	t.Helper()
	const batch = 32
	packets, sizes, eps := make([][]byte, batch), make([]int, batch), make([]conn.Endpoint, batch)
	for i := range packets {
		packets[i] = make([]byte, 2048)
	}
	got, largest, next := 0, 0, 0
	results := make(chan int)
	var mu sync.Mutex
	for got < count {
		go func() {
			mu.Lock()
			defer mu.Unlock()
			n, err := receive(packets, sizes, eps)
			if err != nil {
				n = 0
			}
			for i := 0; i < n; i++ {
				if binary.LittleEndian.Uint32(packets[i][4:]) != uint32(next) {
					t.Errorf("packet %d out of order", next)
				}
				next++
			}
			results <- n
		}()
		select {
		case n := <-results:
			got += n
			largest = max(largest, n)
		case <-time.After(time.Second):
			return got, largest
		}
	}
	return got, largest
}

// With a companion, a stream whose receive queue is full holds its reader
// until there is room: everything arrives, in order, nothing is dropped,
// and a handshake over the companion gets through meanwhile. Without one,
// a full queue drops data, as before.
func TestStreamWaitsForRoomWithCompanion(t *testing.T) {
	const count = 400
	server := newBind(t, Options{ListenTCP: freePort(t, "tcp"), MTU: 1420})
	fns, _, err := server.Open(0)
	if err != nil {
		t.Fatal(err)
	}
	receive, urgent := fns[0], fns[1]
	address := net.JoinHostPort("127.0.0.1", portString(server.opt.ListenTCP))
	c1, token := rawStream(t, address)
	waitUntil(t, time.Second, "stream", func() bool { return len(server.Paths()) == 1 })
	authenticateStreams(server)
	c2, answer := rawCompanion(t, address, token)
	if answer != 1 {
		t.Fatal("companion refused")
	}
	done := flood(c1, count)
	waitUntil(t, 5*time.Second, "reader waiting for room", func() bool { server.mu.Lock(); defer server.mu.Unlock(); return server.run.rxWaiters > 0 })
	initiation := make([]byte, 148)
	binary.LittleEndian.PutUint32(initiation, 1)
	if err := writePlaneFrame(c2, initiation, maxDatagram(1420), 0); err != nil {
		t.Fatal(err)
	}
	got := make(chan []byte, 1)
	go func() {
		p, s, e := [][]byte{make([]byte, 2048)}, []int{0}, []conn.Endpoint{nil}
		if n, err := urgent(p, s, e); err == nil && n == 1 {
			got <- p[0][:s[0]]
		}
	}()
	select {
	case p := <-got:
		if !bytes.Equal(p, initiation) {
			t.Fatal("wrong handshake")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("the handshake waited behind data")
	}
	n, largest := drain(t, receive, count)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	var dropped uint64
	for _, s := range server.Stats() {
		dropped += s.Dropped
	}
	if n != count || dropped != 0 || largest < 2 {
		t.Fatalf("received %d of %d, dropped %d, largest batch %d", n, count, dropped, largest)
	}

	c3, _ := rawStream(t, address)
	waitUntil(t, time.Second, "second stream", func() bool { return len(server.Paths()) == 2 })
	if err := <-flood(c3, count); err != nil {
		t.Fatal(err)
	}
	time.Sleep(200 * time.Millisecond)
	n, _ = drain(t, receive, count)
	dropped = 0
	for _, s := range server.Stats() {
		dropped += s.Dropped
	}
	if n == count || dropped == 0 {
		t.Fatalf("without a companion: received %d of %d, dropped %d", n, count, dropped)
	}
}

// closeConn is what the bind uses while it holds its lock: to a peer that
// reads nothing, as a congested one, a TLS connection's own Close blocks on
// its close_notify for up to five seconds; closeConn ends it at once.
func TestCloseConnDoesNotWaitOnAPeerThatReadsNothing(t *testing.T) {
	cert, _, err := activeCertificate(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	a, b := net.Pipe()
	server := tls.Server(a, &tls.Config{Certificates: []tls.Certificate{*cert}, MinVersion: tls.VersionTLS13})
	client := tls.Client(b, &tls.Config{InsecureSkipVerify: true, MinVersion: tls.VersionTLS13})
	go server.Handshake()
	if err := client.Handshake(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { a.Close() })
	closed := make(chan struct{})
	go func() {
		closeConn(client)
		close(closed)
	}()
	select {
	case <-closed:
	case <-time.After(time.Second):
		t.Fatal("closeConn waited on the peer")
	}
}
