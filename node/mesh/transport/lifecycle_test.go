package transport

import (
	"bytes"
	"crypto/tls"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"golang.zx2c4.com/wireguard/conn"
)

func portString(p int) string { return strconv.Itoa(p) }

func openBind(t *testing.T, b *Bind) conn.ReceiveFunc {
	t.Helper()
	f, _, err := b.Open(0)
	if err != nil {
		t.Fatal(err)
	}
	return f[0]
}
func packet4() []byte { p := make([]byte, 64); binary.LittleEndian.PutUint32(p, 4); return p }
func pathReady(t *testing.T, b *Bind, key string) *endpoint {
	t.Helper()
	waitUntil(t, 5*time.Second, "path connected", func() bool { b.mu.Lock(); defer b.mu.Unlock(); p := b.paths[key]; return p != nil && p.current != nil })
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.paths[key]
}

func TestPinsFailClosedAndCanBeUpdated(t *testing.T) {
	dir := t.TempDir()
	cert, err := EnsureCertificate(dir)
	if err != nil {
		t.Fatal(err)
	}
	server := newBind(t, Options{ListenTLS: freePort(t, "tcp"), TLSDir: dir})
	openBind(t, server)
	client := newBind(t, Options{})
	openBind(t, client)
	key := "tls://" + net.JoinHostPort("127.0.0.1", portString(server.opt.ListenTLS))
	client.SetPins(key, []string{strings.Repeat("00", 32)})
	if _, err := client.ParseEndpoint(key); err != nil {
		t.Fatal(err)
	}
	waitUntil(t, 3*time.Second, "security error", func() bool { s := client.Stats(); return len(s) == 1 && s[0].State == "security_error" })
	attempts := client.Stats()[0].DialAttempts
	time.Sleep(700 * time.Millisecond)
	if client.Stats()[0].DialAttempts != attempts {
		t.Fatal("pin mismatch retried without trust change")
	}
	client.SetPins(key, []string{strings.Repeat("00", 32), cert.Active.FingerprintSHA256})
	ep := pathReady(t, client, key)
	client.mu.Lock()
	first := ep.current
	client.mu.Unlock()
	client.SetPins(key, []string{cert.Active.FingerprintSHA256})
	client.mu.Lock()
	same := first == ep.current
	client.mu.Unlock()
	if !same {
		t.Fatal("removing unused old pin closed valid connection")
	}
	client.SetPins(key, []string{"invalid"})
	waitUntil(t, 3*time.Second, "revoked connection closes", func() bool { client.mu.Lock(); defer client.mu.Unlock(); return ep.current == nil })
	if _, err := client.ParseEndpoint("tls://127.0.0.1:1#pin"); err == nil {
		t.Fatal("fragment bypassed SetPins")
	}
	client.Release(key)
	if len(client.Stats()) != 0 {
		t.Fatal("released path retained")
	}
}

func TestReconfigureRollbackAndClose(t *testing.T) {
	server := newBind(t, Options{ListenTCP: freePort(t, "tcp")})
	oldReceive := openBind(t, server)
	client := newBind(t, Options{})
	openBind(t, client)
	key := "tcp://" + net.JoinHostPort("127.0.0.1", portString(server.opt.ListenTCP))
	client.ParseEndpoint(key)
	ep := pathReady(t, client, key)
	occupied, err := net.Listen("tcp", "[::]:0")
	if err != nil {
		t.Fatal(err)
	}
	defer occupied.Close()
	next := server.opt
	next.ListenTLS = occupied.Addr().(*net.TCPAddr).Port
	next.TLSDir = t.TempDir()
	if err := server.Reconfigure(next); err == nil {
		t.Fatal("occupied listener was accepted")
	}
	client.mu.Lock()
	live := ep.current
	client.mu.Unlock()
	if live == nil {
		t.Fatal("rollback disrupted old listener")
	}
	if err := server.Reconfigure(Options{}); err != nil {
		t.Fatal(err)
	}
	waitUntil(t, time.Second, "server accepted task closed", func() bool { server.mu.Lock(); defer server.mu.Unlock(); return len(server.run.sessions) == 0 })
	client.Release(key)
	if err := client.Send([][]byte{packet4()}, ep); !errors.Is(err, net.ErrClosed) {
		t.Fatalf("released endpoint reusable: %v", err)
	}
	if err := server.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := oldReceive([][]byte{make([]byte, 2048)}, make([]int, 1), make([]conn.Endpoint, 1)); !errors.Is(err, net.ErrClosed) {
		t.Fatalf("receive after close: %v", err)
	}
	openBind(t, server)
	if _, err := oldReceive([][]byte{make([]byte, 2048)}, make([]int, 1), make([]conn.Endpoint, 1)); !errors.Is(err, net.ErrClosed) {
		t.Fatalf("old receive reused after reopen: %v", err)
	}
}

