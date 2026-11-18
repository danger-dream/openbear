package fast

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"io"
	"net"
	"net/netip"
	"slices"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func randKeyB64(t *testing.T) string {
	t.Helper()
	var b [32]byte
	if _, err := rand.Read(b[:]); err != nil {
		t.Fatal(err)
	}
	return base64.StdEncoding.EncodeToString(b[:])
}

func TestHeaderSealExpireReplayRotationAndCrossCipher(t *testing.T) {
	kOld := randKeyB64(t)
	kNew := randKeyB64(t)
	kWrong := randKeyB64(t)

	aesSealer, err := newSealerWith([]string{kOld}, true)
	if err != nil {
		t.Fatal(err)
	}
	chaSealer, err := newSealerWith([]string{kNew, kOld}, false)
	if err != nil {
		t.Fatal(err)
	}
	wrongSealer, err := newSealerWith([]string{kWrong}, true)
	if err != nil {
		t.Fatal(err)
	}

	now := time.Unix(1_795_000_000, 0)
	h := header{
		proto:      protoTCP,
		planned:    true,
		probe:      true,
		alt:        2,
		at:         now,
		dest:       netip.MustParseAddr("100.96.0.3"),
		port:       8388,
		origin:     netip.MustParseAddr("100.96.0.1"),
		source:     netip.MustParseAddr("10.10.10.50"),
		sourcePort: 54321,
		visited:    []netip.Addr{netip.MustParseAddr("100.96.0.1")},
		hops:       []netip.Addr{netip.MustParseAddr("100.96.0.2"), netip.MustParseAddr("100.96.0.3")},
	}

	// Sealed with old key + AES-GCM, opened with [new, old] + ChaCha20-Poly1305.
	wire1, err := aesSealer.seal(h)
	if err != nil || len(wire1) != sealedLen {
		t.Fatalf("seal: len=%d err=%v", len(wire1), err)
	}
	wire2, err := aesSealer.seal(h)
	if err != nil || bytes.Equal(wire1, wire2) {
		t.Fatal("two seals of identical header produced identical bytes")
	}

	// Wrong key must fail.
	if _, err := wrongSealer.open(wire1, now); !errors.Is(err, errSealed) {
		t.Fatalf("wrong key got %v, want errSealed", err)
	}

	// Expired (>30s) and future (>30s) must fail before consuming replay nonce.
	if _, err := chaSealer.open(wire1, now.Add(31*time.Second)); !errors.Is(err, errStale) {
		t.Fatalf("expired got %v, want errStale", err)
	}
	if _, err := chaSealer.open(wire1, now.Add(-31*time.Second)); !errors.Is(err, errStale) {
		t.Fatalf("future got %v, want errStale", err)
	}

	// Within window opens cleanly.
	got, err := chaSealer.open(wire1, now.Add(5*time.Second))
	if err != nil {
		t.Fatalf("open valid: %v", err)
	}
	if got.dest != h.dest || got.port != h.port || got.origin != h.origin || got.source != h.source || !got.planned || !got.probe || got.alt != 2 || len(got.hops) != 2 {
		t.Fatalf("decoded mismatch: %+v", got)
	}

	// Replay of the exact same header is rejected.
	if _, err := chaSealer.open(wire1, now.Add(6*time.Second)); !errors.Is(err, errReplay) {
		t.Fatalf("replay got %v, want errReplay", err)
	}

	// Sealed with ChaCha20-Poly1305 (new key) opens on AES-preferred [new, old] sealer.
	dualAES, err := newSealerWith([]string{kNew, kOld}, true)
	if err != nil {
		t.Fatal(err)
	}
	wireNew, err := chaSealer.seal(h)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := dualAES.open(wireNew, now); err != nil {
		t.Fatalf("dualAES open ChaCha-sealed: %v", err)
	}
}

func freeTCPPort(t *testing.T) int {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	p := ln.Addr().(*net.TCPAddr).Port
	_ = ln.Close()
	return p
}

var loopbackIP = netip.AddrFrom4([4]byte{127, 0, 0, 1})

// loopback takes mesh addresses, where a stream leaves and where a connection
// no path takes goes, to this machine's loopback: a test has no mesh.
func loopback(t *testing.T) {
	origTCP, origUDP := dialAddrTCP, dialAddrUDP
	dialAddrTCP = func(ctx context.Context, ap netip.AddrPort, rcvBuf int) (*net.TCPConn, error) {
		return origTCP(ctx, netip.AddrPortFrom(loopbackIP, ap.Port()), rcvBuf)
	}
	dialAddrUDP = func(ap netip.AddrPort) (net.Conn, error) {
		return origUDP(netip.AddrPortFrom(loopbackIP, ap.Port()))
	}
	t.Cleanup(func() { dialAddrTCP, dialAddrUDP = origTCP, origUDP })
}

