package mesh

import (
	"fmt"
	"os"
	"strings"
	"testing"
	"time"
)

// routedLab is pathLab with the VPN between the two offices as the field
// has it: what comes in from TOY's office reaches HOME's LAN from N1's
// address, masqueraded as a home router does with a VPN of its own, and
// HOME's LAN cannot open anything toward the office. So TOY's LAN address
// does not work for HOME, while HOME's works for TOY, the long way round,
// and HOME sees TOY at N1's LAN address. tunnel and internet are each
// way's delay on the VPN and on each hop of the internet (four a round
// trip).
func routedLab(t *testing.T, tunnel, internet string, more ...string) *nsLab {
	l := pathLab(t, more...)
	l.in("N1", "iptables", "-t", "nat", "-A", "POSTROUTING", "-s", "192.168.2.0/24", "-o", l.ifname("b1"), "-j", "MASQUERADE")
	l.in("N1", "iptables", "-A", "FORWARD", "-i", l.ifname("b1"), "-o", l.ifname("t1"), "-m", "conntrack", "--ctstate", "NEW", "-j", "DROP")
	l.delays(tunnel, internet)
	return l
}

// delays sets each way of the tunnel and of each site's internet hops.
func (l *nsLab) delays(tunnel, internet string) {
	l.delay("N1", "t1", tunnel)
	l.delay("N2", "t2", tunnel)
	l.delay("W", "w1", internet)
	l.delay("W", "w2", internet)
	l.delay("N1", "n1", internet)
	l.delay("N2", "n2", internet)
}

func routedConfigs(l *nsLab) map[string]*Config {
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
	return configs
}

func (l *nsLab) linkDirect() bool {
	a, b := l.peer("HOME", "TOY").Direct, l.peer("TOY", "HOME").Direct
	return a != nil && b != nil && a.State == "direct" && b.State == "direct"
}

// onInternet: both ends of the link on the internet path.
func onInternet(l *nsLab) bool {
	a, b := l.peer("HOME", "TOY"), l.peer("TOY", "HOME")
	return l.linkDirect() && strings.HasPrefix(a.Endpoint, "10.205.2.2:") && strings.HasPrefix(b.Endpoint, "10.205.1.2:") &&
		a.Direct.Via == "public" && b.Direct.Via == "public"
}

// onTunnel: both ends on the VPN, which is no LAN path for either.
func onTunnel(l *nsLab) bool {
	a, b := l.peer("HOME", "TOY"), l.peer("TOY", "HOME")
	return l.linkDirect() && strings.HasPrefix(a.Endpoint, "192.168.1.1:") && strings.HasPrefix(b.Endpoint, "192.168.1.2:") &&
		a.Direct.Via == "routed" && b.Direct.Via == "routed"
}

func (l *nsLab) logLink(what string) {
	for _, x := range [][2]string{{"HOME", "TOY"}, {"TOY", "HOME"}} {
		p := l.peer(x[0], x[1])
		via := ""
		if p.Direct != nil {
			via = p.Direct.State + " via " + p.Direct.Via
		}
		l.t.Logf("%s: %s -> %s at %s, %s, rtt %.1f ms; chosen %v better %v switched %v", what, x[0], x[1], p.Endpoint, via, p.RTTMs,
			l.events(x[0], "direct.chosen"), l.events(x[0], "direct.better"), l.events(x[0], "direct.switched"))
	}
}

// watch waits for ok, logging each end's link as it changes.
func (l *nsLab) watch(what string, within time.Duration, ok func() bool) time.Duration {
	l.t.Helper()
	last := ""
	return l.wait(what, within, func() bool {
		var b strings.Builder
		for _, x := range [][2]string{{"HOME", "TOY"}, {"TOY", "HOME"}} {
			p := l.peer(x[0], x[1])
			fmt.Fprintf(&b, "%s->%s %s %v | ", x[0], x[1], p.Endpoint, p.Direct)
		}
		if s := b.String(); s != last {
			last = s
			l.t.Logf("%s %s", time.Now().Format("15:04:05.0"), s)
		}
		return ok()
	})
}

func (l *nsLab) noLoss(since time.Time) {
	l.t.Helper()
	for _, id := range []string{"HOME", "TOY"} {
		for _, e := range l.status(id).Events {
			if e.At >= since.UnixMilli() && (e.Kind == "direct.down" || e.Kind == "direct.failed") {
				l.t.Errorf("%s: %s %s", id, e.Kind, e.Detail)
			}
		}
	}
}

func (l *nsLab) linkRTT(max float64) {
	l.t.Helper()
	_, rtt, out := l.pings("HOME", "TOY", 10)
	l.t.Logf("HOME -> TOY over the link: median %.2f ms", rtt)
	if rtt == 0 || rtt > max {
		l.t.Errorf("HOME -> TOY: median %.2f ms\n%s", rtt, out)
	}
}

