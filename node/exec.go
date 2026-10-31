//go:build unix

package node

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"os/exec"
	"strings"
	"sync"
	"syscall"
	"time"
)

// Exec runs one shell command in its own process group so a timeout or a
// lost controller connection also ends everything it started. Shell "sh"
// asks for plain /bin/sh, for the controller's own machine-read commands.
func Exec(ctx context.Context, raw json.RawMessage) (ExecResult, error) {
	var p ExecParams
	if err := json.Unmarshal(raw, &p); err != nil || strings.TrimSpace(p.Command) == "" {
		return ExecResult{}, errors.New("invalid_params")
	}
	timeout := p.Timeout
	if timeout <= 0 {
		timeout = 120
	}
	timeout = min(timeout, 3600)
	ctx, cancel := context.WithTimeout(ctx, time.Duration(timeout)*time.Second)
	defer cancel()
	shell := "/bin/sh"
	if p.Shell != "sh" {
		shell = execShell()
	}
	cmd := shellCommand(shell, p.Command)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Dir = p.Cwd
	cmd.Stdin = strings.NewReader(p.Stdin)
	// The stdin copy is the only I/O Wait itself waits for.
	cmd.WaitDelay = execGrace
	stdout, err := newOutputPipe()
	if err != nil {
		return ExecResult{}, errors.New("start_failed")
	}
	stderr, err := newOutputPipe()
	if err != nil {
		stdout.abandon()
		return ExecResult{}, errors.New("start_failed")
	}
	cmd.Stdout, cmd.Stderr = stdout.w, stderr.w
	start := time.Now()
	err = cmd.Start()
	stdout.started()
	stderr.started()
	if err != nil {
		return ExecResult{}, errors.New("start_failed")
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	timedOut := false
	select {
	case err = <-done:
	case <-ctx.Done():
		syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		err = <-done
		timedOut = errors.Is(ctx.Err(), context.DeadlineExceeded)
	}
	grace := make(chan struct{})
	defer time.AfterFunc(execGrace, func() { close(grace) }).Stop()
	out, outCut := stdout.finish(grace)
	errText, errCut := stderr.finish(grace)
	result := ExecResult{Stdout: out, Stderr: errText, Truncated: outCut || errCut,
		TimedOut: timedOut, DurationMs: time.Since(start).Milliseconds(), Shell: shell}
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		result.ExitCode = exitErr.ExitCode()
	} else if err != nil && !errors.Is(err, exec.ErrWaitDelay) {
		result.ExitCode = -1
	}
	return result, nil
}

// outputPipe collects one output stream through a pipe of its own. A
// command is done when its shell is: what it left running in the
// background may hold the pipe for as long as it lives, so finish stops
// collecting after the grace, while the pipe is still read and discarded
// until they close it, rather than closed under them (a write to a closed
// pipe would kill them with SIGPIPE).
type outputPipe struct {
	r, w *os.File
	mu   sync.Mutex
	out  capped
	done chan struct{}
	shut bool
}

func newOutputPipe() (*outputPipe, error) {
	r, w, err := os.Pipe()
	if err != nil {
		return nil, err
	}
	o := &outputPipe{r: r, w: w, done: make(chan struct{})}
	go func() {
		defer close(o.done)
		defer r.Close()
		io.Copy(o, r)
	}()
	return o, nil
}

func (o *outputPipe) Write(p []byte) (int, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	if !o.shut {
		o.out.Write(p)
	}
	return len(p), nil
}

// started drops this process's copy of the write end, which the command
// now has, so the pipe ends when the last process holding it does.
func (o *outputPipe) started() { o.w.Close() }

func (o *outputPipe) abandon() { o.w.Close() }

// finish waits for the stream to end or for grace, whichever is first.
func (o *outputPipe) finish(grace <-chan struct{}) (string, bool) {
	select {
	case <-o.done:
	case <-grace:
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	o.shut = true
	return o.out.String(), o.out.truncated()
}
