//go:build linux

package mesh

import (
	"bytes"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/netip"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	meshfast "openbear/node/mesh/fast"
)

// TestNetnsFastHelper runs inside a target netns when OBLAB_FAST_HELPER is set,
// providing zero-dependency TCP/UDP servers and clients for TestNetnsFastRelay.
func TestNetnsFastHelper(t *testing.T) {
	mode := os.Getenv("OBLAB_FAST_HELPER")
	if mode == "" {
		t.Skip("helper process for TestNetnsFastRelay")
	}
	addr := os.Getenv("OBLAB_FAST_ADDR")
	switch mode {
	case "servers":
		readyFile := os.Getenv("OBLAB_FAST_READY")
		tcpLn, err := net.Listen("tcp4", "0.0.0.0:19001")
		if err != nil {
			t.Fatal(err)
		}
		defer tcpLn.Close()

		streamLn, err := net.Listen("tcp4", "0.0.0.0:19002")
		if err != nil {
			t.Fatal(err)
		}
		defer streamLn.Close()
		resetFile := os.Getenv("OBLAB_FAST_RESET_FILE")

		udpConn, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4zero, Port: 19003})
		if err != nil {
			t.Fatal(err)
		}
		defer udpConn.Close()

		go func() {
			for {
				c, err := tcpLn.Accept()
				if err != nil {
					return
				}
				go func(c net.Conn) {
					defer c.Close()
					b, _ := io.ReadAll(c)
					_, _ = c.Write(append([]byte("ok:"), b...))
				}(c)
			}
		}()

		go func() {
			chunk := bytes.Repeat([]byte("S"), 64<<10)
			for {
				c, err := streamLn.Accept()
				if err != nil {
					return
				}
				go func(c net.Conn) {
					defer c.Close()
					_ = c.SetWriteDeadline(time.Now().Add(30 * time.Second))
					for {
						if _, err := c.Write(chunk); err != nil {
							if resetFile != "" {
								_ = os.WriteFile(resetFile, []byte(err.Error()), 0o644)
							}
							return
						}
					}
				}(c)
			}
		}()

		go func() {
			buf := make([]byte, 4096)
			for {
				n, from, err := udpConn.ReadFromUDP(buf)
				if err != nil {
					return
				}
				_, _ = udpConn.WriteToUDP(append([]byte("uok:"), buf[:n]...), from)
			}
		}()

		_ = os.WriteFile(readyFile, []byte("ready"), 0o644)
		time.Sleep(5 * time.Minute)

	case "tcp-echo":
		msg := os.Getenv("OBLAB_FAST_MSG")
		c, err := net.DialTimeout("tcp4", addr, 5*time.Second)
		if err != nil {
			t.Fatalf("dial %s: %v", addr, err)
		}
		defer c.Close()
		_ = c.SetDeadline(time.Now().Add(5 * time.Second))
		tc := c.(*net.TCPConn)
		if _, err := tc.Write([]byte(msg)); err != nil {
			t.Fatalf("write: %v", err)
		}
		_ = tc.CloseWrite()
		got, err := io.ReadAll(tc)
		if err != nil {
			t.Fatalf("read: %v", err)
		}
		fmt.Printf("REPLY=%s\n", string(got))

	case "tcp-deny":
		c, err := net.DialTimeout("tcp4", addr, 2*time.Second)
		if err != nil {
			fmt.Println("DENIED=dial")
			return
		}
		defer c.Close()
		_ = c.SetDeadline(time.Now().Add(2 * time.Second))
		_, _ = c.Write([]byte("x"))
		buf := make([]byte, 16)
		n, rerr := c.Read(buf)
		if n == 0 && rerr != nil {
			fmt.Println("DENIED=reset")
			return
		}
		t.Fatalf("unexpected success on denied port: n=%d err=%v", n, rerr)

	case "tcp-abort":
		c, err := net.DialTimeout("tcp4", addr, 5*time.Second)
		if err != nil {
			t.Fatalf("dial %s: %v", addr, err)
		}
		tc := c.(*net.TCPConn)
		buf := make([]byte, 8192)
		if _, err := io.ReadFull(tc, buf); err != nil {
			t.Fatalf("read initial chunk: %v", err)
		}
		_ = tc.SetLinger(0)
		_ = tc.Close()
		fmt.Println("ABORTED=1")

	case "udp-echo":
		msg := os.Getenv("OBLAB_FAST_MSG")
		dst, err := netip.ParseAddrPort(addr)
		if err != nil {
			t.Fatal(err)
		}
		pc, err := net.ListenUDP("udp4", &net.UDPAddr{Port: 0})
		if err != nil {
			t.Fatal(err)
		}
		defer pc.Close()
		_ = pc.SetDeadline(time.Now().Add(5 * time.Second))
		for i := range 3 {
			payload := fmt.Sprintf("%s-%d", msg, i)
			if _, err := pc.WriteToUDPAddrPort([]byte(payload), dst); err != nil {
				t.Fatalf("udp write %d: %v", i, err)
			}
			rbuf := make([]byte, 2048)
			rn, from, err := pc.ReadFromUDPAddrPort(rbuf)
			if err != nil {
				t.Fatalf("udp read %d: %v", i, err)
			}
			if from.Addr().Unmap() != dst.Addr() || from.Port() != dst.Port() {
				t.Fatalf("udp reply from %v, want %v", from, dst)
			}
			if string(rbuf[:rn]) != "uok:"+payload {
				t.Fatalf("udp reply %q, want %q", string(rbuf[:rn]), "uok:"+payload)
			}
		}
		fmt.Println("UDP_OK=3")
	}
}

