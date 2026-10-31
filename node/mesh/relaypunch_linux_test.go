package mesh

import (
	"encoding/json"
	"os"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"time"
)

// siteLab: a public relay P on the "internet" router W, 20 ms away each
// way, and two homes, each a plain Linux NAT with a site relay and a member
// on its LAN: S1 and M1 behind N1, S2 and M2 behind N2. The site relays dial
// P and punch a link to each other; each member's entry is its site relay,
// punched, with P as the other candidate.
func siteLab(t *testing.T) *nsLab {
	l := newNSLab(t, "rp", "W", "P", "N1", "N2", "S1", "S2", "M1", "M2")
	l.router("W")
	l.veth("W", "wp", "10.202.0.1/24", "P", "p", "10.202.0.2/24")
	l.route("P", "10.202.0.1")
	for _, x := range [][2]string{{"W", "wp"}, {"P", "p"}} {
		l.in(x[0], "tc", "qdisc", "add", "dev", l.ifname(x[1]), "root", "netem", "delay", "20ms")
	}
	for i, n := range []string{"N1", "N2"} {
		k := strconv.Itoa(i + 1)
		l.veth("W", "w"+k, "10.202."+k+".1/24", n, "n"+k, "10.202."+k+".2/24")
		l.route(n, "10.202."+k+".1")
		l.router(n)
		l.in(n, "iptables", "-t", "nat", "-A", "POSTROUTING", "-o", l.ifname("n"+k), "-j", "MASQUERADE")
		l.in("W", "iptables", "-t", "mangle", "-A", "PREROUTING", "-i", l.ifname("w"+k), "-j", "TTL", "--ttl-dec", "5")
		l.bridge(n, "l"+k, "192.168."+k+".1/24")
		l.onLAN(n, "l"+k, "S"+k, "192.168."+k+".2/24", "192.168."+k+".1")
		l.onLAN(n, "l"+k, "M"+k, "192.168."+k+".3/24", "192.168."+k+".1")
	}
	l.node("P", roleRelay, 1)
	l.node("S1", roleRelay, 2)
	l.node("S2", roleRelay, 3)
	l.node("M1", roleMember, 11, "S1", "P")
	l.node("M2", roleMember, 12, "S2", "P")
	pub := udpEndpoint("10.202.0.2", 51820)
	static := &Routing{Mode: "static"}
	p := l.config("P", Listen{UDP: 51820})
	p.Links = []Link{{Peer: "S1", Kind: kindRelay}, {Peer: "S2", Kind: kindRelay}}
	p.NextHops = map[string]string{"S1": "S1", "S2": "S2"}
	p.Routing = static
	configs := map[string]*Config{"P": p}
	for _, x := range [][2]string{{"S1", "S2"}, {"S2", "S1"}} {
		s := l.config(x[0], Listen{UDP: 51820})
		s.Links = []Link{{Peer: "P", Kind: kindRelay, Dial: true, Endpoint: pub}, {Peer: x[1], Kind: kindRelay, Punch: true}}
		s.NextHops = map[string]string{"P": "P", x[1]: x[1]}
		s.Reflectors = []string{"10.202.0.2:51820"}
		s.Routing = static
		configs[x[0]] = s
	}
	for _, x := range [][2]string{{"M1", "S1"}, {"M2", "S2"}} {
		m := l.config(x[0], Listen{UDP: 51820})
		m.Entry = &Entry{Mode: "failover", AutoReturn: true, Candidates: []Candidate{{ID: "c1", Relay: x[1], Punch: true}, {ID: "c2", Relay: "P", Endpoint: pub}}}
		m.Reflectors = []string{"10.202.0.2:51820"}
		configs[x[0]] = m
	}
	l.run(configs)
	return l
}

var pingRTT = regexp.MustCompile(`ttl=(\d+) time=([\d.]+) ms`)

// pings sends n pings and returns the TTLs and median round trip of the
// replies.
func (l *nsLab) pings(from, to string, n int) ([]int, float64, string) {
	out, _ := execIn(l.ns[from], "ping", "-n", "-c", strconv.Itoa(n), "-i", "0.2", "-W", "1", l.nodes[to].Address.String())
	var ttls []int
	var rtts []float64
	for _, m := range pingRTT.FindAllStringSubmatch(out, -1) {
		ttl, _ := strconv.Atoi(m[1])
		rtt, _ := strconv.ParseFloat(m[2], 64)
		ttls, rtts = append(ttls, ttl), append(rtts, rtt)
	}
	return ttls, median(rtts), out
}

