package proxy

import (
	"bufio"
	"context"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/netip"
	"os"
	"strings"
	"sync"
	"testing"
	"time"
)

var loopback = netip.MustParseAddr("127.0.0.1")

// echo serves TCP echo on loopback.
func echo(t *testing.T) netip.AddrPort {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func() { io.Copy(c, c); c.Close() }()
		}
	}()
	return netip.MustParseAddrPort(ln.Addr().String())
}

// lab is an exit on loopback, which may dial loopback, and an entry whose
// users lead to it; exits lists the users' exits, the lab's own as "x".
type lab struct {
	exit  *exitServer
	entry *entryServer
	pool  *pool
	port  int
	stats map[string]*userStats
}

func newLab(t *testing.T, e Entry, clients ...netip.Addr) *lab {
	t.Helper()
	if clients == nil {
		clients = []netip.Addr{loopback}
	}
	l := &lab{}
	x, err := startExit(netip.AddrPortFrom(loopback, 0), &Exit{Clients: clients}, nil, &exitStats{}, &localAddrs{}, &conns{})
	if err != nil {
		t.Fatal(err)
	}
	x.allow = func(netip.Addr) bool { return true }
	l.exit, l.port = x, int(netip.MustParseAddrPort(x.ln.Addr().String()).Port())
	t.Cleanup(x.close)
	l.pool = newPool(l.port)
	t.Cleanup(l.pool.close)
	l.stats = map[string]*userStats{}
	for _, u := range e.Users {
		l.stats[u.Name] = &userStats{}
	}
	if e.Listen == nil {
		e.Listen = []netip.AddrPort{netip.AddrPortFrom(loopback, 0)}
	}
	if e.Password == "" && e.Name == "" {
		e.Password = "secret"
	}
	l.entry = startEntry(&e, l.pool, l.stats, &conns{})
	l.pool.warm(e.warmed())
	t.Cleanup(l.entry.close)
	return l
}

func (l *lab) addr() string { return l.entry.listeners[0].Addr().String() }

// socksDial does a SOCKS5 handshake: user/password, or none when user is "".
func socksDial(t *testing.T, proxy, user, password string, cmd byte, target Addr) (net.Conn, byte) {
	c, status, _ := socksBind(t, proxy, user, password, cmd, target)
	return c, status
}

// socksBind is socksDial with the address the reply binds.
func socksBind(t *testing.T, proxy, user, password string, cmd byte, target Addr) (net.Conn, byte, netip.AddrPort) {
	t.Helper()
	c, err := net.Dial("tcp", proxy)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { c.Close() })
	c.SetDeadline(time.Now().Add(10 * time.Second))
	method := byte(0)
	if user != "" {
		method = 2
	}
	c.Write([]byte{5, 1, method})
	var r [2]byte
	if _, err := io.ReadFull(c, r[:]); err != nil {
		t.Fatal(err)
	}
	if r[1] == 0xff {
		return c, 0xff, netip.AddrPort{}
	}
	if method == 2 {
		c.Write(append(append(append([]byte{1, byte(len(user))}, user...), byte(len(password))), password...))
		if _, err := io.ReadFull(c, r[:]); err != nil {
			t.Fatal(err)
		}
		if r[1] != 0 {
			return c, 0xfe, netip.AddrPort{}
		}
	}
	c.Write(appendAddr([]byte{5, cmd, 0}, target))
	var head [3]byte
	if _, err := io.ReadFull(c, head[:]); err != nil {
		return c, 0xfd, netip.AddrPort{}
	}
	bound, err := readAddr(c)
	if err != nil {
		t.Fatal(err)
	}
	c.SetDeadline(time.Time{})
	return c, head[1], netip.AddrPortFrom(bound.IP, bound.Port)
}

func roundTrip(t *testing.T, c net.Conn, msg string) {
	t.Helper()
	c.SetDeadline(time.Now().Add(5 * time.Second))
	if _, err := io.WriteString(c, msg); err != nil {
		t.Fatal(err)
	}
	got := make([]byte, len(msg))
	if _, err := io.ReadFull(c, got); err != nil || string(got) != msg {
		t.Fatalf("echo: %q, %v", got, err)
	}
}

