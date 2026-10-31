package mesh

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// The real mesh around revisions 26 to 28: public relays SG and LAX, a home
// behind a plain Linux NAT N1 and an office behind N2. In revision 1 (26)
// HOME is a member entering SG or LAX over TCP and LAX dials SG; in
// revision 2 (27) HOME is a site relay dialing SG and LAX over UDP, SG dials
// LAX, and TOY, a site relay in the office, dials SG and punches a link to
// HOME. Reflectors are as there: HOME [LAX, SG], LAX [SG], SG [LAX], TOY
// [SG, LAX].
const (
	rmSG  = "10.203.0.2:51820"
	rmLAX = "10.203.3.2:51820"
)

func reachMeshLab(t *testing.T) *nsLab {
	l := newNSLab(t, "rm", "W", "SG", "LAX", "N1", "N2", "HOME", "TOY")
	l.router("W")
	l.veth("W", "ws", "10.203.0.1/24", "SG", "s", "10.203.0.2/24")
	l.route("SG", "10.203.0.1")
	l.veth("W", "wl", "10.203.3.1/24", "LAX", "l", "10.203.3.2/24")
	l.route("LAX", "10.203.3.1")
	for i, n := range []string{"N1", "N2"} {
		k := string(rune('1' + i))
		l.veth("W", "w"+k, "10.203."+k+"0.1/24", n, "n"+k, "10.203."+k+"0.2/24")
		l.route(n, "10.203."+k+"0.1")
		l.router(n)
		l.in(n, "iptables", "-t", "nat", "-A", "POSTROUTING", "-o", l.ifname("n"+k), "-j", "MASQUERADE")
		l.in("W", "iptables", "-t", "mangle", "-A", "PREROUTING", "-i", l.ifname("w"+k), "-j", "TTL", "--ttl-dec", "5")
		l.bridge(n, "b"+k, "192.168."+k+"0.1/24")
	}
	l.onLAN("N1", "b1", "HOME", "192.168.10.2/24", "192.168.10.1")
	l.onLAN("N2", "b2", "TOY", "192.168.20.2/24", "192.168.20.1")
	l.node("SG", roleRelay, 1)
	l.node("LAX", roleRelay, 2)
	l.node("HOME", roleMember, 3, "SG", "LAX")
	l.node("TOY", roleRelay, 4)
	return l
}

// rev26 has HOME a member; TOY has no links yet.
func (l *nsLab) rev26() map[string]*Config {
	sg := l.config("SG", Listen{UDP: 51820, TCP: 52000})
	sg.Links = []Link{{Peer: "LAX", Kind: kindRelay}, {Peer: "TOY", Kind: kindRelay}}
	sg.NextHops = map[string]string{"LAX": "LAX", "TOY": "TOY"}
	sg.Reflectors = []string{rmLAX}
	lax := l.config("LAX", Listen{UDP: 51820, TCP: 52000})
	lax.Links = []Link{{Peer: "SG", Kind: kindRelay, Dial: true, Endpoint: "udp://" + rmSG}}
	lax.NextHops = map[string]string{"SG": "SG", "TOY": "SG"}
	lax.Reflectors = []string{rmSG}
	home := l.config("HOME", Listen{UDP: 51820})
	home.Entry = &Entry{Mode: "failover", Candidates: []Candidate{{ID: "c1", Relay: "SG", Endpoint: "tcp://10.203.0.2:52000"}, {ID: "c2", Relay: "LAX", Endpoint: "tcp://10.203.3.2:52000"}}}
	home.Reflectors = []string{rmSG, rmLAX}
	toy := l.config("TOY", Listen{UDP: 51820})
	toy.Links = []Link{{Peer: "SG", Kind: kindRelay, Dial: true, Endpoint: "udp://" + rmSG}}
	toy.NextHops = map[string]string{"SG": "SG", "LAX": "SG"}
	toy.Reflectors = []string{rmSG, rmLAX}
	c := map[string]*Config{"SG": sg, "LAX": lax, "HOME": home, "TOY": toy}
	for _, x := range []*Config{sg, lax, toy} {
		x.Routing = &Routing{Mode: "static"}
	}
	return c
}

