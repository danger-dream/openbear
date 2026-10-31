package browser

// The engine runs like Python's asyncio loop: every goroutine executing
// engine logic holds Engine.mu, and releases it only while blocked on CDP,
// a timer, a lock, an event or a task. Event handlers run with the same
// lock, so state changes interleave exactly at the Python await points.

import (
	"context"
	"time"
)

func (e *Engine) block(fn func()) {
	e.mu.Unlock()
	defer e.mu.Lock()
	fn()
}

// sleep is asyncio.sleep bounded by ctx.
func (e *Engine) sleep(ctx context.Context, d time.Duration) error {
	if err := ctx.Err(); err != nil {
		return ctxErr(ctx)
	}
	var err error
	e.block(func() {
		t := time.NewTimer(d)
		defer t.Stop()
		select {
		case <-t.C:
		case <-ctx.Done():
			err = ctxErr(ctx)
		}
	})
	return err
}

// yield is asyncio.sleep(0): let event handlers and other requests run.
func (e *Engine) yield() {
	e.block(func() {})
}

// retryPause is common.retry_pause. When the remaining budget is shorter
// than the pause, the operation deadline ends the wait (Python's outer
// asyncio.timeout fires in the same loop iteration as that sleep).
func (e *Engine) retryPause(ctx context.Context) error {
	d := 50 * time.Millisecond
	if op := opOf(ctx); op != nil {
		r, err := op.remaining(ctx)
		if err != nil {
			return err
		}
		if r <= d {
			var werr error
			e.block(func() {
				t := time.NewTimer(r + time.Second)
				defer t.Stop()
				select {
				case <-ctx.Done():
					werr = ctxErr(ctx)
				case <-t.C:
				}
			})
			if werr != nil {
				return werr
			}
			_, err := op.remaining(ctx)
			return err
		}
	}
	return e.sleep(ctx, d)
}

// aLock is asyncio.Lock.
type aLock struct{ ch chan struct{} }

func newALock() *aLock { return &aLock{ch: make(chan struct{}, 1)} }

func (e *Engine) acquire(ctx context.Context, l *aLock) error {
	select {
	case l.ch <- struct{}{}:
		return nil
	default:
	}
	var err error
	e.block(func() {
		select {
		case l.ch <- struct{}{}:
		case <-ctx.Done():
			err = ctxErr(ctx)
		}
	})
	return err
}

func (l *aLock) release() { <-l.ch }

// event is asyncio.Event (mutated only under Engine.mu).
type event struct {
	ch  chan struct{}
	set bool
}

func newEvent() *event { return &event{ch: make(chan struct{})} }

func (ev *event) Set() {
	if !ev.set {
		ev.set = true
		close(ev.ch)
	}
}

func (ev *event) Clear() {
	if ev.set {
		ev.set = false
		ev.ch = make(chan struct{})
	}
}

func (ev *event) IsSet() bool { return ev.set }

// wait returns the channel to wait on for the current generation.
func (ev *event) wait() <-chan struct{} { return ev.ch }

// task is an asyncio task created with Engine.spawn: it runs in a fresh
// context (no operation budget) and records its error.
type task struct {
	done   chan struct{}
	err    error
	cancel context.CancelFunc
}

func (t *task) finished() bool {
	select {
	case <-t.done:
		return true
	default:
		return false
	}
}

func (e *Engine) spawn(fn func(ctx context.Context) error) *task {
	ctx, cancel := context.WithCancel(e.bg)
	t := &task{done: make(chan struct{}), cancel: cancel}
	if e.closing {
		cancel()
		t.err = errCancelled
		close(t.done)
		return t
	}
	e.tasks[t] = struct{}{}
	e.wg.Add(1)
	go func() {
		defer e.wg.Done()
		e.mu.Lock()
		defer func() {
			if r := recover(); r != nil {
				t.err = errInternal
			}
			delete(e.tasks, t)
			cancel()
			close(t.done)
			e.mu.Unlock()
		}()
		t.err = fn(ctx)
	}()
	return t
}

// await is `await asyncio.shield(task)`: waiting stops at ctx, the task
// continues; a finished task re-raises its stored error.
func (e *Engine) await(ctx context.Context, t *task) error {
	if !t.finished() {
		var err error
		e.block(func() {
			select {
			case <-t.done:
			case <-ctx.Done():
				err = ctxErr(ctx)
			}
		})
		if err != nil {
			return err
		}
	}
	return t.err
}

// cancelAndWait cancels tasks and waits for them (gather(return_exceptions)).
func (e *Engine) cancelAndWait(tasks []*task) {
	for _, t := range tasks {
		t.cancel()
	}
	e.block(func() {
		for _, t := range tasks {
			<-t.done
		}
	})
}