func routedTest(t *testing.T) {
	if os.Getenv("OBLAB_PATHROUTED") != "1" || os.Getuid() != 0 {
		t.Skip("set OBLAB_PATHROUTED=1 and run as root")
	}
}

// TestPathRouted needs root; OBLAB_PATHROUTED=1 runs it. The field's case:
// TOY reaches HOME's LAN address through the VPN, 34 ms; the internet takes
// 3 ms. Both ends punch to the internet path.
func TestPathRouted(t *testing.T) {
	routedTest(t)
	l := routedLab(t, "17ms", "750us")
	l.run(routedConfigs(l))
	took := l.watch("the link punched", 90*time.Second, l.linkDirect)
	l.logLink("punched after " + took.String())
	if !onInternet(l) {
		t.Error("not both on the internet path")
	}
	l.linkRTT(10)
}

// TestPathRoutedSwitch needs root; OBLAB_PATHROUTED=1 runs it. The VPN is
// the fast way at first, 2 ms against 30, and the link punches to it; then
// the internet becomes the fast one and both ends move to it, with the
// link up, and stay.
func TestPathRoutedSwitch(t *testing.T) {
	routedTest(t)
	l := routedLab(t, "1ms", "7500us")
	l.env = []string{"OBLAB_REPROBE=15s"}
	l.run(routedConfigs(l))
	took := l.watch("the link punched to the VPN", 90*time.Second, func() bool { return onTunnel(l) })
	l.logLink("punched after " + took.String())
	l.linkRTT(6)

	l.delays("17ms", "750us")
	changed := time.Now()
	took = l.watch("both ends on the internet path", 90*time.Second, func() bool { return onInternet(l) })
	l.logLink("moved after " + took.String())
	time.Sleep(40 * time.Second)
	if !onInternet(l) {
		l.logLink("40 s later")
		t.Error("left the internet path")
	}
	l.noLoss(changed)
	l.linkRTT(10)
}

// TestPathRoutedMapping needs root; OBLAB_PATHROUTED=1 runs it. TOY's NAT,
// as the office's in the field, maps its port anew toward one reflector:
// R2, which TOY asks first, sees another port than SG and HOME do. TOY
// offers both, and the link still punches to the internet.
func TestPathRoutedMapping(t *testing.T) {
	routedTest(t)
	l := routedLab(t, "17ms", "750us", "R2")
	l.veth("W", "wr", "10.205.3.1/24", "R2", "r", "10.205.3.2/24")
	l.route("R2", "10.205.3.1")
	l.in("N2", "iptables", "-t", "nat", "-I", "POSTROUTING", "-o", l.ifname("n2"), "-p", "udp", "-d", "10.205.3.2", "-j", "SNAT", "--to-source", "10.205.2.2:43173")
	l.node("R2", roleRelay, 4)
	configs := routedConfigs(l)
	for _, id := range []string{"HOME", "TOY"} {
		configs[id].Reflectors = []string{"10.205.3.2:51820", "10.205.0.2:51820"}
	}
	configs["R2"] = l.config("R2", Listen{UDP: 51820})
	l.run(configs)
	took := l.watch("the link punched", 90*time.Second, l.linkDirect)
	l.logLink("punched after " + took.String())
	if !onInternet(l) {
		t.Error("not both on the internet path")
	}
	l.linkRTT(10)
}

// TestPathRoutedOutage needs root; OBLAB_PATHROUTED=1 runs it. HOME's
// internet goes away for a while and comes back, as when the home router
// restarted in the field: both ends end up on the internet path again.
func TestPathRoutedOutage(t *testing.T) {
	routedTest(t)
	l := routedLab(t, "17ms", "750us")
	l.env = []string{"OBLAB_REPROBE=15s"}
	l.run(routedConfigs(l))
	took := l.watch("the link on the internet", 90*time.Second, func() bool { return onInternet(l) })
	l.logLink("punched after " + took.String())
	drop := [][]string{{"FORWARD", "-o", l.ifname("n1")}, {"FORWARD", "-i", l.ifname("n1")}, {"OUTPUT", "-o", l.ifname("n1")}}
	for _, r := range drop {
		l.in("N1", append(append([]string{"iptables", "-I"}, r...), "-j", "DROP")...)
	}
	time.Sleep(70 * time.Second)
	l.logLink("internet down 70 s")
	for _, r := range drop {
		l.in("N1", append(append([]string{"iptables", "-D"}, r...), "-j", "DROP")...)
	}
	l.in("N1", "conntrack", "-F")
	took = l.watch("both ends on the internet path again", 150*time.Second, func() bool { return onInternet(l) })
	l.logLink("back after " + took.String())
	time.Sleep(30 * time.Second)
	if !onInternet(l) {
		l.logLink("30 s later")
		t.Error("left the internet path")
	}
}
