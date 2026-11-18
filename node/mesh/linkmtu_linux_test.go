package mesh

import (
	"os"
	"os/exec"
	"strconv"
	"strings"
	"testing"
	"time"
)

// narrowLab needs root, python3 and iptables; OBLAB_NARROW=1 runs its tests.
// A member M enters relay R1, which forwards to relay R2 over UDP. The
// router before R2 takes 1450 bytes and fragments what is longer, and R2's
// network drops fragments from R1, as a line smaller than the hosts' before
// a network that drops fragments does: small packets pass, a full-size one
// of the mesh, 1465 bytes on the wire, vanishes, and nothing says so. It
// returns once M reaches R2, with R2's mesh address.
func narrowLab(t *testing.T) (*nsLab, string) {
	if os.Getenv("OBLAB_NARROW") != "1" || os.Getuid() != 0 {
		t.Skip("set OBLAB_NARROW=1 and run as root")
	}
	for _, tool := range []string{"python3", "iptables"} {
		if _, err := exec.LookPath(tool); err != nil {
			t.Skip("needs " + tool)
		}
	}
	l := newNSLab(t, "nl", "W", "M", "R1", "R2")
	l.bridge("W", "la", "10.209.1.1/24")
	l.bridge("W", "lb", "10.209.2.1/24")
	l.router("W")
	l.onLAN("W", "la", "M", "10.209.1.2/24", "10.209.1.1")
	l.onLAN("W", "la", "R1", "10.209.1.3/24", "10.209.1.1")
	l.onLAN("W", "lb", "R2", "10.209.2.2/24", "10.209.2.1")
	// Only W's way out to R2 is small: R2 takes and sends full frames, so
	// its own MTU, and the segments it asks for, stay the network's.
	l.in("W", "ip", "link", "set", l.ifname("lb"), "mtu", "1450")
	// Before reassembly: what INPUT sees is whole again.
	l.in("R2", "iptables", "-t", "raw", "-A", "PREROUTING", "-s", "10.209.1.3", "-f", "-j", "DROP")
	l.node("R1", roleRelay, 1)
	l.node("R2", roleRelay, 2)
	l.node("M", roleMember, 11, "R1")
	r1 := l.config("R1", Listen{UDP: 51820})
	r1.Links = []Link{{Peer: "R2", Kind: kindRelay, Dial: true, Endpoint: udpEndpoint("10.209.2.2", 51820)}}
	r2 := l.config("R2", Listen{UDP: 51820})
	r2.Links = []Link{{Peer: "R1", Kind: kindRelay}}
	m := l.config("M", Listen{UDP: 51820})
	m.Entry = &Entry{Mode: "fixed", Candidates: []Candidate{{ID: "c1", Relay: "R1", Endpoint: udpEndpoint("10.209.1.3", 51820)}}}
	for _, c := range []*Config{r1, r2, m} {
		c.Rules = []Rule{{Proto: "any"}}
	}
	l.run(map[string]*Config{"R1": r1, "R2": r2, "M": m})
	to := l.nodes["R2"].Address.String()
	l.wait("M reaching R2", 90*time.Second, func() bool {
		out, _ := execIn(l.ns["M"], "ping", "-n", "-c", "1", "-W", "1", to)
		return strings.Contains(out, " 0% packet loss")
	})
	return l, to
}

// waitNarrow waits for R1 to hold its link to R2 to 1376 bytes.
func waitNarrow(l *nsLab) {
	l.t.Helper()
	l.wait("R1 holding its link to R2", 90*time.Second, func() bool {
		for _, e := range l.events("R1", "link.mtu") {
			if e == "R2: carries 1376 bytes of the network's 1400" {
				return true
			}
		}
		return false
	})
}

// A receiver on R2 that reports how much it got and the longest it went
// without getting anything once the stream began: what the controller's
// heartbeat, closing a control connection quiet for about 38 seconds,
// would see.
const gapReceiver = `
import socket, sys, time
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind((sys.argv[1], int(sys.argv[2])))
s.listen(1)
c, _ = s.accept()
n, last, gap = 0, None, 0.0
while True:
    b = c.recv(65536)
    now = time.monotonic()
    if not b:
        break
    if last is not None:
        gap = max(gap, now - last)
    last, n = now, n + len(b)
print(n, round(gap, 2), flush=True)
`

