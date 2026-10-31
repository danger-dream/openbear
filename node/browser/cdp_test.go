package browser

import (
	"context"
	"errors"
	"reflect"
	"sync"
	"testing"
	"time"
)

type observed struct {
	method, session string
	params          string
}

func TestCDPMultiplexesEventsAndErrorsWithoutReplay(t *testing.T) {
	tr, fb := newFakeBrowser(func(f *fakeBrowser, msg map[string]any) {
		if msg["method"] == "hang" {
			return
		}
		if msg["method"] == "fail" {
			f.send(map[string]any{"id": msg["id"], "error": map[string]any{"message": "secret detail"}})
			return
		}
		f.send(map[string]any{"method": "Page.loadEventFired", "sessionId": "s", "params": map[string]any{"timestamp": 1}})
		f.reply(msg, map[string]any{"answer": msg["method"]})
	})
	var mu sync.Mutex
	var events []observed
	c := newConn(tr, time.Second, func(method string, params *Obj, session string) {
		body, _ := dumps(params)
		mu.Lock()
		events = append(events, observed{method, session, string(body)})
		mu.Unlock()
	})
	defer c.close()
	result, err := c.call(context.Background(), "test", O("x", 1), "s", time.Second)
	if err != nil || pyStr(result.Must("answer")) != "test" {
		t.Fatalf("call: %v %v", result, err)
	}
	mu.Lock()
	if !reflect.DeepEqual(events, []observed{{"Page.loadEventFired", "s", `{"timestamp":1}`}}) {
		t.Fatalf("events %v", events)
	}
	mu.Unlock()
	fb.mu.Lock()
	first := fb.received[0]
	fb.mu.Unlock()
	if first["sessionId"] != "s" {
		t.Fatalf("session not sent: %v", fb.received[0])
	}
	if _, err := c.call(context.Background(), "hang", nil, "", 20*time.Millisecond); !isTimeout(err) {
		t.Fatalf("expected timeout, got %v", err)
	}
	if _, err := c.call(context.Background(), "fail", nil, "", time.Second); err == nil {
		t.Fatal("expected protocol error")
	} else if _, ok := isProtocol(err); !ok {
		t.Fatalf("expected protocol error, got %T", err)
	}
	c.mu.Lock()
	pending := len(c.pending)
	c.mu.Unlock()
	if pending != 0 || len(fb.methods()) != 3 {
		t.Fatalf("pending=%d methods=%v", pending, fb.methods())
	}
}

func TestCDPOutOfOrderRepliesStayWithTheirCallAndTimeoutDoesNotPoisonNext(t *testing.T) {
	var held []map[string]any
	tr, fb := newFakeBrowser(func(f *fakeBrowser, msg map[string]any) {
		held = append(held, msg)
		switch {
		case msg["method"] == "timeout":
		case len(held) == 2:
			for i := len(held) - 1; i >= 0; i-- {
				f.reply(held[i], map[string]any{"method": held[i]["method"]})
			}
		case msg["method"] == "after":
			f.reply(held[len(held)-2], map[string]any{"late": true})
			f.reply(msg, map[string]any{"method": "after"})
		}
	})
	c := newConn(tr, time.Second, func(string, *Obj, string) {})
	defer c.close()
	var wg sync.WaitGroup
	results := make([]string, 2)
	for i, method := range []string{"first", "second"} {
		wg.Add(1)
		go func() {
			defer wg.Done()
			r, err := c.call(context.Background(), method, nil, "", time.Second)
			if err == nil {
				results[i] = pyStr(r.Must("method"))
			}
		}()
		// Keep the send order deterministic.
		for len(fb.methods()) < i+1 {
			time.Sleep(time.Millisecond)
		}
	}
	wg.Wait()
	if !reflect.DeepEqual(results, []string{"first", "second"}) {
		t.Fatalf("results %v", results)
	}
	if _, err := c.call(context.Background(), "timeout", nil, "", 10*time.Millisecond); !isTimeout(err) {
		t.Fatalf("expected timeout, got %v", err)
	}
	r, err := c.call(context.Background(), "after", nil, "", time.Second)
	if err != nil || pyStr(r.Must("method")) != "after" {
		t.Fatalf("after: %v %v", r, err)
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.pending) != 0 || len(fb.methods()) != 4 {
		t.Fatalf("pending=%d methods=%v", len(c.pending), fb.methods())
	}
}

func TestCDPDisconnectRejectsEveryPendingCallWithoutResending(t *testing.T) {
	tr, fb := newFakeBrowser(func(f *fakeBrowser, msg map[string]any) {
		f.mu.Lock()
		n := len(f.received)
		f.mu.Unlock()
		if n == 2 {
			f.close()
		}
	})
	c := newConn(tr, 5*time.Second, func(string, *Obj, string) {})
	defer c.close()
	var wg sync.WaitGroup
	errs := make([]error, 2)
	for i, method := range []string{"first", "second"} {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, errs[i] = c.call(context.Background(), method, nil, "", 5*time.Second)
		}()
		for len(fb.methods()) < i+1 {
			time.Sleep(time.Millisecond)
		}
	}
	wg.Wait()
	for _, err := range errs {
		if !errors.Is(err, ErrDisconnected) {
			t.Fatalf("expected disconnect, got %v", errs)
		}
	}
	if c.connected() {
		t.Fatal("still connected")
	}
	c.mu.Lock()
	pending := len(c.pending)
	c.mu.Unlock()
	if pending != 0 || !reflect.DeepEqual(fb.methods(), []string{"first", "second"}) {
		t.Fatalf("pending=%d methods=%v", pending, fb.methods())
	}
	if _, err := c.call(context.Background(), "third", nil, "", time.Second); !errors.Is(err, ErrDisconnected) {
		t.Fatalf("call after disconnect: %v", err)
	}
	if len(fb.methods()) != 2 {
		t.Fatal("resent after disconnect")
	}
}

func TestPipeTransportFramesNULTerminatedMessages(t *testing.T) {
	tr, fb := newFakeBrowser(func(f *fakeBrowser, msg map[string]any) {
		// A large reply exercises reads beyond the buffered reader size.
		big := make([]byte, 3<<20)
		for i := range big {
			big[i] = 'a'
		}
		f.reply(msg, map[string]any{"data": string(big)})
	})
	c := newConn(tr, 5*time.Second, func(string, *Obj, string) {})
	defer c.close()
	r, err := c.call(context.Background(), "big", nil, "", 5*time.Second)
	if err != nil || len(asStr(r.Must("data"))) != 3<<20 {
		t.Fatalf("big reply: %v", err)
	}
	_ = fb
}
