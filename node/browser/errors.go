package browser

import (
	"context"
	"errors"
	"time"
)

// Error is a structured browser operation failure (Python BrowserError).
// Outcome is not_started, failed, unknown or completed; Details carries the
// operation phase and effectsPossible plus action-specific facts.
type Error struct {
	Code, Outcome string
	Details       map[string]any
}

func (e *Error) Error() string { return e.Code }

// operation is the per-request budget and effect state (Python Operation).
type operation struct {
	deadline time.Time
	started  bool
	phase    string
}

type opKey struct{}

func withOp(ctx context.Context, op *operation) context.Context {
	return context.WithValue(ctx, opKey{}, op)
}

func opOf(ctx context.Context) *operation {
	op, _ := ctx.Value(opKey{}).(*operation)
	return op
}

// browserError mirrors BrowserError(code, outcome, details=...): outcome
// and details are derived from the operation bound to ctx at creation.
func browserError(ctx context.Context, code string, outcome string, details map[string]any) *Error {
	op := opOf(ctx)
	if outcome == "" {
		switch {
		case op != nil && op.started:
			outcome = "unknown"
		case op != nil:
			outcome = "failed"
		default:
			outcome = "not_started"
		}
	}
	d := map[string]any{}
	if op != nil {
		d["phase"] = op.phase
		d["effectsPossible"] = op.started
	}
	for k, v := range details {
		d[k] = v
	}
	return &Error{Code: code, Outcome: outcome, Details: d}
}

func (op *operation) remaining(ctx context.Context) (time.Duration, error) {
	r := time.Until(op.deadline)
	if r <= 0 {
		return 0, browserError(ctx, "action_timeout", "", nil)
	}
	return r, nil
}

func setPhase(ctx context.Context, phase string) {
	if op := opOf(ctx); op != nil {
		op.phase = phase
	}
}

func markEffect(ctx context.Context) {
	if op := opOf(ctx); op != nil {
		op.started = true
	}
}

// protocolError is an internal CDP failure (Python ProtocolError); its text
// never crosses IPC.
type protocolError struct{ msg string }

func (p *protocolError) Error() string { return p.msg }

func protoErr(msg string) error { return &protocolError{msg} }

func isProtocol(err error) (*protocolError, bool) {
	var p *protocolError
	ok := errors.As(err, &p)
	return p, ok
}

type timeoutError struct{}

func (timeoutError) Error() string { return "timeout" }

// Is lets callers test errors.Is(err, context.DeadlineExceeded).
func (timeoutError) Is(target error) bool { return target == context.DeadlineExceeded }

var (
	// ErrDisconnected: the CDP connection is closed (Python ConnectionError("cdp_disconnected")).
	ErrDisconnected = errors.New("cdp_disconnected")
	// ErrTimeout: a CDP call or operation ran out of time (Python TimeoutError).
	ErrTimeout error = timeoutError{}

	errDialogOpened = errors.New("dialog_opened")
	errCancelled    = context.Canceled
	errInternal     = errors.New("internal_error")
)

// CommandError is a CDP error reply returned by Engine.Call/TargetCall,
// formatted like app/browser/cdp.py ("cdp_command_failed: <message[:200]>").
type CommandError struct{ Message string }

func (c *CommandError) Error() string { return "cdp_command_failed: " + pyPrefix(c.Message, 200) }

func isTimeout(err error) bool { return errors.Is(err, ErrTimeout) }

func ctxErr(ctx context.Context) error {
	if errors.Is(ctx.Err(), context.DeadlineExceeded) {
		return ErrTimeout
	}
	return errCancelled
}
