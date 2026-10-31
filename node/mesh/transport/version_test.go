package transport

import (
	"bytes"
	"errors"
	"fmt"
	"net"
	"strings"
	"testing"
	"time"

	"openbear/node/mesh/wire"
)

// Two ranges stand in for two releases: next speaks 1..2 as the release
// after this one will, this one 1..1.
var (
	this = wire.Range{Min: 1, Max: 1}
	next = wire.Range{Min: 1, Max: 2}
)

// WireGuard runs over every transport between a node of the next release
// and one of this release, either one listening, and between two of the
// next; a stream's frames are in the newest protocol both preambles allow.
func TestTransportsAcrossReleases(t *testing.T) {
	for _, transport := range []string{"udp", "tcp", "tls"} {
		for _, c := range []struct {
			name           string
			server, client wire.Range
			version        int
		}{{"next listens", next, this, 1}, {"this listens", this, next, 1}, {"both next", next, next, 2}} {
			t.Run(transport+"/"+c.name, func(t *testing.T) {
				port := freePort(t, "tcp")
				o := Options{MTU: 1280, Protocol: c.server}
				switch transport {
				case "udp":
					port = freePort(t, "udp")
					o.ListenUDP = port
				case "tcp":
					o.ListenTCP = port
				case "tls":
					o.ListenTLS = port
					o.TLSDir = t.TempDir()
				}
				server := newBind(t, o)
				client := newBind(t, Options{MTU: 1280, Protocol: c.client})
				endpoint := fmt.Sprintf("%s://127.0.0.1:%d", transport, port)
				if transport == "tls" {
					cert, err := EnsureCertificate(o.TLSDir)
					if err != nil {
						t.Fatal(err)
					}
					client.SetPins(endpoint, []string{cert.Active.FingerprintSHA256})
				}
				a := wg(t, client, "10.99.0.1")
				z := wg(t, server, "10.99.0.2")
				addPeer(t, z, a, "")
				addPeer(t, a, z, endpoint)
				if err := z.dev.Up(); err != nil {
					t.Fatal(err)
				}
				if err := a.dev.Up(); err != nil {
					t.Fatal(err)
				}
				transfer(t, a, z, "request")
				transfer(t, z, a, "reply")
				if transport == "udp" {
					// Neither knows the other's range: both send their
					// oldest, which is 1.
					for _, b := range []*Bind{client, server} {
						b.mu.Lock()
						for _, ep := range b.paths {
							if ep.rxVersion != 1 {
								t.Errorf("datagrams in protocol %d, want 1", ep.rxVersion)
							}
						}
						b.mu.Unlock()
					}
					return
				}
				for _, b := range []*Bind{client, server} {
					b.mu.Lock()
					for s := range b.run.sessions {
						if s.version != c.version {
							t.Errorf("stream frames in protocol %d, want %d", s.version, c.version)
						}
					}
					b.mu.Unlock()
				}
			})
		}
	}
}

// UDP to a peer goes out in the newest protocol both speak once the peer's
// range is known (the health layer learns it from control messages), in the
// sender's oldest before; and back to it if the peer turns out older again.
func TestDatagramsFollowThePeersRange(t *testing.T) {
	l := newPathLabSpeaking(t, next, next)
	if got := l.a.PeerProtocol("z"); got != 1 {
		t.Fatalf("unknown peer: protocol %d, want 1", got)
	}
	l.a.SetPeerProtocol("z", next)
	l.z.SetPeerProtocol("a", next)
	a, z := l.prepare(t, l.udp)
	l.commit(t, a, z)
	heard := func(b *Bind) int {
		b.mu.Lock()
		defer b.mu.Unlock()
		for _, ep := range b.paths {
			if ep.transport == "udp" && ep.peer != "" {
				return ep.rxVersion
			}
		}
		return 0
	}
	if heard(l.a) != 2 || heard(l.z) != 2 {
		t.Fatalf("both next: datagrams heard in %d and %d, want 2", heard(l.a), heard(l.z))
	}
	// z restarts as this release: a learns it and goes back to 1.
	l.z.mu.Lock()
	l.z.opt.Protocol = this
	l.z.mu.Unlock()
	l.a.SetPeerProtocol("z", this)
	l.z.SetPeerProtocol("a", next)
	transfer(t, l.ad, l.zd, "to the older z")
	transfer(t, l.zd, l.ad, "from the older z")
	if heard(l.z) != 1 || l.a.PeerProtocol("z") != 1 {
		t.Fatalf("to an older peer: heard %d, sending %d; want 1", heard(l.z), l.a.PeerProtocol("z"))
	}
	// z's range forgotten after a path last heard in 2: the path's version
	// goes too, so a silent z that restarted older gets 1.
	l.z.SetPeerProtocol("a", next)
	transfer(t, l.zd, l.ad, "from z in 2 again")
	l.z.mu.Lock()
	l.z.opt.Protocol = next
	l.z.mu.Unlock()
	transfer(t, l.zd, l.ad, "from a z of the next release")
	l.a.mu.Lock()
	var path *endpoint
	for _, ep := range l.a.paths {
		if ep.transport == "udp" && ep.peer == "z" {
			path = ep
		}
	}
	l.a.mu.Unlock()
	if path == nil || heard(l.a) != 2 {
		t.Fatalf("a heard z in %d", heard(l.a))
	}
	l.a.SetPeerProtocol("z", wire.Range{})
	l.a.mu.Lock()
	got := l.a.datagramVersion("z", path.rxVersion)
	l.a.mu.Unlock()
	if got != 1 {
		t.Fatalf("after forgetting z: datagrams in %d, want 1", got)
	}
}

