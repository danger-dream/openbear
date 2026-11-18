package proxy

import (
	"bufio"
	"bytes"
	"context"
	"io"
	"net"
	"net/netip"
	"slices"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// bothFree is a port free on loopback for TCP and UDP alike.
func bothFree(t *testing.T) int {
	t.Helper()
	for range 20 {
		p := freePort(t)
		if pc, err := net.ListenUDP("udp", &net.UDPAddr{Port: int(p.Port())}); err == nil {
			pc.Close()
			return int(p.Port())
		}
	}
	t.Fatal("no port free for both")
	return 0
}

// udpEcho answers each datagram on loopback with itself.
func udpEcho(t *testing.T) netip.AddrPort {
	t.Helper()
	pc, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pc.Close() })
	go func() {
		buf := make([]byte, 2048)
		for {
			n, from, err := pc.ReadFromUDPAddrPort(buf)
			if err != nil {
				return
			}
			pc.WriteToUDPAddrPort(buf[:n], from)
		}
	}()
	return netip.MustParseAddrPort(pc.LocalAddr().String())
}

// fwdLab is a node on loopback that is both a forward's entry and its target.
func fwdLab(t *testing.T, f Forward, sets map[string][]netip.Prefix) (*Forwarder, *ForwardConfig) {
	t.Helper()
	f.Node = loopback
	if f.ID == "" {
		f.ID = "f1"
	}
	if f.Listen == 0 {
		f.Listen = bothFree(t)
	}
	c := &ForwardConfig{Port: int(freePort(t).Port()), Forwards: []Forward{f}, Sets: sets,
		Targets: []ForwardTarget{{From: loopback, Host: f.Host, Port: f.Port, Proto: f.Proto}}}
	if err := c.Check(); err != nil {
		t.Fatal(err)
	}
	s := &Forwarder{}
	s.Apply(c, loopback)
	t.Cleanup(s.Close)
	return s, c
}

