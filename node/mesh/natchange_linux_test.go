package mesh

import (
	"fmt"
	"os"
	"testing"
	"time"
)

// TestMemberNewAddresses needs root; OBLAB_NATCHANGE=1 runs it. A member
// behind a NAT whose public address changes again and again, as a PPPoE
// line's does at every redial, keeps reaching its relay: the relay takes
// each new address, not only the first few.
func TestMemberNewAddresses(t *testing.T) {
	if os.Getenv("OBLAB_NATCHANGE") != "1" || os.Getuid() != 0 {
		t.Skip("set OBLAB_NATCHANGE=1 and run as root")
	}
	l := newNSLab(t, "ac", "W", "R", "N", "M")
	l.router("W")
	l.veth("W", "wr", "10.211.0.1/24", "R", "r", "10.211.0.2/24")
	l.route("R", "10.211.0.1")
	l.veth("W", "wn", "10.211.1.1/24", "N", "n", "10.211.1.10/24")
	l.route("N", "10.211.1.1")
	l.router("N")
	l.in("N", "iptables", "-t", "nat", "-A", "POSTROUTING", "-o", l.ifname("n"), "-j", "MASQUERADE")
	l.bridge("N", "b", "192.168.30.1/24")
	l.onLAN("N", "b", "M", "192.168.30.2/24", "192.168.30.1")
	l.node("R", roleRelay, 1)
	l.node("M", roleMember, 11, "R")
	r := l.config("R", Listen{UDP: 51820})
	r.Routing = &Routing{Mode: "static"}
	m := l.config("M", Listen{UDP: 51820})
	m.Entry = &Entry{Mode: "fixed", Candidates: []Candidate{{ID: "c1", Relay: "R", Endpoint: udpEndpoint("10.211.0.2", 51820)}}}
	l.run(map[string]*Config{"R": r, "M": m})
	reached := func() bool { _, rtt, _ := l.pings("M", "R", 1); return rtt > 0 }
	l.wait("M reaching R", 60*time.Second, reached)
	old := "10.211.1.10/24"
	for i := 11; i <= 17; i++ {
		next := fmt.Sprintf("10.211.1.%d/24", i)
		l.in("N", "ip", "addr", "add", next, "dev", l.ifname("n"))
		l.in("N", "ip", "addr", "del", old, "dev", l.ifname("n"))
		l.in("N", "ip", "route", "replace", "default", "via", "10.211.1.1")
		l.in("N", "conntrack", "-F")
		old = next
		took := l.wait(fmt.Sprintf("M reaching R from address %d", i-10), 90*time.Second, reached)
		t.Logf("address %s: M reached R after %v; R sees M at %s", next, took.Round(time.Second), l.peer("R", "M").Endpoint)
	}
}