func target(a netip.AddrPort) Addr { return Addr{IP: a.Addr(), Port: a.Port()} }

func TestSOCKS5UserLeadsToItsExit(t *testing.T) {
	e := echo(t)
	l := newLab(t, Entry{Users: []User{{Name: "us", Exits: []netip.Addr{loopback}}}})
	c, status := socksDial(t, l.addr(), "US", "secret", cmdConnect, target(e))
	if status != statusOK {
		t.Fatalf("status %d", status)
	}
	roundTrip(t, c, "hello through the exit")
	c.Close()
	deadline := time.Now().Add(2 * time.Second)
	for l.stats["us"].BytesUp.Load() == 0 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if st := l.stats["us"]; st.Opened.Load() != 1 || st.Failed.Load() != 0 || st.BytesUp.Load() != 22 {
		t.Fatalf("stats opened %d failed %d up %d", st.Opened.Load(), st.Failed.Load(), st.BytesUp.Load())
	}
	// A domain is resolved by the exit.
	c, status = socksDial(t, l.addr(), "us", "secret", cmdConnect, Addr{Host: "localhost", Port: e.Port()})
	if status != statusOK {
		t.Fatalf("domain: status %d", status)
	}
	roundTrip(t, c, "by name")
}

func TestSOCKS5Authentication(t *testing.T) {
	e := echo(t)
	l := newLab(t, Entry{Users: []User{{Name: "us", Exits: []netip.Addr{loopback}}}})
	if _, status := socksDial(t, l.addr(), "us", "wrong", cmdConnect, target(e)); status != 0xfe {
		t.Fatalf("wrong password: %d", status)
	}
	if _, status := socksDial(t, l.addr(), "nobody", "secret", cmdConnect, target(e)); status != 0xfe {
		t.Fatalf("unknown user: %d", status)
	}
	if _, status := socksDial(t, l.addr(), "", "", cmdConnect, target(e)); status != 0xff {
		t.Fatalf("no default user: %d", status)
	}
	d := newLab(t, Entry{Users: []User{{Name: "us", Exits: []netip.Addr{loopback}}}, Default: "us"})
	c, status := socksDial(t, d.addr(), "", "", cmdConnect, target(e))
	if status != statusOK {
		t.Fatalf("default user: %d", status)
	}
	roundTrip(t, c, "anonymous")
}

// A proxy port without a password takes every client as its one user,
// whatever credentials it brings; with one, only its user and password.
func TestEntryWithoutPassword(t *testing.T) {
	e := echo(t)
	l := newLab(t, Entry{Name: "p1", Users: []User{{Name: "us", Exits: []netip.Addr{loopback}}}, Default: "us"})
	for _, who := range [][2]string{{"", ""}, {"anyone", "anything"}} {
		c, status := socksDial(t, l.addr(), who[0], who[1], cmdConnect, target(e))
		if status != statusOK {
			t.Fatalf("%q: status %d", who[0], status)
		}
		roundTrip(t, c, "open port")
		c.Close()
	}
	x := newLab(t, Entry{Name: "exit-eu", Password: "pw", Users: []User{{Name: "exit-eu", Exits: []netip.Addr{loopback}}}})
	if _, status := socksDial(t, x.addr(), "", "", cmdConnect, target(e)); status != 0xff {
		t.Fatalf("no credentials: %d", status)
	}
	if _, status := socksDial(t, x.addr(), "exit-eu", "wrong", cmdConnect, target(e)); status != 0xfe {
		t.Fatalf("wrong password: %d", status)
	}
	c, status := socksDial(t, x.addr(), "exit-eu", "pw", cmdConnect, target(e))
	if status != statusOK {
		t.Fatalf("exit's own: status %d", status)
	}
	roundTrip(t, c, "exit socks")
}

