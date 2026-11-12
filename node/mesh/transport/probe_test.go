package transport

import (
	"context"
	"net/netip"
	"strconv"
	"testing"
	"time"
)

// A relay answers a newcomer's probes on each port it listens on, without
// keys: a reflector reply on UDP, a connect on TCP, a pinned handshake on TLS.
func TestProbesReachEachTransportARelayListensOn(t *testing.T) {
	dir := t.TempDir()
	certs, err := EnsureCertificate(dir)
	if err != nil {
		t.Fatal(err)
	}
	udp, tcp, tlsPort := freePort(t, "udp"), freePort(t, "tcp"), freePort(t, "tcp")
	relay := newBind(t, Options{ListenUDP: udp, ListenTCP: tcp, ListenTLS: tlsPort, TLSDir: dir, Reflector: true})
	openBind(t, relay)
	ctx := context.Background()
	local := netip.MustParseAddr("127.0.0.1")

	rtts, state := ProbeUDP(ctx, netip.AddrPortFrom(local, uint16(udp)), 3, time.Second)
	if state != ProbeOK || len(rtts) != 3 {
		t.Fatalf("UDP: %s with %d answers", state, len(rtts))
	}
	if _, state := ProbeTCP(ctx, "127.0.0.1:"+strconv.Itoa(tcp), time.Second); state != ProbeOK {
		t.Fatalf("TCP: %s", state)
	}
	if state := ProbeTLS(ctx, "127.0.0.1:"+strconv.Itoa(tlsPort), []string{certs.Active.FingerprintSHA256}, time.Second); state != ProbeOK {
		t.Fatalf("TLS: %s", state)
	}
	// Another certificate than the one pinned is refused, as a link would refuse it.
	if state := ProbeTLS(ctx, "127.0.0.1:"+strconv.Itoa(tlsPort), []string{"ab" + certs.Active.FingerprintSHA256[2:]}, time.Second); state != ProbeRefused {
		t.Fatalf("TLS with the wrong pin: %s", state)
	}
}

func TestProbesTellAClosedPortFromOneThatDoesNotAnswer(t *testing.T) {
	ctx := context.Background()
	closed := freePort(t, "tcp")
	if _, state := ProbeTCP(ctx, "127.0.0.1:"+strconv.Itoa(closed), time.Second); state != ProbeRefused {
		t.Fatalf("closed TCP port: %s", state)
	}
	// Nothing on the UDP port: the host answers port unreachable.
	if rtts, state := ProbeUDP(ctx, netip.AddrPortFrom(netip.MustParseAddr("127.0.0.1"), uint16(freePort(t, "udp"))), 2, 300*time.Millisecond); state != ProbeRefused || len(rtts) != 0 {
		t.Fatalf("closed UDP port: %s with %d answers", state, len(rtts))
	}
	// A UDP socket that never answers is a timeout.
	quiet := newBind(t, Options{ListenUDP: freePort(t, "udp")})
	openBind(t, quiet)
	if _, state := ProbeUDP(ctx, netip.AddrPortFrom(netip.MustParseAddr("127.0.0.1"), uint16(quiet.UDPPort())), 1, 300*time.Millisecond); state != ProbeTimeout {
		t.Fatalf("UDP port of a node that is no reflector: %s", state)
	}
}
