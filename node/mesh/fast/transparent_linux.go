//go:build linux

package fast

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"golang.org/x/sys/unix"

	"openbear/node/mesh/bypass"
)

var (
	transparentCmd  = runSystemCmd
	transparentLook = lookSystemBin
)

func runSystemCmd(stdin string, args ...string) (string, error) {
	path, err := lookSystemBin(args[0])
	if err != nil {
		return "", err
	}
	cmd := exec.Command(path, args[1:]...)
	if stdin != "" {
		cmd.Stdin = strings.NewReader(stdin)
	}
	var stderr strings.Builder
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		msg := strings.TrimSpace(stderr.String())
		if msg == "" {
			msg = err.Error()
		}
		return string(out), fmt.Errorf("%s: %s", args[0], msg)
	}
	return string(out), nil
}

func lookSystemBin(name string) (string, error) {
	if p, err := exec.LookPath(name); err == nil {
		return p, nil
	}
	for _, dir := range []string{"/usr/local/sbin", "/usr/sbin", "/sbin", "/usr/bin", "/bin"} {
		p := filepath.Join(dir, name)
		if st, err := os.Stat(p); err == nil && !st.IsDir() {
			return p, nil
		}
	}
	return "", fmt.Errorf("%s not found", name)
}

// captureRunner takes this machine's and its LAN's connections to the fast
// ports whose channel is ready, with an nftables table of its own: TCP is
// redirected to two listeners (local, LAN), UDP sent by TPROXY to a third.
type captureRunner struct {
	svc        *Service
	sessions   *udpSessionTable // immutable capture lifetime, not Service's next table
	opMu       sync.Mutex       // serializes kernel transactions, healing and final removal
	lnLocalTCP net.Listener
	lnLANTCP   net.Listener
	udpConn    *net.UDPConn
	portLocal  int
	portLAN    int
	portUDP    int
	udpScope   uint64
	own        ownAddrs

	mu      sync.Mutex
	active  bool
	udpOn   bool
	err     string
	missing string
	tcp     []string // the ready sets' elements
	udp     []string

	wg     sync.WaitGroup
	closed bool
}

func (s *Service) applyCaptureLocked(c *Config) {
	if !c.Capture {
		if s.capture != nil {
			s.capture.close()
			s.capture = nil
		}
		return
	}
	if s.capture != nil && !s.capture.failed() {
		return
	}
	if s.capture != nil {
		s.capture.close()
		s.capture = nil
	}
	cr := &captureRunner{svc: s, sessions: s.udpSessions, udpScope: s.udpSessions.newScope()}
	cr.start()
	s.capture = cr
}

// failed reports that the capture rules are not in place. Running with TCP
// alone, where the kernel has no UDP TPROXY, is not a failure: retrying would
// only tear down and reinstall the same rules. Nor is a machine without nft
// until nft shows up on it.
func (t *captureRunner) failed() bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.missing != "" {
		_, err := transparentLook(t.missing)
		return err == nil
	}
	return !t.active
}

func (t *captureRunner) status() *CaptureStatus {
	t.mu.Lock()
	defer t.mu.Unlock()
	return &CaptureStatus{
		Mode:    "nft",
		Active:  t.active,
		UDP:     t.udpOn,
		Error:   t.err,
		Missing: t.missing,
	}
}