// A sender on M: a report's worth of small writes a second for five
// seconds, then size bytes at once.
const gapSender = `
import os, socket, sys, time
c = socket.create_connection((sys.argv[1], int(sys.argv[2])))
for _ in range(100):
    c.sendall(b"r" * 100)
    time.sleep(0.05)
c.sendall(os.urandom(int(sys.argv[3])))
c.close()
`

// stream sends size bytes from M to R2 as gapSender does, the sender up to
// within, and returns what R2 got and its longest gap.
func stream(l *nsLab, to string, port, size int, opened func(), within time.Duration) (int, float64) {
	l.t.Helper()
	recv := exec.Command("ip", "netns", "exec", l.ns["R2"], "python3", "-c", gapReceiver, to, strconv.Itoa(port))
	var out strings.Builder
	recv.Stdout = &out
	if err := recv.Start(); err != nil {
		l.t.Fatal(err)
	}
	l.t.Cleanup(func() { recv.Process.Kill(); recv.Wait() })
	time.Sleep(time.Second)
	send := exec.Command("ip", "netns", "exec", l.ns["M"], "timeout", strconv.Itoa(int(within.Seconds())), "python3", "-c", gapSender, to, strconv.Itoa(port), strconv.Itoa(size))
	if err := send.Start(); err != nil {
		l.t.Fatal(err)
	}
	if opened != nil {
		opened()
	}
	if err := send.Wait(); err != nil {
		l.t.Fatalf("the sender: %v", err)
	}
	recv.Wait()
	f := strings.Fields(out.String())
	if len(f) != 2 {
		l.t.Fatalf("the receiver said %q", out.String())
	}
	n, _ := strconv.Atoi(f[0])
	gap, _ := strconv.ParseFloat(f[1], 64)
	return n, gap
}

func heldPath(l *nsLab, id string) int {
	if e := l.status("M").E2E; e != nil {
		for _, p := range e.Peers {
			if p.ID == id {
				return p.PathMTU
			}
		}
	}
	return 0
}

// The link is known narrow when a connection comes over it, as a route
// moves onto a link probed all along: the first full segment R1 cannot
// carry is answered, M holds its path and tells its OS, and the stream
// goes on without a pause a control connection's heartbeat would notice.
func TestNarrowLinkKnownNetns(t *testing.T) {
	l, to := narrowLab(t)
	waitNarrow(l)
	const size = 4 << 20
	n, gap := stream(l, to, 5300, size, nil, 120*time.Second)
	t.Logf("%d bytes from M to R2 over the link known narrow, the longest gap %.2fs", n, gap)
	if n != 100*100+size {
		t.Fatalf("R2 got %d bytes", n)
	}
	if gap > 5 {
		t.Errorf("a %.2fs gap: a control connection could be closed", gap)
	}
	// status.json is written every few seconds.
	l.wait("M's status holding its path to R2", 15*time.Second, func() bool { return heldPath(l, "R2") == 1376 })
	if routes := l.in("M", "ip", "route", "show", to); !strings.Contains(routes, "mtu 1312") {
		t.Errorf("M's host route to R2: %q", routes)
	}
}

// The connection opens before R1 knows the link: its full segments vanish
// until R1 finds the link takes 1376 bytes; then it is answered, M holds
// its path to that, and the connection, open all along, carries the rest.
// How long it went quiet is logged, not held to a limit: a link that
// narrows while in use takes up to a probe interval and the rounds that
// confirm it to be found.
func TestNarrowLinkNetns(t *testing.T) {
	l, to := narrowLab(t)
	start := time.Now()
	const size = 4 << 20
	n, gap := stream(l, to, 5299, size, func() {
		waitNarrow(l)
		t.Logf("R1 held its link to R2 to 1376 bytes %v after the connection opened", time.Since(start).Round(time.Second))
	}, 240*time.Second)
	t.Logf("%d bytes from M to R2 over the narrow link in %v, the longest gap %.2fs", n, time.Since(start).Round(time.Second), gap)
	if n != 100*100+size {
		t.Fatalf("R2 got %d bytes", n)
	}
	if held := heldPath(l, "R2"); held != 1376 || l.peer("R1", "R2").PathMTU != 1376 {
		t.Errorf("M's path to R2: pathMtu %d, R1's link: %d; want 1376", held, l.peer("R1", "R2").PathMTU)
	}
	if routes := l.in("M", "ip", "route", "show", to); !strings.Contains(routes, "mtu 1312") {
		t.Errorf("M's host route to R2: %q", routes)
	}
}