func TestInboundFrameRejectionAndUnauthenticatedLimit(t *testing.T) {
	server := newBind(t, Options{ListenTCP: freePort(t, "tcp")})
	openBind(t, server)
	address := net.JoinHostPort("127.0.0.1", portString(server.opt.ListenTCP))
	for _, frame := range [][]byte{{0, 1, 0}, {0x10, 0, 1}, {255, 255, 0}, {0, 0, 0}} {
		c, err := net.Dial("tcp", address)
		if err != nil {
			t.Fatal(err)
		}
		c.SetDeadline(time.Now().Add(time.Second))
		if err := exchangePreamble(c); err != nil {
			t.Fatal(err)
		}
		c.Write(frame)
		c.SetReadDeadline(time.Now().Add(time.Second))
		var one [1]byte
		if _, err := c.Read(one[:]); err == nil {
			t.Fatal("invalid frame connection remained open")
		}
		c.Close()
	}
	waitUntil(t, time.Second, "invalid sessions removed", func() bool { server.mu.Lock(); defer server.mu.Unlock(); return len(server.run.sessions) == 0 })
	var conns []net.Conn
	for i := 0; i < 10; i++ {
		c, err := net.Dial("tcp", address)
		if err != nil {
			t.Fatal(err)
		}
		conns = append(conns, c)
	}
	defer func() {
		for _, c := range conns {
			c.Close()
		}
	}()
	waitUntil(t, time.Second, "connections accepted", func() bool { server.mu.Lock(); defer server.mu.Unlock(); return len(server.run.sessions) > 0 })
	server.mu.Lock()
	n := len(server.run.sessions)
	unauth := server.run.unauth
	cost := server.run.socketBytes
	server.mu.Unlock()
	if n > 4 || unauth > 4 || cost > server.opt.limits().socketBytes {
		t.Fatalf("limits: connections=%d unauth=%d bytes=%d", n, unauth, cost)
	}
	for _, c := range conns {
		c.Close()
	}
	waitUntil(t, time.Second, "unauth slots reclaimed", func() bool {
		server.mu.Lock()
		defer server.mu.Unlock()
		return server.run.unauth == 0 && len(server.run.sessions) == 0
	})
}

func TestCookieDoesNotAuthenticateAndDeadline(t *testing.T) {
	server := newBind(t, Options{ListenTCP: freePort(t, "tcp")})
	receive := openBind(t, server)
	c, err := net.Dial("tcp", net.JoinHostPort("127.0.0.1", portString(server.opt.ListenTCP)))
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	if err = exchangePreamble(c); err != nil {
		t.Fatal(err)
	}
	if err = writeFrame(c, packet4(), maxDatagram(1400)); err != nil {
		t.Fatal(err)
	}
	eps := make([]conn.Endpoint, 1)
	if _, err = receive([][]byte{make([]byte, 2048)}, make([]int, 1), eps); err != nil {
		t.Fatal(err)
	}
	cookie := make([]byte, 64)
	binary.LittleEndian.PutUint32(cookie, 3)
	if err = server.Send([][]byte{cookie}, eps[0]); err != nil {
		t.Fatal(err)
	}
	if _, err = readFrame(c, make([]byte, 2048)); err != nil {
		t.Fatal(err)
	}
	server.mu.Lock()
	unconfirmed := server.run.unauth == 1
	server.mu.Unlock()
	if !unconfirmed {
		t.Fatal("cookie incorrectly authenticated peer")
	}
	c.SetReadDeadline(time.Now().Add(12 * time.Second))
	var one [1]byte
	_, err = c.Read(one[:])
	if err == nil {
		t.Fatal("unconfirmed connection survived deadline")
	}
	if e, ok := err.(net.Error); ok && e.Timeout() {
		t.Fatal("server did not enforce auth deadline")
	}
	waitUntil(t, time.Second, "expired slot reclaimed", func() bool { server.mu.Lock(); defer server.mu.Unlock(); return server.run.unauth == 0 })
}

