package mesh

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/netip"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"
)

// TestNetnsGatewayRoutes runs a relay R and members A and B, each a real
// data plane in its own oblab-g namespace. B is a gateway for two LANs;
// A's gatewayRoutes name only one, R's none. A routes only that one into
// the mesh and reaches it through R, which forwards it without an OS
// route for it; status shows what each installed. OBLAB_NETNS=1 enables
// it; it needs root.
func TestNetnsGatewayRoutes(t *testing.T) {
	if os.Getenv("OBLAB_NETNS") != "1" || os.Getuid() != 0 {
		t.Skip("set OBLAB_NETNS=1 and run as root")
	}
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	ns := map[string]string{"R": "oblab-gr", "A": "oblab-ga", "B": "oblab-gb"}
	sh := func(args ...string) {
		t.Helper()
		if out, err := exec.Command(args[0], args[1:]...).CombinedOutput(); err != nil {
			t.Fatalf("%s: %v: %s", strings.Join(args, " "), err, out)
		}
	}
	in := func(name string, args ...string) (string, error) {
		out, err := exec.Command("ip", append([]string{"netns", "exec", ns[name]}, args...)...).CombinedOutput()
		return string(out), err
	}
	cleanup := func() {
		for _, n := range ns {
			exec.Command("ip", "netns", "del", n).Run()
		}
	}
	cleanup()
	t.Cleanup(func() {
		cleanup()
		if out, _ := exec.Command("ip", "netns", "list").Output(); bytes.Contains(out, []byte("oblab-g")) {
			t.Errorf("namespaces left behind: %s", out)
		}
	})
	for _, n := range ns {
		sh("ip", "netns", "add", n)
		sh("ip", "-n", n, "link", "set", "lo", "up")
	}
	for i, m := range []string{"A", "B"} {
		r, x := fmt.Sprintf("oblgr%d", i), fmt.Sprintf("oblgm%d", i)
		sh("ip", "link", "add", r, "netns", ns["R"], "type", "veth", "peer", "name", x, "netns", ns[m])
		sh("ip", "-n", ns["R"], "addr", "add", fmt.Sprintf("10.99.%d.1/24", i+1), "dev", r)
		sh("ip", "-n", ns[m], "addr", "add", fmt.Sprintf("10.99.%d.2/24", i+1), "dev", x)
		sh("ip", "-n", ns["R"], "link", "set", r, "up")
		sh("ip", "-n", ns[m], "link", "set", x, "up")
	}
	lan1, lan2 := netip.MustParsePrefix("10.98.1.0/24"), netip.MustParsePrefix("10.98.2.0/24")
	for i, lan := range []string{"10.98.1.1/24", "10.98.2.1/24"} {
		dev := fmt.Sprintf("obllan%d", i)
		sh("ip", "-n", ns["B"], "link", "add", dev, "type", "dummy")
		sh("ip", "-n", ns["B"], "addr", "add", lan, "dev", dev)
		sh("ip", "-n", ns["B"], "link", "set", dev, "up")
	}

	addr := map[string]netip.Addr{"R": netip.MustParseAddr("100.96.0.1"), "A": netip.MustParseAddr("100.96.0.11"), "B": netip.MustParseAddr("100.96.0.12")}
	pubs, ctlPubs, dirs := map[string]string{}, map[string]string{}, map[string]string{}
	for n := range ns {
		var key, ctl string
		key, pubs[n] = newKey(t)
		ctl, ctlPubs[n] = newKey(t)
		dirs[n] = t.TempDir()
		os.MkdirAll(filepath.Join(dirs[n], "keys"), 0o700)
		os.WriteFile(filepath.Join(dirs[n], "keys", "wg.key"), []byte(key), 0o600)
		os.WriteFile(filepath.Join(dirs[n], "keys", "control.key"), []byte(ctl), 0o600)
		writeTestKeys(t, dirs[n], n)
	}
	node := func(n string) Node {
		nd := Node{ID: n, Name: n, Role: roleMember, Address: addr[n], ControlAddress: netip.AddrFrom4([4]byte{100, 96, 255, addr[n].As4()[3]}),
			PublicKey: pubs[n], ControlKey: ctlPubs[n], SignKey: testSignPub(n), E2EKey: testE2EPub(n), Relays: []string{"R"}}
		switch n {
		case "B":
			nd.Subnets = []netip.Prefix{lan1, lan2}
		case "R":
			nd.Role, nd.Relays = roleRelay, nil
		}
		return nd
	}
	gw := map[string][]netip.Prefix{"R": {}, "A": {lan1}, "B": {}}
	config := func(n string) *Config {
		routes := gw[n]
		c := &Config{Schema: ConfigSchema, Revision: 1, Network: Network{ID: "oblab", Prefix: meshPrefix, ControlPrefix: controlPrefix, MTU: 1400},
			Self:  Self{ID: n, Name: n, Role: roleMember, Address: addr[n], ControlAddress: node(n).ControlAddress, Listen: Listen{UDP: 51820}, GatewayRoutes: &routes},
			Rules: []Rule{{Proto: "icmp"}}, Links: []Link{}}
		for _, o := range []string{"R", "A", "B"} {
			if o != n {
				c.Nodes = append(c.Nodes, node(o))
			}
		}
		switch n {
		case "R":
			c.Self.Role, c.NextHops = roleRelay, map[string]string{}
			return c
		case "B":
			c.Self.Gateway = &Gateway{Subnets: []netip.Prefix{lan1, lan2}}
		}
		relay := map[string]string{"A": "10.99.1.1", "B": "10.99.2.1"}[n]
		c.Entry = &Entry{Mode: "fixed", Candidates: []Candidate{{ID: "c1", Relay: "R", Endpoint: "udp://" + relay + ":51820"}}}
		return c
	}
	status := func(n string) Status {
		var s Status
		raw, _ := os.ReadFile(filepath.Join(dirs[n], "status.json"))
		json.Unmarshal(raw, &s)
		return s
	}
	procs := map[string]*exec.Cmd{}
	for _, n := range []string{"R", "A", "B"} {
		raw, _ := json.Marshal(config(n))
		if err := Validate(raw); err != nil {
			t.Fatalf("%s: %v", n, err)
		}
		os.WriteFile(filepath.Join(dirs[n], "config.json"), raw, 0o600)
		log, _ := os.Create(filepath.Join(dirs[n], "mesh.log"))
		cmd := exec.Command("ip", "netns", "exec", ns[n], self, "-test.run", "^TestNetnsNode$", "-test.v")
		cmd.Env = append(os.Environ(), "OBLAB_MESH_DIR="+dirs[n])
		cmd.Stdout, cmd.Stderr = log, log
		if err := cmd.Start(); err != nil {
			t.Fatal(err)
		}
		procs[n] = cmd
	}
	t.Cleanup(func() {
		for n, cmd := range procs {
			cmd.Process.Signal(os.Interrupt)
			done := make(chan error, 1)
			go func() { done <- cmd.Wait() }()
			select {
			case <-done:
			case <-time.After(10 * time.Second):
				cmd.Process.Kill()
				t.Errorf("%s did not stop on SIGINT", n)
			}
			if t.Failed() {
				raw, _ := os.ReadFile(filepath.Join(dirs[n], "mesh.log"))
				t.Logf("%s log:\n%s", n, raw)
			}
		}
	})
	for _, n := range []string{"R", "A", "B"} {
		for deadline := time.Now().Add(20 * time.Second); !status(n).Ready; time.Sleep(200 * time.Millisecond) {
			if time.Now().After(deadline) {
				t.Fatalf("%s not ready", n)
			}
		}
	}

	route := func(n string, p netip.Prefix) string {
		out, _ := in(n, "ip", "route", "show", p.String())
		return out
	}
	if !strings.Contains(route("A", lan1), "dev obmesh0") || route("A", lan2) != "" {
		t.Fatalf("A's routes: %s: %q, %s: %q", lan1, route("A", lan1), lan2, route("A", lan2))
	}
	if route("R", lan1) != "" || route("R", lan2) != "" {
		t.Fatalf("R routes the LANs: %q %q", route("R", lan1), route("R", lan2))
	}
	if _, err := in("A", "ping", "-c", "1", "-W", "2", "10.98.1.1"); err != nil {
		t.Fatal("A cannot ping B's first LAN through R")
	}
	// A has no route to the second LAN: its packets go anywhere but the mesh.
	forwarded := status("R").Counters.Forwarded
	in("A", "ping", "-c", "1", "-W", "1", "10.98.2.1")
	if f := status("R").Counters.Forwarded; f != forwarded {
		t.Fatalf("A's ping to the second LAN went through R: %d -> %d", forwarded, f)
	}
	for n, want := range map[string][]string{"A": {lan1.String()}, "R": {}, "B": {}} {
		if got := status(n).GatewayRoutes; !slices.Equal(got, want) {
			t.Errorf("%s status gatewayRoutes %v, want %v", n, got, want)
		}
	}
}