// A Service runs every entry of Entries beside the old Entry, each its own
// listener and status, and stops only the ones that changed.
func TestServiceRunsEntries(t *testing.T) {
	free := func() netip.AddrPort {
		ln, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		defer ln.Close()
		return netip.MustParseAddrPort(ln.Addr().String())
	}
	exit := []netip.Addr{netip.MustParseAddr("127.0.0.2")}
	port := func(name, password string) Entry {
		e := Entry{Name: name, Listen: []netip.AddrPort{free()}, Password: password, Users: []User{{Name: name, Exits: exit}}}
		if password == "" {
			e.Default = name
		}
		return e
	}
	c := &Config{Port: 51080, Entry: &Entry{Listen: []netip.AddrPort{free()}, Password: "pw", Users: []User{{Name: "us", Exits: exit}}},
		Entries: []Entry{port("p1", ""), port("exit-eu", "pw")}}
	if err := c.Check(); err != nil {
		t.Fatal(err)
	}
	var s Service
	defer s.Close()
	s.Apply(c, netip.Addr{}, nil)
	st := s.Status()
	if st == nil || st.Entry == nil || len(st.Entries) != 2 || st.Entries[0].Name != "exit-eu" || st.Entries[1].Name != "p1" {
		t.Fatalf("status %+v", st)
	}
	kept := s.entries["p1"].srv
	c2 := *c
	c2.Entry = nil
	c2.Entries = []Entry{c.Entries[0], port("exit-eu", "new")}
	s.Apply(&c2, netip.Addr{}, nil)
	if s.entries["p1"].srv != kept || s.entries[""] != nil || len(s.entries) != 2 {
		t.Fatalf("entries after a change: %v", s.entries)
	}
	if s.Status().Entry != nil {
		t.Fatal("old entry still reported")
	}
	s.Apply(nil, netip.Addr{}, nil)
	if s.Status() != nil || s.pool != nil {
		t.Fatal("not stopped")
	}
}

// freePort is a loopback port nothing listens on.
func freePort(t *testing.T) netip.AddrPort {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	return netip.MustParseAddrPort(ln.Addr().String())
}

// closed: c was cut, its peer gone.
func closed(c net.Conn) bool {
	c.SetReadDeadline(time.Now().Add(2 * time.Second))
	_, err := c.Read(make([]byte, 1))
	return err != nil && !errors.Is(err, os.ErrDeadlineExceeded)
}

// An exit's tunnels run on through a change of it, but not past the entry
// they came from losing it, nor past a target it may no longer dial.
func TestExitCutsWhatItNoLongerAllows(t *testing.T) {
	e := echo(t)
	port := int(freePort(t).Port())
	var s Service
	defer s.Close()
	exit := func(lan bool, clients ...string) *Config {
		x := &Exit{LAN: lan}
		for _, c := range clients {
			x.Clients = append(x.Clients, netip.MustParseAddr(c))
		}
		return &Config{Port: port, Exit: x}
	}
	tunnel := func() net.Conn {
		t.Helper()
		c, err := net.Dial("tcp", fmt.Sprintf("127.0.0.1:%d", port))
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { c.Close() })
		c.Write(tunnelHeader(cmdConnect, target(e)))
		var st [1]byte
		if _, err := io.ReadFull(c, st[:]); err != nil || st[0] != statusOK {
			t.Fatalf("status %v %v", st, err)
		}
		return c
	}
	s.Apply(exit(true, "127.0.0.1"), loopback, nil)
	a := tunnel()
	roundTrip(t, a, "before")
	s.Apply(exit(true, "127.0.0.1", "100.96.0.9"), loopback, nil)
	roundTrip(t, a, "another entry let in")
	b := tunnel()
	s.Apply(exit(false, "127.0.0.1", "100.96.0.9"), loopback, nil)
	if !closed(a) || !closed(b) {
		t.Fatal("a target no longer allowed is still reached")
	}
	s.Apply(exit(true, "127.0.0.1"), loopback, nil)
	c := tunnel()
	s.Apply(exit(true, "100.96.0.9"), loopback, nil)
	if !closed(c) {
		t.Fatal("an entry no longer allowed still has its tunnel")
	}
}