func (t *captureRunner) start() {
	if _, err := transparentLook("nft"); err != nil {
		t.mu.Lock()
		t.err, t.missing = err.Error(), "nft"
		t.mu.Unlock()
		return
	}
	// Listener A: local output TCP redirect, fixed 256 KB SO_RCVBUF before listen().
	lnA, err := listenFixedTCP("tcp4", "127.0.0.1:0", fixedRcvBuf)
	if err != nil {
		t.setErr(false, false, err.Error())
		return
	}
	// Listener B: LAN prerouting TCP redirect, kernel auto-tuned receive buffer.
	lnB, err := listenFixedTCP("tcp4", "0.0.0.0:0", 0)
	if err != nil {
		_ = lnA.Close()
		t.setErr(false, false, err.Error())
		return
	}
	t.lnLocalTCP = lnA
	t.lnLANTCP = lnB
	t.portLocal = lnA.Addr().(*net.TCPAddr).Port
	t.portLAN = lnB.Addr().(*net.TCPAddr).Port

	udpConn, udpErr := listenTPROXYUDP("127.0.0.1:0")
	if udpErr == nil {
		t.udpConn = udpConn
		t.portUDP = udpConn.LocalAddr().(*net.UDPAddr).Port
	}

	if err := t.installRules(udpErr); err != nil {
		_ = lnA.Close()
		_ = lnB.Close()
		if t.udpConn != nil {
			_ = t.udpConn.Close()
			t.udpConn = nil
		}
		t.lnLocalTCP, t.lnLANTCP = nil, nil
		t.setErr(false, false, err.Error())
		return
	}

	t.wg.Add(2)
	go t.acceptTCP(lnA, false)
	go t.acceptTCP(lnB, true)
	if t.udpConn != nil && t.udpOn {
		t.wg.Add(1)
		go t.serveUDP(t.udpConn)
	} else if t.udpConn != nil {
		_ = t.udpConn.Close()
		t.udpConn = nil
	}
}

func (t *captureRunner) setErr(active, udpOn bool, err string) {
	t.mu.Lock()
	t.active, t.udpOn, t.err = active, udpOn, err
	t.mu.Unlock()
}

func (t *captureRunner) script(udpPort int) string {
	t.mu.Lock()
	tcp, udp := t.tcp, t.udp
	t.mu.Unlock()
	return NFTScript(t.portLocal, t.portLAN, udpPort, tcp, udp)
}

func (t *captureRunner) installRules(udpSockErr error) error {
	if t.portUDP > 0 {
		if err := installUDPRoute(); err == nil {
			if _, err := transparentCmd(t.script(t.portUDP), "nft", "-f", "-"); err == nil {
				t.setErr(true, true, "")
				return nil
			} else {
				removeUDPRoute()
				// Fall back to TCP alone if nft_tproxy is unavailable.
				if _, err2 := transparentCmd(t.script(0), "nft", "-f", "-"); err2 == nil {
					t.setErr(true, false, "UDP TPROXY unavailable: "+err.Error())
					return nil
				} else {
					return err2
				}
			}
		} else {
			udpSockErr = err
		}
	}
	if _, err := transparentCmd(t.script(0), "nft", "-f", "-"); err != nil {
		return err
	}
	msg := ""
	if udpSockErr != nil {
		msg = "UDP TPROXY unavailable: " + udpSockErr.Error()
	}
	t.setErr(true, false, msg)
	return nil
}

// update makes keys the fast ports taken; the rest go over the mesh.
func (t *captureRunner) update(keys []targetKey) {
	t.opMu.Lock()
	defer t.opMu.Unlock()
	tcp, udp := readyElements(keys)
	t.mu.Lock()
	if t.closed || (slices.Equal(tcp, t.tcp) && slices.Equal(udp, t.udp)) {
		t.mu.Unlock()
		return
	}
	t.tcp, t.udp = tcp, udp
	active := t.active
	t.mu.Unlock()
	if active {
		if _, err := transparentCmd(NFTSets(tcp, udp), "nft", "-f", "-"); err != nil {
			t.healLocked()
		}
	}
}

func (t *captureRunner) heal() {
	t.opMu.Lock()
	defer t.opMu.Unlock()
	t.healLocked()
}

