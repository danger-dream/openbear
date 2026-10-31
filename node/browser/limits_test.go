package browser

import (
	"bufio"
	"context"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"
)

// filler yields n bytes of 'a' without holding them.
type filler struct{ n int64 }

func (f *filler) Read(p []byte) (int, error) {
	if f.n <= 0 {
		return 0, io.EOF
	}
	p = p[:min(int64(len(p)), f.n)]
	for i := range p {
		p[i] = 'a'
	}
	f.n -= int64(len(p))
	return len(p), nil
}

// allocated runs fn and returns the bytes it allocated.
func allocated(fn func()) uint64 {
	var before, after runtime.MemStats
	runtime.GC()
	runtime.ReadMemStats(&before)
	fn()
	runtime.ReadMemStats(&after)
	return after.TotalAlloc - before.TotalAlloc
}

const hugeEvent = 300 << 20

func TestPipeDiscardsOversizedEventWhileReading(t *testing.T) {
	stream := io.MultiReader(
		strings.NewReader(`{"method":"Console.messageAdded","params":{"text":"`),
		&filler{hugeEvent},
		strings.NewReader("\"}}\x00{\"method\":\"next\"}\x00"),
	)
	p := NewPipe(stream, io.Discard, nil).(*pipeTransport)
	p.setSizer(func([]byte) int { return maxEvent })
	var err error
	if n := allocated(func() { _, err = p.Receive() }); n > 32<<20 {
		t.Fatalf("held %d bytes of a discarded event", n)
	}
	var dropped *droppedMessage
	if !errors.As(err, &dropped) || !strings.HasPrefix(string(dropped.head), `{"method":"Console.messageAdded"`) {
		t.Fatalf("oversized event: %v", err)
	}
	if msg, err := p.Receive(); err != nil || string(msg) != `{"method":"next"}` {
		t.Fatalf("next message %q %v", msg, err)
	}
}

func TestWebSocketDiscardsOversizedEventWhileReading(t *testing.T) {
	prefix := []byte(`{"method":"Runtime.bindingCalled","params":{"payload":"`)
	header := binary.BigEndian.AppendUint64([]byte{0x81, 127}, uint64(len(prefix)+hugeEvent+3))
	next := `{"method":"next"}`
	stream := io.MultiReader(
		strings.NewReader(string(header)+string(prefix)),
		&filler{hugeEvent},
		strings.NewReader(`"}}`+string([]byte{0x81, byte(len(next))})+next),
	)
	c := &wsConn{r: bufio.NewReader(stream), closed: make(chan struct{}), limit: maxMessage, activity: make(chan struct{}, 1)}
	c.setSizer(func([]byte) int { return maxEvent })
	var err error
	if n := allocated(func() { _, err = c.Receive() }); n > 32<<20 {
		t.Fatalf("held %d bytes of a discarded event", n)
	}
	var dropped *droppedMessage
	if !errors.As(err, &dropped) || !strings.HasPrefix(string(dropped.head), `{"method":"Runtime.bindingCalled"`) {
		t.Fatalf("oversized event: %v", err)
	}
	if msg, err := c.Receive(); err != nil || string(msg) != next {
		t.Fatalf("next message %q %v", msg, err)
	}
}

func TestOnlyArtifactRepliesMayGrowLarge(t *testing.T) {
	c := &conn{large: map[int64]bool{5: true}}
	for head, want := range map[string]int{
		`{"id":5,"result":{"data":"`:                maxMessage,
		`{"id":6,"result":{"data":"`:                maxReply,
		`{"method":"Console.messageAdded","params"`: maxEvent,
		`{"error":{"code":1},"id":5}`:               maxEvent,
		`{"method":"x","params":{"id":5}}`:          maxEvent,
	} {
		if got := c.ceiling([]byte(head)); got != want {
			t.Fatalf("%s: ceiling %d, want %d", head, got, want)
		}
	}
	// Calls made for an artifact are marked while they wait for the reply.
	var e *Engine
	var mu sync.Mutex
	marks := map[string]bool{}
	e, _, _ = testEngine(t, func(f *fakeBrowser, msg map[string]any) {
		e.cdp.mu.Lock()
		large := e.cdp.large[int64(msg["id"].(float64))]
		e.cdp.mu.Unlock()
		mu.Lock()
		marks[msg["method"].(string)] = large
		mu.Unlock()
		f.reply(msg, map[string]any{"result": map[string]any{"value": 1}})
	})
	if _, err := e.Execute(context.Background(), "evaluate", raw(map[string]any{"targetId": "target", "expression": "1"})); err != nil {
		t.Fatal(err)
	}
	if _, err := e.Call(context.Background(), "Browser.getVersion", nil); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	defer mu.Unlock()
	if !marks["Runtime.evaluate"] || marks["Browser.getVersion"] {
		t.Fatalf("marks %v", marks)
	}
}

func TestOversizedEventIsSkippedAndTheConnectionCarriesOn(t *testing.T) {
	events := make(chan string, 4)
	tr, fb := newFakeBrowser(func(f *fakeBrowser, msg map[string]any) {
		f.send(map[string]any{"method": "Console.messageAdded", "params": map[string]any{"text": strings.Repeat("x", maxEvent)}})
		f.send(map[string]any{"method": "small", "params": map[string]any{}})
		f.reply(msg, map[string]any{"data": strings.Repeat("y", maxEvent+1)})
	})
	defer fb.close()
	c := newConn(tr, 5*time.Second, func(method string, _ *Obj, _ string) { events <- method })
	defer c.close()
	r, err := c.call(context.Background(), "x", nil, "", 5*time.Second)
	if err != nil || len(asStr(r.Must("data"))) != maxEvent+1 {
		t.Fatalf("reply beyond the event bound: %v", err)
	}
	if method := <-events; method != "small" || len(events) != 0 {
		t.Fatalf("events %q", method)
	}
}

func TestHandshakeResponseIsBounded(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	go func() {
		c, err := ln.Accept()
		if err != nil {
			return
		}
		defer c.Close()
		bufio.NewReader(c).ReadString('\n')
		c.Write([]byte("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"))
		line := []byte("X-Filler: " + strings.Repeat("z", 1000) + "\r\n")
		for {
			if _, err := c.Write(line); err != nil {
				return
			}
		}
	}()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_, err = DialWebSocket(ctx, "ws://"+ln.Addr().String()+"/devtools/browser/x")
	if err == nil || !strings.Contains(err.Error(), "too large") {
		t.Fatalf("endless handshake: %v", err)
	}
}
