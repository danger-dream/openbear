package mesh

import (
	"net/netip"
	"os"
	"strings"
	"testing"
	"time"
)

// TestRoamingGatewayRoutes needs root; OBLAB_ROAMING=1 runs it. G is a
// gateway for 10.212.50.0/24; M, a laptop, starts on a network with the
// same addresses (at the office), so that subnet is not routed into the
// mesh. M leaves it (goes home): the route appears and M reaches the LAN
// through the mesh, without a new config or a restart. Back on such a
// network, the route goes again and the local network is M's own.
func TestRoamingGatewayRoutes(t *testing.T) {
	if os.Getenv("OBLAB_ROAMING") != "1" || os.Getuid() != 0 {
		t.Skip("set OBLAB_ROAMING=1 and run as root")
	}
	l := newNSLab(t, "ro", "R", "M", "G")
	l.veth("R", "rm", "10.212.1.1/24", "M", "m", "10.212.1.2/24")
	l.veth("R", "rg", "10.212.2.1/24", "G", "g", "10.212.2.2/24")
	lan := netip.MustParsePrefix("10.212.50.0/24")
	l.sh("ip", "-n", l.ns["G"], "link", "add", l.ifname("glan"), "type", "dummy")
	l.sh("ip", "-n", l.ns["G"], "addr", "add", "10.212.50.1/24", "dev", l.ifname("glan"))
	l.sh("ip", "-n", l.ns["G"], "link", "set", l.ifname("glan"), "up")
	office := l.ifname("mlan")
	l.sh("ip", "-n", l.ns["M"], "link", "add", office, "type", "dummy")
	l.sh("ip", "-n", l.ns["M"], "addr", "add", "10.212.50.7/24", "dev", office)
	l.sh("ip", "-n", l.ns["M"], "link", "set", office, "up")
	l.node("R", roleRelay, 1)
	l.node("M", roleMember, 11, "R")
	l.node("G", roleMember, 12, "R")
	g := l.nodes["G"]
	g.Subnets = []netip.Prefix{lan}
	l.nodes["G"] = g
	r := l.config("R", Listen{UDP: 51820})
	r.Routing = &Routing{Mode: "static"}
	m := l.config("M", Listen{UDP: 51820})
	m.Entry = &Entry{Mode: "fixed", Candidates: []Candidate{{ID: "c1", Relay: "R", Endpoint: udpEndpoint("10.212.1.1", 51820)}}}
	gc := l.config("G", Listen{UDP: 51820})
	gc.Entry = &Entry{Mode: "fixed", Candidates: []Candidate{{ID: "c1", Relay: "R", Endpoint: udpEndpoint("10.212.2.1", 51820)}}}
	gc.Self.Gateway = &Gateway{Subnets: []netip.Prefix{lan}}
	l.run(map[string]*Config{"R": r, "M": m, "G": gc})
	routed := func() bool { return strings.Contains(l.in("M", "ip", "route", "show", lan.String()), "obmesh0") }
	l.wait("M up", 60*time.Second, func() bool { return l.status("M").Ready })
	time.Sleep(2 * time.Second)
	if routed() || !strings.Contains(strings.Join(l.status("M").SkippedRoutes, " "), lan.String()) {
		t.Fatalf("M routes its own network into the mesh: %q; skipped %v", l.in("M", "ip", "route"), l.status("M").SkippedRoutes)
	}

	l.sh("ip", "-n", l.ns["M"], "addr", "del", "10.212.50.7/24", "dev", office)
	took := l.wait("the gateway's subnet routed once M left that network", 30*time.Second, routed)
	t.Logf("routed %v after M left the network", took.Round(time.Second))
	l.wait("M reaching the gateway's LAN", 30*time.Second, func() bool {
		out, _ := execIn(l.ns["M"], "ping", "-n", "-c", "1", "-W", "1", "10.212.50.1")
		return strings.Contains(out, " 0% packet loss")
	})

	l.sh("ip", "-n", l.ns["M"], "addr", "add", "10.212.50.7/24", "dev", office)
	l.wait("the route gone once M is on such a network again", 30*time.Second, func() bool { return !routed() })
}
