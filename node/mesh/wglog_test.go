package mesh

import (
	"net"
	"testing"
	"time"

	"openbear/node/mesh/transport"
)

func TestWGLogLimitsEachFormat(t *testing.T) {
	var got []string
	l := newWGLog(func(msg string) { got = append(got, msg) })
	for range 5 {
		l.errorf("%v - Failed to send data packets: %v", "peer(a)", "transport buffer limit reached")
	}
	l.errorf("%v - Failed to send handshake initiation: %v", "peer(a)", "closed")
	l.errorf("%v - no known endpoint for peer", "peer(b)")
	l.errorf("%v - Failed to send handshake response: %v", "peer(c)", transport.ErrNoPath)
	if len(got) != 2 {
		t.Fatalf("one line per format, none for peers with no endpoint or path: %q", got)
	}
	// A path that closed under the peer is a failure to see.
	l.errorf("%v - Failed to send handshake response: %v", "peer(c)", net.ErrClosed)
	if len(got) != 3 {
		t.Fatalf("a closed path not shown: %q", got)
	}
	for k := range l.last {
		l.last[k] = l.last[k].Add(-time.Minute)
	}
	l.errorf("%v - Failed to send data packets: %v", "peer(b)", "transport buffer limit reached")
	if want := "peer(b) - Failed to send data packets: transport buffer limit reached (4 similar not shown since the last one)"; got[3] != want {
		t.Fatalf("after a minute: %q, want %q", got[3], want)
	}
}