// A proxy port's clients run on through a change of its exits, but not past
// a new password or the port closing; past maxConns a client is refused.
func TestEntryCutsClientsPastANewPassword(t *testing.T) {
	e := echo(t)
	l := newLab(t, Entry{Users: []User{{Name: "us", Exits: []netip.Addr{loopback}}}})
	var s Service
	defer s.Close()
	listen := freePort(t)
	entry := func(password string, exits ...netip.Addr) *Config {
		return &Config{Port: l.port, Entries: []Entry{{Name: "p1", Listen: []netip.AddrPort{listen}, Password: password, Users: []User{{Name: "us", Exits: exits}}}}}
	}
	client := func(password string) net.Conn {
		t.Helper()
		c, status := socksDial(t, listen.String(), "us", password, cmdConnect, target(e))
		if status != statusOK {
			t.Fatalf("status %d", status)
		}
		return c
	}
	s.Apply(entry("pw", loopback), loopback, nil)
	a := client("pw")
	s.Apply(entry("pw", loopback, netip.MustParseAddr("127.0.0.2")), loopback, nil)
	roundTrip(t, a, "other exits")
	s.Apply(entry("new", loopback), loopback, nil)
	if !closed(a) {
		t.Fatal("a client of the old password still connected")
	}
	b := client("new")
	s.Apply(nil, loopback, nil)
	if !closed(b) {
		t.Fatal("a client of a closed port still connected")
	}

	s.Apply(entry("pw", loopback), loopback, nil)
	o := s.entries["p1"].open
	o.mu.Lock()
	o.max = 1
	o.mu.Unlock()
	client("pw")
	c, err := net.Dial("tcp", listen.String())
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	if !closed(c) || s.Status().Entries[0].Rejected != 1 {
		t.Fatal("a client past the limit taken")
	}
}

// A user's exit that cannot be reached gives way to its backup.
func TestUserFallsBackToItsBackupExit(t *testing.T) {
	e := echo(t)
	dead := netip.MustParseAddr("127.0.0.3")
	l := newLab(t, Entry{Users: []User{{Name: "us", Exits: []netip.Addr{dead, loopback}}}})
	for i := range 3 {
		c, status := socksDial(t, l.addr(), "us", "secret", cmdConnect, target(e))
		if status != statusOK {
			t.Fatalf("connection %d: status %d", i, status)
		}
		roundTrip(t, c, fmt.Sprintf("via the backup %d", i))
		if got := l.stats["us"].exit.Load(); got == nil || *got != loopback {
			t.Fatalf("connection %d went to %v", i, got)
		}
	}
	if !l.pool.down(dead) || l.pool.down(loopback) {
		t.Fatal("the dead exit is not marked down, or the live one is")
	}
}

// Strict entries answer with the exit's error; fast ones answer at once and
// cut the client off when the exit fails.
func TestStrictAndFastAnswers(t *testing.T) {
	ln, _ := net.Listen("tcp", "127.0.0.1:0")
	closed := netip.MustParseAddrPort(ln.Addr().String())
	ln.Close()
	users := []User{{Name: "us", Exits: []netip.Addr{loopback}}}
	strict := newLab(t, Entry{Users: users, Strict: true})
	if _, status := socksDial(t, strict.addr(), "us", "secret", cmdConnect, target(closed)); status != statusRefused {
		t.Fatalf("strict: status %d, want %d", status, statusRefused)
	}
	fast := newLab(t, Entry{Users: users})
	c, status := socksDial(t, fast.addr(), "us", "secret", cmdConnect, target(closed))
	if status != statusOK {
		t.Fatalf("fast: status %d", status)
	}
	c.SetReadDeadline(time.Now().Add(5 * time.Second))
	if n, err := c.Read(make([]byte, 1)); n != 0 || err == nil {
		t.Fatalf("fast: read %d, %v; want the connection closed", n, err)
	}
	deadline := time.Now().Add(2 * time.Second)
	for fast.stats["us"].Failed.Load() == 0 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if fast.stats["us"].Failed.Load() != 1 {
		t.Fatal("the failure is not counted")
	}
}

