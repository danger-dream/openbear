package mesh

import (
	"net/netip"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"testing"
	"time"

	meshproxy "openbear/node/mesh/proxy"
)

// The exit proxy between two data planes: A an entry on 127.0.0.1:1080
// whose user b (also the default) leads to B, an exit that may reach its own
// loopback; the link has OBLAB_PROXY_RTT ms of round trip (0). It then runs
// OBLAB_PROXY_SCRIPT, if any, with NS_A, NS_B, ADDR_A and ADDR_B set, and
// logs what it prints: benchmarks against other proxies on the same link.
// OBLAB_NETNS=1 OBLAB_PROXY=1 enables it; it needs root.
func TestProxyNetns(t *testing.T) {
	if os.Getenv("OBLAB_NETNS") != "1" || os.Getenv("OBLAB_PROXY") != "1" || os.Getuid() != 0 {
		t.Skip("set OBLAB_NETNS=1 OBLAB_PROXY=1 and run as root")
	}
	rtt := os.Getenv("OBLAB_PROXY_RTT")
	if rtt == "" {
		rtt = "0"
	}
	l := newRTLab(t, []string{"A", "B"}, []string{"A-B:" + rtt}, nil)
	a, b := l.addr["A"], l.addr["B"]
	l.configure = func(id string, c *Config) {
		c.Self.Resources = roleMember
		c.Rules = []Rule{{Proto: "any"}}
		c.Proxy = &meshproxy.Config{Port: 51080}
		if id == "A" {
			c.Proxy.Entry = &meshproxy.Entry{Listen: []netip.AddrPort{netip.MustParseAddrPort("127.0.0.1:1080")}, Password: "pw",
				Users: []meshproxy.User{{Name: "b", Exits: []netip.Addr{b}}}, Default: "b"}
		} else {
			c.Proxy.Exit = &meshproxy.Exit{LAN: true, Clients: []netip.Addr{a}}
		}
	}
	l.start()
	l.paths(60*time.Second, "A>B", "B>A")
	l.waitFor("the entry and the exit", 30*time.Second, func() bool {
		sa, sb := l.status("A"), l.status("B")
		return sa.Proxy != nil && sa.Proxy.Entry != nil && len(sa.Proxy.Entry.Listen) == 1 &&
			sb.Proxy != nil && sb.Proxy.Exit != nil && sb.Proxy.Exit.Listen != "" && sb.Proxy.Exit.Error == "" &&
			len(sa.Proxy.Entry.Users) == 1 && sa.Proxy.Entry.Users[0].Exits[0].Ready > 0
	})
	t.Logf("link rtt %s ms; entry %v; exit %s", rtt, l.status("A").Proxy.Entry.Listen, l.status("B").Proxy.Exit.Listen)
	script := os.Getenv("OBLAB_PROXY_SCRIPT")
	if script == "" {
		return
	}
	cmd := exec.Command("bash", script)
	cmd.Env = append(os.Environ(), "NS_A="+l.ns["A"], "NS_B="+l.ns["B"], "ADDR_A="+a.String(), "ADDR_B="+b.String(), "RTT="+rtt)
	out, err := cmd.CombinedOutput()
	for _, line := range strings.Split(strings.TrimSpace(string(out)), "\n") {
		t.Log(line)
	}
	if err != nil {
		t.Fatalf("%s: %v", script, err)
	}
	s := l.status("A").Proxy.Entry.Users[0]
	t.Logf("entry counted %d opened, %d failed; exit %s", s.Opened, s.Failed, strconv.FormatInt(l.status("B").Proxy.Exit.Opened, 10))
}
