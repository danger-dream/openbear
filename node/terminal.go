//go:build unix || windows

package node

import (
	"context"
	"encoding/binary"
	"io"
	"sync"
)

const (
	maxTerminals = 16
	// Output in flight per terminal before the controller acknowledges it.
	// Without a window one busy terminal would stall reports and every other
	// terminal sharing this connection.
	outputWindow = 256 << 10
	inputBacklog = 1 << 20
)

// console is one shell attached to a pseudo terminal.
type console interface {
	io.ReadWriter
	Resize(cols, rows int) error
	// Hangup ends the shell and everything it started; Read then fails.
	Hangup()
	Wait()
}

type terminal struct {
	con   console
	input chan []byte
	quit  chan struct{}

	mu     sync.Mutex
	cond   *sync.Cond
	window int
	queued int
	reason string
}

func newTerminal(con console) *terminal {
	t := &terminal{con: con, input: make(chan []byte, 1024), quit: make(chan struct{}), window: outputWindow}
	t.cond = sync.NewCond(&t.mu)
	return t
}

// stop hangs up the shell. The first reason given is the one reported.
func (t *terminal) stop(reason string) {
	t.mu.Lock()
	if t.reason != "" {
		t.mu.Unlock()
		return
	}
	t.reason = reason
	t.mu.Unlock()
	t.cond.Broadcast()
	close(t.quit)
	t.con.Hangup()
}

func (t *terminal) resize(cols, rows int) { t.con.Resize(cols, rows) }

func (t *terminal) acquire(n int) bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	for t.window < n && t.reason == "" {
		t.cond.Wait()
	}
	if t.reason != "" {
		return false
	}
	t.window -= n
	return true
}

func (t *terminal) ack(n int) {
	if n <= 0 {
		return
	}
	t.mu.Lock()
	t.window = min(t.window+n, outputWindow)
	t.mu.Unlock()
	t.cond.Broadcast()
}

// write never blocks the session loop: a shell that stops reading while input
// keeps arriving is closed once its backlog passes the limit.
func (t *terminal) write(data []byte) {
	t.mu.Lock()
	over := t.queued+len(data) > inputBacklog
	if !over {
		t.queued += len(data)
	}
	t.mu.Unlock()
	if !over {
		select {
		case t.input <- data:
			return
		case <-t.quit:
			return
		default:
		}
	}
	t.stop("input_overflow")
}

func (t *terminal) pumpInput() {
	for {
		select {
		case <-t.quit:
			return
		case data := <-t.input:
			t.mu.Lock()
			t.queued -= len(data)
			t.mu.Unlock()
			if _, err := t.con.Write(data); err != nil {
				t.stop("exited")
				return
			}
		}
	}
}

type terminals struct {
	mu   sync.Mutex
	open map[uint32]*terminal
}

func newTerminals() *terminals { return &terminals{open: map[uint32]*terminal{}} }

func (a *Agent) termOpen(ctx context.Context, ch *channel, ts *terminals, m envelope) {
	closed := func(reason string) { ch.send(map[string]any{"t": "term.closed", "sid": m.Sid, "reason": reason}) }
	if !a.config().Capabilities.Terminal {
		closed("capability_disabled")
		return
	}
	ts.mu.Lock()
	if _, exists := ts.open[m.Sid]; exists || len(ts.open) >= maxTerminals {
		ts.mu.Unlock()
		closed("too_many_terminals")
		return
	}
	con, err := startShell(m.Cols, m.Rows)
	if err != nil {
		ts.mu.Unlock()
		closed("start_failed")
		return
	}
	t := newTerminal(con)
	ts.open[m.Sid] = t
	ts.mu.Unlock()
	a.active.Add(1)
	ch.send(map[string]any{"t": "term.opened", "sid": m.Sid})
	go t.pumpInput()
	go func() {
		sid := binary.BigEndian.AppendUint32(nil, m.Sid)
		buf := make([]byte, 32<<10)
		for {
			n, err := con.Read(buf)
			if n > 0 && (!t.acquire(n) || ch.write(kindTerminal, sid, buf[:n]) != nil) {
				break
			}
			if err != nil {
				break
			}
		}
		t.stop("exited")
		con.Wait()
		ts.mu.Lock()
		delete(ts.open, m.Sid)
		ts.mu.Unlock()
		a.active.Add(-1)
		if ctx.Err() == nil {
			t.mu.Lock()
			reason := t.reason
			t.mu.Unlock()
			closed(reason)
		}
	}()
}

func (ts *terminals) get(sid uint32) *terminal {
	ts.mu.Lock()
	defer ts.mu.Unlock()
	return ts.open[sid]
}

// input takes [session id uint32 BE][bytes] from the controller.
func (ts *terminals) input(frame []byte) {
	if len(frame) < 5 {
		return
	}
	if t := ts.get(binary.BigEndian.Uint32(frame[:4])); t != nil {
		t.write(frame[4:])
	}
}

func (ts *terminals) closeAll() {
	ts.mu.Lock()
	open := ts.open
	ts.open = map[uint32]*terminal{}
	ts.mu.Unlock()
	for _, t := range open {
		t.stop("node_disconnected")
	}
}