func dialForward(t *testing.T, c *ForwardConfig) net.Conn {
	t.Helper()
	conn, err := net.DialTimeout("tcp", netip.AddrPortFrom(loopback, uint16(c.Forwards[0].Listen)).String(), 2*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	return conn
}

func TestForwardTCPToLoopback(t *testing.T) {
	e := echo(t)
	s, c := fwdLab(t, Forward{Proto: "tcp", Host: "127.0.0.1", Port: int(e.Port())}, nil)
	conn := dialForward(t, c)
	roundTrip(t, conn, "hello through the forward")
	st := s.Status()
	if len(st.Forwards) != 1 || st.Forwards[0].Active != 1 || len(st.Forwards[0].Listen) != 1 || st.Target == nil || st.Target.Active != 1 {
		t.Fatalf("status %+v", st)
	}
	conn.Close()
	deadline := time.Now().Add(3 * time.Second)
	for s.Status().Forwards[0].Active != 0 || s.Status().Forwards[0].BytesIn != int64(len("hello through the forward")) {
		if time.Now().After(deadline) {
			t.Fatalf("after close: %+v", s.Status().Forwards[0])
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func TestForwardUDP(t *testing.T) {
	e := udpEcho(t)
	_, c := fwdLab(t, Forward{Proto: "any", Host: "127.0.0.1", Port: int(e.Port())}, nil)
	conn, err := net.Dial("udp", netip.AddrPortFrom(loopback, uint16(c.Forwards[0].Listen)).String())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	buf := make([]byte, 64)
	for i, msg := range []string{"one", "two"} {
		conn.Write([]byte(msg))
		conn.SetReadDeadline(time.Now().Add(3 * time.Second))
		n, err := conn.Read(buf)
		if err != nil || string(buf[:n]) != msg {
			t.Fatalf("datagram %d: %q, %v", i, buf[:n], err)
		}
	}
}

// proxySink accepts one connection and gives what arrived on it.
func proxySink(t *testing.T) (netip.AddrPort, <-chan []byte) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	got := make(chan []byte, 1)
	go func() {
		c, err := ln.Accept()
		if err != nil {
			return
		}
		defer c.Close()
		c.SetReadDeadline(time.Now().Add(3 * time.Second))
		var b bytes.Buffer
		io.Copy(&b, c)
		got <- b.Bytes()
	}()
	return netip.MustParseAddrPort(ln.Addr().String()), got
}

func TestForwardProxyProtocol(t *testing.T) {
	for _, v := range []int{1, 2} {
		sink, got := proxySink(t)
		_, c := fwdLab(t, Forward{ID: "pp", Proto: "tcp", Host: "127.0.0.1", Port: int(sink.Port()), ProxyProtocol: v}, nil)
		conn := dialForward(t, c)
		from := netip.MustParseAddrPort(conn.LocalAddr().String())
		conn.Write([]byte("data"))
		conn.(*net.TCPConn).CloseWrite()
		b := <-got
		switch v {
		case 1:
			line, rest, _ := strings.Cut(string(b), "\r\n")
			want := "PROXY TCP4 127.0.0.1 127.0.0.1 " + strconv.Itoa(int(from.Port())) + " " + strconv.Itoa(c.Forwards[0].Listen)
			if line != want || rest != "data" {
				t.Fatalf("v1: %q", b)
			}
		case 2:
			want := proxyHeader(2, from, netip.AddrPortFrom(loopback, uint16(c.Forwards[0].Listen)))
			if !bytes.HasPrefix(b, proxyV2Sig) || !bytes.Equal(b[:len(want)], want) || string(b[len(want):]) != "data" {
				t.Fatalf("v2: %x", b)
			}
			if b[13] != 0x11 || int(b[24])<<8|int(b[25]) != int(from.Port()) {
				t.Fatalf("v2 family or port: %x", b)
			}
		}
	}
}

// A client the forward does not allow is turned away; a change of what it
// allows cuts the clients it no longer takes, and keeps the listener and the
// rest.
func TestForwardAllowList(t *testing.T) {
	e := echo(t)
	s, c := fwdLab(t, Forward{Proto: "tcp", Host: "127.0.0.1", Port: int(e.Port()), Allow: []netip.Prefix{netip.MustParsePrefix("10.0.0.0/8")}}, nil)
	conn := dialForward(t, c)
	if !closed(conn) {
		t.Fatal("a client outside the allow list was served")
	}
	if s.Status().Forwards[0].Refused != 1 {
		t.Fatalf("refused: %+v", s.Status().Forwards[0])
	}

	// By a named set: loopback's province.
	next := *c
	next.Forwards = []Forward{c.Forwards[0]}
	next.Forwards[0].Allow = nil
	next.Forwards[0].AllowSets = []string{"province:test"}
	next.Sets = map[string][]netip.Prefix{"province:test": {netip.MustParsePrefix("127.0.0.0/24")}}
	s.Apply(&next, loopback)
	ln := s.entries["f1"].tcp
	kept := dialForward(t, c)
	roundTrip(t, kept, "allowed")

	// The set's data changes: the client is cut, the listener stays.
	moved := next
	moved.Sets = map[string][]netip.Prefix{"province:test": {netip.MustParsePrefix("192.168.0.0/16")}}
	s.Apply(&moved, loopback)
	if !closed(kept) {
		t.Fatal("a client no longer allowed kept its connection")
	}
	if s.entries["f1"].tcp != ln {
		t.Fatal("a change of the allow list restarted the listener")
	}
}

func TestAllowListMatches(t *testing.T) {
	f := Forward{Allow: []netip.Prefix{netip.MustParsePrefix("1.2.3.4/32"), netip.MustParsePrefix("10.0.0.0/8"), netip.MustParsePrefix("10.1.0.0/16"),
		netip.MustParsePrefix("11.0.0.0/8"), netip.MustParsePrefix("2001:db8::/32")}, AllowSets: []string{"p"}}
	l := newAllowList(&f, map[string][]netip.Prefix{"p": {netip.MustParsePrefix("100.64.0.0/10")}})
	for ip, want := range map[string]bool{
		"1.2.3.4": true, "1.2.3.5": false, "1.2.3.3": false, "10.0.0.0": true, "11.255.255.255": true, "12.0.0.0": false,
		"9.255.255.255": false, "100.64.0.1": true, "100.128.0.0": false, "2001:db8::1": true, "2001:db9::1": false,
		"::ffff:10.2.3.4": true, "255.255.255.255": false, "0.0.0.0": false,
	} {
		if got := l.has(netip.MustParseAddr(ip)); got != want {
			t.Errorf("%s: %v, want %v", ip, got, want)
		}
	}
	if len(l.v4) != 3 {
		t.Errorf("10/8 and 11/8 merge, 10.1/16 inside: %v", l.v4)
	}
	if !newAllowList(&Forward{}, nil).has(netip.MustParseAddr("8.8.8.8")) {
		t.Error("an empty allow list takes every client")
	}
}

// A target dials only what its forwards name, for the entry they come from;
// the fast port also checks the entry; self is not a universal fast identity.
func TestForwardTargetDialsOnlyWhatItIsTold(t *testing.T) {
	e := echo(t)
	other := echo(t)
	self := loopback
	entry := netip.MustParseAddr("127.0.0.2")
	c := &ForwardConfig{Port: int(freePort(t).Port()), FastPort: int(freePort(t).Port()),
		Targets: []ForwardTarget{{From: entry, Host: "127.0.0.1", Port: int(e.Port()), Proto: "tcp"},
			{From: self, Host: "127.0.0.1", Port: int(e.Port()), Proto: "udp"},
			{From: entry, Fast: true, Host: "localhost", Port: int(e.Port()), Proto: "tcp"}}}
	if err := c.Check(); err != nil {
		t.Fatal(err)
	}
	var s Forwarder
	s.Apply(c, self)
	defer s.Close()
	open := func(from netip.Addr, port int, cmd byte, a Addr) (net.Conn, byte) {
		d := net.Dialer{LocalAddr: &net.TCPAddr{IP: from.AsSlice()}, Timeout: 2 * time.Second}
		conn, err := d.Dial("tcp", netip.AddrPortFrom(self, uint16(port)).String())
		if err != nil {
			t.Fatal(err)
		}
		conn.Write(tunnelHeader(cmd, a))
		conn.SetReadDeadline(time.Now().Add(3 * time.Second))
		var b [1]byte
		if _, err := io.ReadFull(conn, b[:]); err != nil {
			return conn, 255
		}
		return conn, b[0]
	}
	if conn, st := open(entry, c.Port, cmdConnect, target(e)); st != statusOK {
		t.Fatalf("named target: %d", st)
	} else {
		roundTrip(t, conn, "ok")
		conn.Close()
	}
	for name, try := range map[string]struct {
		from netip.Addr
		port int
		cmd  byte
		a    Addr
	}{
		"another port":              {entry, c.Port, cmdConnect, target(other)},
		"local self is not trusted": {self, c.FastPort, cmdConnect, Addr{Host: "localhost", Port: e.Port()}},
		"udp for a tcp target":      {entry, c.Port, cmdUDP, target(e)},
		"tcp for a udp target":      {self, c.Port, cmdConnect, target(e)},
		"a fast target, mesh port":  {entry, c.Port, cmdConnect, Addr{Host: "localhost", Port: e.Port()}},
	} {
		conn, st := open(try.from, try.port, try.cmd, try.a)
		conn.Close()
		if st == statusOK {
			t.Errorf("%s: served", name)
		}
	}
	for _, from := range []netip.Addr{entry} {
		conn, st := open(from, c.FastPort, cmdConnect, Addr{Host: "localhost", Port: e.Port()})
		if st != statusOK {
			t.Fatalf("fast target from %s: %d", from, st)
		}
		roundTrip(t, conn, "fast")
		conn.Close()
	}
	// A node no forward names gets no connection at all.
	d := net.Dialer{LocalAddr: &net.TCPAddr{IP: net.IPv4(127, 0, 0, 3)}, Timeout: 2 * time.Second}
	if conn, err := d.Dial("tcp", netip.AddrPortFrom(self, uint16(c.Port)).String()); err == nil {
		conn.Write(tunnelHeader(cmdConnect, target(e)))
		if !closed(conn) {
			t.Fatal("a stranger's tunnel stayed open")
		}
		conn.Close()
	}
}

// A fast forward's tunnels open by FastDial, the fast relay's, to the
// target's fast port, a mesh forward's as before; its status names the line
// new connections take and that line's round trip.
func TestFastForwardTunnelsTakeTheFastRelay(t *testing.T) {
	e := echo(t)
	c := &ForwardConfig{Port: int(freePort(t).Port()), FastPort: int(freePort(t).Port()),
		Forwards: []Forward{{ID: "fast", Listen: bothFree(t), Proto: "tcp", Node: loopback, Fast: true, Host: "127.0.0.1", Port: int(e.Port())},
			{ID: "mesh", Listen: bothFree(t), Proto: "tcp", Node: loopback, Host: "127.0.0.1", Port: int(e.Port())}},
		Targets: []ForwardTarget{{From: loopback, Fast: true, Host: "127.0.0.1", Port: int(e.Port()), Proto: "tcp"},
			{From: loopback, Host: "127.0.0.1", Port: int(e.Port()), Proto: "tcp"}}}
	if err := c.Check(); err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	var dialed []netip.AddrPort
	var fastUp atomic.Bool
	fastUp.Store(true)
	s := &Forwarder{
		FastDial: func(ctx context.Context, to netip.AddrPort) (*net.TCPConn, error) {
			mu.Lock()
			dialed = append(dialed, to)
			mu.Unlock()
			var d net.Dialer
			conn, err := d.DialContext(ctx, "tcp", to.String())
			if err != nil {
				return nil, err
			}
			return conn.(*net.TCPConn), nil
		},
		FastRTT: func(to netip.AddrPort) (time.Duration, bool) {
			if to != netip.AddrPortFrom(loopback, uint16(c.FastPort)) || !fastUp.Load() {
				return 0, false
			}
			return 7 * time.Millisecond, true
		},
	}
	s.Apply(c, loopback)
	defer s.Close()
	for _, f := range c.Forwards {
		conn, err := net.DialTimeout("tcp", netip.AddrPortFrom(loopback, uint16(f.Listen)).String(), 2*time.Second)
		if err != nil {
			t.Fatal(err)
		}
		roundTrip(t, conn, "through "+f.ID)
		conn.Close()
	}
	mu.Lock()
	if len(dialed) == 0 || slices.ContainsFunc(dialed, func(a netip.AddrPort) bool { return a != netip.AddrPortFrom(loopback, uint16(c.FastPort)) }) {
		t.Fatalf("fast dials %v", dialed)
	}
	mu.Unlock()
	by := func() map[string]ForwardEntryStatus {
		out := map[string]ForwardEntryStatus{}
		for _, f := range s.Status().Forwards {
			out[f.ID] = f
		}
		return out
	}
	st := by()
	if f := st["fast"]; f.Via != "fast" || f.RTTMs == nil || *f.RTTMs != 7 {
		t.Fatalf("fast forward %+v", f)
	}
	if m := st["mesh"]; m.Via != "" {
		t.Fatalf("mesh forward %+v", m)
	}
	fastUp.Store(false)
	if f := by()["fast"]; f.Via != "mesh" || f.RTTMs != nil {
		t.Fatalf("fast forward without a fast path %+v", f)
	}
}

// A port another program holds is reported and taken once it is free; the
// forward's other listener keeps its clients meanwhile.
func TestForwardRetriesAHeldPort(t *testing.T) {
	e := echo(t)
	port := bothFree(t)
	held, err := net.ListenUDP("udp", &net.UDPAddr{Port: port})
	if err != nil {
		t.Fatal(err)
	}
	s, c := fwdLab(t, Forward{Proto: "any", Listen: port, Host: "127.0.0.1", Port: int(e.Port())}, nil)
	st := s.Status().Forwards[0]
	if len(st.Listen) != 1 || len(st.Errors) != 1 || !strings.HasPrefix(st.Errors[0], "udp: ") {
		t.Fatalf("held: %+v", st)
	}
	conn := dialForward(t, c)
	roundTrip(t, conn, "before")
	held.Close()
	s.Retry()
	if st := s.Status().Forwards[0]; len(st.Listen) != 2 || len(st.Errors) != 0 {
		t.Fatalf("after retry: %+v", st)
	}
	roundTrip(t, conn, "after")
}

func TestForwardConfigCheck(t *testing.T) {
	good := func() ForwardConfig {
		return ForwardConfig{Port: 52004, FastPort: 52005, Sets: map[string][]netip.Prefix{"province:x": nil},
			Forwards: []Forward{{ID: "a", Listen: 443, Proto: "any", Node: netip.MustParseAddr("10.0.0.2"), Host: "127.0.0.1", Port: 443, AllowSets: []string{"province:x"}},
				{ID: "b", Listen: 8443, Proto: "tcp", Node: netip.MustParseAddr("10.0.0.2"), Fast: true, Host: "example.com", Port: 8443, ProxyProtocol: 2}},
			Targets: []ForwardTarget{{From: netip.MustParseAddr("10.0.0.3"), Host: "db.local", Port: 5432, Proto: "tcp"}}}
	}
	c := good()
	if err := c.Check(); err != nil {
		t.Fatal(err)
	}
	for name, spoil := range map[string]func(c *ForwardConfig){
		"same tcp port":       func(c *ForwardConfig) { c.Forwards[1].Listen = 443 },
		"proxy protocol udp":  func(c *ForwardConfig) { c.Forwards[1].Proto = "udp" },
		"proxy protocol 3":    func(c *ForwardConfig) { c.Forwards[1].ProxyProtocol = 3 },
		"fast without a port": func(c *ForwardConfig) { c.FastPort = 0 },
		"unknown set":         func(c *ForwardConfig) { c.Forwards[0].AllowSets = []string{"province:y"} },
		"repeated id":         func(c *ForwardConfig) { c.Forwards[1].ID = "a" },
		"bad host":            func(c *ForwardConfig) { c.Forwards[0].Host = "a b" },
		"bad proto":           func(c *ForwardConfig) { c.Forwards[0].Proto = "sctp" },
		"same tunnel ports":   func(c *ForwardConfig) { c.FastPort = c.Port },
	} {
		c := good()
		spoil(&c)
		if c.Check() == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	c = good()
	c.Forwards[1].Proto, c.Forwards[1].ProxyProtocol, c.Forwards[1].Listen = "udp", 0, 443
	if err := c.Check(); err == nil {
		t.Error("udp 443 is the any forward's too")
	}
	c = good()
	if tcp, udp := c.ListenPorts(); len(tcp) != 2 || len(udp) != 1 || udp[0] != 443 {
		t.Errorf("listen ports %v %v", tcp, udp)
	}
}

func TestProxyHeaderV1IPv6(t *testing.T) {
	h := proxyHeader(1, netip.MustParseAddrPort("[2001:db8::1]:1234"), netip.MustParseAddrPort("10.0.0.1:443"))
	line, _ := bufio.NewReader(bytes.NewReader(h)).ReadString('\n')
	if line != "PROXY TCP6 2001:db8::1 :: 1234 443\r\n" {
		t.Fatalf("%q", line)
	}
}