// rev27 makes HOME a relay; toyPort is TOY's UDP listener.
func (l *nsLab) rev27(toyPort int) map[string]*Config {
	n := l.nodes["HOME"]
	n.Role, n.Relays = roleRelay, nil
	l.nodes["HOME"] = n
	sg := l.config("SG", Listen{UDP: 51820, TCP: 52000})
	sg.Links = []Link{{Peer: "LAX", Kind: kindRelay, Dial: true, Endpoint: "udp://" + rmLAX}, {Peer: "HOME", Kind: kindRelay}, {Peer: "TOY", Kind: kindRelay}}
	sg.NextHops = map[string]string{"LAX": "LAX", "HOME": "HOME", "TOY": "TOY"}
	sg.Reflectors = []string{rmLAX}
	lax := l.config("LAX", Listen{UDP: 51820, TCP: 52000})
	lax.Links = []Link{{Peer: "HOME", Kind: kindRelay}, {Peer: "SG", Kind: kindRelay}}
	lax.NextHops = map[string]string{"SG": "SG", "HOME": "HOME", "TOY": "SG"}
	lax.Reflectors = []string{rmSG}
	home := l.config("HOME", Listen{UDP: 51820})
	home.Links = []Link{{Peer: "LAX", Kind: kindRelay, Dial: true, Endpoint: "udp://" + rmLAX}, {Peer: "TOY", Kind: kindRelay, Dial: true, Punch: true},
		{Peer: "SG", Kind: kindRelay, Dial: true, Endpoint: "udp://" + rmSG}}
	home.NextHops = map[string]string{"SG": "SG", "LAX": "LAX", "TOY": "TOY"}
	home.Reflectors = []string{rmLAX, rmSG}
	toy := l.config("TOY", Listen{UDP: toyPort})
	toy.Links = []Link{{Peer: "HOME", Kind: kindRelay, Punch: true}, {Peer: "SG", Kind: kindRelay, Dial: true, Endpoint: "udp://" + rmSG}}
	toy.NextHops = map[string]string{"SG": "SG", "HOME": "HOME", "LAX": "SG"}
	toy.Reflectors = []string{rmSG, rmLAX}
	c := map[string]*Config{"SG": sg, "LAX": lax, "HOME": home, "TOY": toy}
	for _, x := range c {
		x.Routing = &Routing{Mode: "static"}
	}
	return c
}

// rewrite gives running nodes a new revision of their configs.
func (l *nsLab) rewrite(rev int64, configs map[string]*Config) {
	fillRelayLinks(configs)
	for id, c := range configs {
		c.Revision = rev
		raw, err := json.Marshal(c)
		if err != nil {
			l.t.Fatal(err)
		}
		if err := Validate(raw); err != nil {
			l.t.Fatalf("%s: %v", id, err)
		}
		tmp := filepath.Join(l.dirs[id], "config.json.tmp")
		os.WriteFile(tmp, raw, 0o600)
		os.Rename(tmp, filepath.Join(l.dirs[id], "config.json"))
	}
	for id := range configs {
		l.wait(id+" applying revision", 30*time.Second, func() bool { return l.status(id).AppliedRevision == rev })
	}
}

// recheck makes id judge its reachability again: its interface addresses
// changed.
func (l *nsLab) recheck(id, dev, extra string) int64 {
	before := l.status(id).Reachability.CheckedAt
	l.in(id, "ip", "addr", "add", extra, "dev", l.ifname(dev))
	l.wait(id+" checking again", 90*time.Second, func() bool {
		r := l.status(id).Reachability
		return r != nil && r.CheckedAt > before
	})
	return l.status(id).Reachability.CheckedAt
}

func linkUp(l *nsLab, id, peer string) bool {
	p := l.peer(id, peer)
	if p.Direct != nil {
		return p.Direct.State == "direct"
	}
	return p.Active && p.Endpoint != ""
}