func (t *captureRunner) healLocked() {
	t.mu.Lock()
	if t.closed || !t.active {
		t.mu.Unlock()
		return
	}
	udpOn := t.udpOn
	portLocal, portUDP := t.portLocal, t.portUDP
	tcp, udp := t.tcp, t.udp
	t.mu.Unlock()

	out, err := transparentCmd("", "nft", "list", "table", "ip", Table)
	needNFT := err != nil || !strings.Contains(out, setTCP) || !strings.Contains(out, ":"+strconv.Itoa(portLocal)) || !hasElements(out, tcp) || !hasElements(out, udp)
	if udpOn {
		rules, rerr := transparentCmd("", "ip", "-4", "rule", "show")
		routes, terr := transparentCmd("", "ip", "-4", "route", "show", "table", strconv.Itoa(RouteTable))
		if rerr != nil || !hasFastMarkRule(rules) {
			_ = installUDPRoute()
		} else if terr != nil || !strings.Contains(routes, "local") {
			_ = installUDPRoute()
		}
		if !strings.Contains(out, ":"+strconv.Itoa(portUDP)) {
			needNFT = true
		}
	}
	if !needNFT {
		// Reconcile both additions and removals, not just a subset of listing text.
		_, _ = transparentCmd(NFTSets(tcp, udp), "nft", "-f", "-")
	}
	if needNFT {
		uPort := 0
		if udpOn {
			uPort = portUDP
		}
		_, _ = transparentCmd(t.script(uPort), "nft", "-f", "-")
	}
}

// hasElements: every element of list is in nft's listing out, which writes
// "addr . port" as the script does.
func hasElements(out string, list []string) bool {
	for _, e := range list {
		if !strings.Contains(out, e) {
			return false
		}
	}
	return true
}

func (t *captureRunner) close() {
	t.opMu.Lock()
	defer t.opMu.Unlock()
	t.mu.Lock()
	if t.closed {
		t.mu.Unlock()
		return
	}
	t.closed = true
	t.active = false
	missing := t.missing
	t.mu.Unlock()

	if missing == "" {
		SweepTransparent()
	}
	if t.sessions != nil {
		t.sessions.dropScope(t.udpScope)
	}
	if t.lnLocalTCP != nil {
		_ = t.lnLocalTCP.Close()
	}
	if t.lnLANTCP != nil {
		_ = t.lnLANTCP.Close()
	}
	if t.udpConn != nil {
		_ = t.udpConn.Close()
	}
	t.wg.Wait()
}

// SweepTransparent removes openbear_fast's nftables table and UDP routing
// rules without touching fw4 or any other table.
func SweepTransparent() {
	_, _ = transparentCmd("", "nft", "delete", "table", "ip", Table)
	removeUDPRoute()
}

func installUDPRoute() error {
	markHex := fmt.Sprintf("0x%x", Mark)
	tableStr := strconv.Itoa(RouteTable)
	prefStr := strconv.Itoa(RulePref)
	rules, _ := transparentCmd("", "ip", "-4", "rule", "show")
	if !hasFastMarkRule(rules) {
		if _, err := transparentCmd("", "ip", "-4", "rule", "add", "pref", prefStr, "fwmark", markHex, "lookup", tableStr); err != nil {
			return err
		}
	}
	// Never replace somebody else's route. Tag our route so cleanup removes
	// only what this feature owns, including after a crash.
	if _, err := transparentCmd("", "ip", "-4", "route", "add", "local", "0.0.0.0/0", "dev", "lo", "proto", "242", "table", tableStr); err != nil {
		out, showErr := transparentCmd("", "ip", "-4", "route", "show", "table", tableStr)
		if showErr != nil || !(strings.Contains(out, "local default dev lo") || strings.Contains(out, "local 0.0.0.0/0 dev lo")) {
			return err
		}
	}
	return nil
}

func hasFastMarkRule(rules string) bool {
	prefix := strconv.Itoa(RulePref) + ":"
	tableStr := strconv.Itoa(RouteTable)
	for _, line := range strings.Split(rules, "\n") {
		if strings.HasPrefix(strings.TrimSpace(line), prefix) && strings.Contains(line, fmt.Sprintf("fwmark 0x%x", Mark)) && strings.HasSuffix(strings.TrimSpace(line), "lookup "+tableStr) {
			return true
		}
	}
	return false
}