func waitFor(t *testing.T, what string, within time.Duration, ok func() bool) {
	t.Helper()
	deadline := time.Now().Add(within)
	for !ok() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

// clientPair is a connection as capture hands it over: the client's end and
// the end the entry carries.
func clientPair(t *testing.T) (client, entry *net.TCPConn) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	accepted := make(chan net.Conn, 1)
	go func() {
		c, _ := ln.Accept()
		accepted <- c
	}()
	c, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	return c.(*net.TCPConn), (<-accepted).(*net.TCPConn)
}

// connect has a captured connection from A to target carried by svc and
// returns the client's end.
func connect(t *testing.T, svc *Service, target netip.AddrPort) *net.TCPConn {
	t.Helper()
	client, entry := clientPair(t)
	from := netip.MustParseAddrPort(entry.RemoteAddr().String())
	go func() {
		defer entry.Close()
		svc.entryTCP(entry, from, false, target, nil)
	}()
	return client
}

func echoThrough(t *testing.T, svc *Service, target netip.AddrPort, msg string) string {
	t.Helper()
	c := connect(t, svc, target)
	defer c.Close()
	_ = c.SetDeadline(time.Now().Add(5 * time.Second))
	if _, err := c.Write([]byte(msg)); err != nil {
		return ""
	}
	_ = c.CloseWrite()
	reply, _ := io.ReadAll(c)
	return string(reply)
}

func channelOf(svc *Service, dest string) (ChannelStatus, bool) {
	st := svc.Status()
	if st == nil {
		return ChannelStatus{}, false
	}
	for _, c := range st.Channels {
		if c.Dest == dest {
			return c, true
		}
	}
	return ChannelStatus{}, false
}