func TestHTTPProxy(t *testing.T) {
	e := echo(t)
	web, _ := net.Listen("tcp", "127.0.0.1:0")
	defer web.Close()
	go http.Serve(web, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, "path %s host %s auth %q", r.URL.Path, r.Host, r.Header.Get("Proxy-Authorization"))
	}))
	l := newLab(t, Entry{Users: []User{{Name: "us", Exits: []netip.Addr{loopback}}}})
	auth := "Basic " + base64.StdEncoding.EncodeToString([]byte("us:secret"))

	c, _ := net.Dial("tcp", l.addr())
	defer c.Close()
	c.SetDeadline(time.Now().Add(5 * time.Second))
	fmt.Fprintf(c, "CONNECT %s HTTP/1.1\r\nHost: %s\r\nProxy-Authorization: %s\r\n\r\n", e, e, auth)
	br := bufio.NewReader(c)
	resp, err := http.ReadResponse(br, nil)
	if err != nil || resp.StatusCode != 200 {
		t.Fatalf("CONNECT: %v %v", resp, err)
	}
	io.WriteString(c, "tunnelled")
	got := make([]byte, 9)
	if _, err := io.ReadFull(br, got); err != nil || string(got) != "tunnelled" {
		t.Fatalf("CONNECT echo %q %v", got, err)
	}

	p, _ := net.Dial("tcp", l.addr())
	defer p.Close()
	p.SetDeadline(time.Now().Add(5 * time.Second))
	fmt.Fprintf(p, "GET http://%s/hello HTTP/1.1\r\nHost: %s\r\nProxy-Authorization: %s\r\n\r\n", web.Addr(), web.Addr(), auth)
	resp, err = http.ReadResponse(bufio.NewReader(p), nil)
	if err != nil || resp.StatusCode != 200 {
		t.Fatalf("GET: %v %v", resp, err)
	}
	body, _ := io.ReadAll(resp.Body)
	if want := fmt.Sprintf("path /hello host %s auth \"\"", web.Addr()); string(body) != want {
		t.Fatalf("GET body %q, want %q", body, want)
	}

	n, _ := net.Dial("tcp", l.addr())
	defer n.Close()
	n.SetDeadline(time.Now().Add(5 * time.Second))
	fmt.Fprintf(n, "CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n", e, e)
	if resp, err := http.ReadResponse(bufio.NewReader(n), nil); err != nil || resp.StatusCode != 407 {
		t.Fatalf("no credentials: %v %v", resp, err)
	}
}

// A plain request's upload counts like a tunnel's.
func TestHTTPProxyCountsWhatGoesUp(t *testing.T) {
	web, _ := net.Listen("tcp", "127.0.0.1:0")
	defer web.Close()
	go http.Serve(web, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.Copy(io.Discard, r.Body)
		io.WriteString(w, "ok")
	}))
	l := newLab(t, Entry{Users: []User{{Name: "us", Exits: []netip.Addr{loopback}}}})
	auth := "Basic " + base64.StdEncoding.EncodeToString([]byte("us:secret"))
	body := strings.Repeat("x", 4000)
	p, _ := net.Dial("tcp", l.addr())
	defer p.Close()
	p.SetDeadline(time.Now().Add(5 * time.Second))
	fmt.Fprintf(p, "POST http://%s/up HTTP/1.1\r\nHost: %s\r\nProxy-Authorization: %s\r\nContent-Length: %d\r\n\r\n%s", web.Addr(), web.Addr(), auth, len(body), body)
	if resp, err := http.ReadResponse(bufio.NewReader(p), nil); err != nil || resp.StatusCode != 200 {
		t.Fatalf("POST: %v %v", resp, err)
	}
	deadline := time.Now().Add(2 * time.Second)
	for l.stats["us"].BytesUp.Load() < int64(len(body)) && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if up := l.stats["us"].BytesUp.Load(); up < int64(len(body)) {
		t.Fatalf("up %d, want at least the %d byte body", up, len(body))
	}
}

