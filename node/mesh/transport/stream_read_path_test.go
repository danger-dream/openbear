package transport

import (
	"bytes"
	"errors"
	"io"
	"net"
	"testing"
	"time"

	"openbear/node/mesh/wire"
)

// This wraps the connection, not readStream: deadlines still use their real
// idle and five-second durations. It records every request from readStream.
type readPathConn struct {
	net.Conn
	deadlines chan time.Time
	failAt    int
	calls     int
	failure   error
}

func (c *readPathConn) SetReadDeadline(d time.Time) error {
	c.calls++
	c.deadlines <- d
	if c.failAt == c.calls {
		return c.failure
	}
	return c.Conn.SetReadDeadline(d)
}

func runReadPath(t *testing.T, transport string, version int, failAt int) (*bindRun, *readPathConn, net.Conn, <-chan error) {
	t.Helper()
	b := newBind(t, Options{MTU: 1420})
	openBind(t, b)
	local, remote := net.Pipe()
	c := &readPathConn{Conn: local, deadlines: make(chan time.Time, 32), failAt: failAt, failure: errors.New("injected deadline failure")}
	ep := &endpoint{b: b, transport: transport}
	s := &session{conn: c, ep: ep, version: version, done: make(chan struct{})}
	done := make(chan error, 1)
	go func() { done <- b.readStream(b.run, s); close(s.done) }()
	t.Cleanup(func() { remote.Close(); local.Close() })
	return b.run, c, remote, done
}

func pathResult(t *testing.T, done <-chan error, within time.Duration) error {
	t.Helper()
	select {
	case err := <-done:
		return err
	case <-time.After(within):
		t.Fatal("readStream did not finish")
		return nil
	}
}

// Both complete frames are read in the first bufio fill, including an
// unknown plane. The next incomplete frame must reinstall a real deadline.
func TestReadStreamBufferedThenPartialTimeout(t *testing.T) {
	for _, partialBody := range []bool{false, true} {
		name := "partial-header"
		if partialBody {
			name = "partial-body"
		}
		t.Run(name, func(t *testing.T) {
			fastLiveness(t, time.Hour, 250*time.Millisecond)
			r, c, remote, done := runReadPath(t, "tcp", wire.LargeFrames, 0)
			input := []byte{0, 2, 0, 42, 0, 2, 99, 17}
			if partialBody {
				input = append(input, 0, 4, 0, 88)
			} else {
				input = append(input, 0)
			}
			go func() { _, _ = remote.Write(input) }()
			err := pathResult(t, done, 7*time.Second)
			var ne net.Error
			if !errors.As(err, &ne) || !ne.Timeout() {
				t.Fatalf("want timeout, got %v", err)
			}
			select {
			case p := <-r.rx:
				if !bytes.Equal(p.data, []byte{42}) {
					t.Fatalf("first frame %v", p.data)
				}
			default:
				t.Fatal("buffered first frame not delivered")
			}
			close(c.deadlines)
			var ds []time.Time
			for d := range c.deadlines {
				ds = append(ds, d)
			}
			if len(ds) < 5 || !ds[1].IsZero() || !ds[2].IsZero() || !ds[3].IsZero() || ds[len(ds)-1].IsZero() {
				t.Fatalf("buffered/partial deadline sequence: %v", ds)
			}
		})
	}
}

func TestReadStreamBufferedDeadlineErrorAndClose(t *testing.T) {
	t.Run("deadline-error", func(t *testing.T) {
		r, c, remote, done := runReadPath(t, "tcp", 0, 2)
		go func() { _, _ = remote.Write([]byte{0, 2, 0, 42}) }()
		if err := pathResult(t, done, time.Second); !errors.Is(err, c.failure) {
			t.Fatalf("error lost: %v", err)
		}
		select {
		case <-r.rx:
			t.Fatal("delivered after deadline failure")
		default:
		}
		<-c.deadlines
		if d := <-c.deadlines; !d.IsZero() {
			t.Fatalf("body was not buffered: %v", d)
		}
	})
	t.Run("close-while-partial", func(t *testing.T) {
		_, _, remote, done := runReadPath(t, "tcp", 0, 0)
		go func() { _, _ = remote.Write([]byte{0, 2, 0, 42, 0}); remote.Close() }()
		if err := pathResult(t, done, time.Second); !errors.Is(err, io.ErrUnexpectedEOF) && !errors.Is(err, io.ErrClosedPipe) {
			t.Fatalf("close error: %v", err)
		}
	})
}

// TLS/non-TCP paths are not wrapped in a bufio.Reader by streamReader.
func TestReadStreamNonTCPFallback(t *testing.T) {
	r, c, remote, done := runReadPath(t, "tls", 0, 0)
	go func() { _, _ = remote.Write([]byte{0, 2, 0, 42}); remote.Close() }()
	if err := pathResult(t, done, time.Second); !errors.Is(err, io.EOF) && !errors.Is(err, io.ErrClosedPipe) {
		t.Fatalf("fallback close: %v", err)
	}
	select {
	case p := <-r.rx:
		if !bytes.Equal(p.data, []byte{42}) {
			t.Fatalf("frame: %v", p.data)
		}
	default:
		t.Fatal("missing frame")
	}
	close(c.deadlines)
	count := 0
	for d := range c.deadlines {
		count++
		if d.IsZero() {
			t.Fatal("non-TCP fallback cleared deadline")
		}
	}
	if count < 3 {
		t.Fatalf("only %d deadline calls", count)
	}
}
