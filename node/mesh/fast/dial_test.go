package fast

import (
	"context"
	"errors"
	"io"
	"net"
	"net/netip"
	"strconv"
	"strings"
	"testing"
	"time"
)

// TestDialCarriesThisNodesOwnConnections: a program of the node (a fast
// forward) reaches a fast port without capture: over the mesh while the
// channel warms up, then up its path through the exit's relay port, the
// path's round trip known; a stream that cannot open fails the dial; the
// loopback listener takes nobody without its key, and a closed service dials
// nothing.
func TestDialCarriesThisNodesOwnConnections(t *testing.T) {
	loopback(t)
	key := randKeyB64(t)
	meshPrefix := netip.MustParsePrefix("100.96.0.0/16")
	addrA, addrC := netip.MustParseAddr("100.96.0.1"), netip.MustParseAddr("100.96.0.3")
	nodes := map[string]netip.Addr{"A": addrA, "C": addrC}

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

	portC := freeTCPPort(t)
	var svcA, svcC Service
	defer svcA.Close()
	defer svcC.Close()
	isolateHost(t, &svcA)
	isolateHost(t, &svcC)
	dir := t.TempDir()
	svcA.SetHooks(Hooks{Dir: dir + "/a", Paths: func(dest string, k int, allow func(string) bool) []Plan {
		if dest != "C" {
			return nil
		}
		return []Plan{{Hops: []string{"C"}, Planned: true}}
	}})
	svcC.SetHooks(Hooks{Dir: dir + "/c"})
	svcC.Apply(&Config{Port: portC, MeshPort: portC, Keys: []string{key}, Nodes: nodes}, "C", addrC, meshPrefix, []Rule{{
		Src: []netip.Prefix{netip.PrefixFrom(addrA, 32)}, Dst: []netip.Prefix{netip.PrefixFrom(addrC, 32)}, Proto: "any",
		Ports: [][2]uint16{{echoPort, echoPort}},
	}})
	svcA.Apply(&Config{MeshPort: portC, Keys: []string{key}, Nodes: nodes, Targets: []Target{{Node: "C", Port: echoPort, Proto: "tcp"}},
		Next: map[string]string{"C": net.JoinHostPort("127.0.0.1", strconv.Itoa(portC))}}, "A", addrA, meshPrefix, nil)
	svcA.pool.dial = func(_ contextContext, addr string) (*net.TCPConn, error) {
		if strings.HasPrefix(addr, "100.96.") {
			return nil, errors.New("no mesh in a test")
		}
		c, err := net.DialTimeout("tcp", addr, 200*time.Millisecond)
		if err != nil {
			return nil, err
		}
		return c.(*net.TCPConn), nil
	}

	echoC := netip.AddrPortFrom(addrC, echoPort)
	through := func(msg string) string {
		t.Helper()
		c, err := svcA.Dial(context.Background(), echoC)
		if err != nil {
			t.Fatal(err)
		}
		defer c.Close()
		_ = c.SetDeadline(time.Now().Add(5 * time.Second))
		if _, err := c.Write([]byte(msg)); err != nil {
			return ""
		}
		_ = c.CloseWrite()
		reply, _ := io.ReadAll(c)
		return string(reply)
	}

	if got := through("first"); got != "echo:first" {
		t.Fatalf("first connection over the mesh: %q", got)
	}
	if svcC.Status().Exit.Opened != 0 {
		t.Fatal("the first connection went through C's relay port")
	}
	waitFor(t, "the channel to C to be ready", 5*time.Second, func() bool {
		ch, ok := channelOf(&svcA, "C")
		return ok && ch.Ready
	})
	if got := through("fast"); got != "echo:fast" {
		t.Fatalf("fast connection: %q", got)
	}
	if svcC.Status().Exit.Opened != 1 || svcA.Status().Entry.Opened != 2 {
		t.Fatalf("not through C's relay port: entry %+v exit %+v", svcA.Status().Entry, svcC.Status().Exit)
	}
	if d, ok := svcA.PathRTT(echoC); !ok || d <= 0 {
		t.Fatalf("path round trip: %v %v", d, ok)
	}
	if _, ok := svcA.PathRTT(netip.AddrPortFrom(addrC, echoPort+1)); ok {
		t.Fatal("a round trip for what is no fast port")
	}
	// A stream that cannot open fails the dial itself, not the first write.
	opened := svcA.Status().Entry.Opened
	if c, err := svcA.Dial(context.Background(), netip.AddrPortFrom(addrC, echoPort+1)); err == nil {
		c.Close()
		t.Fatal("a dial to what is no fast port succeeded")
	}
	if svcA.Status().Entry.Opened != opened+1 {
		t.Fatal("the failed dial did not reach the entry")
	}

	ln, _, err := svcA.dialListener()
	if err != nil {
		t.Fatal(err)
	}
	c, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	pre := make([]byte, dialPreamble)
	a := echoC.Addr().As16()
	copy(pre[16:], a[:])
	pre[32], pre[33] = byte(echoPort>>8), byte(echoPort)
	_, _ = c.Write(append(pre, "x"...))
	_ = c.SetReadDeadline(time.Now().Add(3 * time.Second))
	if n, _ := c.Read(make([]byte, 16)); n != 0 {
		t.Fatal("a connection without the key was carried")
	}
	c.Close()
	if svcA.Status().Entry.Opened != opened+1 {
		t.Fatal("a connection without the key reached the entry")
	}

	var closed Service
	closed.Close()
	if _, err := closed.Dial(context.Background(), echoC); !errors.Is(err, net.ErrClosed) {
		t.Fatalf("dial on a closed service: %v", err)
	}
}