// TestChannelWarmsFollowsPathsAndFallsBack: a fast port's first connection
// goes over the mesh while its channel warms up; then connections take the
// mesh's first path, A -> B -> C, whose B -> C leg falls back to C's mesh
// address; entry and exit rules hold; an abort reaches the server; with B
// gone the next path, A -> C, takes new connections; UDP goes along; an idle
// channel is dropped.
func TestChannelWarmsFollowsPathsAndFallsBack(t *testing.T) {
	origIdle, origSweep := udpIdleDefault, udpSweepEvery
	udpIdleDefault, udpSweepEvery = 250*time.Millisecond, 50*time.Millisecond
	defer func() { udpIdleDefault, udpSweepEvery = origIdle, origSweep }()
	loopback(t)

	key := randKeyB64(t)
	meshPrefix := netip.MustParsePrefix("100.96.0.0/16")
	addrA := netip.MustParseAddr("100.96.0.1")
	addrB := netip.MustParseAddr("100.96.0.2")
	addrC := netip.MustParseAddr("100.96.0.3")
	nodes := map[string]netip.Addr{"A": addrA, "B": addrB, "C": addrC}

	echoSrv, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer echoSrv.Close()
	echoPort := uint16(echoSrv.Addr().(*net.TCPAddr).Port)
	go func() {
		for {
			c, err := echoSrv.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				defer c.Close()
				data, _ := io.ReadAll(c)
				_, _ = c.Write(append([]byte("echo:"), data...))
			}(c)
		}
	}()

	var resetObserved atomic.Bool
	streamSrv, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer streamSrv.Close()
	streamPort := uint16(streamSrv.Addr().(*net.TCPAddr).Port)
	go func() {
		chunk := bytes.Repeat([]byte("Z"), 64<<10)
		for {
			c, err := streamSrv.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				defer c.Close()
				for range 512 {
					if _, err := c.Write(chunk); err != nil {
						resetObserved.Store(true)
						return
					}
				}
			}(c)
		}
	}()

	udpSrv, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer udpSrv.Close()
	udpPort := uint16(udpSrv.LocalAddr().(*net.UDPAddr).Port)
	go func() {
		buf := make([]byte, 4096)
		for {
			n, from, err := udpSrv.ReadFromUDP(buf)
			if err != nil {
				return
			}
			_, _ = udpSrv.WriteToUDP(append([]byte("udp:"), buf[:n]...), from)
		}
	}()

	deniedPort := uint16(freeTCPPort(t))
	exitDeniedPort := uint16(freeTCPPort(t))
	portB, portC := freeTCPPort(t), freeTCPPort(t)
	deadPort := freeTCPPort(t)

	var svcA, svcB, svcC Service
	defer svcA.Close()
	defer svcB.Close()
	defer svcC.Close()
	for _, s := range []*Service{&svcA, &svcB, &svcC} {
		isolateHost(t, s)
	}
	dir := t.TempDir()
	svcA.SetHooks(Hooks{Dir: dir + "/a", Paths: func(dest string, k int, allow func(string) bool) []Plan {
		if dest != "C" {
			return nil
		}
		return []Plan{{Hops: []string{"B", "C"}, Planned: true}, {Hops: []string{"C"}, Planned: true}}[:min(k, 2)]
	}})
	svcB.SetHooks(Hooks{Dir: dir + "/b"})
	svcC.SetHooks(Hooks{Dir: dir + "/c"})

	fromA := []netip.Prefix{netip.PrefixFrom(addrA, 32)}
	svcC.Apply(&Config{Port: portC, MeshPort: portC, Keys: []string{key}, Nodes: nodes}, "C", addrC, meshPrefix, []Rule{{
		Src: fromA, Dst: []netip.Prefix{netip.PrefixFrom(addrC, 32)}, Proto: "any",
		Ports: [][2]uint16{{echoPort, echoPort}, {streamPort, streamPort}, {udpPort, udpPort}},
	}})

	// B's way to C, Next, is a dead port: its pool falls back to C's mesh
	// address, which the pool's dial takes to C's port.
	svcB.Apply(&Config{Port: portB, MeshPort: portC, Keys: []string{key}, Nodes: nodes,
		Next: map[string]string{"C": net.JoinHostPort("127.0.0.1", strconv.Itoa(deadPort))}}, "B", addrB, meshPrefix, nil)
	svcB.pool.dial = func(_ contextContext, addr string) (*net.TCPConn, error) {
		if addr == netip.AddrPortFrom(addrC, uint16(portC)).String() {
			addr = net.JoinHostPort("127.0.0.1", strconv.Itoa(portC))
		}
		c, err := net.DialTimeout("tcp", addr, 200*time.Millisecond)
		if err != nil {
			return nil, err
		}
		return c.(*net.TCPConn), nil
	}

	targets := []Target{
		{Node: "C", Port: echoPort, Proto: "tcp"},
		{Node: "C", Port: streamPort, Proto: "tcp"},
		{Node: "C", Port: udpPort, Proto: "udp"},
		{Node: "C", Port: exitDeniedPort, Proto: "tcp"},
		{Node: "C", Port: deniedPort, Proto: "tcp", Src: []netip.Prefix{netip.MustParsePrefix("10.0.0.0/8")}},
	}
	svcA.Apply(&Config{MeshPort: portC, Keys: []string{key}, Nodes: nodes, Targets: targets,
		Next: map[string]string{"B": net.JoinHostPort("127.0.0.1", strconv.Itoa(portB)), "C": net.JoinHostPort("127.0.0.1", strconv.Itoa(portC))},
	}, "A", addrA, meshPrefix, nil)
	var bGone atomic.Bool
	svcA.pool.dial = func(_ contextContext, addr string) (*net.TCPConn, error) {
		if bGone.Load() && (addr == net.JoinHostPort("127.0.0.1", strconv.Itoa(portB)) || strings.HasPrefix(addr, addrB.String()+":")) {
			return nil, errors.New("B is gone")
		}
		if strings.HasPrefix(addr, "100.96.") {
			return nil, errors.New("no mesh in a test")
		}
		c, err := net.DialTimeout("tcp", addr, 200*time.Millisecond)
		if err != nil {
			return nil, err
		}
		return c.(*net.TCPConn), nil
	}
	if svcA.RelayAddr() != "" {
		t.Fatal("A, port 0, listens")
	}

	echoC := netip.AddrPortFrom(addrC, echoPort)

	// 1. The first connection goes over the mesh and opens the channel.
	if got := echoThrough(t, &svcA, echoC, "first"); got != "echo:first" {
		t.Fatalf("first connection over the mesh: %q", got)
	}
	if svcC.Status().Exit.Opened != 0 {
		t.Fatal("the first connection went through C's relay port")
	}
	waitFor(t, "the channel to C to be ready", 5*time.Second, func() bool {
		ch, ok := channelOf(&svcA, "C")
		return ok && ch.Ready && ch.Using == 0
	})
	ch, _ := channelOf(&svcA, "C")
	if ch.Fallbacks != 1 || len(ch.Paths) != 2 || !slices.Equal(ch.Paths[0].Hops, []string{"B", "C"}) || ch.Paths[0].State != pathReady {
		t.Fatalf("channel after warming: %+v", ch)
	}
	if svcB.Status().Transit.Opened != 0 || svcC.Status().Exit.Opened != 0 {
		t.Fatal("probes counted as streams")
	}

	// 2. Now along A -> B -> C, B -> C over C's mesh address.
	if got := echoThrough(t, &svcA, echoC, "fast"); got != "echo:fast" {
		t.Fatalf("fast connection: %q", got)
	}
	if svcB.Status().Transit.Opened != 1 || svcC.Status().Exit.Opened != 1 {
		t.Fatalf("not through B and C: transit %+v exit %+v", svcB.Status().Transit, svcC.Status().Exit)
	}
	if peers := svcB.Status().Peers; len(peers) == 0 || !peers[0].ViaMesh {
		t.Fatalf("B -> C did not fall back to the mesh address: %+v", peers)
	}

	// 3. A source the fast port does not allow is refused at the entry.
	c := connect(t, &svcA, netip.AddrPortFrom(addrC, deniedPort))
	_ = c.SetReadDeadline(time.Now().Add(3 * time.Second))
	if _, err := c.Read(make([]byte, 1)); err == nil {
		t.Fatal("a refused connection carried data")
	}
	c.Close()
	if svcA.Status().RefusedEntry != 1 {
		t.Fatalf("refusedEntry = %d", svcA.Status().RefusedEntry)
	}

	// 4. C's rules refuse what they do not allow.
	c = connect(t, &svcA, netip.AddrPortFrom(addrC, exitDeniedPort))
	_, _ = c.Write([]byte("x"))
	_, _ = c.Read(make([]byte, 1))
	c.Close()
	waitFor(t, "C to refuse", 2*time.Second, func() bool { return svcC.Status().RefusedExit == 1 })

	// 5. An abort at the client reaches the server across all hops.
	dl := connect(t, &svcA, netip.AddrPortFrom(addrC, streamPort))
	if _, err := io.ReadFull(dl, make([]byte, 4096)); err != nil {
		t.Fatalf("read stream start: %v", err)
	}
	abortTCP(dl)
	waitFor(t, "the server to see the abort", 3*time.Second, resetObserved.Load)

	// 6. UDP along the channel; an idle session expires.
	exits := svcC.Status().Exit.Opened
	replies := make(chan string, 2)
	key0 := udpSessionKey{scope: svcA.udpSessions.newScope(), client: netip.MustParseAddrPort("127.0.0.1:40001"), target: netip.AddrPortFrom(addrC, udpPort)}
	makeReply := func() (func([]byte) int, func(), error) {
		return func(b []byte) int { replies <- string(b); return len(b) }, nil, nil
	}
	if err := svcA.udpSessions.send(key0, []byte("ping-udp"), makeReply, nil, nil, nil); err != nil {
		t.Fatal(err)
	}
	select {
	case got := <-replies:
		if got != "udp:ping-udp" {
			t.Fatalf("udp reply %q", got)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("no udp reply")
	}
	if svcC.Status().Exit.Opened != exits+1 {
		t.Fatalf("udp did not go through C: %+v", svcC.Status().Exit)
	}
	svcA.udpSessions.sweep(time.Now().Add(500 * time.Millisecond))
	if n := svcA.udpSessions.count(); n != 0 {
		t.Fatalf("idle udp sessions left: %d", n)
	}

	// 7. With B gone, the next connection passes over A -> B -> C to A -> C.
	bGone.Store(true)
	svcA.pool.mu.Lock()
	for _, x := range svcA.pool.peers["B"].idle {
		_ = x.c.Close()
	}
	svcA.pool.peers["B"].idle = nil
	svcA.pool.mu.Unlock()
	opened := svcC.Status().Exit.Opened
	if got := echoThrough(t, &svcA, echoC, "next"); got != "echo:next" {
		t.Fatalf("connection with B gone: %q", got)
	}
	if svcC.Status().Exit.Opened != opened+1 {
		t.Fatal("the connection with B gone went over the mesh, not A -> C")
	}
	ch, _ = channelOf(&svcA, "C")
	if ch.Paths[0].State != pathDown || ch.Using != 1 {
		t.Fatalf("channel with B gone: %+v", ch)
	}

	// 8. A channel nothing uses is dropped; the next connection goes over the mesh.
	channelIdle = 0
	defer func() { channelIdle = 10 * time.Minute }()
	waitFor(t, "the idle channel to go", 3*time.Second, func() bool {
		_, ok := channelOf(&svcA, "C")
		return !ok && !svcA.isReady("C")
	})
}