// TestSiteRelays needs root; OBLAB_SITE=1 runs it.
func TestSiteRelays(t *testing.T) {
	if os.Getenv("OBLAB_SITE") != "1" || os.Getuid() != 0 {
		t.Skip("set OBLAB_SITE=1 and run as root")
	}
	l := siteLab(t)
	took := l.wait("the relay link punched on both site relays", 90*time.Second, func() bool {
		a, b := l.peer("S1", "S2").Direct, l.peer("S2", "S1").Direct
		return a != nil && b != nil && a.State == "direct" && b.State == "direct"
	})
	t.Logf("relay link direct %v after start: S1 %+v; S2 %+v; endpoint %s", took, *l.peer("S1", "S2").Direct, *l.peer("S2", "S1").Direct, l.peer("S1", "S2").Endpoint)

	for _, x := range [][2]string{{"M1", "S1"}, {"M2", "S2"}} {
		took := l.wait(x[0]+" entered through its site relay", 90*time.Second, func() bool {
			e := l.status(x[0]).Entry
			return e != nil && e.Candidate == "c1" && e.Transport == "udp"
		})
		p := l.peer(x[0], x[1])
		if os.Getenv("OBLAB_DEBUG") == "1" {
			for _, id := range []string{x[0], x[1]} {
				for _, e := range l.status(id).Events {
					t.Logf("%s event %d %s %s", id, e.At, e.Kind, e.Detail)
				}
			}
		}
		t.Logf("%s on c1 %v after start through %s; candidates %+v; reach %+v", x[0], took, p.Endpoint, l.status(x[0]).Entry.Candidates, l.status(x[0]).Reachability)
		if !strings.HasPrefix(p.Endpoint, "192.168.") {
			t.Fatalf("%s reaches %s at %s, not on the LAN", x[0], x[1], p.Endpoint)
		}
	}

	// M1 -> S1 -> S2 -> M2, with none of P's 80 ms. M1 and M2 talk end to
	// end: the relays lower only the outer packet's TTL, the reply's is 64.
	var ttls []int
	var rtt float64
	var out string
	l.wait("M1 to M2 over the punched relay link", 60*time.Second, func() bool {
		ttls, rtt, out = l.pings("M1", "M2", 5)
		if os.Getenv("OBLAB_DEBUG") == "1" {
			for _, id := range []string{"M1", "S1", "S2", "M2"} {
				s := l.status(id)
				b, _ := json.Marshal(struct {
					C any
					E any
				}{s.Counters, s.E2E})
				t.Logf("debug %s %s", id, b)
			}
			rt, _ := execIn(l.ns["M1"], "ip", "route", "get", l.nodes["M2"].Address.String())
			pe, _ := json.Marshal(l.status("M1").Peers)
			t.Logf("debug ping %v %.1f %q route %q peers %s", ttls, rtt, out, rt, pe)
		}
		return len(ttls) >= 4 && ttls[len(ttls)-1] == 64 && rtt < 20
	})
	t.Logf("M1 -> M2: TTLs %v, median %.2f ms\n%s", ttls, rtt, out)

	// S1 goes away: M1 moves to P, and M2 is still reached, now through P.
	l.stopNode("S1")
	cut := time.Now()
	moved := l.wait("M1 on P", 60*time.Second, func() bool {
		e := l.status("M1").Entry
		return e != nil && e.Candidate == "c2" && e.Relay == "P"
	})
	l.wait("M1 to M2 through P", 60*time.Second, func() bool {
		ttls, rtt, out = l.pings("M1", "M2", 5)
		return len(ttls) >= 3 && rtt > 70
	})
	c1 := l.candidate("M1", "c1")
	t.Logf("M1 on P %v after S1 stopped (%v ago); c1 %+v; M1 -> M2 TTLs %v, median %.1f ms; S2 %+v", moved, time.Since(cut).Round(time.Second), c1, ttls, rtt, l.peer("S2", "S1").Direct)
	if c1.State != "punching" && c1.State != "failing" {
		t.Fatalf("c1 is %s after its relay went away", c1.State)
	}
	// S2 notices on its own, by the link's probes (about 5 s), which can be
	// just after the checks above.
	l.wait("S2 to drop the relay link to S1", 15*time.Second, func() bool {
		d := l.peer("S2", "S1").Direct
		return d != nil && d.State != "direct"
	})
	t.Logf("S2 dropped the relay link to S1 by %v after S1 stopped: %+v", time.Since(cut).Round(100*time.Millisecond), *l.peer("S2", "S1").Direct)
}

func execIn(ns string, args ...string) (string, error) {
	out, err := exec.Command("ip", append([]string{"netns", "exec", ns}, args...)...).CombinedOutput()
	return string(out), err
}
