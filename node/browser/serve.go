package browser

import (
	"bytes"
	"context"
	"encoding/binary"
	"io"
	"sync"
	"sync/atomic"
)

const (
	maxFrame       = 20 * 1024 * 1024
	maxConcurrency = 64
)

// Serve runs the worker protocol of app/browser/pyworker/main.py: frames
// are a 4-byte big-endian length plus JSON; requests {id, action, params};
// replies {id, result} or {id, error, outcome, details}. It returns when
// input ends, a frame is invalid or ctx is cancelled. The browser is never
// closed; only this engine's sessions are detached.
func Serve(ctx context.Context, in io.Reader, out io.Writer) error {
	engine := New()
	ctx, stop := context.WithCancel(ctx)
	defer stop()
	var writeMu sync.Mutex
	var active atomic.Int64
	var wg sync.WaitGroup
	var startMu sync.Mutex
	stopped := false

	reply := func(value *Obj) {
		body := encodeReply(value)
		frame := make([]byte, 4+len(body))
		binary.BigEndian.PutUint32(frame, uint32(len(body)))
		copy(frame[4:], body)
		writeMu.Lock()
		defer writeMu.Unlock()
		if ctx.Err() == nil {
			if _, err := out.Write(frame); err != nil {
				stop()
			}
		}
	}

	dispatch := func(message *Obj) {
		defer wg.Done()
		defer active.Add(-1)
		id, _ := message.Get("id")
		var result any
		var err error
		func() {
			defer func() {
				if r := recover(); r != nil {
					err = &Error{Code: "browser_operation_failed", Outcome: "unknown"}
				}
			}()
			action, hasAction := message.Get("action")
			name, _ := action.(string)
			params, hasParams := message.Get("params")
			switch {
			case engine.initialized() == false && name != "initialize":
				err = &Error{Code: "worker_not_initialized", Outcome: "not_started", Details: map[string]any{}}
			case !hasAction || !hasParams:
				err = &Error{Code: "browser_operation_failed", Outcome: "unknown"}
			default:
				raw, derr := dumps(params)
				if derr != nil {
					err = &Error{Code: "browser_operation_failed", Outcome: "unknown"}
					return
				}
				result, err = engine.Execute(ctx, name, raw)
			}
		}()
		if ctx.Err() != nil {
			return // cancelled at shutdown: no reply, like a cancelled asyncio task
		}
		if err != nil {
			be, ok := err.(*Error)
			if !ok {
				be = &Error{Code: "browser_operation_failed", Outcome: "unknown"}
			}
			response := O("id", id, "error", be.Code, "outcome", be.Outcome)
			if be.Details != nil {
				response.Set("details", be.Details)
			}
			reply(response)
			return
		}
		reply(O("id", id, "result", result))
	}

	readDone := make(chan struct{})
	go func() {
		defer close(readDone)
		var header [4]byte
		for {
			if _, err := io.ReadFull(in, header[:]); err != nil {
				return
			}
			size := binary.BigEndian.Uint32(header[:])
			if size == 0 || size > maxFrame {
				return
			}
			body := make([]byte, size)
			if _, err := io.ReadFull(in, body); err != nil {
				return
			}
			parsed, err := parseJSON(body)
			if err != nil {
				return
			}
			message, ok := parsed.(*Obj)
			if !ok {
				return
			}
			if active.Load() >= maxConcurrency {
				id, _ := message.Get("id")
				reply(O("id", id, "error", "worker_busy", "outcome", "not_started"))
				continue
			}
			startMu.Lock()
			if stopped {
				startMu.Unlock()
				return
			}
			active.Add(1)
			wg.Add(1)
			startMu.Unlock()
			go dispatch(message)
		}
	}()

	select {
	case <-ctx.Done():
	case <-readDone:
	}
	stop()
	startMu.Lock()
	stopped = true
	startMu.Unlock()
	wg.Wait()
	engine.Close()
	return nil
}

// encodeReply serializes one reply like main.py: compact UTF-8 JSON, or a
// fixed worker_response_too_large error when the body exceeds 20MB.
func encodeReply(value *Obj) []byte {
	body, err := dumps(value)
	if err == nil && len(body) <= maxFrame {
		return body
	}
	id, _ := value.Get("id")
	code := "worker_response_too_large"
	if err != nil {
		code = "browser_operation_failed"
	}
	// json.dumps default formatting for this fixed error reply.
	var b bytes.Buffer
	b.WriteString(`{"id": `)
	b.WriteString(dumpsASCII(id))
	b.WriteString(`, "error": "` + code + `", "outcome": "unknown"}`)
	return b.Bytes()
}

func (e *Engine) initialized() bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.config.Len() > 0
}
