package mesh

import (
	"os"
	"strconv"
	"strings"
	"testing"
	"time"
)

// TestPathMTU needs root; OBLAB_MTU=1 runs it. A member M and a relay R,
// each on a 1500-byte link, joined through two routers whose link carries
// only 1400 bytes, and neither router says so: their "fragmentation
// needed" messages are dropped, as a cloud security group without ICMP
// does. A full-size packet in the mesh is about 1465 bytes on the wire; it
// has to get across both ways, fragmented on the way, as with kernel
// WireGuard, rather than vanish while small packets pass (SSH works, scp
// stalls).
func TestPathMTU(t *testing.T) {
	if os.Getenv("OBLAB_MTU") != "1" || os.Getuid() != 0 {
		t.Skip("set OBLAB_MTU=1 and run as root")
	}
	l := newNSLab(t, "mt", "W1", "W2", "R", "M")
	l.veth("W1", "a", "10.208.0.1/30", "W2", "b", "10.208.0.2/30")
	l.veth("W2", "wr", "10.208.2.1/24", "R", "r", "10.208.2.2/24")
	l.veth("W1", "wm", "10.208.1.1/24", "M", "m", "10.208.1.2/24")
	for _, w := range []string{"W1", "W2"} {
		l.router(w)
		l.in(w, "iptables", "-A", "OUTPUT", "-p", "icmp", "--icmp-type", "fragmentation-needed", "-j", "DROP")
	}
	l.in("W1", "ip", "link", "set", l.ifname("a"), "mtu", "1400")
	l.in("W2", "ip", "link", "set", l.ifname("b"), "mtu", "1400")
	l.in("W1", "ip", "route", "add", "10.208.2.0/24", "via", "10.208.0.2")
	l.in("W2", "ip", "route", "add", "10.208.1.0/24", "via", "10.208.0.1")
	l.route("R", "10.208.2.1")
	l.route("M", "10.208.1.1")
	if out, _ := execIn(l.ns["M"], "ping", "-n", "-c", "2", "-W", "1", "-s", "1400", "10.208.2.2"); !strings.Contains(out, " 100% packet loss") {
		t.Fatalf("the underlay does not lose large packets as it should:\n%s", out)
	}
	l.node("R", roleRelay, 1)
	l.node("M", roleMember, 11, "R")
	r := l.config("R", Listen{UDP: 51820})
	r.Routing = &Routing{Mode: "static"}
	m := l.config("M", Listen{UDP: 51820})
	m.Entry = &Entry{Mode: "fixed", Candidates: []Candidate{{ID: "c1", Relay: "R", Endpoint: udpEndpoint("10.208.2.2", 51820)}}}
	l.run(map[string]*Config{"R": r, "M": m})
	l.wait("M entering R", 60*time.Second, func() bool {
		_, rtt, _ := l.pings("M", "R", 1)
		return rtt > 0
	})
	// 1372 bytes of ICMP data make a 1400-byte packet, the mesh's MTU.
	for _, size := range []int{1200, 1372} {
		out, _ := execIn(l.ns["M"], "ping", "-n", "-c", "5", "-i", "0.2", "-W", "1", "-s", strconv.Itoa(size), l.nodes["R"].Address.String())
		if !strings.Contains(out, " 0% packet loss") {
			t.Errorf("%d-byte pings through the mesh:\n%s", size, out)
		}
	}

	// The network's MTU changes and R took it while M did not yet; M enters
	// over TCP now. M's full-size packets reach R, whose replies go back in
	// fragments, and the connection stays up.
	for _, w := range [][2]string{{"W1", "a"}, {"W2", "b"}} {
		l.in(w[0], "ip", "link", "set", l.ifname(w[1]), "mtu", "1500")
	}
	r.Self.Listen.TCP = 52000
	r.Network.MTU = 1280
	m.Network.MTU = 1420
	m.Entry.Candidates[0].Endpoint = "tcp://10.208.2.2:52000"
	l.rewrite(2, map[string]*Config{"R": r, "M": m})
	l.wait("M entering R over TCP", 60*time.Second, func() bool {
		p := l.peer("M", "R")
		return p.Active && p.Transport == "tcp"
	})
	reconnects := func() int { return l.peer("M", "R").Reconnects }
	before := reconnects()
	out, _ := execIn(l.ns["M"], "ping", "-n", "-c", "5", "-i", "0.2", "-W", "1", "-s", "1392", l.nodes["R"].Address.String())
	if !strings.Contains(out, " 0% packet loss") {
		t.Errorf("1420-byte pings from MTU 1420 to MTU 1280 over TCP:\n%s", out)
	}
	if after := reconnects(); after != before {
		t.Errorf("M reconnected %d times while pinging", after-before)
	}
}
