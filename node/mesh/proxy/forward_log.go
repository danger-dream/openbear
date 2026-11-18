package proxy

import (
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"syscall"
	"time"
)

const forwardLogBytes = 1 << 20

// Each node keeps its own bounded journal. An unavailable log never changes a forward's result.
type forwardLog struct {
	mu     sync.Mutex
	path   string
	limit  int64
	warned time.Time
}

type forwardFailure struct {
	Time   time.Time `json:"time"`
	Side   string    `json:"side"`
	Rule   string    `json:"rule,omitempty"`
	Match  string    `json:"match,omitempty"`
	Port   uint16    `json:"backendPort,omitempty"`
	Proto  string    `json:"proto,omitempty"`
	Stage  string    `json:"stage"`
	Reason string    `json:"reason"`
}

// The existing wire header has no rule ID. Both sides can identify the same policy tuple
// without writing addresses to disk; two entry rules for that tuple share the match key.
func forwardMatch(entry netip.Addr, fast bool, dst Addr) string {
	sum := sha256.Sum256([]byte(fmt.Sprintf("%s|%t|%s|%s|%d", entry.Unmap(), fast, dst.IP, dst.Host, dst.Port)))
	return fmt.Sprintf("%x", sum[:8])
}

func (j *forwardLog) record(f forwardFailure, stage, reason string) {
	if j == nil {
		return
	}
	f.Time, f.Stage, f.Reason = time.Now().UTC(), stage, reason
	line, err := json.Marshal(f)
	if err != nil {
		return
	}
	j.mu.Lock()
	defer j.mu.Unlock()
	if err := j.append(append(line, '\n')); err != nil && time.Since(j.warned) >= time.Minute {
		j.warned = time.Now()
		log.Printf("mesh: could not write forward failure log: %v", err)
	}
}

func (j *forwardLog) append(line []byte) error {
	st, err := os.Stat(j.path)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if err == nil && st.Size()+int64(len(line)) > j.limit {
		for n := 3; n > 0; n-- {
			old := j.path
			if n > 1 {
				old += "." + strconv.Itoa(n-1)
			}
			if err := os.Rename(old, j.path+"."+strconv.Itoa(n)); err != nil && !errors.Is(err, os.ErrNotExist) {
				return err
			}
		}
	}
	w, err := os.OpenFile(j.path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0600)
	if err != nil {
		return err
	}
	_, err = w.Write(line)
	return errors.Join(err, w.Close())
}

// SetFailureLog enables the node-local log before the first Apply.
func (s *Forwarder) SetFailureLog(dir string) {
	s.journal = &forwardLog{path: filepath.Join(dir, "forward-failures.jsonl"), limit: forwardLogBytes}
}

// Never serialize err.Error(): net.OpError includes the socket addresses.
func socketReason(err error) string {
	var ne net.Error
	var errno syscall.Errno
	switch {
	case errors.Is(err, io.EOF):
		return "eof"
	case errors.Is(err, io.ErrUnexpectedEOF):
		return "unexpected_eof"
	case errors.Is(err, syscall.ECONNRESET):
		return "connection_reset"
	case errors.Is(err, syscall.ECONNREFUSED):
		return "connection_refused"
	case errors.Is(err, syscall.EPIPE):
		return "broken_pipe"
	case errors.Is(err, net.ErrClosed):
		return "local_socket_closed"
	case errors.As(err, &ne) && ne.Timeout():
		return "timeout"
	case errors.As(err, &errno):
		return "errno_" + strconv.FormatUint(uint64(errno), 10)
	default:
		return "io_error"
	}
}

// Both copy directions can fail when one socket is closed. Keep the first local
// observation, not the errors induced by closing the rest of the stream.
type forwardAttempt struct {
	journal *forwardLog
	fields  forwardFailure
	once    sync.Once
}

func (a *forwardAttempt) fail(stage, reason string) {
	a.once.Do(func() { a.journal.record(a.fields, stage, reason) })
}

func (a *forwardAttempt) streamError(stage string, err error) {
	if err != nil && !errors.Is(err, io.EOF) && !errors.Is(err, net.ErrClosed) {
		a.fail(stage, socketReason(err))
	}
}

func (e *fwdEntry) attempt(proto string) *forwardAttempt {
	return &forwardAttempt{journal: e.journal, fields: forwardFailure{Side: "entry", Rule: e.cfg.ID, Proto: proto, Match: e.match, Port: e.to.Port}}
}

func (a *forwardAttempt) confirmed(t *net.TCPConn, stats *fwdStats) bool {
	var status [1]byte
	_, err := io.ReadFull(t, status[:])
	if err != nil {
		stats.fail(statusReason(statusFailure)) // retain the status API's failure count/reason
		a.fail("read_target_ack", socketReason(err))
		return false
	}
	if status[0] != statusOK {
		reason := statusReason(status[0])
		stats.fail(reason)
		a.fail("target_reply", reason+"_"+strconv.Itoa(int(status[0])))
		return false
	}
	return true
}

type headerReader struct {
	io.Reader
	n int
}

func (r *headerReader) Read(p []byte) (int, error) {
	n, err := r.Reader.Read(p)
	r.n += n
	return n, err
}
