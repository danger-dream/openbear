package transport

import (
	"errors"
	"io"
	"net"
	"testing"
	"time"
)

func pipeSender(t *testing.T) (*Bind, *session, net.Conn) {
	t.Helper()
	b := newBind(t, Options{})
	openBind(t, b)
	local, remote := net.Pipe()
	ep := &endpoint{b: b, key: "tcp://127.0.0.1:1", transport: "tcp"}
	s := &session{conn: local, ep: ep, done: make(chan struct{})}
	ep.current = s
	b.mu.Lock()
	b.run.sessions[s] = true
	b.mu.Unlock()
	t.Cleanup(func() { remote.Close() })
	return b, s, remote
}

func sendAsync(b *Bind, s *session, plane uint8, value byte) <-chan error {
	done := make(chan error, 1)
	go func() { done <- b.send([][]byte{{value}}, s.ep, plane, false) }()
	return done
}

func waitSendQueue(t *testing.T, b *Bind, s *session, plane uint8, n int) {
	t.Helper()
	waitUntil(t, time.Second, "bounded send queue", func() bool {
		b.mu.Lock()
		defer b.mu.Unlock()
		return len(s.waiters[plane]) == n
	})
}

func receiveSend(t *testing.T, remote net.Conn, plane uint8, value byte) {
	t.Helper()
	remote.SetReadDeadline(time.Now().Add(time.Second))
	n, gotPlane, err := readFrameHeader(remote, 1400)
	if err != nil {
		t.Fatal(err)
	}
	data := make([]byte, n)
	if _, err := io.ReadFull(remote, data); err != nil {
		t.Fatal(err)
	}
	if gotPlane != plane || len(data) != 1 || data[0] != value {
		t.Fatalf("frame plane=%d data=%x, want plane=%d byte=%d", gotPlane, data, plane, value)
	}
}

func TestSendWaitsWithControlPriorityAndReservedSlots(t *testing.T) {
	b, s, remote := pipeSender(t)
	results := []<-chan error{sendAsync(b, s, 0, 0)}
	waitUntil(t, time.Second, "writer blocked on socket", func() bool {
		b.mu.Lock()
		defer b.mu.Unlock()
		return s.writing
	})
	for i := 1; i <= maxSendWaiters; i++ {
		results = append(results, sendAsync(b, s, 0, byte(i)))
		waitSendQueue(t, b, s, 0, i)
	}
	if err := b.Send([][]byte{{99}}, s.ep); !errors.Is(err, ErrBackpressure) {
		t.Fatalf("accepted excess data waiter: %v", err)
	}
	for i := 1; i <= maxSendWaiters; i++ {
		results = append(results, sendAsync(b, s, 1, byte(10+i)))
		waitSendQueue(t, b, s, 1, i)
	}
	if err := b.ControlBind().Send([][]byte{{99}}, s.ep); !errors.Is(err, ErrBackpressure) {
		t.Fatalf("accepted excess control waiter: %v", err)
	}
	receiveSend(t, remote, 0, 0)
	for i := 1; i <= maxSendWaiters; i++ {
		receiveSend(t, remote, 1, byte(10+i))
	}
	for i := 1; i <= maxSendWaiters; i++ {
		receiveSend(t, remote, 0, byte(i))
	}
	for _, result := range results {
		if err := <-result; err != nil {
			t.Fatal("queued send dropped", err)
		}
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if s.writing || len(s.waiters[0])+len(s.waiters[1]) != 0 || s.ep.stats.TxBufferedBytes != 0 {
		t.Fatal("send state not reclaimed")
	}
}

func TestSendWaiterTimeoutAndClose(t *testing.T) {
	b, s, _ := pipeSender(t)
	first := sendAsync(b, s, 0, 1)
	waitUntil(t, time.Second, "blocked writer", func() bool {
		b.mu.Lock()
		defer b.mu.Unlock()
		return s.writing
	})
	start := time.Now()
	err := b.sendDatagram([]byte{2}, s.ep, 0, false, nil, start.Add(50*time.Millisecond))
	if !errors.Is(err, ErrBackpressure) || time.Since(start) > time.Second {
		t.Fatalf("wait timeout: %v, %v", err, time.Since(start))
	}
	waitSendQueue(t, b, s, 0, 0)
	waiting := sendAsync(b, s, 0, 3)
	waitSendQueue(t, b, s, 0, 1)
	start = time.Now()
	b.Close()
	for _, result := range []<-chan error{first, waiting} {
		select {
		case err := <-result:
			if err == nil {
				t.Fatal("closed socket send succeeded")
			}
		case <-time.After(time.Second):
			t.Fatal("close did not unblock Send")
		}
	}
	if time.Since(start) > time.Second {
		t.Fatal("close delayed")
	}
}

func TestSendBatchHasOneDeadline(t *testing.T) {
	b, s, _ := pipeSender(t)
	packets := make([][]byte, 128)
	for i := range packets {
		packets[i] = []byte{1}
	}
	start := time.Now()
	err := b.Send(packets, s.ep)
	elapsed := time.Since(start)
	if err == nil || elapsed < sendTimeout || elapsed > sendTimeout+time.Second {
		t.Fatalf("batch deadline multiplied by packet count: err=%v elapsed=%v", err, elapsed)
	}
	if !s.closed.Load() {
		t.Fatal("timed out partial frame did not close socket")
	}
}
