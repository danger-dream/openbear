package mesh

import (
	"os"
	"strings"
	"testing"
	"time"

	"openbear/node/mesh/health"
)

// Data planes a test starts with OBLAB_REPROBE look for a faster path that
// often.
func init() {
	if d, err := time.ParseDuration(os.Getenv("OBLAB_REPROBE")); err == nil && d > 0 {
		health.ReprobeEvery = d
	}
}

// pathLab: a public relay SG and two sites behind plain Linux NATs, HOME
// behind N1 and TOY behind N2, both site relays dialing SG and punching a
// link to each other. The two LANs also reach each other through a tunnel
// between N1 and N2, without NAT, as two offices joined by a VPN: each
// one's LAN address works for the other, the long way round.
func pathLab(t *testing.T, more ...string) *nsLab {
	l := newNSLab(t, "pc", append([]string{"W", "SG", "N1", "N2", "HOME", "TOY"}, more...)...)
	l.router("W")
	l.veth("W", "ws", "10.205.0.1/24", "SG", "s", "10.205.0.2/24")
	l.route("SG", "10.205.0.1")
	for i, n := range []string{"N1", "N2"} {
		k := string(rune('1' + i))
		l.veth("W", "w"+k, "10.205."+k+".1/24", n, "n"+k, "10.205."+k+".2/24")
		l.route(n, "10.205."+k+".1")
		l.router(n)
		l.in(n, "iptables", "-t", "nat", "-A", "POSTROUTING", "-o", l.ifname("n"+k), "-j", "MASQUERADE")
		l.in("W", "iptables", "-t", "mangle", "-A", "PREROUTING", "-i", l.ifname("w"+k), "-j", "TTL", "--ttl-dec", "5")
		l.bridge(n, "b"+k, "192.168."+k+".1/24")
	}
	l.onLAN("N1", "b1", "HOME", "192.168.1.2/24", "192.168.1.1")
	l.onLAN("N2", "b2", "TOY", "192.168.2.2/24", "192.168.2.1")
	l.veth("N1", "t1", "172.31.9.1/30", "N2", "t2", "172.31.9.2/30")
	l.in("N1", "ip", "route", "add", "192.168.2.0/24", "via", "172.31.9.2")
	l.in("N2", "ip", "route", "add", "192.168.1.0/24", "via", "172.31.9.1")
	l.node("SG", roleRelay, 1)
	l.node("HOME", roleRelay, 2)
	l.node("TOY", roleRelay, 3)
	return l
}

func (l *nsLab) delay(n, dev, d string) {
	l.in(n, "tc", "qdisc", "replace", "dev", l.ifname(dev), "root", "netem", "delay", d)
}

// TestPathChoice needs root; OBLAB_PATHCHOICE=1 runs it.
func TestPathChoice(t *testing.T) {
	if os.Getenv("OBLAB_PATHCHOICE") != "1" || os.Getuid() != 0 {
		t.Skip("set OBLAB_PATHCHOICE=1 and run as root")
	}
	l := pathLab(t)
	l.env = []string{"OBLAB_REPROBE=15s"}
	// The tunnel: 18 ms each way.
	l.delay("N1", "t1", "18ms")
	l.delay("N2", "t2", "18ms")
	out, _ := execIn(l.ns["TOY"], "ping", "-n", "-c", "3", "-i", "0.2", "192.168.1.2")
	t.Logf("HOME's LAN address from TOY, through the tunnel:\n%s", out)
	if !strings.Contains(out, " 0% packet loss") {
		t.Fatal("the tunnel does not work")
	}

	sg := udpEndpoint("10.205.0.2", 51820)
	static := &Routing{Mode: "static"}
	s := l.config("SG", Listen{UDP: 51820})
	s.Links = []Link{{Peer: "HOME", Kind: kindRelay}, {Peer: "TOY", Kind: kindRelay}}
	s.NextHops = map[string]string{"HOME": "HOME", "TOY": "TOY"}
	s.Routing = static
	configs := map[string]*Config{"SG": s}
	for _, x := range [][2]string{{"HOME", "TOY"}, {"TOY", "HOME"}} {
		c := l.config(x[0], Listen{UDP: 51820})
		c.Links = []Link{{Peer: "SG", Kind: kindRelay, Dial: true, Endpoint: sg}, {Peer: x[1], Kind: kindRelay, Punch: true}}
		c.NextHops = map[string]string{"SG": "SG", x[1]: x[1]}
		c.Reflectors = []string{"10.205.0.2:51820"}
		c.Routing = static
		configs[x[0]] = c
	}
	l.run(configs)
	direct := func() bool {
		a, b := l.peer("HOME", "TOY").Direct, l.peer("TOY", "HOME").Direct
		return a != nil && b != nil && a.State == "direct" && b.State == "direct"
	}
	took := l.wait("the link punched", 90*time.Second, direct)
	for _, x := range [][2]string{{"HOME", "TOY"}, {"TOY", "HOME"}} {
		p := l.peer(x[0], x[1])
		t.Logf("%s -> %s direct %v after start: %+v via %s; chosen %v", x[0], x[1], took, p.Endpoint, p.Direct.Via, l.events(x[0], "direct.chosen"))
		if p.Direct.Via != "public" || strings.HasPrefix(p.Endpoint, "192.168.") {
			t.Errorf("%s took the long way to %s: %s via %s", x[0], x[1], p.Endpoint, p.Direct.Via)
		}
	}
	_, rtt, out := l.pings("HOME", "TOY", 10)
	t.Logf("HOME -> TOY over the link: median %.2f ms", rtt)
	if rtt == 0 || rtt > 10 {
		t.Errorf("HOME -> TOY: median %.2f ms\n%s", rtt, out)
	}

	// Now the tunnel is the fast way and the internet the slow one: the
	// driver, HOME, finds the path to TOY's LAN address, which is no LAN
	// path, when it next looks and both ends move to it with the link up
	// throughout.
	l.delay("N1", "t1", "1ms")
	l.delay("N2", "t2", "1ms")
	l.delay("W", "w1", "20ms")
	l.delay("W", "w2", "20ms")
	changed := time.Now()
	took = l.wait("both on the tunnel", 90*time.Second, func() bool {
		p, q := l.peer("HOME", "TOY"), l.peer("TOY", "HOME")
		return p.Direct != nil && p.Direct.Via == "routed" && strings.HasPrefix(p.Endpoint, "192.168.2.2:") &&
			q.Direct != nil && q.Direct.Via == "routed" && strings.HasPrefix(q.Endpoint, "192.168.1.2:")
	})
	t.Logf("HOME moved %v after the change: %v; TOY -> HOME %s via %s, %v", took, l.events("HOME", "direct.switched"), l.peer("TOY", "HOME").Endpoint, l.peer("TOY", "HOME").Direct.Via, l.events("TOY", "direct.switched"))
	for _, id := range []string{"HOME", "TOY"} {
		for _, e := range l.status(id).Events {
			if e.At >= changed.UnixMilli() && (e.Kind == "direct.down" || e.Kind == "direct.failed") {
				t.Errorf("%s: %s %s", id, e.Kind, e.Detail)
			}
		}
	}
	if !direct() {
		t.Error("the link is not direct on both ends")
	}
	_, rtt, out = l.pings("HOME", "TOY", 10)
	t.Logf("HOME -> TOY after the move: median %.2f ms", rtt)
	if rtt == 0 || rtt > 10 {
		t.Errorf("HOME -> TOY: median %.2f ms\n%s", rtt, out)
	}
}