// Families alternate after the first IPv4, so a dead family costs one try.
func TestExitAlternatesFamilies(t *testing.T) {
	var in []netip.Addr
	for _, s := range []string{"192.0.2.1", "192.0.2.2", "192.0.2.3", "2001:db8::1", "2001:db8::2"} {
		in = append(in, netip.MustParseAddr(s))
	}
	got := fmt.Sprint(interleave(in))
	if want := "[192.0.2.1 2001:db8::1 192.0.2.2 2001:db8::2 192.0.2.3]"; got != want {
		t.Fatalf("order %s, want %s", got, want)
	}
	if got := fmt.Sprint(interleave(in[3:])); got != "[2001:db8::1 2001:db8::2]" {
		t.Fatalf("IPv6 only %s", got)
	}
}

func TestUDPAssociate(t *testing.T) {
	pc, _ := net.ListenUDP("udp", net.UDPAddrFromAddrPort(netip.AddrPortFrom(loopback, 0)))
	defer pc.Close()
	go func() {
		buf := make([]byte, 2048)
		for {
			n, from, err := pc.ReadFromUDPAddrPort(buf)
			if err != nil {
				return
			}
			pc.WriteToUDPAddrPort(append([]byte("re:"), buf[:n]...), from)
		}
	}()
	server := netip.MustParseAddrPort(pc.LocalAddr().String())
	l := newLab(t, Entry{Users: []User{{Name: "us", Exits: []netip.Addr{loopback}}}})
	// Two associations at once share the listener's own port, each its datagrams.
	for i, word := range []string{"ping", "pong"} {
		ctl, status, bound := socksBind(t, l.addr(), "us", "secret", cmdUDP, Addr{IP: netip.IPv4Unspecified()})
		if status != statusOK {
			t.Fatalf("status %d", status)
		}
		defer ctl.Close()
		if bound.String() != l.addr() {
			t.Fatalf("association %d bound at %v, not the listener's %s", i, bound, l.addr())
		}
		client, _ := net.ListenUDP("udp", net.UDPAddrFromAddrPort(netip.AddrPortFrom(loopback, 0)))
		defer client.Close()
		msg := appendAddr([]byte{0, 0, 0}, target(server))
		client.WriteToUDPAddrPort(append(msg, word...), bound)
		client.SetReadDeadline(time.Now().Add(5 * time.Second))
		buf := make([]byte, 2048)
		n, _, err := client.ReadFromUDPAddrPort(buf)
		if err != nil {
			t.Fatal(err)
		}
		from, payload, err := parseAddr(buf[3:n])
		if err != nil || string(payload) != "re:"+word || from.IP != server.Addr() || from.Port != server.Port() {
			t.Fatalf("got %v %q %v", from, payload, err)
		}
	}
}

// An exit takes tunnels only from its clients' addresses.
func TestExitRefusesOtherAddresses(t *testing.T) {
	e := echo(t)
	l := newLab(t, Entry{Users: []User{{Name: "us", Exits: []netip.Addr{loopback}}}, Strict: true}, netip.MustParseAddr("127.0.0.9"))
	if _, status := socksDial(t, l.addr(), "us", "secret", cmdConnect, target(e)); status == statusOK {
		t.Fatal("a tunnel from an address not among the clients went through")
	}
	if l.exit.stats.Refused.Load() == 0 {
		t.Fatal("the refusal is not counted")
	}
}