func removeUDPRoute() {
	markHex := fmt.Sprintf("0x%x", Mark)
	tableStr := strconv.Itoa(RouteTable)
	prefStr := strconv.Itoa(RulePref)
	for range 4 {
		if _, err := transparentCmd("", "ip", "-4", "rule", "del", "pref", prefStr, "fwmark", markHex, "lookup", tableStr); err != nil {
			break
		}
	}
	_, _ = transparentCmd("", "ip", "-4", "route", "del", "local", "0.0.0.0/0", "dev", "lo", "proto", "242", "table", tableStr)
}

func (t *captureRunner) acceptTCP(ln net.Listener, preserveLAN bool) {
	defer t.wg.Done()
	for {
		c, err := ln.Accept()
		if err != nil {
			if errors.Is(err, net.ErrClosed) {
				return
			}
			time.Sleep(50 * time.Millisecond)
			continue
		}
		tc := c.(*net.TCPConn)
		_ = tc.SetNoDelay(true)
		if !preserveLAN {
			_ = tc.SetReadBuffer(fixedRcvBuf)
		}
		go t.handleRedirectedTCP(tc, preserveLAN)
	}
}

func (t *captureRunner) handleRedirectedTCP(c *net.TCPConn, preserveLAN bool) {
	defer c.Close()
	origDst, err := originalDst(c)
	if err != nil {
		return
	}
	from, _ := netip.ParseAddrPort(c.RemoteAddr().String())
	t.svc.entryTCP(c, from, preserveLAN, origDst, nil)
}

func listenTPROXYUDP(addr string) (*net.UDPConn, error) {
	lc := net.ListenConfig{
		Control: func(network, address string, c syscall.RawConn) error {
			if err := bypass.Control(network, address, c); err != nil {
				return err
			}
			var serr error
			if err := c.Control(func(fd uintptr) {
				if e := unix.SetsockoptInt(int(fd), unix.SOL_IP, unix.IP_TRANSPARENT, 1); e != nil {
					serr = e
					return
				}
				if e := unix.SetsockoptInt(int(fd), unix.SOL_IP, unix.IP_RECVORIGDSTADDR, 1); e != nil {
					serr = e
					return
				}
				_ = unix.SetsockoptInt(int(fd), unix.SOL_SOCKET, unix.SO_REUSEADDR, 1)
			}); err != nil {
				return err
			}
			return serr
		},
	}
	var pc net.PacketConn
	err := withFastSocket(func() error {
		var e error
		pc, e = lc.ListenPacket(context.Background(), "udp4", addr)
		return e
	})
	if err != nil {
		return nil, err
	}
	setUDPRcvBuf(pc.(*net.UDPConn), udpSocketBuf)
	return pc.(*net.UDPConn), nil
}

func (t *captureRunner) serveUDP(pc *net.UDPConn) {
	defer t.wg.Done()
	buf := make([]byte, 0x10000)
	oob := make([]byte, 128)
	for {
		n, oobn, _, from, err := pc.ReadMsgUDPAddrPort(buf, oob)
		if err != nil {
			if errors.Is(err, net.ErrClosed) {
				return
			}
			continue
		}
		origDst, ok := parseOrigDstOOB(oob[:oobn])
		if !ok || origDst.Port() == 0 {
			continue
		}
		if st := t.svc.state.Load(); st == nil {
			continue
		}
		payload := append([]byte(nil), buf[:n]...)
		t.dispatchUDP(from.Addr().Unmap(), from.Port(), origDst, payload)
	}
}

func (t *captureRunner) dispatchUDP(clientIP netip.Addr, clientPort uint16, origDst netip.AddrPort, payload []byte) {
	from := netip.AddrPortFrom(clientIP, clientPort)
	preserveLAN := !clientIP.IsLoopback() && !t.own.has(clientIP)
	key := udpSessionKey{
		scope:       t.udpScope,
		client:      from,
		target:      origDst,
		preserveLAN: preserveLAN,
	}
	_ = t.sessions.send(
		key,
		payload,
		func() (func([]byte) int, func(), error) {
			rsock, err := openTransparentReplyUDP(origDst, from)
			if err != nil {
				return nil, nil, err
			}
			// Subsequent datagrams from the same 4-tuple may land directly on
			// the connected IP_TRANSPARENT socket; forward them into the same session.
			go func() {
				rbuf := make([]byte, 0x10000)
				for {
					rn, rerr := rsock.Read(rbuf)
					if rerr != nil {
						return
					}
					cp := append([]byte(nil), rbuf[:rn]...)
					_ = t.sessions.send(key, cp, nil, nil, nil, nil)
				}
			}()
			reply := func(resp []byte) int {
				wn, werr := rsock.Write(resp)
				if werr != nil {
					return 0
				}
				return wn
			}
			cleanup := func() {
				_ = rsock.Close()
			}
			return reply, cleanup, nil
		},
		nil,
		nil,
		nil,
	)
}