// A config pushed again leaves alone what its fast ports and rules still
// allow, and closes what they no longer do: at the entry when the fast port
// goes, at the destination when its rules stop allowing the source.
func TestReapplyKeepsStreamsTheRulesStillAllow(t *testing.T) {
	loopback(t)
	key := randKeyB64(t)
	meshPrefix := netip.MustParsePrefix("100.96.0.0/16")
	addrA := netip.MustParseAddr("100.96.0.1")
	addrC := netip.MustParseAddr("100.96.0.3")
	nodes := map[string]netip.Addr{"A": addrA, "C": addrC}

	srv, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer srv.Close()
	port := uint16(srv.Addr().(*net.TCPAddr).Port)
	go func() {
		for {
			c, err := srv.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) { defer c.Close(); _, _ = io.Copy(c, c) }(c)
		}
	}()

	portC := freeTCPPort(t)
	var svcA, svcC Service
	defer svcA.Close()
	defer svcC.Close()
	isolateHost(t, &svcA)
	isolateHost(t, &svcC)
	svcA.SetHooks(Hooks{Dir: t.TempDir()})
	svcC.SetHooks(Hooks{Dir: t.TempDir()})
	allow := func(ports ...uint16) []Rule {
		var rs [][2]uint16
		for _, p := range ports {
			rs = append(rs, [2]uint16{p, p})
		}
		return []Rule{{Src: []netip.Prefix{netip.PrefixFrom(addrA, 32)}, Proto: "tcp", Ports: rs}}
	}
	svcC.Apply(&Config{Port: portC, MeshPort: portC, Keys: []string{key}, Nodes: nodes}, "C", addrC, meshPrefix, allow(port))
	cfgA := &Config{MeshPort: portC, Keys: []string{key}, Nodes: nodes, Targets: []Target{{Node: "C", Port: port, Proto: "tcp"}},
		Next: map[string]string{"C": net.JoinHostPort("127.0.0.1", strconv.Itoa(portC))}}
	svcA.Apply(cfgA, "A", addrA, meshPrefix, nil)
	target := netip.AddrPortFrom(addrC, port)
	svcA.live.Load().warm("C")
	waitFor(t, "the channel to C", 5*time.Second, func() bool { return svcA.isReady("C") })

	open := func() *net.TCPConn {
		c := connect(t, &svcA, target)
		_ = c.SetDeadline(time.Now().Add(3 * time.Second))
		if _, err := c.Write([]byte("hi")); err != nil {
			t.Fatal(err)
		}
		if _, err := io.ReadFull(c, make([]byte, 2)); err != nil {
			t.Fatalf("stream did not echo: %v", err)
		}
		return c
	}
	alive := func(c *net.TCPConn) bool {
		_ = c.SetDeadline(time.Now().Add(time.Second))
		if _, err := c.Write([]byte("ok")); err != nil {
			return false
		}
		_, err := io.ReadFull(c, make([]byte, 2))
		return err == nil
	}

	c1 := open()
	defer c1.Close()
	svcA.Apply(cfgA, "A", addrA, meshPrefix, nil)
	svcC.Apply(&Config{Port: portC, MeshPort: portC, Keys: []string{key}, Nodes: nodes}, "C", addrC, meshPrefix, allow(port, port+1))
	if !alive(c1) {
		t.Fatal("a reapply that still allows the stream closed it")
	}
	svcC.Apply(&Config{Port: portC, MeshPort: portC, Keys: []string{key}, Nodes: nodes}, "C", addrC, meshPrefix, allow(port+1))
	if alive(c1) {
		t.Fatal("the destination kept a stream its rules stopped allowing")
	}

	svcC.Apply(&Config{Port: portC, MeshPort: portC, Keys: []string{key}, Nodes: nodes}, "C", addrC, meshPrefix, allow(port))
	c2 := open()
	defer c2.Close()
	cfgA2 := *cfgA
	cfgA2.Targets = nil
	svcA.Apply(&cfgA2, "A", addrA, meshPrefix, nil)
	if alive(c2) {
		t.Fatal("the entry kept a stream to a fast port that went")
	}
}

