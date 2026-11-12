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
	// A tunnel carries SSH, which keeps up to its channel window (2 MiB in OpenSSH) in flight
	// unacknowledged: an exit whose link onwards is slower than the controller's must hold it.
	tunnelBacklog = 4 << 20
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

	mu      sync.Mutex
	cond    *sync.Cond
	window  int
	queued  int
	backlog int
	reason  string
}

func newTerminal(con console, backlog int) *terminal {
	t := &terminal{con: con, input: make(chan []byte, 1024), quit: make(chan struct{}), window: outputWindow, backlog: backlog}
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
	over := t.queued+len(data) > t.backlog
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
	// dialing holds the tunnels still connecting, which a term.close or the session's end calls off.
	dialing map[uint32]context.CancelFunc
	ended   bool
}

// tunnelDial connects a terminal's tunnel; tests put others.
var tunnelDial = dialTunnel

func newTerminals() *terminals {
	return &terminals{open: map[uint32]*terminal{}, dialing: map[uint32]context.CancelFunc{}}
}

func (a *Agent) termOpen(ctx context.Context, ch *channel, ts *terminals, m envelope) {
	closed := func(reason string) { ch.send(map[string]any{"t": "term.closed", "sid": m.Sid, "reason": reason}) }
	if m.Dial != "" {
		// Reaching another machine from this one is what running commands here allows.
		if !a.config().Capabilities.Exec {
			closed("capability_disabled")
			return
		}
		dctx, cancel := context.WithCancel(ctx)
		ts.mu.Lock()
		if ts.ended || ts.takenLocked(m.Sid) {
			ts.mu.Unlock()
			cancel()
			closed("too_many_terminals")
			return
		}
		ts.dialing[m.Sid] = cancel
		ts.mu.Unlock()
		// Dialed off the session loop, which must keep reading meanwhile.
		go func() {
			con, err := tunnelDial(dctx, m.Dial)
			ts.mu.Lock()
			delete(ts.dialing, m.Sid)
			ts.mu.Unlock()
			called := dctx.Err() != nil
			cancel()
			if err != nil {
				if !called {
					closed("connect_failed")
				} else if ctx.Err() == nil {
					closed("closed")
				}
				return
			}
			if called {
				con.Hangup()
				con.Wait()
				if ctx.Err() == nil {
					closed("closed")
				}
				return
			}
			a.attachTerminal(ctx, ch, ts, m.Sid, con, tunnelBacklog, closed)
		}()
		return
	}
	if !a.config().Capabilities.Terminal {
		closed("capability_disabled")
		return
	}
	if ts.taken(m.Sid) {
		closed("too_many_terminals")
		return
	}
	con, err := startShell(m.Cols, m.Rows)
	if err != nil {
		closed("start_failed")
		return
	}
	a.attachTerminal(ctx, ch, ts, m.Sid, con, inputBacklog, closed)
}

func (a *Agent) attachTerminal(ctx context.Context, ch *channel, ts *terminals, sid uint32, con console, backlog int, closed func(string)) {
	ts.mu.Lock()
	if ts.ended || ts.takenLocked(sid) {
		ts.mu.Unlock()
		con.Hangup()
		con.Wait()
		closed("too_many_terminals")
		return
	}
	t := newTerminal(con, backlog)
	ts.open[sid] = t
	ts.mu.Unlock()
	a.active.Add(1)
	ch.send(map[string]any{"t": "term.opened", "sid": sid})
	go t.pumpInput()
	go func() {
		prefix := binary.BigEndian.AppendUint32(nil, sid)
		buf := make([]byte, 32<<10)
		for {
			n, err := con.Read(buf)
			if n > 0 && (!t.acquire(n) || ch.write(kindTerminal, prefix, buf[:n]) != nil) {
				break
			}
			if err != nil {
				break
			}
		}
		t.stop("exited")
		con.Wait()
		ts.mu.Lock()
		delete(ts.open, sid)
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

func (ts *terminals) taken(sid uint32) bool {
	ts.mu.Lock()
	defer ts.mu.Unlock()
	return ts.takenLocked(sid)
}

func (ts *terminals) takenLocked(sid uint32) bool {
	_, exists := ts.open[sid]
	_, dialing := ts.dialing[sid]
	return exists || dialing || len(ts.open)+len(ts.dialing) >= maxTerminals
}

// close ends the terminal sid, or calls off its tunnel while it still connects.
func (ts *terminals) close(sid uint32) {
	ts.mu.Lock()
	t, cancel := ts.open[sid], ts.dialing[sid]
	ts.mu.Unlock()
	if t != nil {
		t.stop("closed")
	} else if cancel != nil {
		cancel()
	}
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
	open, dialing := ts.open, ts.dialing
	ts.open, ts.dialing, ts.ended = map[uint32]*terminal{}, map[uint32]context.CancelFunc{}, true
	ts.mu.Unlock()
	for _, cancel := range dialing {
		cancel()
	}
	for _, t := range open {
		t.stop("node_disconnected")
	}
}
