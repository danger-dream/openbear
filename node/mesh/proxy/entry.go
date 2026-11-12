package proxy

import (
	"bufio"
	"cmp"
	"crypto/subtle"
	"encoding/base64"
	"errors"
	"io"
	"net"
	"net/http"
	"net/netip"
	"slices"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// handshakeWait bounds a client's SOCKS5 or HTTP handshake.
const handshakeWait = 30 * time.Second

// strictWait is how long a strict entry waits for the exit's answer.
const strictWait = 15 * time.Second

// A latency user's exit: looked at every pickEvery; another is taken only
// once it has been the lowest by pickMargin of the current one's round trip,
// and at least pickMarginMin, for pickRounds looks in a row, so two exits
// about as near do not take turns. One that fails is left at once.
const (
	pickEvery     = 5 * time.Second
	pickRounds    = 3
	pickMargin    = 0.15
	pickMarginMin = 10 * time.Millisecond
)

type latencyPick struct {
	current, candidate netip.Addr
	streak             int
}

type userStats struct {
	Opened, Failed, Active, BytesUp, BytesDown atomic.Int64
	// exit is the address of the exit the user's last tunnel went to.
	exit atomic.Pointer[netip.Addr]
}

// entryServer takes SOCKS5 and HTTP proxy clients on its listeners.
type entryServer struct {
	cfg       *Entry
	pool      *pool
	listeners []net.Listener
	hubs      map[netip.AddrPort]*udpHub
	errs      []string
	stats     map[string]*userStats
	wg        sync.WaitGroup
	rejected  atomic.Int64
	mu        sync.Mutex
	picks     map[string]*latencyPick
	stop      chan struct{}
	conns     *conns
}

// startEntry takes clients for e; open ones are counted in open, which the
// entry that takes over from it may go on with.
func startEntry(e *Entry, p *pool, stats map[string]*userStats, open *conns) *entryServer {
	s := &entryServer{cfg: e, pool: p, stats: stats, hubs: map[netip.AddrPort]*udpHub{}, picks: map[string]*latencyPick{}, stop: make(chan struct{}),
		conns: open}
	for _, l := range e.Listen {
		ln, err := net.Listen("tcp", l.String())
		if err != nil {
			s.errs = append(s.errs, err.Error())
			continue
		}
		s.listeners = append(s.listeners, ln)
		if h := startHub(ln.Addr()); h != nil {
			s.hubs[netip.MustParseAddrPort(ln.Addr().String())] = h
		}
		s.wg.Add(1)
		go s.accept(ln)
	}
	if slices.ContainsFunc(e.Users, func(u User) bool { return u.Select == selectLatency }) {
		s.wg.Add(1)
		go s.pickLoop()
	}
	return s
}

// warmed are the exits to keep tunnels ready to: each user's first, and
// every exit of a latency user, opening them being what measures its round
// trip.
func (e *Entry) warmed() []netip.Addr {
	out := make([]netip.Addr, 0, len(e.Users))
	for _, u := range e.Users {
		if u.Select == selectLatency {
			out = append(out, u.Exits...)
		} else {
			out = append(out, u.Exits[0])
		}
	}
	return out
}

func (s *entryServer) close() {
	close(s.stop)
	for _, ln := range s.listeners {
		ln.Close()
	}
	for _, h := range s.hubs {
		h.close()
	}
	s.wg.Wait()
}

func (s *entryServer) pickLoop() {
	defer s.wg.Done()
	t := time.NewTicker(pickEvery)
	defer t.Stop()
	for {
		s.pick()
		select {
		case <-s.stop:
			return
		case <-t.C:
		}
	}
}

// pick looks again at each latency user's exits (see pickEvery).
func (s *entryServer) pick() {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, u := range s.cfg.Users {
		if u.Select != selectLatency {
			continue
		}
		var best netip.Addr
		var bestRTT time.Duration
		for _, a := range u.Exits {
			if rtt, ok := s.pool.latency(a); ok && !s.pool.down(a) && (!best.IsValid() || rtt < bestRTT) {
				best, bestRTT = a, rtt
			}
		}
		lp := s.picks[u.Name]
		if lp == nil {
			lp = &latencyPick{}
			s.picks[u.Name] = lp
		}
		if !best.IsValid() {
			continue
		}
		curRTT, ok := s.pool.latency(lp.current)
		switch {
		case !ok || s.pool.down(lp.current) || !slices.Contains(u.Exits, lp.current):
			lp.current, lp.streak = best, 0
		case best != lp.current && curRTT-bestRTT >= max(pickMarginMin, time.Duration(float64(curRTT)*pickMargin)):
			if lp.candidate != best {
				lp.candidate, lp.streak = best, 0
			}
			if lp.streak++; lp.streak >= pickRounds {
				lp.current, lp.streak = best, 0
			}
		default:
			lp.streak = 0
		}
	}
}

// picked is a latency user's exit now, if one has been picked.
func (s *entryServer) picked(name string) (netip.Addr, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if lp := s.picks[name]; lp != nil && lp.current.IsValid() {
		return lp.current, true
	}
	return netip.Addr{}, false
}

func (s *entryServer) accept(ln net.Listener) {
	defer s.wg.Done()
	for {
		c, err := ln.Accept()
		if err != nil {
			if errors.Is(err, net.ErrClosed) {
				return
			}
			time.Sleep(50 * time.Millisecond)
			continue
		}
		from, _ := netip.ParseAddrPort(c.RemoteAddr().String())
		if !s.conns.add(c, from.Addr().Unmap()) {
			s.rejected.Add(1)
			c.Close()
			continue
		}
		go s.serve(c.(*net.TCPConn))
	}
}

func (s *entryServer) serve(c *net.TCPConn) {
	defer s.conns.done(c)
	defer c.Close()
	c.SetNoDelay(true)
	c.SetDeadline(time.Now().Add(handshakeWait))
	br := bufio.NewReaderSize(c, 4096)
	first, err := br.Peek(1)
	if err != nil {
		return
	}
	if first[0] == 5 {
		s.socks(c, br)
	} else {
		s.http(c, br)
	}
}

// login is the user a username and password stand for; nil when they do
// not match. Without credentials it is the default user, if any.
func (s *entryServer) login(name, password string, given bool) *User {
	if !given || s.cfg.Password == "" {
		if s.cfg.Default == "" {
			return nil
		}
		return s.cfg.user(s.cfg.Default)
	}
	if subtle.ConstantTimeCompare([]byte(password), []byte(s.cfg.Password)) != 1 {
		return nil
	}
	return s.cfg.user(strings.ToLower(name))
}

// open sends the header down a tunnel to the user's first exit that takes
// it, the exits that failed lately last; a latency user's picked exit first,
// the others by round trip.
func (s *entryServer) open(u *User, cmd byte, target Addr) (*net.TCPConn, error) {
	exits := u.Exits
	if u.Select == selectLatency {
		exits = s.byLatency(u)
	}
	order := make([]netip.Addr, 0, len(exits))
	var later []netip.Addr
	for _, a := range exits {
		if s.pool.down(a) {
			later = append(later, a)
		} else {
			order = append(order, a)
		}
	}
	err := errors.New("no exit")
	for _, a := range append(order, later...) {
		var c *net.TCPConn
		if c, err = s.pool.get(a); err != nil {
			continue
		}
		if _, err = c.Write(tunnelHeader(cmd, target)); err != nil {
			c.Close()
			continue
		}
		if st := s.stats[u.Name]; st != nil {
			st.exit.Store(&a)
		}
		return c, nil
	}
	return nil, err
}

func (s *entryServer) byLatency(u *User) []netip.Addr {
	out := slices.Clone(u.Exits)
	rtt := func(a netip.Addr) time.Duration {
		if d, ok := s.pool.latency(a); ok {
			return d
		}
		return time.Hour
	}
	// Stable: exits not measured yet keep their order, after those that are.
	slices.SortStableFunc(out, func(a, b netip.Addr) int { return cmp.Compare(rtt(a), rtt(b)) })
	if p, ok := s.picked(u.Name); ok {
		if i := slices.Index(out, p); i > 0 {
			out = append([]netip.Addr{p}, slices.Delete(out, i, i+1)...)
		}
	}
	return out
}

// waitStatus reads the exit's answer within strictWait.
func waitStatus(t *net.TCPConn) byte {
	t.SetReadDeadline(time.Now().Add(strictWait))
	defer t.SetReadDeadline(time.Time{})
	var b [1]byte
	if _, err := io.ReadFull(t, b[:]); err != nil {
		return statusFailure
	}
	return b[0]
}

// pipe relays a client and its tunnel. ahead is what the client sent that
// the handshake already read. answered: the exit's status byte is still to
// come, ahead of the target's data; the client is cut off if it is an error.
func (s *entryServer) pipe(u *User, client *net.TCPConn, ahead []byte, t *net.TCPConn, answered bool) {
	st := s.stats[u.Name]
	st.Active.Add(1)
	defer st.Active.Add(-1)
	client.SetDeadline(time.Time{})
	if len(ahead) > 0 {
		if _, err := t.Write(ahead); err != nil {
			return
		}
	}
	down := make(chan int64, 1)
	go func() {
		if !answered {
			if waitStatusForever(t) != statusOK {
				st.Failed.Add(1)
				client.Close()
				t.Close()
				down <- 0
				return
			}
		}
		n, _ := io.Copy(client, t)
		client.CloseWrite()
		down <- n
	}()
	up, _ := io.Copy(t, client)
	t.CloseWrite()
	st.BytesUp.Add(up + int64(len(ahead)))
	st.BytesDown.Add(<-down)
}

func waitStatusForever(t *net.TCPConn) byte {
	var b [1]byte
	if _, err := io.ReadFull(t, b[:]); err != nil {
		return statusFailure
	}
	return b[0]
}

// ----- SOCKS5 -----

func (s *entryServer) socks(c *net.TCPConn, br *bufio.Reader) {
	var head [2]byte
	if _, err := io.ReadFull(br, head[:]); err != nil {
		return
	}
	methods := make([]byte, head[1])
	if _, err := io.ReadFull(br, methods); err != nil {
		return
	}
	var u *User
	switch {
	case containsByte(methods, 0) && s.cfg.Password == "":
		u = s.login("", "", false)
		c.Write([]byte{5, 0})
	case containsByte(methods, 2):
		c.Write([]byte{5, 2})
		name, password, ok := readUserPass(br)
		if u = s.login(name, password, true); !ok || u == nil {
			s.rejected.Add(1)
			c.Write([]byte{1, 1})
			return
		}
		c.Write([]byte{1, 0})
	case containsByte(methods, 0) && s.cfg.Default != "":
		u = s.login("", "", false)
		c.Write([]byte{5, 0})
	default:
		s.rejected.Add(1)
		c.Write([]byte{5, 0xff})
		return
	}
	var req [3]byte
	if _, err := io.ReadFull(br, req[:]); err != nil || req[0] != 5 {
		return
	}
	target, err := readAddr(br)
	if err != nil {
		socksReply(c, statusAddressType, netip.AddrPort{})
		return
	}
	st := s.stats[u.Name]
	switch req[1] {
	case cmdConnect:
		st.Opened.Add(1)
		t, err := s.open(u, cmdConnect, target)
		if err != nil {
			st.Failed.Add(1)
			socksReply(c, statusNetwork, netip.AddrPort{})
			return
		}
		defer t.Close()
		answered := false
		if s.cfg.Strict {
			if status := waitStatus(t); status != statusOK {
				st.Failed.Add(1)
				socksReply(c, status, netip.AddrPort{})
				return
			}
			answered = true
		}
		if socksReply(c, statusOK, netip.AddrPort{}) != nil {
			return
		}
		ahead, _ := br.Peek(br.Buffered())
		s.pipe(u, c, ahead, t, answered)
	case cmdUDP:
		st.Opened.Add(1)
		s.socksUDP(c, u)
	default:
		socksReply(c, statusCommand, netip.AddrPort{})
	}
}

func containsByte(b []byte, x byte) bool {
	for _, v := range b {
		if v == x {
			return true
		}
	}
	return false
}

// readUserPass reads RFC 1929's username and password.
func readUserPass(r io.Reader) (string, string, bool) {
	var h [2]byte
	if _, err := io.ReadFull(r, h[:]); err != nil || h[0] != 1 {
		return "", "", false
	}
	name := make([]byte, h[1])
	if _, err := io.ReadFull(r, name); err != nil {
		return "", "", false
	}
	var n [1]byte
	if _, err := io.ReadFull(r, n[:]); err != nil {
		return "", "", false
	}
	password := make([]byte, n[0])
	if _, err := io.ReadFull(r, password); err != nil {
		return "", "", false
	}
	return string(name), string(password), true
}

func socksReply(c net.Conn, status byte, bound netip.AddrPort) error {
	a := Addr{IP: netip.IPv4Unspecified()}
	if bound.IsValid() {
		a = Addr{IP: bound.Addr().Unmap(), Port: bound.Port()}
	}
	_, err := c.Write(appendAddr([]byte{5, status, 0}, a))
	return err
}

// ----- HTTP -----

func (s *entryServer) http(c *net.TCPConn, br *bufio.Reader) {
	req, err := http.ReadRequest(br)
	if err != nil {
		return
	}
	name, password, given := proxyAuth(req.Header.Get("Proxy-Authorization"))
	u := s.login(name, password, given)
	if u == nil {
		s.rejected.Add(1)
		io.WriteString(c, "HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm=\"OpenBear\"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
		return
	}
	connect := req.Method == http.MethodConnect
	hostport := req.Host
	if !connect {
		if req.URL.Scheme != "http" || req.URL.Host == "" {
			io.WriteString(c, "HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
			return
		}
		hostport = req.URL.Host
	}
	target, ok := hostPort(hostport, connect)
	if !ok {
		io.WriteString(c, "HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
		return
	}
	st := s.stats[u.Name]
	st.Opened.Add(1)
	t, err := s.open(u, cmdConnect, target)
	if err != nil {
		st.Failed.Add(1)
		io.WriteString(c, "HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
		return
	}
	defer t.Close()
	answered := false
	if s.cfg.Strict || !connect {
		// A plain request is answered by the target itself; its error is the
		// exit's, as a 502, only before anything came back.
		if status := waitStatus(t); status != statusOK {
			st.Failed.Add(1)
			io.WriteString(c, "HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
			return
		}
		answered = true
	}
	if connect {
		if _, err := io.WriteString(c, "HTTP/1.1 200 Connection established\r\n\r\n"); err != nil {
			return
		}
		ahead, _ := br.Peek(br.Buffered())
		s.pipe(u, c, ahead, t, answered)
		return
	}
	// One request per connection: a client's next one may be for another host.
	req.Header.Del("Proxy-Authorization")
	req.Header.Del("Proxy-Connection")
	req.Close = true
	req.RequestURI, req.URL.Scheme, req.URL.Host = "", "", ""
	c.SetDeadline(time.Time{})
	go func() {
		w := &counted{w: t}
		req.Write(w)
		t.CloseWrite()
		st.BytesUp.Add(w.n)
	}()
	st.Active.Add(1)
	defer st.Active.Add(-1)
	n, _ := io.Copy(c, t)
	st.BytesDown.Add(n)
}

type counted struct {
	w io.Writer
	n int64
}

func (c *counted) Write(p []byte) (int, error) {
	n, err := c.w.Write(p)
	c.n += int64(n)
	return n, err
}

// proxyAuth reads Basic credentials; given is false without any.
func proxyAuth(h string) (name, password string, given bool) {
	scheme, value, ok := strings.Cut(h, " ")
	if !ok || !strings.EqualFold(scheme, "Basic") {
		return "", "", false
	}
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(value))
	if err != nil {
		return "", "", true
	}
	name, password, _ = strings.Cut(string(raw), ":")
	return name, password, true
}

// hostPort reads host:port, the port defaulting to 80 for a plain request.
func hostPort(s string, connect bool) (Addr, bool) {
	host, port, err := net.SplitHostPort(s)
	if err != nil {
		if connect {
			return Addr{}, false
		}
		host, port = strings.Trim(s, "[]"), "80"
	}
	p, err := strconv.ParseUint(port, 10, 16)
	if err != nil || p == 0 || host == "" || len(host) > 255 {
		return Addr{}, false
	}
	if ip, err := netip.ParseAddr(host); err == nil {
		return Addr{IP: ip.Unmap(), Port: uint16(p)}, true
	}
	return Addr{Host: host, Port: uint16(p)}, true
}
