package transport

import (
	"bufio"
	"bytes"
	"errors"
	"io"
	"net"
	"testing"
	"time"
)

type deadlineRecordingConn struct {
	net.Conn
	deadline time.Time
	err      error
}

func (c *deadlineRecordingConn) SetReadDeadline(t time.Time) error {
	c.deadline = t
	if c.err != nil {
		return c.err
	}
	return c.Conn.SetReadDeadline(t)
}

func TestStreamReadDeadlineBufferedAndFallback(t *testing.T) {
	local, remote := net.Pipe()
	defer local.Close()
	defer remote.Close()
	c := &deadlineRecordingConn{Conn: local}
	// A buffered frame including an unknown plane: both header and body
	// have no possibility of entering the socket.
	r := bufio.NewReader(bytes.NewReader([]byte{0, 4, 99, 1, 2, 3}))
	if _, err := r.Peek(6); err != nil {
		t.Fatal(err)
	}
	if err := streamReadDeadline(c, r, 3, time.Second); err != nil || !c.deadline.IsZero() {
		t.Fatalf("buffered header: %v %v", c.deadline, err)
	}
	n, plane, err := readFrameHeader(r, 1400)
	if err != nil || n != 3 || plane != 99 {
		t.Fatalf("header: %d %d %v", n, plane, err)
	}
	if err := streamReadDeadline(c, r, n, time.Second); err != nil || !c.deadline.IsZero() {
		t.Fatalf("buffered body: %v %v", c.deadline, err)
	}
	if _, err := io.CopyN(io.Discard, r, int64(n)); err != nil {
		t.Fatal(err)
	}
	// A partial header and a non-buffered reader must install real deadlines.
	for _, reader := range []io.Reader{r, bytes.NewReader(nil)} {
		before := time.Now()
		if err := streamReadDeadline(c, reader, 3, time.Second); err != nil || c.deadline.Before(before.Add(time.Second)) {
			t.Fatalf("fallback deadline: %v %v", c.deadline, err)
		}
	}
	// Clearing a buffered deadline must still return the connection's error.
	failure := errors.New("closed")
	c.err = failure
	full := bufio.NewReader(bytes.NewReader([]byte{1, 2, 3}))
	if _, err := full.Peek(3); err != nil {
		t.Fatal(err)
	}
	if err := streamReadDeadline(c, full, 3, time.Second); !errors.Is(err, failure) {
		t.Fatalf("deadline error: %v", err)
	}
}

func TestStreamReadDeadlinePartialFrameTimesOut(t *testing.T) {
	for _, header := range []bool{true, false} {
		t.Run(map[bool]string{true: "header", false: "body"}[header], func(t *testing.T) {
			local, remote := net.Pipe()
			defer local.Close()
			defer remote.Close()
			reader := bufio.NewReader(local)
			go func() {
				if header {
					_, _ = remote.Write([]byte{0})
				} else {
					_, _ = remote.Write([]byte{0, 4, 0, 42})
				}
			}()
			if err := streamReadDeadline(local, reader, 3, 60*time.Millisecond); err != nil {
				t.Fatal(err)
			}
			n, _, err := readFrameHeader(reader, 1400)
			if !header && err == nil {
				if err = streamReadDeadline(local, reader, n, 60*time.Millisecond); err == nil {
					_, err = io.ReadFull(reader, make([]byte, n))
				}
			}
			var ne net.Error
			if !errors.As(err, &ne) || !ne.Timeout() {
				t.Fatalf("partial frame did not time out: %v", err)
			}
		})
	}
}