func TestNetnsFastRelay(t *testing.T) {
	if os.Getenv("OBLAB_NETNS") != "1" || os.Getuid() != 0 {
		t.Skip("set OBLAB_NETNS=1 and run as root")
	}
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}

	l := newNSLab(t, "fr", "A", "B", "C", "H")
	// Underlay links: A (198.51.100.1) <-> B (198.51.100.2) and B (198.51.100.5) <-> C (198.51.100.6).
	l.veth("A", "ab", "198.51.100.1/30", "B", "ba", "198.51.100.2/30")
	l.veth("B", "bc", "198.51.100.5/30", "C", "cb", "198.51.100.6/30")
	l.router("A")
	l.router("B")
	// Give A and C default routes as real hosts have.
	l.route("A", "198.51.100.2")
	l.route("C", "198.51.100.5")

	// Bridge LAN behind A with host H (192.168.77.10/24 -> gw 192.168.77.1).
	l.bridge("A", "lan", "192.168.77.1/24")
	l.onLAN("A", "lan", "H", "192.168.77.10/24", "192.168.77.1")

	l.node("A", roleRelay, 1)
	l.node("B", roleRelay, 2)
	l.node("C", roleRelay, 3)

	var keyBytes [32]byte
	if _, err := rand.Read(keyBytes[:]); err != nil {
		t.Fatal(err)
	}
	fastKey := base64.StdEncoding.EncodeToString(keyBytes[:])
	fastPort := 52003

	nodesMap := map[string]netip.Addr{
		"A": l.nodes["A"].Address,
		"B": l.nodes["B"].Address,
		"C": l.nodes["C"].Address,
	}
	dialPairs := [][2]string{{"A", "B"}, {"B", "A"}, {"B", "C"}, {"C", "B"}}

	// Access rules: A -> C allowed on 19001, 19002, 19003 (and fastPort 52003 for mesh fallback);
	// the fast port 19004 is A's own LAN's alone.
	allowedPorts := [][2]uint16{{19001, 19003}, {uint16(fastPort), uint16(fastPort)}}
	targets := []meshfast.Target{
		{Node: "C", Port: 19001, Proto: "tcp"},
		{Node: "C", Port: 19002, Proto: "tcp"},
		{Node: "C", Port: 19003, Proto: "udp"},
		{Node: "C", Port: 19004, Proto: "tcp", Src: []netip.Prefix{netip.MustParsePrefix("10.99.0.0/16")}},
	}
	addrC := l.nodes["C"].Address.String()

	cfgA := l.config("A", Listen{UDP: 51999, TCP: 52000, Fast: fastPort})
	cfgA.Self.Gateway = &Gateway{
		Subnets:    []netip.Prefix{netip.MustParsePrefix("192.168.77.0/24")},
		Masquerade: true,
		SNAT:       true,
	}
	cfgA.Links = []Link{{Peer: "B", Kind: kindRelay, Dial: true, Endpoint: "tcp://198.51.100.2:52000"}}
	cfgA.NextHops = map[string]string{"B": "B", "C": "B"}
	cfgA.Routing = &Routing{Mode: "dynamic"}
	cfgA.Fast = &meshfast.Config{
		Port:     fastPort,
		MeshPort: fastPort,
		Keys:     []string{fastKey},
		Nodes:    nodesMap,
		Next:     map[string]string{"B": "198.51.100.2:52003"},
		Dial:     dialPairs,
		Targets:  targets,
		Capture:  true,
	}

	cfgB := l.config("B", Listen{UDP: 51999, TCP: 52000, Fast: fastPort})
	cfgB.Links = []Link{
		{Peer: "A", Kind: kindRelay, Dial: false},
		{Peer: "C", Kind: kindRelay, Dial: true, Endpoint: "tcp://198.51.100.6:52000"},
	}
	cfgB.NextHops = map[string]string{"A": "A", "C": "C"}
	cfgB.Routing = &Routing{Mode: "dynamic"}
	cfgB.Rules = append(cfgB.Rules, Rule{Proto: "tcp", Ports: [][2]uint16{{uint16(fastPort), uint16(fastPort)}}})
	cfgB.Fast = &meshfast.Config{
		Port:     fastPort,
		MeshPort: fastPort,
		Keys:     []string{fastKey},
		Nodes:    nodesMap,
		Next:     map[string]string{"A": "198.51.100.1:52003", "C": "198.51.100.6:52003"},
		Dial:     dialPairs,
	}

	cfgC := l.config("C", Listen{UDP: 51999, TCP: 52000, Fast: fastPort})
	cfgC.Links = []Link{{Peer: "B", Kind: kindRelay, Dial: false}}
	cfgC.NextHops = map[string]string{"B": "B", "A": "B"}
	cfgC.Routing = &Routing{Mode: "dynamic"}
	cfgC.Rules = append(cfgC.Rules, Rule{
		Src:   []netip.Prefix{netip.PrefixFrom(l.nodes["A"].Address, 32), netip.PrefixFrom(l.nodes["B"].Address, 32)},
		Dst:   []netip.Prefix{netip.PrefixFrom(l.nodes["C"].Address, 32)},
		Proto: "any",
		Ports: allowedPorts,
	})
	cfgC.Fast = &meshfast.Config{
		Port:     fastPort,
		MeshPort: fastPort,
		Keys:     []string{fastKey},
		Nodes:    nodesMap,
		Next:     map[string]string{"B": "198.51.100.5:52003"},
		Dial:     dialPairs,
	}

	// Start target TCP/UDP servers inside C's namespace.
	tmpDir := t.TempDir()
	readyFile := filepath.Join(tmpDir, "c.ready")
	resetFile := filepath.Join(tmpDir, "c.reset")
	srvCmd := exec.Command("ip", "netns", "exec", l.ns["C"], self, "-test.run", "^TestNetnsFastHelper$")
	srvCmd.Env = append(os.Environ(),
		"OBLAB_FAST_HELPER=servers",
		"OBLAB_FAST_READY="+readyFile,
		"OBLAB_FAST_RESET_FILE="+resetFile,
	)
	if err := srvCmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = srvCmd.Process.Kill()
		_, _ = srvCmd.Process.Wait()
	})
	l.wait("target servers in C", 10*time.Second, func() bool {
		_, err := os.Stat(readyFile)
		return err == nil
	})

	configs := map[string]*Config{"A": cfgA, "B": cfgB, "C": cfgC}
	l.run(configs)

	l.wait("A, B, C mesh + fast ready", 30*time.Second, func() bool {
		sa, sb, sc := l.status("A"), l.status("B"), l.status("C")
		return sa.Fast != nil && sa.Fast.ListenError == "" && sa.Fast.Capture != nil && sa.Fast.Capture.Active &&
			sb.Fast != nil && sb.Fast.ListenError == "" &&
			sc.Fast != nil && sc.Fast.ListenError == "" &&
			len(sa.Routing.Routes) >= 2 && sa.Routing.Routes[0].NextHop != ""
	})

	runHelper := func(nsName, mode, addr, msg string) string {
		t.Helper()
		cmd := exec.Command("ip", "netns", "exec", l.ns[nsName], self, "-test.run", "^TestNetnsFastHelper$", "-test.v")
		cmd.Env = append(os.Environ(),
			"OBLAB_FAST_HELPER="+mode,
			"OBLAB_FAST_ADDR="+addr,
			"OBLAB_FAST_MSG="+msg,
		)
		out, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("%s (%s %s): %v\n%s", nsName, mode, addr, err, out)
		}
		return string(out)
	}

	// 1. The first connection to the fast port goes over the mesh, and opens
	// A's channel to C; once ready, connections from A itself go A -> B -> C.
	l.wait("encrypted mesh A -> C up", 20*time.Second, func() bool {
		return exec.Command("ip", "netns", "exec", l.ns["A"], "ping", "-c", "1", "-W", "1", addrC).Run() == nil
	})
	if out := runHelper("A", "tcp-echo", addrC+":19001", "first"); !strings.Contains(out, "REPLY=ok:first") {
		t.Fatalf("first connection over the mesh: %s", out)
	}
	if l.status("C").Fast.Exit.Opened != 0 {
		t.Fatal("the first connection went through the fast relay")
	}
	l.wait("A's channel to C ready along B", 15*time.Second, func() bool {
		for _, ch := range l.status("A").Fast.Channels {
			if ch.Dest == "C" && ch.Ready && ch.Using >= 0 && len(ch.Paths[ch.Using].Hops) == 2 && ch.Paths[ch.Using].Hops[0] == "B" {
				return true
			}
		}
		return false
	})
	if out := runHelper("A", "tcp-echo", addrC+":19001", "local-tcp"); !strings.Contains(out, "REPLY=ok:local-tcp") {
		t.Fatalf("local TCP output: %s", out)
	}
	// 2. And from H on A's LAN.
	if out := runHelper("H", "tcp-echo", addrC+":19001", "lan-tcp"); !strings.Contains(out, "REPLY=ok:lan-tcp") {
		t.Fatalf("LAN TCP prerouting: %s", out)
	}
	l.wait("transit & exit TCP counters", 5*time.Second, func() bool {
		return l.status("B").Fast.Transit.Opened >= 2 && l.status("C").Fast.Exit.Opened >= 2
	})

	// 3. Local output UDP + LAN prerouting UDP TPROXY takeover.
	if l.status("A").Fast.Capture.UDP {
		exits := l.status("C").Fast.Exit.Opened
		if out := runHelper("A", "udp-echo", addrC+":19003", "local-udp"); !strings.Contains(out, "UDP_OK=3") {
			t.Fatalf("local UDP TPROXY: %s", out)
		}
		if out := runHelper("H", "udp-echo", addrC+":19003", "lan-udp"); !strings.Contains(out, "UDP_OK=3") {
			t.Fatalf("LAN UDP TPROXY: %s", out)
		}
		l.wait("UDP through C's relay port", 5*time.Second, func() bool {
			return l.status("C").Fast.Exit.Opened >= exits+2
		})
	} else {
		t.Logf("kernel lacks nft_tproxy in netns (%s); TCP capture alone verified", l.status("A").Fast.Capture.Error)
	}

	// 4. A fast port A's own programs may not use is refused at the entry.
	if out := runHelper("A", "tcp-deny", addrC+":19004", ""); !strings.Contains(out, "DENIED=") {
		t.Fatalf("expected denied port to fail: %s", out)
	}
	l.wait("RefusedEntry incremented on A", 5*time.Second, func() bool {
		return l.status("A").Fast.RefusedEntry >= 1
	})

	// 5. Mid-stream TCP reset propagation across 3 namespaces (A -> B -> C).
	// Two UDP sessions from step 3 remain open during their 60 s idle window,
	// so TCP reset brings Active back to that exact baseline.
	baseTransit := l.status("B").Fast.Transit.Active
	baseExit := l.status("C").Fast.Exit.Active
	if out := runHelper("A", "tcp-abort", addrC+":19002", ""); !strings.Contains(out, "ABORTED=1") {
		t.Fatalf("abort helper: %s", out)
	}
	for i := 0; ; i++ {
		_, rerr := os.Stat(resetFile)
		sa, sb, sc := l.status("A").Fast, l.status("B").Fast, l.status("C").Fast
		if rerr == nil && sb != nil && sc != nil && sb.Transit.Active <= baseTransit && sc.Exit.Active <= baseExit {
			break
		}
		if i == 50 {
			t.Fatalf("reset wait timeout: resetFileErr=%v\nA.Fast=%+v\nB.Fast=%+v\nC.Fast=%+v", rerr, sa, sb, sc)
		}
		time.Sleep(100 * time.Millisecond)
	}

	// 6. Fallback to encrypted WireGuard mesh address when B -> C has no direct fast endpoint.
	// First ensure encrypted mesh ping works between B and C.
	l.wait("encrypted mesh B -> C up", 20*time.Second, func() bool {
		err := exec.Command("ip", "netns", "exec", l.ns["B"], "ping", "-c", "1", "-W", "1", l.nodes["C"].Address.String()).Run()
		return err == nil
	})
	cfgB.Revision = 2
	cfgB.Fast.Next = map[string]string{"A": "198.51.100.1:52003"} // no direct C!
	cfgB.Fast.Dial = [][2]string{{"A", "B"}, {"B", "A"}}
	rawB, err := json.Marshal(cfgB)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(l.dirs["B"], "config.json"), rawB, 0o600); err != nil {
		t.Fatal(err)
	}
	l.wait("B applied revision 2", 10*time.Second, func() bool {
		return l.status("B").AppliedRevision == 2
	})
	if out := runHelper("H", "tcp-echo", addrC+":19001", "via-wg-fallback"); !strings.Contains(out, "REPLY=ok:via-wg-fallback") {
		t.Fatalf("fallback over WireGuard mesh failed: %s", out)
	}
	l.wait("B pool records ViaMesh=true for C", 5*time.Second, func() bool {
		for _, p := range l.status("B").Fast.Peers {
			if p.Peer == "C" && p.ViaMesh {
				return true
			}
		}
		return false
	})
}
