package mesh

import (
	"os"
	"strings"
	"testing"
	"time"
)

// reachLab: relays A and B on the "internet" router W, and a member M
// either on W itself or behind a NAT router N whose kind the case picks.
// M's entry is A; its standby to B's TCP port goes nowhere.
func reachLab(t *testing.T, nat string) *nsLab {
	names := []string{"W", "A", "B", "M"}
	if nat != "open" {
		names = append(names, "N")
	}
	l := newNSLab(t, "rq", names...)
	l.router("W")
	l.veth("W", "wa", "10.201.10.1/24", "A", "a", "10.201.10.2/24")
	l.route("A", "10.201.10.1")
	l.veth("W", "wb", "10.201.11.1/24", "B", "b", "10.201.11.2/24")
	l.route("B", "10.201.11.1")
	l.in("B", "iptables", "-A", "INPUT", "-p", "tcp", "--dport", "52099", "-j", "DROP")
	if nat == "open" {
		l.veth("W", "wm", "10.201.1.1/24", "M", "m", "10.201.1.2/24")
		l.route("M", "10.201.1.1")
	} else {
		l.veth("W", "wn", "10.201.1.1/24", "N", "nw", "10.201.1.2/24")
		l.route("N", "10.201.1.1")
		l.router("N")
		l.bridge("N", "lan", "192.168.1.1/24")
		l.onLAN("N", "lan", "M", "192.168.1.2/24", "192.168.1.1")
		wan := l.ifname("nw")
		masq := []string{"iptables", "-t", "nat", "-A", "POSTROUTING", "-o", wan, "-j", "MASQUERADE"}
		switch nat {
		case "port":
			l.in("N", masq...)
			l.in("N", "iptables", "-A", "INPUT", "-i", wan, "-m", "conntrack", "--ctstate", "NEW", "-j", "DROP")
		case "varies":
			l.in("N", append(masq, "--random-fully")...)
		case "address":
			// Packets from an address M sent UDP to pass, from any port.
			l.in("N", masq...)
			l.in("N", "iptables", "-t", "mangle", "-A", "FORWARD", "-o", wan, "-p", "udp", "-m", "recent", "--name", "out", "--rdest", "--set")
			l.in("N", "iptables", "-t", "nat", "-A", "PREROUTING", "-i", wan, "-p", "udp", "--dport", "51820", "-m", "recent", "--name", "out", "--rsource", "--rcheck", "--seconds", "600",
				"-j", "DNAT", "--to-destination", "192.168.1.2:51820")
		case "no_udp":
			l.in("N", masq...)
			l.in("N", "iptables", "-A", "FORWARD", "-o", wan, "-p", "udp", "-j", "DROP")
		case "inconsistent":
			// Each relay sees M from another public address.
			l.sh("ip", "-n", l.ns["N"], "addr", "add", "10.201.1.3/24", "dev", wan)
			l.in("N", "iptables", "-t", "nat", "-A", "POSTROUTING", "-o", wan, "-d", "10.201.10.0/24", "-j", "SNAT", "--to-source", "10.201.1.2")
			l.in("N", "iptables", "-t", "nat", "-A", "POSTROUTING", "-o", wan, "-d", "10.201.11.0/24", "-j", "SNAT", "--to-source", "10.201.1.3")
		default:
			t.Fatalf("unknown case %s", nat)
		}
	}
	l.node("A", roleRelay, 1)
	l.node("B", roleRelay, 2)
	l.node("M", roleMember, 11, "A", "B")
	a := l.config("A", Listen{UDP: 51820, TCP: 52000})
	a.Links = []Link{{Peer: "B", Kind: kindRelay, Dial: true, Endpoint: udpEndpoint("10.201.11.2", 51820)}}
	a.NextHops = map[string]string{"B": "B"}
	a.Reflectors = []string{"10.201.11.2:51820"}
	b := l.config("B", Listen{UDP: 51820})
	b.Links = []Link{{Peer: "A", Kind: kindRelay}}
	b.NextHops = map[string]string{"A": "A"}
	b.Reflectors = []string{"10.201.10.2:51820"}
	// Without a UDP listener when open: the port is an ephemeral one.
	listen := Listen{UDP: 51820}
	if nat == "open" {
		listen = Listen{}
	}
	m := l.config("M", listen)
	entry := udpEndpoint("10.201.10.2", 51820)
	if nat == "no_udp" {
		entry = "tcp://10.201.10.2:52000"
	}
	m.Entry = &Entry{Mode: "failover", Candidates: []Candidate{{ID: "c1", Relay: "A", Endpoint: entry}, {ID: "c2", Relay: "B", Endpoint: "tcp://10.201.11.2:52099"}}}
	m.Reflectors = []string{"10.201.10.2:51820", "10.201.11.2:51820"}
	l.run(map[string]*Config{"A": a, "B": b, "M": m})
	return l
}

// TestReachability needs root; OBLAB_REACH=1 runs every case.
func TestReachability(t *testing.T) {
	if os.Getenv("OBLAB_REACH") != "1" || os.Getuid() != 0 {
		t.Skip("set OBLAB_REACH=1 and run as root")
	}
	for _, c := range []struct {
		nat                                   string
		verdict, mapping, filtering, publicIP string
		consistent                            bool
	}{
		{"open", "public", "stable", "open", "10.201.1.2", true},
		{"address", "punchable", "stable", "address", "10.201.1.2", true},
		{"port", "punchable", "stable", "port", "10.201.1.2", true},
		{"varies", "symmetric", "varies", "port", "10.201.1.2", true},
		{"no_udp", "no_udp", "unknown", "unknown", "", true},
		{"inconsistent", "unknown", "stable", "port", "10.201.1.2", false},
	} {
		t.Run(c.nat, func(t *testing.T) {
			l := reachLab(t, c.nat)
			took := l.wait("M's reachability", 90*time.Second, func() bool {
				r := l.status("M").Reachability
				return r != nil && r.CheckedAt > 0
			})
			r := l.status("M").Reachability
			t.Logf("checked %v after start: %+v", took, *r)
			if r.Verdict != c.verdict || r.Mapping != c.mapping || r.Filtering != c.filtering || r.Consistent != c.consistent || !strings.HasPrefix(r.PublicAddr, c.publicIP) {
				t.Fatalf("got %s/%s/%s consistent=%v %s, want %s/%s/%s consistent=%v %s", r.Verdict, r.Mapping, r.Filtering, r.Consistent, r.PublicAddr,
					c.verdict, c.mapping, c.filtering, c.consistent, c.publicIP)
			}
			if len(r.Observers) != 2 || r.Observers[0].ID != "A" {
				t.Fatalf("observers %+v", r.Observers)
			}
			if ev := l.events("M", "reach.changed"); c.verdict != "unknown" && (len(ev) == 0 || ev[len(ev)-1] != "unknown -> "+c.verdict) {
				t.Fatalf("reach.changed events %v", ev)
			}
			// The standby that goes nowhere shows so.
			l.wait("the standby failing", 60*time.Second, func() bool { return l.candidate("M", "c2").State == "failing" })
			c1, c2 := l.candidate("M", "c1"), l.candidate("M", "c2")
			t.Logf("candidates %+v %+v; A %+v", c1, c2, l.status("A").Reachability)
			if c1.State != "active" || c1.LastOKAt == 0 || c2.LastError != "timeout" || c2.LastOKAt != 0 || c2.Transport != "tcp" {
				t.Fatalf("candidates %+v %+v", c1, c2)
			}
		})
	}
}