// What a node of a later protocol may send before it knows this node's
// range, a datagram of a newer version or of an unknown plane, is dropped;
// the socket and the paths on it go on.
func TestUnknownDatagramsAreDropped(t *testing.T) {
	l := newPathLabSpeaking(t, this, this)
	a, z := l.prepare(t, l.udp)
	l.commit(t, a, z)
	c, err := net.Dial("udp", strings.TrimPrefix(l.udp, "udp://"))
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	for _, d := range [][]byte{append([]byte("OBM3\x00"), make([]byte, 64)...), append([]byte("OBM2\x07"), make([]byte, 64)...)} {
		if _, err := c.Write(d); err != nil {
			t.Fatal(err)
		}
	}
	transfer(t, l.ad, l.zd, "after unknown datagrams")
	transfer(t, l.zd, l.ad, "and back")
}

// A stream peer of a later protocol announces it in its preamble ("OBM3")
// and sends a frame of a plane this node does not know: the stream stays,
// and this node answers in its own preamble. A peer whose newest protocol
// is below this node's oldest is refused with that reason.
func TestStreamsFromALaterProtocol(t *testing.T) {
	server := newBind(t, Options{ListenTCP: freePort(t, "tcp"), Protocol: this})
	openBind(t, server)
	address := net.JoinHostPort("127.0.0.1", portString(server.opt.ListenTCP))
	c, err := net.Dial("tcp", address)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	c.SetDeadline(time.Now().Add(2 * time.Second))
	c.Write([]byte("OBM3"))
	var got [4]byte
	if _, err := c.Read(got[:]); err != nil || string(got[:]) != "OBM2" {
		t.Fatalf("preamble %q %v", got, err)
	}
	if err := writePlaneFrame(c, bytes.Repeat([]byte{1}, 40), 1<<16, 0); err != nil {
		t.Fatal(err)
	}
	unknown := []byte{0, 41, 6}
	unknown = append(unknown, bytes.Repeat([]byte{2}, 40)...)
	c.Write(unknown)
	c.SetReadDeadline(time.Now().Add(500 * time.Millisecond))
	var one [1]byte
	var ne net.Error
	if _, err := c.Read(one[:]); !errors.As(err, &ne) || !ne.Timeout() {
		t.Fatalf("the stream ended after an unknown plane: %v", err)
	}
	server.mu.Lock()
	n := len(server.run.sessions)
	var version int
	for s := range server.run.sessions {
		version = s.version
	}
	server.mu.Unlock()
	if n != 1 || version != 1 {
		t.Fatalf("sessions %d, version %d", n, version)
	}

	newer := newBind(t, Options{ListenTCP: freePort(t, "tcp"), Protocol: wire.Range{Min: 2, Max: 3}})
	openBind(t, newer)
	c2, err := net.Dial("tcp", net.JoinHostPort("127.0.0.1", portString(newer.opt.ListenTCP)))
	if err != nil {
		t.Fatal(err)
	}
	defer c2.Close()
	c2.SetDeadline(time.Now().Add(2 * time.Second))
	if _, err := exchangeVersions(c2, this); err != nil {
		t.Fatalf("this end refused: %v", err)
	}
	if _, err := c2.Read(one[:]); err == nil {
		t.Fatal("a 2..3 node kept a stream from a node speaking only 1")
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	go func() {
		if sv, err := ln.Accept(); err == nil {
			exchangeVersions(sv, this)
			sv.Close()
		}
	}()
	cl, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer cl.Close()
	if _, err := exchangeVersions(cl, wire.Range{Min: 2, Max: 3}); err == nil || !strings.Contains(err.Error(), "speaks mesh protocol up to 1, this node 2..3") {
		t.Fatalf("got %v", err)
	}
}

func TestPreambleVersions(t *testing.T) {
	for v, want := range map[int]string{1: "OBM2", 2: "OBM3", 8: "OBM9"} {
		if p := preambleFor(v); string(p[:]) != want || preambleVersion(p[:]) != v {
			t.Errorf("version %d: %q", v, p)
		}
	}
	for _, bad := range []string{"OBM1", "OBM0", "OBMx", "OBR1", "OB"} {
		if preambleVersion([]byte(bad)) != 0 {
			t.Errorf("%q taken as a preamble", bad)
		}
	}
	if _, plane, v, err := unwrapDatagram([]byte("OBM3\x05abc"), 2); err != nil || plane != 5 || v != 2 {
		t.Fatalf("unknown plane and version not handed on: %d %d %v", plane, v, err)
	}
}