func openTransparentReplyUDP(origDst, client netip.AddrPort) (net.Conn, error) {
	var conn net.Conn
	err := withFastSocket(func() error {
		fd, e := unix.Socket(unix.AF_INET, unix.SOCK_DGRAM|unix.SOCK_CLOEXEC|unix.SOCK_NONBLOCK, 0)
		if e != nil {
			return e
		}
		ok := false
		defer func() {
			if !ok {
				_ = unix.Close(fd)
			}
		}()
		if e := unix.SetsockoptInt(fd, unix.SOL_SOCKET, unix.SO_REUSEADDR, 1); e != nil {
			return e
		}
		_ = unix.SetsockoptInt(fd, unix.SOL_SOCKET, unix.SO_REUSEPORT, 1)
		if e := unix.SetsockoptInt(fd, unix.SOL_IP, unix.IP_TRANSPARENT, 1); e != nil {
			return e
		}
		_ = unix.SetsockoptInt(fd, unix.SOL_SOCKET, unix.SO_MARK, bypass.Mark)
		saBind := &unix.SockaddrInet4{Port: int(origDst.Port()), Addr: origDst.Addr().As4()}
		if e := unix.Bind(fd, saBind); e != nil {
			return e
		}
		saPeer := &unix.SockaddrInet4{Port: int(client.Port()), Addr: client.Addr().Unmap().As4()}
		if e := unix.Connect(fd, saPeer); e != nil {
			return e
		}
		f := os.NewFile(uintptr(fd), "fast-tproxy-udp")
		if f == nil {
			return errors.New("os.NewFile failed")
		}
		ok = true
		c, ferr := net.FileConn(f)
		_ = f.Close()
		if ferr != nil {
			return ferr
		}
		setUDPRcvBuf(c, udpSocketBuf)
		conn = c
		return nil
	})
	return conn, err
}

func parseOrigDstOOB(oob []byte) (netip.AddrPort, bool) {
	msgs, err := syscall.ParseSocketControlMessage(oob)
	if err != nil {
		return netip.AddrPort{}, false
	}
	for _, m := range msgs {
		if m.Header.Level == syscall.SOL_IP && m.Header.Type == unix.IP_ORIGDSTADDR && len(m.Data) >= 8 {
			port := binary.BigEndian.Uint16(m.Data[2:4])
			ip := netip.AddrFrom4([4]byte{m.Data[4], m.Data[5], m.Data[6], m.Data[7]})
			return netip.AddrPortFrom(ip, port), true
		}
	}
	return netip.AddrPort{}, false
}

// ownAddrs are this machine's interface addresses, which the UDP reader asks
// about for every datagram; it reads them again after a few seconds.
type ownAddrs struct {
	mu  sync.Mutex
	at  time.Time
	set map[netip.Addr]bool
}

func (o *ownAddrs) has(ip netip.Addr) bool {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.set == nil || time.Since(o.at) > 5*time.Second {
		o.set, o.at = map[netip.Addr]bool{}, time.Now()
		if addrs, err := net.InterfaceAddrs(); err == nil {
			for _, a := range addrs {
				if n, ok := a.(*net.IPNet); ok {
					if parsed, ok := netip.AddrFromSlice(n.IP); ok {
						o.set[parsed.Unmap()] = true
					}
				}
			}
		}
	}
	return o.set[ip]
}
