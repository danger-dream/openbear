package mesh

import (
	"encoding/json"
	"net/netip"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"
)

// TestNetnsTakeover runs relays A and B, real data planes in their own
// namespaces, B a gateway for a LAN on a dummy link. A has a route of its
// own for exactly that LAN, through a dummy wg0 as a WireGuard tunnel's: A
// leaves it alone until told to take it over, then reaches the LAN through
// the mesh, and gets its route back when released and when stopped. A's
// docker0 holds the LAN in its wider network, which never blocks it.
// OBLAB_NETNS=1 enables it; it needs root.
func TestNetnsTakeover(t *testing.T) {
	if os.Getenv("OBLAB_NETNS") != "1" || os.Getuid() != 0 {
		t.Skip("set OBLAB_NETNS=1 and run as root")
	}
	l := newRTLab(t, []string{"A", "B"}, []string{"A-B:1"}, nil)
	lan := netip.MustParsePrefix("10.98.1.0/24")
	own := "10.98.1.0/24 dev wg0 scope link"
	for _, args := range [][]string{
		{"-n", l.ns["B"], "link", "add", "obllan", "type", "dummy"}, {"-n", l.ns["B"], "addr", "add", "10.98.1.1/24", "dev", "obllan"},
		{"-n", l.ns["B"], "link", "set", "obllan", "up"},
		{"-n", l.ns["A"], "link", "add", "wg0", "type", "dummy"}, {"-n", l.ns["A"], "link", "set", "wg0", "up"},
		{"-n", l.ns["A"], "route", "add", lan.String(), "dev", "wg0"},
		{"-n", l.ns["A"], "link", "add", "docker0", "type", "dummy"}, {"-n", l.ns["A"], "addr", "add", "10.98.0.1/16", "dev", "docker0"},
		{"-n", l.ns["A"], "link", "set", "docker0", "up"},
	} {
		rtSh(t, append([]string{"ip"}, args...)...)
	}
	revision, takeover := int64(1), []netip.Prefix(nil)
	l.configure = func(id string, c *Config) {
		c.Revision = revision
		for i := range c.Nodes {
			if c.Nodes[i].ID == "B" {
				c.Nodes[i].Subnets = []netip.Prefix{lan}
			}
		}
		routes := []netip.Prefix{}
		switch id {
		case "A":
			routes, c.Self.Takeover = append(routes, lan), takeover
		case "B":
			c.Self.Gateway = &Gateway{Subnets: []netip.Prefix{lan}, Masquerade: true}
		}
		c.Self.GatewayRoutes = &routes
	}
	write := func(id string) {
		raw, _ := json.Marshal(l.config(id))
		if err := Validate(raw); err != nil {
			t.Fatalf("%s: %v", id, err)
		}
		path := filepath.Join(l.dirs[id], "config.json")
		os.WriteFile(path+".tmp", raw, 0o600)
		if err := os.Rename(path+".tmp", path); err != nil {
			t.Fatal(err)
		}
	}
	route := func() string {
		out, _ := exec.Command("ip", "-n", l.ns["A"], "route", "show", "exact", lan.String()).Output()
		return strings.TrimSpace(string(out))
	}
	applied := func(rev int64) {
		t.Helper()
		l.waitFor("A to apply its revision", 20*time.Second, func() bool { return l.status("A").AppliedRevision == rev })
	}
	taken := func() bool {
		_, err := os.Stat(filepath.Join(l.dirs["A"], TakenFile))
		return err == nil
	}
	l.start()
	l.waitFor("both ready", 20*time.Second, func() bool { return l.status("A").Ready && l.status("B").Ready })

	applied(1)
	// B's subnet is routed once the line to B is up.
	l.waitFor("A to skip B's LAN", 20*time.Second, func() bool { return slices.Contains(l.status("A").SkippedRoutes, lan.String()) })
	if route() != own || taken() {
		t.Fatalf("not taken over: skipped %v, route %q", l.status("A").SkippedRoutes, route())
	}

	revision, takeover = 2, []netip.Prefix{lan}
	write("A")
	applied(2)
	if r := route(); !strings.Contains(r, "dev obmesh0") || strings.Contains(r, "wg0") || !taken() || len(l.status("A").SkippedRoutes) != 0 {
		t.Fatalf("taken over: route %q, kept %v, skipped %v", r, taken(), l.status("A").SkippedRoutes)
	}
	if out, err := exec.Command("ip", "netns", "exec", l.ns["A"], "ping", "-c", "1", "-W", "2", "10.98.1.1").CombinedOutput(); err != nil {
		t.Fatalf("A cannot reach B's LAN through the mesh: %s", out)
	}

	revision, takeover = 3, nil
	write("A")
	applied(3)
	if r := route(); r != own || taken() || !slices.Contains(l.status("A").SkippedRoutes, lan.String()) {
		t.Fatalf("released: route %q, kept %v", r, taken())
	}

	revision, takeover = 4, []netip.Prefix{lan}
	write("A")
	applied(4)
	if r := route(); !strings.Contains(r, "dev obmesh0") {
		t.Fatalf("taken over again: %q", r)
	}
	l.stop()
	if r := route(); r != own || taken() {
		t.Fatalf("stopped: route %q, kept %v", r, taken())
	}
}
