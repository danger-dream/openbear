package mesh

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/netip"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// TestNetnsGatewaySNAT runs a relay R and gateways A and B, each a real data
// plane in its own oblab-s namespace, with two LAN devices behind each on a
// bridge: a talker that routes the other LAN through its gateway and a quiet
// one with no route beyond its own LAN, as a machine behind a router that
// knows nothing of the mesh. Without SNAT a talker cannot reach the other
// side's quiet device: it answers an address it has no route to. With SNAT
// on both gateways it can, either way. OBLAB_NETNS=1 enables it; it needs
// root.
func TestNetnsGatewaySNAT(t *testing.T) {
	if os.Getenv("OBLAB_NETNS") != "1" || os.Getuid() != 0 {
		t.Skip("set OBLAB_NETNS=1 and run as root")
	}
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	ids := []string{"R", "A", "B"}
	ns := map[string]string{"R": "oblab-sr", "A": "oblab-sa", "B": "oblab-sb", "A1": "oblab-sa1", "A2": "oblab-sa2", "B1": "oblab-sb1", "B2": "oblab-sb2"}
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
		if out, _ := exec.Command("ip", "netns", "list").Output(); bytes.Contains(out, []byte("oblab-s")) {
			t.Errorf("namespaces left behind: %s", out)
		}
	})
	for _, n := range ns {
		sh("ip", "netns", "add", n)
		sh("ip", "-n", n, "link", "set", "lo", "up")
	}
	for i, m := range []string{"A", "B"} {
		r, x := fmt.Sprintf("oblsr%d", i), fmt.Sprintf("oblsm%d", i)
		sh("ip", "link", "add", r, "netns", ns["R"], "type", "veth", "peer", "name", x, "netns", ns[m])
		sh("ip", "-n", ns["R"], "addr", "add", fmt.Sprintf("10.99.%d.1/24", i+1), "dev", r)
		sh("ip", "-n", ns[m], "addr", "add", fmt.Sprintf("10.99.%d.2/24", i+1), "dev", x)
		sh("ip", "-n", ns["R"], "link", "set", r, "up")
		sh("ip", "-n", ns[m], "link", "set", x, "up")
	}
	lan := map[string]netip.Prefix{"A": netip.MustParsePrefix("10.98.1.0/24"), "B": netip.MustParsePrefix("10.98.2.0/24")}
	device := map[string]string{"A1": "10.98.1.2", "A2": "10.98.1.3", "B1": "10.98.2.2", "B2": "10.98.2.3"}
	for i, g := range []string{"A", "B"} {
		gwLAN := fmt.Sprintf("10.98.%d.1", i+1)
		sh("ip", "-n", ns[g], "link", "add", "oblsbr", "type", "bridge")
		sh("ip", "-n", ns[g], "addr", "add", gwLAN+"/24", "dev", "oblsbr")
		sh("ip", "-n", ns[g], "link", "set", "oblsbr", "up")
		for k, d := range []string{g + "1", g + "2"} {
			port := fmt.Sprintf("oblsp%d", k)
			sh("ip", "link", "add", port, "netns", ns[g], "type", "veth", "peer", "name", "obllan", "netns", ns[d])
			sh("ip", "-n", ns[g], "link", "set", port, "master", "oblsbr")
			sh("ip", "-n", ns[g], "link", "set", port, "up")
			sh("ip", "-n", ns[d], "addr", "add", device[d]+"/24", "dev", "obllan")
			sh("ip", "-n", ns[d], "link", "set", "obllan", "up")
		}
		other := lan[map[string]string{"A": "B", "B": "A"}[g]]
		sh("ip", "-n", ns[g+"1"], "route", "add", other.String(), "via", gwLAN)
	}

	addr := map[string]netip.Addr{"R": netip.MustParseAddr("100.96.0.1"), "A": netip.MustParseAddr("100.96.0.11"), "B": netip.MustParseAddr("100.96.0.12")}
	pubs, ctlPubs, dirs := map[string]string{}, map[string]string{}, map[string]string{}
	for _, n := range ids {
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
		if n == "R" {
			nd.Role, nd.Relays = roleRelay, nil
		} else {
			nd.Subnets = []netip.Prefix{lan[n]}
		}
		return nd
	}
	config := func(n string, revision int64, snat bool) *Config {
		routes := []netip.Prefix{}
		switch n {
		case "A":
			routes = append(routes, lan["B"])
		case "B":
			routes = append(routes, lan["A"])
		}
		c := &Config{Schema: ConfigSchema, Revision: revision, Network: Network{ID: "oblab", Prefix: meshPrefix, ControlPrefix: controlPrefix, MTU: 1400},
			Self:  Self{ID: n, Name: n, Role: roleMember, Address: addr[n], ControlAddress: node(n).ControlAddress, Listen: Listen{UDP: 51820}, GatewayRoutes: &routes},
			Rules: []Rule{{Proto: "icmp"}}, Links: []Link{}}
		for _, o := range ids {
			if o != n {
				c.Nodes = append(c.Nodes, node(o))
			}
		}
		if n == "R" {
			c.Self.Role, c.NextHops = roleRelay, map[string]string{}
			return c
		}
		c.Self.Gateway = &Gateway{Subnets: []netip.Prefix{lan[n]}, Masquerade: true, SNAT: snat}
		relay := map[string]string{"A": "10.99.1.1", "B": "10.99.2.1"}[n]
		c.Entry = &Entry{Mode: "fixed", Candidates: []Candidate{{ID: "c1", Relay: "R", Endpoint: "udp://" + relay + ":51820"}}}
		return c
	}
	write := func(n string, c *Config) {
		raw, err := json.Marshal(c)
		if err != nil {
			t.Fatal(err)
		}
		if err := Validate(raw); err != nil {
			t.Fatalf("%s: %v", n, err)
		}
		path := filepath.Join(dirs[n], "config.json")
		os.WriteFile(path+".tmp", raw, 0o600)
		if err := os.Rename(path+".tmp", path); err != nil {
			t.Fatal(err)
		}
	}
	status := func(n string) Status {
		var s Status
		raw, _ := os.ReadFile(filepath.Join(dirs[n], "status.json"))
		json.Unmarshal(raw, &s)
		return s
	}
	waitFor := func(what string, ok func() bool) {
		t.Helper()
		for deadline := time.Now().Add(20 * time.Second); !ok(); time.Sleep(200 * time.Millisecond) {
			if time.Now().After(deadline) {
				t.Fatalf("timed out waiting for %s", what)
			}
		}
	}
	procs := map[string]*exec.Cmd{}
	for _, n := range ids {
		write(n, config(n, 1, false))
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
	for _, n := range ids {
		waitFor(n+" ready", func() bool { return status(n).Ready })
	}
	ping := func(from, to string) error {
		_, err := in(from, "ping", "-c", "1", "-W", "2", device[to])
		return err
	}
	// The talkers reach each other: each has a route to the other's LAN.
	if err := ping("A1", "B1"); err != nil {
		t.Fatal("A1 cannot ping B1 through the gateways")
	}
	if ping("A1", "B2") == nil || ping("B1", "A2") == nil {
		t.Fatal("a quiet device answered a LAN it has no route to without SNAT")
	}

	write("A", config("A", 2, true))
	write("B", config("B", 2, true))
	waitFor("the SNAT revisions", func() bool { return status("A").AppliedRevision == 2 && status("B").AppliedRevision == 2 })
	for _, g := range []string{"A", "B"} {
		nat, _ := in(g, "iptables", "-t", "nat", "-S", chainNat)
		if want := "-A " + chainNat + " -s " + lan[g].String() + " -o obmesh0 -j MASQUERADE"; !strings.Contains(nat, want) {
			t.Fatalf("%s's nat chain lacks %q: %s", g, want, nat)
		}
	}
	if err := ping("A1", "B2"); err != nil {
		t.Fatal("A1 cannot ping B2 with SNAT on")
	}
	if err := ping("B1", "A2"); err != nil {
		t.Fatal("B1 cannot ping A2 with SNAT on")
	}
	if err := ping("A1", "B1"); err != nil {
		t.Fatal("A1 cannot ping B1 with SNAT on")
	}
}
