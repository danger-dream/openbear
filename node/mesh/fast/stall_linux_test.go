//go:build linux

package fast

import (
	"errors"
	"io"
	"net"
	"net/netip"
	"slices"
	"strconv"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

// goSilent has the kernel drop all that reaches c unanswered, as from a peer
// gone dark: no acknowledgement and no reset gets through to c any more.
func goSilent(t *testing.T, c *net.TCPConn) {
	t.Helper()
	rc, err := c.SyscallConn()
	if err != nil {
		t.Fatal(err)
	}
	drop := []unix.SockFilter{{Code: unix.BPF_RET | unix.BPF_K, K: 0}}
	prog := unix.SockFprog{Len: 1, Filter: &drop[0]}
	var serr error
	if err := rc.Control(func(fd uintptr) {
		serr = unix.SetsockoptSockFprog(int(fd), unix.SOL_SOCKET, unix.SO_ATTACH_FILTER, &prog)
	}); err != nil || serr != nil {
		if errors.Is(serr, unix.EPERM) {
			t.Skipf("a socket filter is not permitted here (capability-dropped container): %v", serr)
		}
		t.Fatalf("drop filter: %v %v", err, serr)
	}
}

func tcpInfoOf(t *testing.T, c *net.TCPConn) *unix.TCPInfo {
	t.Helper()
	rc, err := c.SyscallConn()
	if err != nil {
		t.Fatal(err)
	}
	var info *unix.TCPInfo
	var gerr error
	if err := rc.Control(func(fd uintptr) {
		info, gerr = unix.GetsockoptTCPInfo(int(fd), unix.IPPROTO_TCP, unix.TCP_INFO)
	}); err != nil || gerr != nil {
		t.Fatalf("TCP_INFO: %v %v", err, gerr)
	}
	return info
}

func shortStalls(t *testing.T) {
	origCheck, origAfter := stallCheck, stallAfter
	stallCheck, stallAfter = 100*time.Millisecond, time.Second
	t.Cleanup(func() { stallCheck, stallAfter = origCheck, origAfter })
}

// socksOf are the watched sockets of each of svc's streams of role.
func socksOf(svc *Service, role connRole) [][]*net.TCPConn {
	svc.openMu.Lock()
	defer svc.openMu.Unlock()
	var out [][]*net.TCPConn
	for _, t := range svc.open {
		if t.role == role {
			out = append(out, slices.Clone(t.socks))
		}
	}
	return out
}

// A socket with data waiting on a peer gone silent is cut once nothing was
// acknowledged for stallAfter; one whose reader only stopped reading is left
// alone while its window probes wait longer than that, and goes on after.
func TestStallCutsSilentPeerNotPausedReader(t *testing.T) {
	var s Service
	stop := make(chan struct{})
	defer close(stop)
	go s.watchStalls(stop, 100*time.Millisecond, time.Second)
	addr := netip.MustParseAddr("100.96.0.1")
	s.state.Store(&runtimeState{selfAddr: addr, inbound: []Rule{{Proto: "tcp", Ports: [][2]uint16{{1, 2}}}}})

	start := time.Now()
	sender, peer := clientPair(t)
	defer sender.Close()
	defer peer.Close()
	if !s.trackConn(sender, addr, addr, protoTCP, 1, roleExit) {
		t.Fatal("track")
	}
	defer s.untrackConn(sender)
	goSilent(t, peer)
	if _, err := sender.Write(make([]byte, 1024)); err != nil {
		t.Fatal(err)
	}
	_ = sender.SetReadDeadline(time.Now().Add(5 * time.Second))
	if _, err := sender.Read(make([]byte, 1)); !errors.Is(err, net.ErrClosed) {
		t.Fatalf("socket with a silent peer: %v, want it cut", err)
	}
	if took := time.Since(start); took < time.Second || took > 2500*time.Millisecond {
		t.Fatalf("cut after %v, want stallAfter and a look or two", took)
	}

	writer, reader := clientPair(t)
	defer writer.Close()
	defer reader.Close()
	if !s.trackConn(writer, addr, addr, protoTCP, 2, roleExit) {
		t.Fatal("track")
	}
	defer s.untrackConn(writer)
	const total = 64 << 20
	wrote := make(chan error, 1)
	go func() {
		_, err := io.Copy(writer, io.LimitReader(zeros{}, total))
		_ = writer.CloseWrite()
		wrote <- err
	}()
	time.Sleep(4 * time.Second)
	select {
	case err := <-wrote:
		t.Fatalf("writer to a paused reader ended: %v", err)
	default:
	}
	if info := tcpInfoOf(t, writer); info.Backoff < 3 {
		t.Fatalf("window probes backed off %d times, want them waiting longer than stallAfter", info.Backoff)
	}
	n, err := io.Copy(io.Discard, reader)
	if err != nil || n != total {
		t.Fatalf("paused reader read on: %d %v", n, err)
	}
	if err := <-wrote; err != nil {
		t.Fatalf("writer: %v", err)
	}
}

type zeros struct{}

func (zeros) Read(p []byte) (int, error) {
	clear(p)
	return len(p), nil
}

// Along A -> B -> C, a hop whose peer goes silent is cut within stallAfter
// and its reset ends the stream along the rest of the path: C sending a
// download into a silent B, and B sending an upload into a silent C. A client
// that stops reading for longer is cut nowhere and reads on.
func TestStalledHopIsCutPausedClientIsNot(t *testing.T) {
	shortStalls(t)
	loopback(t)

	key := randKeyB64(t)
	meshPrefix := netip.MustParsePrefix("100.96.0.0/16")
	addrA, addrB, addrC := netip.MustParseAddr("100.96.0.1"), netip.MustParseAddr("100.96.0.2"), netip.MustParseAddr("100.96.0.3")
	nodes := map[string]netip.Addr{"A": addrA, "B": addrB, "C": addrC}

	serve := func(handle func(net.Conn)) uint16 {
		ln, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = ln.Close() })
		go func() {
			for {
				c, err := ln.Accept()
				if err != nil {
					return
				}
				go func() {
					defer c.Close()
					handle(c)
				}()
			}
		}()
		return uint16(ln.Addr().(*net.TCPAddr).Port)
	}
	flood := netip.AddrPortFrom(addrC, serve(func(c net.Conn) { _, _ = io.Copy(c, zeros{}) }))
	sink := netip.AddrPortFrom(addrC, serve(func(c net.Conn) { _, _ = io.Copy(io.Discard, c) }))

	portB, portC := freeTCPPort(t), freeTCPPort(t)
	var svcA, svcB, svcC Service
	defer svcA.Close()
	defer svcB.Close()
	defer svcC.Close()
	for _, s := range []*Service{&svcA, &svcB, &svcC} {
		isolateHost(t, s)
	}
	svcA.SetHooks(Hooks{Dir: t.TempDir(), Paths: func(string, int, func(string) bool) []Plan {
		return []Plan{{Hops: []string{"B", "C"}, Planned: true}}
	}})
	svcB.SetHooks(Hooks{Dir: t.TempDir()})
	svcC.SetHooks(Hooks{Dir: t.TempDir()})
	svcC.Apply(&Config{Port: portC, MeshPort: portC, Keys: []string{key}, Nodes: nodes}, "C", addrC, meshPrefix,
		[]Rule{{Src: []netip.Prefix{netip.PrefixFrom(addrA, 32)}, Proto: "tcp"}})
	svcB.Apply(&Config{Port: portB, MeshPort: portC, Keys: []string{key}, Nodes: nodes,
		Next: map[string]string{"C": net.JoinHostPort("127.0.0.1", strconv.Itoa(portC))}}, "B", addrB, meshPrefix, nil)
	svcA.Apply(&Config{MeshPort: portC, Keys: []string{key}, Nodes: nodes,
		Targets: []Target{{Node: "C", Port: flood.Port(), Proto: "tcp"}, {Node: "C", Port: sink.Port(), Proto: "tcp"}},
		Next:    map[string]string{"B": net.JoinHostPort("127.0.0.1", strconv.Itoa(portB))},
	}, "A", addrA, meshPrefix, nil)

	_ = connect(t, &svcA, sink).Close()
	waitFor(t, "the path to answer", 5*time.Second, func() bool {
		ch, ok := channelOf(&svcA, "C")
		return ok && len(ch.Paths) == 1 && ch.Paths[0].State == pathReady
	})
	along := func() bool {
		return svcA.Status().Entry.Active == 1 && svcB.Status().Transit.Active == 1 && svcC.Status().Exit.Active == 1
	}
	gone := func() bool {
		return svcA.Status().Entry.Active == 0 && svcB.Status().Transit.Active == 0 && svcC.Status().Exit.Active == 0
	}
	waitFor(t, "the first connection to end", 2*time.Second, gone)

	// A download; then what reaches C from B is dropped.
	dl := connect(t, &svcA, flood)
	defer dl.Close()
	if _, err := io.ReadFull(dl, make([]byte, 1<<20)); err != nil {
		t.Fatalf("download: %v", err)
	}
	waitFor(t, "the download along A -> B -> C", 2*time.Second, along)
	goSilent(t, socksOf(&svcC, roleExit)[0][0])
	start := time.Now()
	waitFor(t, "C to cut the download", 3*time.Second, func() bool { return svcC.Status().Exit.Active == 0 })
	if took := time.Since(start); took < 500*time.Millisecond {
		t.Fatalf("C cut the download after %v, before it stalled for stallAfter", took)
	}
	waitFor(t, "the download to end along the path", 2*time.Second, gone)
	_ = dl.SetReadDeadline(time.Now().Add(2 * time.Second))
	var ne net.Error
	if _, err := io.Copy(io.Discard, dl); errors.As(err, &ne) && ne.Timeout() {
		t.Fatal("the client's download hangs")
	}

	// An upload; then what reaches B from C is dropped.
	ul := connect(t, &svcA, sink)
	defer ul.Close()
	sent := make(chan error, 1)
	go func() {
		_, err := io.Copy(ul, zeros{})
		sent <- err
	}()
	waitFor(t, "the upload along A -> B -> C", 2*time.Second, along)
	streams := socksOf(&svcB, roleTransit)
	if len(streams) != 1 || len(streams[0]) != 2 {
		t.Fatalf("B watches %v, want its stream's sockets down and up", streams)
	}
	goSilent(t, streams[0][1])
	waitFor(t, "B to cut the upload", 3*time.Second, func() bool { return svcB.Status().Transit.Active == 0 })
	waitFor(t, "the upload to end along the path", 2*time.Second, gone)
	select {
	case err := <-sent:
		if err == nil {
			t.Fatal("the client's upload ended without an error")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("the client's upload hangs")
	}

	// A download whose client stops reading for a while: every window along
	// the path closes, and nothing is cut.
	paused := connect(t, &svcA, flood)
	defer paused.Close()
	if _, err := io.ReadFull(paused, make([]byte, 1<<20)); err != nil {
		t.Fatalf("download: %v", err)
	}
	waitFor(t, "the download along A -> B -> C", 2*time.Second, along)
	time.Sleep(4 * time.Second)
	if !along() {
		t.Fatalf("a paused client's stream was cut: A %+v B %+v C %+v", svcA.Status().Entry, svcB.Status().Transit, svcC.Status().Exit)
	}
	if info := tcpInfoOf(t, socksOf(&svcC, roleExit)[0][0]); info.Backoff < 3 {
		t.Fatalf("C's window probes backed off %d times, want them waiting longer than stallAfter", info.Backoff)
	}
	_ = paused.SetReadDeadline(time.Now().Add(5 * time.Second))
	if _, err := io.ReadFull(paused, make([]byte, 32<<20)); err != nil {
		t.Fatalf("the paused client reads on: %v", err)
	}
}