func TestSlowReceiverHasBoundedSendMemory(t *testing.T) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	stop := make(chan struct{})
	done := make(chan struct{})
	go func() {
		defer close(done)
		c, e := l.Accept()
		if e != nil {
			return
		}
		defer c.Close()
		c.(*net.TCPConn).SetReadBuffer(1024)
		if exchangePreamble(c) != nil {
			return
		}
		<-stop
	}()
	defer func() { close(stop); <-done }()
	b := newBind(t, Options{})
	openBind(t, b)
	key := "tcp://" + l.Addr().String()
	b.ParseEndpoint(key)
	ep := pathReady(t, b, key)
	payload := make([]byte, maxDatagram(1400))
	binary.LittleEndian.PutUint32(payload, 4)
	var backpressure atomic.Uint64
	var workers sync.WaitGroup
	runtime.GC()
	var before runtime.MemStats
	runtime.ReadMemStats(&before)
	for i := 0; i < 8; i++ {
		workers.Add(1)
		go func() {
			defer workers.Done()
			for j := 0; j < 2000; j++ {
				err := b.Send([][]byte{payload}, ep)
				if errors.Is(err, ErrBackpressure) {
					backpressure.Add(1)
				}
			}
		}()
	}
	waitUntil(t, 2*time.Second, "backpressure observed", func() bool { return backpressure.Load() > 100 })
	for i := 0; i < 10; i++ {
		stats := b.Stats()
		if len(stats) != 1 || stats[0].TxBufferedBytes > uint64(len(payload)+3) {
			t.Fatalf("unbounded send state: %+v", stats)
		}
		time.Sleep(10 * time.Millisecond)
	}
	b.Release(key)
	workers.Wait()
	runtime.GC()
	var after runtime.MemStats
	runtime.ReadMemStats(&after)
	if after.HeapAlloc > before.HeapAlloc+4<<20 {
		t.Fatalf("retained heap grew: before=%d after=%d", before.HeapAlloc, after.HeapAlloc)
	}
	t.Logf("slow receiver: backpressure=%d; retained heap %d -> %d; no application send queue", backpressure.Load(), before.HeapAlloc, after.HeapAlloc)
}

func TestUDPDisabledAndEphemeralReclaimed(t *testing.T) {
	b := newBind(t, Options{})
	openBind(t, b)
	b.mu.Lock()
	none := b.run.udp == nil
	b.mu.Unlock()
	if !none {
		t.Fatal("zero UDP opened a listener")
	}
	ep, err := b.ParseEndpoint("udp://127.0.0.1:50000")
	if err != nil {
		t.Fatal(err)
	}
	b.mu.Lock()
	u := b.run.udp
	b.mu.Unlock()
	if u == nil {
		t.Fatal("outbound UDP has no ephemeral socket")
	}
	b.Release(ep.DstToString())
	select {
	case <-u.done:
	default:
		t.Fatal("ephemeral receive task not reclaimed")
	}
	if err = b.Send([][]byte{[]byte{1}}, ep); !errors.Is(err, net.ErrClosed) {
		t.Fatal(err)
	}
}

func TestRetryBoundedAndFrameWriteFailure(t *testing.T) {
	for i := 0; i < 100; i++ {
		d := retryDelay(i)
		if d < 400*time.Millisecond || d > 30*time.Second {
			t.Fatal(d)
		}
	}
	if err := writeFrame(failingWriter{}, []byte{1}, 1400); !errors.Is(err, io.ErrClosedPipe) {
		t.Fatal(err)
	}
	if err := writeFrame(&bytes.Buffer{}, make([]byte, 1450), 1440); !errors.Is(err, ErrFrame) {
		t.Fatal(err)
	}
}

type failingWriter struct{}

func (failingWriter) Write([]byte) (int, error) { return 0, io.ErrClosedPipe }

func TestTLSMinimumVersion(t *testing.T) {
	dir := t.TempDir()
	b := newBind(t, Options{ListenTLS: freePort(t, "tcp"), TLSDir: dir})
	openBind(t, b)
	c, err := tls.Dial("tcp", net.JoinHostPort("127.0.0.1", portString(b.opt.ListenTLS)), &tls.Config{MinVersion: tls.VersionTLS12, MaxVersion: tls.VersionTLS12, InsecureSkipVerify: true})
	if c != nil {
		c.Close()
	}
	if err == nil {
		t.Fatal("TLS 1.2 was accepted")
	}
}