func TestExitPolicy(t *testing.T) {
	mesh := []netip.Prefix{netip.MustParsePrefix("100.96.0.0/16")}
	closed := &policy{mesh: mesh, self: &localAddrs{}}
	open := &policy{lan: true, mesh: mesh, self: &localAddrs{}}
	for _, c := range []struct {
		addr         string
		closed, open bool
	}{
		{"1.1.1.1", true, true}, {"2606:4700::1111", true, true},
		{"10.1.2.3", false, true}, {"192.168.1.1", false, true}, {"172.20.0.1", false, true}, {"100.64.1.1", false, true},
		{"127.0.0.1", false, true}, {"::1", false, true}, {"fd00::1", false, true}, {"169.254.1.1", false, true},
		{"100.96.0.5", false, false}, {"224.0.0.1", false, false}, {"0.0.0.0", false, false}, {"255.255.255.255", false, false},
	} {
		a := netip.MustParseAddr(c.addr)
		if closed.allowed(a) != c.closed || open.allowed(a) != c.open {
			t.Errorf("%s: allowed %v without LAN and %v with it, want %v and %v", a, closed.allowed(a), open.allowed(a), c.closed, c.open)
		}
	}
}

// Users' first exits get tunnels opened before any client asks.
func TestPoolWarmsFirstExits(t *testing.T) {
	l := newLab(t, Entry{Users: []User{{Name: "us", Exits: []netip.Addr{loopback}}}})
	deadline := time.Now().Add(3 * time.Second)
	for l.pool.status(loopback).Ready < poolMin && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if got := l.pool.status(loopback).Ready; got < poolMin {
		t.Fatalf("%d tunnels ready, want %d", got, poolMin)
	}
	// One taken is replaced.
	c, err := l.pool.get(loopback)
	if err != nil {
		t.Fatal(err)
	}
	c.Close()
	deadline = time.Now().Add(3 * time.Second)
	for l.pool.status(loopback).Ready < poolMin && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if got := l.pool.status(loopback).Ready; got < poolMin {
		t.Fatalf("%d tunnels ready after one was taken", got)
	}
}

// stubPool dials a listener that holds what it accepts, whatever the exit,
// taking delay per dial; it reports the most dials ever under way at once.
func stubPool(t *testing.T, delay time.Duration) (*pool, func() int) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	var held []net.Conn
	var now, most int
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			mu.Lock()
			held = append(held, c)
			mu.Unlock()
		}
	}()
	p := newPool(0)
	p.dial = func(ctx context.Context, _ netip.Addr) (*net.TCPConn, error) {
		mu.Lock()
		now++
		most = max(most, now)
		mu.Unlock()
		time.Sleep(delay)
		mu.Lock()
		now--
		mu.Unlock()
		c, err := (&net.Dialer{}).DialContext(ctx, "tcp", ln.Addr().String())
		if err != nil {
			return nil, err
		}
		return c.(*net.TCPConn), nil
	}
	t.Cleanup(func() {
		p.close()
		ln.Close()
		mu.Lock()
		defer mu.Unlock()
		for _, c := range held {
			c.Close()
		}
	})
	return p, func() int { mu.Lock(); defer mu.Unlock(); return most }
}

func waitReady(t *testing.T, p *pool, a netip.Addr, n int) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for p.status(a).Ready < n && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if got := p.status(a).Ready; got < n {
		t.Fatalf("%s: %d tunnels ready, want %d", a, got, n)
	}
}

// Missing tunnels are dialed together, so refilling takes one dial's time.
func TestPoolFillsAtOnce(t *testing.T) {
	p, most := stubPool(t, 100*time.Millisecond)
	a := netip.MustParseAddr("100.96.0.1")
	p.warm([]netip.Addr{a})
	waitReady(t, p, a, poolMin)
	if most() != poolMin {
		t.Fatalf("%d dials at once, want %d", most(), poolMin)
	}
}