// TestReachMesh needs root; OBLAB_REACHMESH=1 runs it.
func TestReachMesh(t *testing.T) {
	if os.Getenv("OBLAB_REACHMESH") != "1" || os.Getuid() != 0 {
		t.Skip("set OBLAB_REACHMESH=1 and run as root")
	}
	l := reachMeshLab(t)
	l.run(l.rev26())
	l.wait("reachability of all in revision 1", 90*time.Second, func() bool {
		for _, n := range []string{"SG", "LAX", "HOME", "TOY"} {
			if r := l.status(n).Reachability; r == nil || r.CheckedAt == 0 {
				return false
			}
		}
		return true
	})
	for _, n := range []string{"SG", "LAX", "HOME"} {
		t.Logf("rev 1 %s %+v", n, *l.status(n).Reachability)
	}

	// HOME becomes a relay while running.
	l.rewrite(2, l.rev27(51820))
	l.wait("HOME's relay links", 60*time.Second, func() bool { return linkUp(l, "HOME", "LAX") && linkUp(l, "HOME", "SG") })
	l.wait("HOME and TOY punched", 90*time.Second, func() bool { return linkUp(l, "HOME", "TOY") && linkUp(l, "TOY", "HOME") })
	l.recheck("HOME", "eth", "192.168.10.9/24")
	l.recheck("LAX", "l", "10.203.3.9/24")
	for _, n := range []string{"SG", "LAX", "HOME", "TOY"} {
		t.Logf("rev 2 %s %+v; events %v", n, *l.status(n).Reachability, l.events(n, "reach.changed"))
	}
	home, lax := l.status("HOME").Reachability, l.status("LAX").Reachability
	var failed []string
	for _, o := range home.Observers {
		if !o.OK {
			failed = append(failed, o.ID)
		}
	}
	if len(failed) != 0 || home.Mapping != "stable" {
		t.Errorf("HOME after becoming a relay: observers failing %v, mapping %s", failed, home.Mapping)
	}
	if lax.Verdict != "public" || lax.Filtering != "open" || len(lax.Observers) == 0 || lax.Observers[0].ID != "SG" {
		t.Errorf("LAX with site relays: %s/%s, observers %+v", lax.Verdict, lax.Filtering, lax.Observers)
	}

	// TOY moves its UDP listener while running.
	l.rewrite(3, l.rev27(51821))
	took := l.wait("TOY's links back on the new port", 60*time.Second, func() bool {
		return linkUp(l, "TOY", "SG") && linkUp(l, "TOY", "HOME") && linkUp(l, "HOME", "TOY") && strings.HasSuffix(l.peer("SG", "TOY").Endpoint, ":51821")
	})
	up := time.Now()
	t.Logf("TOY's links up %v after the move: SG %+v; HOME %+v", took, l.peer("TOY", "SG"), *l.peer("TOY", "HOME").Direct)
	time.Sleep(30 * time.Second)
	closed := 0
	for _, e := range l.status("TOY").Events {
		if e.At >= up.UnixMilli() && strings.Contains(e.Detail, "use of closed network connection") {
			closed++
		}
	}
	if closed != 0 || !linkUp(l, "TOY", "SG") || !linkUp(l, "TOY", "HOME") {
		t.Errorf("30 s after: %d closed-connection errors, SG up %v, HOME up %v", closed, linkUp(l, "TOY", "SG"), linkUp(l, "TOY", "HOME"))
	}
}

// TestReachMeshLong watches HOME's observers for minutes after it becomes a
// relay while TOY's UDP is blocked, as in the real mesh until revision 28:
// HOME's punch toward TOY keeps failing and keeps it measuring again.
// OBLAB_REACHLONG=1 runs it.
func TestReachMeshLong(t *testing.T) {
	if os.Getenv("OBLAB_REACHLONG") != "1" || os.Getuid() != 0 {
		t.Skip("set OBLAB_REACHLONG=1 and run as root")
	}
	l := reachMeshLab(t)
	l.in("N2", "iptables", "-A", "FORWARD", "-s", "192.168.20.2", "-p", "udp", "-j", "DROP")
	l.run(l.rev26())
	l.wait("HOME's reachability in revision 1", 90*time.Second, func() bool {
		r := l.status("HOME").Reachability
		return r != nil && r.CheckedAt != 0
	})
	c := l.rev27(51820)
	c["HOME"].Reflectors = []string{rmSG, rmLAX}
	l.rewrite(2, c)
	end := time.Now().Add(9 * time.Minute)
	var last int64
	bad := 0
	for time.Now().Before(end) {
		time.Sleep(5 * time.Second)
		r := l.status("HOME").Reachability
		if r == nil || r.CheckedAt == last {
			continue
		}
		last = r.CheckedAt
		for _, o := range r.Observers {
			if !o.OK {
				bad++
			}
		}
		t.Logf("HOME %s %s/%s/%s %+v; LAX link up %v", time.UnixMilli(r.CheckedAt).Format("15:04:05"), r.Verdict, r.Mapping, r.Filtering, r.Observers, linkUp(l, "HOME", "LAX"))
	}
	t.Logf("HOME reach events: %v; direct: %v", l.events("HOME", "reach.changed"), l.events("HOME", "direct.failed"))
	if bad != 0 {
		t.Errorf("%d failed observations", bad)
	}
}
