package mesh

import (
	"fmt"
	"os/exec"
	"strings"
	"testing"
	"time"
)

// TestRoutingNetnsShortInterface: A's own interface takes 1400 bytes, as on
// a VPS, the others' 1500, and each drops what is longer than its own; A
// reaches B through X over UDP. A holds its own packets to 1264 bytes: a ping
// of that size goes both ways, B padding its answer no longer, a longer one is
// refused on A, and TCP, which takes A's MTU both ways, carries 2 MB each way.
func TestRoutingNetnsShortInterface(t *testing.T) {
	rtSkip(t)
	l := newRTLab(t, []string{"A", "X", "B"}, []string{"A-X:10", "X-B:10"}, nil)
	k := l.link("A", "X")
	rtSh(t, "ip", "-n", l.ns["A"], "link", "set", "dev", k.devA, "mtu", "1400")
	l.configure = func(id string, c *Config) { c.Rules = append(c.Rules, Rule{Proto: "tcp"}) }
	l.start()
	l.paths(60*time.Second, "A>X>B", "B>X>A")

	l.waitFor("A's own MTU", 20*time.Second, func() bool {
		s := l.status("A")
		return s.OwnMTU != nil && *s.OwnMTU == OwnMTUStatus{Iface: k.devA, IfaceMTU: 1400, MTU: 1264}
	})
	if s := l.status("X"); s.OwnMTU != nil {
		t.Errorf("X: %+v", *s.OwnMTU)
	}
	found := false
	for _, e := range l.status("A").Events {
		found = found || e.Kind == "mtu.own" && strings.HasPrefix(e.Detail, k.devA+" 1400: ")
	}
	if !found {
		t.Error("no mtu.own event on A")
	}

	ping := func(size int) string {
		out, _ := exec.Command("ip", "netns", "exec", l.ns["A"], "ping", "-n", "-M", "do", "-c", "3", "-i", "0.3", "-W", "2", "-s", fmt.Sprint(size), l.addr["B"].String()).CombinedOutput()
		return string(out)
	}
	if out := ping(1264 - 28); !strings.Contains(out, " 3 received") {
		t.Errorf("a ping of 1264 bytes: %s", out)
	}
	// Refused by the interface's MTU here; with large packets on, by the route's ("mtu=1264").
	if out := ping(1264 - 27); !strings.Contains(out, "Message too long") && !strings.Contains(out, "mtu=1264") {
		t.Errorf("a ping of 1265 bytes: %s", out)
	}
	for _, way := range [][2]string{{"A", "B"}, {"B", "A"}} {
		if n := tcpTransfer(t, l, way[0], way[1], 2_000_000); n != 2_000_000 {
			t.Errorf("TCP %s to %s: %d bytes", way[0], way[1], n)
		}
	}
}

// tcpTransfer sends n bytes over TCP from node from to node to through the
// mesh and returns how many arrived within 20 seconds.
func tcpTransfer(t *testing.T, l *rtLab, from, to string, n int) int {
	t.Helper()
	addr := l.addr[from].String()
	server := exec.Command("ip", "netns", "exec", l.ns[from], "python3", "-c", fmt.Sprintf(
		"import socket\ns=socket.socket()\ns.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\ns.bind(('%s',5001))\ns.listen(1)\ns.settimeout(20)\nc,_=s.accept()\nc.sendall(b'x'*%d)\nc.close()", addr, n))
	if err := server.Start(); err != nil {
		t.Fatal(err)
	}
	defer server.Process.Kill()
	time.Sleep(500 * time.Millisecond)
	out, err := exec.Command("ip", "netns", "exec", l.ns[to], "timeout", "20", "python3", "-c", fmt.Sprintf(
		"import socket\nc=socket.create_connection(('%s',5001),timeout=15)\nn=0\nwhile True:\n    b=c.recv(65536)\n    if not b: break\n    n+=len(b)\nprint(n)", addr)).CombinedOutput()
	server.Wait()
	var got int
	if _, serr := fmt.Sscan(string(out), &got); serr != nil {
		t.Logf("TCP %s to %s: %v %s", from, to, err, out)
	}
	return got
}