// A user's first exit keeps its tunnels however long nobody uses it; an exit
// only failed over to lets them go.
func TestPoolKeepsFirstExits(t *testing.T) {
	p, _ := stubPool(t, 0)
	first, backup := netip.MustParseAddr("100.96.0.1"), netip.MustParseAddr("100.96.0.2")
	p.warm([]netip.Addr{first})
	c, err := p.get(backup)
	if err != nil {
		t.Fatal(err)
	}
	c.Close()
	waitReady(t, p, first, poolMin)
	waitReady(t, p, backup, poolMin)
	p.mu.Lock()
	for _, e := range p.exits {
		e.used = time.Now().Add(-poolIdle - time.Second)
	}
	p.mu.Unlock()
	p.tidy()
	if got := p.status(first).Ready; got < poolMin {
		t.Fatalf("first exit: %d tunnels ready after a quiet spell, want %d", got, poolMin)
	}
	if got := p.status(backup).Ready; got != 0 {
		t.Fatalf("backup exit: %d tunnels ready after a quiet spell, want none", got)
	}
}

func TestTunnelHeaderRoundTrip(t *testing.T) {
	for _, a := range []Addr{{IP: loopback, Port: 80}, {IP: netip.MustParseAddr("2001:db8::1"), Port: 443}, {Host: "example.com", Port: 8443}} {
		cmd, got, err := readHeader(strings.NewReader(string(tunnelHeader(cmdConnect, a)) + "rest"))
		if err != nil || cmd != cmdConnect || got != a {
			t.Fatalf("%v: got %v %v %v", a, cmd, got, err)
		}
	}
	var frame strings.Builder
	writeFrame(&frame, Addr{IP: loopback, Port: 53}, []byte("query"))
	if binary.BigEndian.Uint16([]byte(frame.String())) != uint16(frame.Len()-2) {
		t.Fatal("frame length")
	}
	a, payload, err := readFrame(strings.NewReader(frame.String()), make([]byte, 100))
	if err != nil || a.Port != 53 || string(payload) != "query" {
		t.Fatalf("frame: %v %q %v", a, payload, err)
	}
}

func TestConfigCheck(t *testing.T) {
	ok := Config{Port: 51080, Entry: &Entry{Listen: []netip.AddrPort{netip.MustParseAddrPort("127.0.0.1:1080")}, Password: "p",
		Users: []User{{Name: "us", Exits: []netip.Addr{netip.MustParseAddr("100.96.0.2")}}}, Default: "us"}, Exit: &Exit{}}
	if err := ok.Check(); err != nil {
		t.Fatal(err)
	}
	open := Config{Port: 51080, Entries: []Entry{{Name: "p", Listen: ok.Entry.Listen, Users: ok.Entry.Users, Default: "us"}}}
	if err := open.Check(); err != nil {
		t.Fatal(err)
	}
	for name, bad := range map[string]func(c *Config){
		"port":      func(c *Config) { c.Port = 0 },
		"no listen": func(c *Config) { c.Entry.Listen = nil },
		"password":  func(c *Config) { c.Entry.Password = "" },
		"name":      func(c *Config) { c.Entry.Users[0].Name = "Bad Name" },
		"no exits":  func(c *Config) { c.Entry.Users[0].Exits = nil },
		"default":   func(c *Config) { c.Entry.Default = "jp" },
		"open on the LAN": func(c *Config) {
			c.Entries = []Entry{{Name: "p", Listen: []netip.AddrPort{netip.MustParseAddrPort("192.168.1.10:1080")}, Users: c.Entry.Users, Default: "us"}}
		},
		"default on the LAN": func(c *Config) {
			c.Entry.Listen = append(c.Entry.Listen, netip.MustParseAddrPort("192.168.1.10:1080"))
		},
		"open port without default": func(c *Config) {
			c.Entries = []Entry{{Name: "p", Listen: c.Entry.Listen, Users: c.Entry.Users}}
		},
		"port name": func(c *Config) {
			c.Entries = []Entry{{Name: "", Listen: c.Entry.Listen, Password: "p", Users: c.Entry.Users}}
		},
	} {
		c := ok
		e := *ok.Entry
		e.Users = []User{{Name: "us", Exits: []netip.Addr{netip.MustParseAddr("100.96.0.2")}}}
		c.Entry = &e
		bad(&c)
		if c.Check() == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}