// A UDP session whose next hop hangs while dialing holds up neither the
// sender, whose datagrams beyond the queue are dropped, nor another session.
func TestUDPSessionsDoNotWaitOnEachOther(t *testing.T) {
	loopback(t)
	key := randKeyB64(t)
	meshPrefix := netip.MustParsePrefix("100.96.0.0/16")
	addrA := netip.MustParseAddr("100.96.0.1")
	addrC := netip.MustParseAddr("100.96.0.3")
	addrD := netip.MustParseAddr("100.96.0.4")
	nodes := map[string]netip.Addr{"A": addrA, "C": addrC, "D": addrD}

	echo, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer echo.Close()
	go func() {
		buf := make([]byte, 2048)
		for {
			n, from, err := echo.ReadFromUDP(buf)
			if err != nil {
				return
			}
			_, _ = echo.WriteToUDP(append([]byte("udp:"), buf[:n]...), from)
		}
	}()
	port := uint16(echo.LocalAddr().(*net.UDPAddr).Port)

	portC := freeTCPPort(t)
	var svcA, svcC Service
	defer svcA.Close()
	defer svcC.Close()
	isolateHost(t, &svcA)
	isolateHost(t, &svcC)
	svcA.SetHooks(Hooks{Dir: t.TempDir()})
	svcC.SetHooks(Hooks{Dir: t.TempDir()})
	fromA := []netip.Prefix{netip.PrefixFrom(addrA, 32)}
	svcC.Apply(&Config{Port: portC, MeshPort: portC, Keys: []string{key}, Nodes: nodes}, "C", addrC, meshPrefix,
		[]Rule{{Src: fromA, Proto: "udp"}})
	toC := net.JoinHostPort("127.0.0.1", strconv.Itoa(portC))
	toD := net.JoinHostPort("127.0.0.1", strconv.Itoa(freeTCPPort(t)))
	svcA.Apply(&Config{MeshPort: portC, Keys: []string{key}, Nodes: nodes,
		Targets: []Target{{Node: "C", Port: port, Proto: "udp"}, {Node: "D", Port: port, Proto: "udp"}},
		Next:    map[string]string{"C": toC, "D": toD}}, "A", addrA, meshPrefix, nil)
	release := make(chan struct{})
	defer close(release)
	svcA.pool.dial = func(ctx contextContext, addr string) (*net.TCPConn, error) {
		if addr != toC {
			select {
			case <-release:
			case <-ctx.Done():
			}
			return nil, errors.New("next hop does not answer")
		}
		c, err := net.DialTimeout("tcp", addr, time.Second)
		if err != nil {
			return nil, err
		}
		return c.(*net.TCPConn), nil
	}
	// Both channels as ready as a probe would leave them: D's hop then hangs.
	chans := svcA.live.Load()
	chans.mu.Lock()
	for _, d := range []string{"C", "D"} {
		now := time.Now()
		chans.byDest[d] = &channel{dest: d, opened: now, lastUse: now, refreshed: now.Add(time.Hour),
			cands: []*candidate{{plan: Plan{Hops: []string{d}, Planned: true}, state: pathReady, since: now, next: now.Add(time.Hour)}}}
	}
	chans.mu.Unlock()
	svcA.readyChanged()

	replies := make(chan string, 4)
	makeReply := func() (func([]byte) int, func(), error) {
		return func(b []byte) int { replies <- string(b); return len(b) }, nil, nil
	}
	scope := svcA.udpSessions.newScope()
	client := netip.MustParseAddrPort("127.0.0.1:40000")
	stuck := udpSessionKey{scope: scope, client: client, target: netip.AddrPortFrom(addrD, port)}
	live := udpSessionKey{scope: scope, client: client, target: netip.AddrPortFrom(addrC, port)}

	start := time.Now()
	var dropped error
	for range udpQueueLen + 8 {
		if err := svcA.udpSessions.send(stuck, []byte("x"), makeReply, nil, nil, nil); err != nil {
			dropped = err
		}
	}
	if err := svcA.udpSessions.send(live, []byte("ping"), makeReply, nil, nil, nil); err != nil {
		t.Fatal(err)
	}
	if took := time.Since(start); took > 500*time.Millisecond {
		t.Fatalf("sending waited %v on a session still dialing", took)
	}
	if !errors.Is(dropped, errQueueFull) {
		t.Fatalf("datagrams beyond a full queue: %v, want errQueueFull", dropped)
	}
	select {
	case got := <-replies:
		if got != "udp:ping" {
			t.Fatalf("reply %q", got)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("no reply while another session was still dialing")
	}
}

// The data plane's packets to a fast port whose channel is not ready open
// the channel, at most once a second; others are none of its business.
func TestOutboundWarmsChannelOnFirstPacket(t *testing.T) {
	key := randKeyB64(t)
	meshPrefix := netip.MustParsePrefix("100.96.0.0/16")
	addrA := netip.MustParseAddr("100.96.0.1")
	addrC := netip.MustParseAddr("100.96.0.3")
	var svc Service
	defer svc.Close()
	isolateHost(t, &svc)
	svc.SetHooks(Hooks{Dir: t.TempDir()})
	svc.Apply(&Config{MeshPort: 52003, Keys: []string{key}, Nodes: map[string]netip.Addr{"A": addrA, "C": addrC},
		Targets: []Target{{Node: "C", Port: 48888, Proto: "tcp"}}}, "A", addrA, meshPrefix, nil)

	packet := func(dst netip.Addr, port uint16, flags byte) []byte {
		b := make([]byte, 40)
		b[0], b[9] = 0x45, 6
		d := dst.As4()
		copy(b[16:20], d[:])
		b[22], b[23] = byte(port>>8), byte(port)
		b[33] = flags
		return b
	}
	if svc.Outbound(packet(addrC, 48888, 0x10)) || svc.Outbound(packet(addrC, 22, 0x02)) {
		t.Fatal("Outbound took a packet")
	}
	if _, ok := channelOf(&svc, "C"); ok {
		t.Fatal("an ACK or another port opened the channel")
	}
	svc.Outbound(packet(addrC, 48888, 0x02))
	if _, ok := channelOf(&svc, "C"); !ok {
		t.Fatal("a SYN to the fast port did not open its channel")
	}
}

func TestNFTScriptSetsStandaloneAndSkipsBypass(t *testing.T) {
	s := NFTScript(41001, 41002, 41003, []string{"100.96.0.2 . 48888"}, nil)
	for _, want := range []string{
		"table ip openbear_fast",
		"delete table ip openbear_fast",
		"set tcp_ready {\n\t\ttype ipv4_addr . inet_service\n\t\telements = { 100.96.0.2 . 48888 }",
		"set udp_ready {\n\t\ttype ipv4_addr . inet_service\n\t}",
		"type nat hook prerouting priority -105",
		"ip daddr . tcp dport @tcp_ready redirect to :41002",
		"type nat hook output priority -105",
		"meta skgid 52999 return",
		"meta mark 0x4f420000 return",
		"ip daddr . tcp dport @tcp_ready redirect to :41001",
		"type filter hook prerouting priority -160",
		"ip daddr . udp dport @udp_ready tproxy to 127.0.0.1:41003 meta mark set 0x4f420001 accept",
		"type route hook output priority -55",
	} {
		if !strings.Contains(s, want) {
			t.Fatalf("NFTScript missing %q in:\n%s", want, s)
		}
	}
	tcp, udp := readyElements([]targetKey{
		{netip.MustParseAddr("100.96.0.2"), 48888, 0},
		{netip.MustParseAddr("100.96.0.3"), 53, protoUDP},
	})
	if !slices.Equal(tcp, []string{"100.96.0.2 . 48888"}) || !slices.Equal(udp, []string{"100.96.0.2 . 48888", "100.96.0.3 . 53"}) {
		t.Fatalf("readyElements = %v %v", tcp, udp)
	}
	if got := NFTSets(tcp, nil); got != "flush set ip openbear_fast tcp_ready\nflush set ip openbear_fast udp_ready\nadd element ip openbear_fast tcp_ready { 100.96.0.2 . 48888 }\n" {
		t.Fatalf("NFTSets = %q", got)
	}
}

// silencer forwards TCP to target until silenced: then it neither forwards
// nor closes, as a next hop gone dark drops packets.
type silencer struct {
	ln     net.Listener
	silent atomic.Bool
}

func newSilencer(t *testing.T, target string) *silencer {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	s := &silencer{ln: ln}
	t.Cleanup(func() { _ = ln.Close() })
	pipe := func(dst, src net.Conn) {
		buf := make([]byte, 32<<10)
		for {
			n, err := src.Read(buf)
			for s.silent.Load() {
				time.Sleep(20 * time.Millisecond)
			}
			if n > 0 {
				if _, werr := dst.Write(buf[:n]); werr != nil {
					return
				}
			}
			if err != nil {
				_ = dst.(*net.TCPConn).CloseWrite()
				return
			}
		}
	}
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			up, err := net.Dial("tcp", target)
			if err != nil {
				_ = c.Close()
				continue
			}
			t.Cleanup(func() { _ = c.Close(); _ = up.Close() })
			go pipe(up, c)
			go pipe(c, up)
		}
	}()
	return s
}

