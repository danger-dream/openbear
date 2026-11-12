package transport

import (
	"context"
	"crypto/rand"
	"crypto/tls"
	"errors"
	"net"
	"net/netip"
	"syscall"
	"time"
)

// Probes are what a server about to join measures to a relay (mesh.probe):
// a reflector request on UDP, a connect on TCP, a pinned handshake on TLS.
// None goes past that into the mesh protocol, so they need no keys.

// Probe states.
const (
	ProbeOK      = "ok"
	ProbeTimeout = "timeout"
	ProbeRefused = "refused"
)

// ProbeUDP sends count reflector requests to a relay's UDP port, one at a
// time, and returns the round trips of those answered and the state: ok
// when any was, refused when the relay's host said the port is closed.
func ProbeUDP(ctx context.Context, addr netip.AddrPort, count int, timeout time.Duration) ([]time.Duration, string) {
	c, err := net.DialUDP("udp", nil, net.UDPAddrFromAddrPort(addr))
	if err != nil {
		return nil, probeState(err)
	}
	defer c.Close()
	var rtts []time.Duration
	state := ProbeTimeout
	buf := make([]byte, 64)
	for i := 0; i < count && ctx.Err() == nil; i++ {
		var id [8]byte
		rand.Read(id[:])
		req := make([]byte, reflectSize)
		copy(req, reflectMagic)
		req[4] = reflectRequest
		copy(req[5:13], id[:])
		start := time.Now()
		if _, err := c.Write(req); err != nil {
			if probeState(err) == ProbeRefused && state != ProbeOK {
				state = ProbeRefused
			}
			continue
		}
		deadline := start.Add(timeout)
		if d, ok := ctx.Deadline(); ok && d.Before(deadline) {
			deadline = d
		}
		c.SetReadDeadline(deadline)
		for {
			n, err := c.Read(buf)
			if err != nil {
				if probeState(err) == ProbeRefused && state != ProbeOK {
					state = ProbeRefused
				}
				break
			}
			if n == reflectSize && isReflect(buf[:n]) && buf[4] == reflectReply && string(buf[5:13]) == string(id[:]) {
				rtts = append(rtts, time.Since(start))
				state = ProbeOK
				break
			}
		}
	}
	return rtts, state
}

// ProbeTCP connects to a relay's TCP port and closes at once.
func ProbeTCP(ctx context.Context, addr string, timeout time.Duration) (time.Duration, string) {
	d := net.Dialer{Timeout: timeout}
	start := time.Now()
	c, err := d.DialContext(ctx, "tcp", addr)
	if err != nil {
		return 0, probeState(err)
	}
	took := time.Since(start)
	c.Close()
	return took, ProbeOK
}

// ProbeTLS completes a TLS handshake with a relay's TLS port, its
// certificate checked against pins as a link's would be: a certificate
// that does not match is refused.
func ProbeTLS(ctx context.Context, addr string, pins []string, timeout time.Duration) string {
	d := tls.Dialer{NetDialer: &net.Dialer{Timeout: timeout}, Config: &tls.Config{MinVersion: tls.VersionTLS13, InsecureSkipVerify: true,
		SessionTicketsDisabled: true, VerifyConnection: func(cs tls.ConnectionState) error { return verifyPins(cs, pinSet{values: pins}) }}}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	c, err := d.DialContext(ctx, "tcp", addr)
	if err != nil {
		if errors.Is(err, ErrSecurity) {
			return ProbeRefused
		}
		return probeState(err)
	}
	c.Close()
	return ProbeOK
}

// Windows says a closed port with its own codes: WSAECONNREFUSED for TCP,
// WSAECONNRESET on a UDP socket an ICMP port unreachable reached.
const (
	wsaConnRefused syscall.Errno = 10061
	wsaConnReset   syscall.Errno = 10054
)

func probeState(err error) string {
	var errno syscall.Errno
	if errors.As(err, &errno) && (errno == syscall.ECONNREFUSED || errno == wsaConnRefused || errno == wsaConnReset) {
		return ProbeRefused
	}
	return ProbeTimeout
}
