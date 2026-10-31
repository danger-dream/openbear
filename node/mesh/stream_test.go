package mesh

import (
	"fmt"
	"io"
	"net"
	"net/netip"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"openbear/node/mesh/transport"
)

func freeTCP(t *testing.T) int {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	return l.Addr().(*net.TCPAddr).Port
}

// Relays linked over TCP, one member entering over TLS with a pin, the
// other over TCP: traffic crosses both relays on streams only.
func TestStreamTransports(t *testing.T) {
	s := spec{relays: []string{"R1", "R2"}, members: map[string][]string{"A": {"R1"}, "B": {"R2"}},
		rules: map[string][]Rule{"A": allowICMP, "B": allowICMP}}
	l := newLab(t, s)
	tcp := map[string]int{"R1": freeTCP(t), "R2": freeTCP(t)}
	tlsPort := freeTCP(t)
	l.dirs["R1"] = t.TempDir()
	certs, err := transport.EnsureCertificate(filepath.Join(l.dirs["R1"], "tls"))
	if err != nil {
		t.Fatal(err)
	}
	pin := certs.Active.FingerprintSHA256
	l.edit = func(name string, c *Config) {
		switch name {
		case "R1":
			c.Self.Listen.TCP, c.Self.Listen.TLS = tcp["R1"], tlsPort
			c.Links[0].Endpoint = fmt.Sprintf("tcp://127.0.0.1:%d", tcp["R2"])
		case "R2":
			c.Self.Listen.TCP = tcp["R2"]
		case "A":
			c.Entry.Candidates[0].Endpoint = fmt.Sprintf("tls://127.0.0.1:%d", tlsPort)
			c.Entry.Candidates[0].TLSPins = []string{pin}
		case "B":
			c.Entry.Candidates[0].Endpoint = fmt.Sprintf("tcp://127.0.0.1:%d", tcp["R2"])
		}
	}
	n := l.up()
	A, B := l.addr("A"), l.addr("B")
	send(n["A"], packet(A, B, protoICMP, 64, 1, 0, false, icmp(8, 1, nil)))
	expect(t, n["B"], "A to B over tls, tcp, tcp")
	if n["R1"].tun.stats.Forwarded.Load() == 0 || n["R2"].tun.stats.Forwarded.Load() == 0 {
		t.Fatal("not through both relays")
	}
	send(n["B"], packet(B, A, protoICMP, 64, 2, 0, false, icmp(0, 1, nil)))
	expect(t, n["A"], "reply")

	r1 := n["R1"].status()
	if r1.TLSCertificate != certs.Active.ID {
		t.Fatalf("R1 reports certificate %q, want %q", r1.TLSCertificate, certs.Active.ID)
	}
	for _, p := range r1.Peers {
		want := map[string]string{"R2": "tcp", "A": "tls"}[p.ID]
		if p.Transport != want || p.LocalAddr == "" {
			t.Fatalf("R1 peer %+v", p)
		}
	}
	if e := n["A"].status().Entry; e == nil || e.Transport != "tls" {
		t.Fatalf("A entry %+v", e)
	}
	// A config rewritten unchanged reloads the certificate in place.
	if err := n["R1"].reloadCertificate(); err != nil {
		t.Fatal(err)
	}

	// Dropping the link releases its paths in the transport.
	c := l.config("R1")
	c.Revision = 2
	c.Links, c.NextHops = nil, map[string]string{}
	if err := n["R1"].applyConfig(c); err != nil {
		t.Fatal(err)
	}
	for _, p := range n["R1"].bind.(*streamBind).Stats() {
		if strings.HasSuffix(p.Endpoint, fmt.Sprint(tcp["R2"])) {
			t.Fatalf("R1 still holds a path to R2: %+v", p)
		}
	}
	if got := allowedOf(n["R1"], "A"); len(got) != 1 || got[0] != netip.PrefixFrom(A, 32) {
		t.Fatalf("R1's A carries %v after the link change", got)
	}
}

// proxy forwards TCP to a relay so a test can cut the member's connection.
type proxy struct {
	ln       net.Listener
	mu       sync.Mutex
	conns    []net.Conn
	accepted int
}

func newProxy(t *testing.T, target string) *proxy {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	p := &proxy{ln: ln}
	t.Cleanup(func() { ln.Close(); p.cut() })
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			up, err := net.Dial("tcp", target)
			if err != nil {
				c.Close()
				continue
			}
			p.mu.Lock()
			p.accepted++
			p.conns = append(p.conns, c, up)
			p.mu.Unlock()
			go func() { io.Copy(up, c); up.Close(); c.Close() }()
			go func() { io.Copy(c, up); up.Close(); c.Close() }()
		}
	}()
	return p
}

func (p *proxy) port() int { return p.ln.Addr().(*net.TCPAddr).Port }

func (p *proxy) cut() {
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, c := range p.conns {
		c.Close()
	}
	p.conns = nil
}

func (p *proxy) count() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.accepted
}

func keepaliveOf(n *running, peer string) int {
	_, st := deviceState(n.dev)
	for _, p := range n.peers {
		if p.ID == peer && st[p.Key] != nil {
			return st[p.Key].keepalive
		}
	}
	return -1
}

// A member's TCP entry is cut and comes back; the relay must not close
// the new connection as unauthenticated while the link sits idle.
func TestStreamSurvivesReconnect(t *testing.T) {
	s := spec{relays: []string{"R"}, members: map[string][]string{"A": {"R"}, "B": {"R"}},
		rules: map[string][]Rule{"A": allowICMP, "B": allowICMP}}
	l := newLab(t, s)
	relayTCP := freeTCP(t)
	px := newProxy(t, fmt.Sprintf("127.0.0.1:%d", relayTCP))
	l.edit = func(name string, c *Config) {
		switch name {
		case "R":
			c.Self.Listen.TCP = relayTCP
		case "A":
			c.Entry.Candidates[0].Endpoint = fmt.Sprintf("tcp://127.0.0.1:%d", px.port())
		}
	}
	n := l.up()
	A, B := l.addr("A"), l.addr("B")
	ping := func(id uint16, what string) {
		t.Helper()
		send(n["A"], packet(A, B, protoICMP, 64, id, 0, false, icmp(8, id, nil)))
		expect(t, n["B"], what)
		send(n["B"], packet(B, A, protoICMP, 64, id, 0, false, icmp(0, id, nil)))
		expect(t, n["A"], what+", back")
	}
	ping(1, "before the cut")
	want := map[string]int{"A/R": keepaliveStream, "B/R": keepaliveUDP, "R/A": keepaliveStream, "R/B": keepaliveStream}
	for k, v := range want {
		from, to, _ := strings.Cut(k, "/")
		if got := keepaliveOf(n[from], to); got != v {
			t.Errorf("%s keepalive %d, want %d", k, got, v)
		}
	}

	px.cut()
	deadline := time.Now().Add(5 * time.Second)
	for px.count() < 2 {
		if time.Now().After(deadline) {
			t.Fatal("A did not reconnect")
		}
		time.Sleep(50 * time.Millisecond)
	}
	// Idle past the 10 s the relay gives a new connection to carry
	// WireGuard's answer: only the handshake on reconnect and the keepalives
	// can confirm it.
	time.Sleep(12 * time.Second)
	if c := px.count(); c != 2 {
		t.Fatalf("A connected %d times: the relay closed the new connection", c)
	}
	ping(2, "12 s after the reconnect")
	if c := px.count(); c != 2 {
		t.Fatalf("A connected %d times", c)
	}
}