// A hop that goes silent mid-path: the next probe of the path times out, the
// streams on it are cut, new connections take the next path at once, and
// the path is taken again once it answers.
func TestSilentHopCutsStreamsAndNextPathTakesOver(t *testing.T) {
	origBusy, origMin, origRetry := probeBusy, probeTimeoutMin, probeRetryMin
	probeBusy, probeTimeoutMin, probeRetryMin = 200*time.Millisecond, 300*time.Millisecond, 200*time.Millisecond
	defer func() { probeBusy, probeTimeoutMin, probeRetryMin = origBusy, origMin, origRetry }()
	loopback(t)

	key := randKeyB64(t)
	meshPrefix := netip.MustParsePrefix("100.96.0.0/16")
	addrA, addrB, addrC := netip.MustParseAddr("100.96.0.1"), netip.MustParseAddr("100.96.0.2"), netip.MustParseAddr("100.96.0.3")
	nodes := map[string]netip.Addr{"A": addrA, "B": addrB, "C": addrC}

	echo, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer echo.Close()
	go func() {
		for {
			c, err := echo.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				defer c.Close()
				data, _ := io.ReadAll(c)
				_, _ = c.Write(append([]byte("echo:"), data...))
			}(c)
		}
	}()
	echoPort := uint16(echo.Addr().(*net.TCPAddr).Port)
	stream, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer stream.Close()
	go func() {
		chunk := bytes.Repeat([]byte("Z"), 16<<10)
		for {
			c, err := stream.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				defer c.Close()
				for {
					if _, err := c.Write(chunk); err != nil {
						return
					}
					time.Sleep(10 * time.Millisecond)
				}
			}(c)
		}
	}()
	streamPort := uint16(stream.Addr().(*net.TCPAddr).Port)
	// late answers a request only after a while.
	late, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer late.Close()
	go func() {
		for {
			c, err := late.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				defer c.Close()
				_, _ = io.ReadAll(c)
				time.Sleep(3 * time.Second)
				_, _ = c.Write([]byte("late"))
			}(c)
		}
	}()
	latePort := uint16(late.Addr().(*net.TCPAddr).Port)

	portB, portC := freeTCPPort(t), freeTCPPort(t)
	var svcA, svcB, svcC Service
	defer svcA.Close()
	defer svcB.Close()
	defer svcC.Close()
	for _, s := range []*Service{&svcA, &svcB, &svcC} {
		isolateHost(t, s)
	}
	svcA.SetHooks(Hooks{Dir: t.TempDir(), Paths: func(dest string, k int, allow func(string) bool) []Plan {
		return []Plan{{Hops: []string{"B", "C"}, Planned: true}, {Hops: []string{"C"}, Planned: true}}[:min(k, 2)]
	}})
	svcB.SetHooks(Hooks{Dir: t.TempDir()})
	svcC.SetHooks(Hooks{Dir: t.TempDir()})
	svcC.Apply(&Config{Port: portC, MeshPort: portC, Keys: []string{key}, Nodes: nodes}, "C", addrC, meshPrefix,
		[]Rule{{Src: []netip.Prefix{netip.PrefixFrom(addrA, 32)}, Proto: "tcp"}})
	dark := newSilencer(t, net.JoinHostPort("127.0.0.1", strconv.Itoa(portC)))
	svcB.Apply(&Config{Port: portB, MeshPort: portC, Keys: []string{key}, Nodes: nodes,
		Next: map[string]string{"C": dark.ln.Addr().String()}}, "B", addrB, meshPrefix, nil)
	svcA.Apply(&Config{MeshPort: portC, Keys: []string{key}, Nodes: nodes,
		Targets: []Target{{Node: "C", Port: echoPort, Proto: "tcp"}, {Node: "C", Port: streamPort, Proto: "tcp"}, {Node: "C", Port: latePort, Proto: "tcp"}},
		Next:    map[string]string{"B": net.JoinHostPort("127.0.0.1", strconv.Itoa(portB)), "C": net.JoinHostPort("127.0.0.1", strconv.Itoa(portC))},
	}, "A", addrA, meshPrefix, nil)

	echoC := netip.AddrPortFrom(addrC, echoPort)
	_ = echoThrough(t, &svcA, echoC, "warm")
	waitFor(t, "both paths to answer", 5*time.Second, func() bool {
		ch, ok := channelOf(&svcA, "C")
		return ok && len(ch.Paths) == 2 && ch.Paths[0].State == pathReady && ch.Paths[1].State == pathReady
	})

	// A download along A -> B -> C; then B -> C goes silent.
	dl := connect(t, &svcA, netip.AddrPortFrom(addrC, streamPort))
	defer dl.Close()
	if _, err := io.ReadFull(dl, make([]byte, 64<<10)); err != nil {
		t.Fatalf("download start: %v", err)
	}
	// And a client that sent its request and closed its side, waiting for the answer.
	half := connect(t, &svcA, netip.AddrPortFrom(addrC, latePort))
	defer half.Close()
	_, _ = half.Write([]byte("req"))
	_ = half.CloseWrite()
	waitFor(t, "both along A -> B -> C", 2*time.Second, func() bool { return svcB.Status().Transit.Active == 2 })
	dark.silent.Store(true)
	cut := make(chan error, 1)
	go func() {
		_ = dl.SetReadDeadline(time.Now().Add(10 * time.Second))
		_, err := io.Copy(io.Discard, dl)
		cut <- err
	}()
	select {
	case err := <-cut:
		var ne net.Error
		if errors.As(err, &ne) && ne.Timeout() {
			t.Fatal("the stalled download was not cut")
		}
	case <-time.After(12 * time.Second):
		t.Fatal("the stalled download hangs")
	}
	ch, _ := channelOf(&svcA, "C")
	if ch.Paths[0].State != pathDown || ch.Using != 1 {
		t.Fatalf("channel with B -> C silent: %+v", ch)
	}
	// The half-closed one is cut too, its stream up the path with it.
	waitFor(t, "no stream left at A", 2*time.Second, func() bool { return svcA.Status().Entry.Active == 0 })

	// New connections take A -> C straight away.
	exits := svcC.Status().Exit.Opened
	start := time.Now()
	if got := echoThrough(t, &svcA, echoC, "next"); got != "echo:next" {
		t.Fatalf("new connection with B -> C silent: %q", got)
	}
	if took := time.Since(start); took > 2*time.Second {
		t.Fatalf("the new connection took %v", took)
	}
	if svcC.Status().Exit.Opened != exits+1 {
		t.Fatal("the new connection did not go A -> C")
	}

	// B -> C answers again: A -> B -> C is taken again.
	dark.silent.Store(false)
	waitFor(t, "A -> B -> C to answer again", 10*time.Second, func() bool {
		ch, _ := channelOf(&svcA, "C")
		return ch.Paths[0].State == pathReady && ch.Using == 0
	})
}
