package mesh

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// TestLANEntry needs root; OBLAB_LANENTRY=1 runs it. A public relay P on
// the "internet" router W, a plain Linux NAT N1 with a site relay S1 and
// two members on its LAN. Both members enter through S1, punched, with P
// as the other candidate; M1's candidate carries S1's LAN address as the
// controller fills it in for two nodes on one network, M2's has none, as
// before. With P stopped and the members restarted, M1 still enters S1
// over the LAN; M2, with no relay to exchange addresses through, cannot.
func TestLANEntry(t *testing.T) {
	if os.Getenv("OBLAB_LANENTRY") != "1" || os.Getuid() != 0 {
		t.Skip("set OBLAB_LANENTRY=1 and run as root")
	}
	l := newNSLab(t, "le", "W", "P", "N1", "S1", "M1", "M2")
	l.router("W")
	l.veth("W", "wp", "10.206.0.1/24", "P", "p", "10.206.0.2/24")
	l.route("P", "10.206.0.1")
	l.in("W", "tc", "qdisc", "add", "dev", l.ifname("wp"), "root", "netem", "delay", "20ms")
	l.veth("W", "w1", "10.206.1.1/24", "N1", "n1", "10.206.1.2/24")
	l.route("N1", "10.206.1.1")
	l.router("N1")
	l.in("N1", "iptables", "-t", "nat", "-A", "POSTROUTING", "-o", l.ifname("n1"), "-j", "MASQUERADE")
	l.bridge("N1", "l1", "192.168.1.1/24")
	l.onLAN("N1", "l1", "S1", "192.168.1.2/24", "192.168.1.1")
	l.onLAN("N1", "l1", "M1", "192.168.1.3/24", "192.168.1.1")
	l.onLAN("N1", "l1", "M2", "192.168.1.4/24", "192.168.1.1")
	l.node("P", roleRelay, 1)
	l.node("S1", roleRelay, 2)
	l.node("M1", roleMember, 11, "S1", "P")
	l.node("M2", roleMember, 12, "S1", "P")
	pub := udpEndpoint("10.206.0.2", 51820)
	p := l.config("P", Listen{UDP: 51820})
	p.Links = []Link{{Peer: "S1", Kind: kindRelay}}
	p.NextHops = map[string]string{"S1": "S1"}
	p.Routing = &Routing{Mode: "static"}
	s := l.config("S1", Listen{UDP: 51998})
	s.Links = []Link{{Peer: "P", Kind: kindRelay, Dial: true, Endpoint: pub}}
	s.NextHops = map[string]string{"P": "P"}
	s.Reflectors = []string{"10.206.0.2:51820"}
	s.Routing = &Routing{Mode: "static"}
	configs := map[string]*Config{"P": p, "S1": s}
	for _, id := range []string{"M1", "M2"} {
		c1 := Candidate{ID: "c1", Relay: "S1", Punch: true}
		if id == "M1" {
			c1.Endpoint = udpEndpoint("192.168.1.2", 51998)
		}
		m := l.config(id, Listen{UDP: 51820})
		m.Entry = &Entry{Mode: "failover", AutoReturn: true, Candidates: []Candidate{c1, {ID: "c2", Relay: "P", Endpoint: pub}}}
		m.Reflectors = []string{"10.206.0.2:51820"}
		configs[id] = m
	}
	l.run(configs)
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}

	onLAN := func(id string) func() bool {
		return func() bool {
			e := l.status(id).Entry
			c := l.candidate(id, "c1")
			return e != nil && e.Candidate == "c1" && c.State == "active" && c.Via == "lan" && strings.HasPrefix(l.peer(id, "S1").Endpoint, "192.168.1.2:")
		}
	}
	// With P up both enter S1 over the LAN, as before.
	for _, id := range []string{"M1", "M2"} {
		took := l.wait(id+" on S1 over the LAN with P up", 90*time.Second, onLAN(id))
		t.Logf("%s on c1 %v after start: %+v; c2 %+v; S1 at %s", id, took, l.candidate(id, "c1"), l.candidate(id, "c2"), l.peer(id, "S1").Endpoint)
	}

	// P stops; the members restart.
	l.stopNode("P")
	for _, id := range []string{"M1", "M2"} {
		l.stopNode(id)
		os.Remove(filepath.Join(l.dirs[id], "status.json"))
	}
	started := time.Now()
	for _, id := range []string{"M1", "M2"} {
		l.startNode(id, self)
	}
	took := l.wait("M1 on S1 over the LAN with P down", 60*time.Second, onLAN("M1"))
	t.Logf("M1 on c1 %v after restart with P down: %+v; c2 %+v; S1 at %s", took, l.candidate("M1", "c1"), l.candidate("M1", "c2"), l.peer("M1", "S1").Endpoint)
	_, rtt, out := l.pings("M1", "S1", 5)
	t.Logf("M1 -> S1: median %.2f ms", rtt)
	if rtt == 0 {
		t.Errorf("M1 does not reach S1 through the mesh:\n%s", out)
	}
	for time.Since(started) < 40*time.Second {
		time.Sleep(time.Second)
	}
	e := l.status("M2").Entry
	t.Logf("M2 40 s after restart with P down: entry %+v; c1 %+v", e, l.candidate("M2", "c1"))
	if e != nil && e.Candidate != "" {
		t.Errorf("M2, with no endpoint, entered %s without P", e.Candidate)
	}
}
