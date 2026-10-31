package mesh

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/netip"
	meshclock "openbear/node/mesh/clock"
	"openbear/node/mesh/health"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"
)

// TestNetnsNode is the data plane process the namespace test starts.
func TestNetnsNode(t *testing.T) {
	dir := os.Getenv("OBLAB_MESH_DIR")
	if dir == "" {
		t.Skip("run by TestNetns")
	}
	// A node of another release: OBLAB_PROTOCOL and OBLAB_CONFIG_SCHEMAS,
	// "min,max", stand in for its ranges.
	if v := os.Getenv("OBLAB_PROTOCOL"); v != "" {
		fmt.Sscanf(v, "%d,%d", &protocol.Min, &protocol.Max)
	}
	if v := os.Getenv("OBLAB_CONFIG_SCHEMAS"); v != "" {
		fmt.Sscanf(v, "%d,%d", &configSchemas[0], &configSchemas[1])
	}
	// A node on Windows' clock: OBLAB_CLOCK=windows.
	if os.Getenv("OBLAB_CLOCK") == "windows" {
		defer meshclock.Simulate(windowsClock())()
	}
	// Redialing on test time: OBLAB_REDIAL=fast.
	if os.Getenv("OBLAB_REDIAL") == "fast" {
		defer health.SetRedialParams(fastRedial())()
	}
	if err := RunService(dir); err != nil {
		t.Fatal(err)
	}
}

// rootLinksLeft returns a check, for the end of a namespace test, that the
// root namespace has no link with one of prefixes it did not have when
// rootLinksLeft ran: this machine may run a mesh node of its own.
func rootLinksLeft(t *testing.T, prefixes ...string) func() {
	links := func() []string {
		var out []string
		b, _ := exec.Command("ip", "-o", "link", "show").Output()
		for _, l := range strings.Split(string(b), "\n") {
			if f := strings.Fields(l); len(f) > 1 {
				name, _, _ := strings.Cut(strings.TrimSuffix(f[1], ":"), "@")
				if slices.ContainsFunc(prefixes, func(p string) bool { return strings.HasPrefix(name, p) }) {
					out = append(out, name)
				}
			}
		}
		return out
	}
	had := links()
	return func() {
		for _, name := range links() {
			if !slices.Contains(had, name) {
				t.Errorf("link left behind in the root namespace: %s", name)
			}
		}
	}
}

// TestNetns runs two members and a relay, each a real mesh.Run on obmesh0
// in its own oblab- namespace: R in the middle, A and B on separate links
// to it. OBLAB_NETNS=1 enables it; it needs root.
func TestNetns(t *testing.T) {
	if os.Getenv("OBLAB_NETNS") != "1" || os.Getuid() != 0 {
		t.Skip("set OBLAB_NETNS=1 and run as root")
	}
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	ns := map[string]string{"R": "oblab-mr", "A": "oblab-ma", "B": "oblab-mb"}
	sh := func(args ...string) string {
		t.Helper()
		out, err := exec.Command(args[0], args[1:]...).CombinedOutput()
		if err != nil {
			t.Fatalf("%s: %v: %s", strings.Join(args, " "), err, out)
		}
		return string(out)
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
	linksLeft := rootLinksLeft(t, "oblab", "obmesh")
	t.Cleanup(func() {
		cleanup()
		if out, _ := exec.Command("ip", "netns", "list").Output(); bytes.Contains(out, []byte("oblab-m")) {
			t.Errorf("namespaces left behind: %s", out)
		}
		linksLeft()
	})
	for _, n := range ns {
		sh("ip", "netns", "add", n)
		sh("ip", "-n", n, "link", "set", "lo", "up")
	}
	// Namespaces may inherit forwarding; B's gateway must turn it on and back off.
	sh("ip", "netns", "exec", ns["B"], "sysctl", "-qw", "net.ipv4.ip_forward=0")
	for i, m := range []string{"A", "B"} {
		r, x := fmt.Sprintf("oblr%d", i), fmt.Sprintf("oblm%d", i)
		sh("ip", "link", "add", r, "netns", ns["R"], "type", "veth", "peer", "name", x, "netns", ns[m])
		sh("ip", "-n", ns["R"], "addr", "add", fmt.Sprintf("10.99.%d.1/24", i+1), "dev", r)
		sh("ip", "-n", ns[m], "addr", "add", fmt.Sprintf("10.99.%d.2/24", i+1), "dev", x)
		sh("ip", "-n", ns["R"], "link", "set", r, "up")
		sh("ip", "-n", ns[m], "link", "set", x, "up")
	}

	addr := map[string]netip.Addr{"R": netip.MustParseAddr("100.96.0.1"), "A": netip.MustParseAddr("100.96.0.11"), "B": netip.MustParseAddr("100.96.0.12")}
	keys, pubs, ctlPubs := map[string]string{}, map[string]string{}, map[string]string{}
	dirs := map[string]string{}
	for n := range ns {
		keys[n], pubs[n] = newKey(t)
		dirs[n] = t.TempDir()
		os.MkdirAll(filepath.Join(dirs[n], "keys"), 0o700)
		os.WriteFile(filepath.Join(dirs[n], "keys", "wg.key"), []byte(keys[n]), 0o600)
		var ctl string
		ctl, ctlPubs[n] = newKey(t)
		os.WriteFile(filepath.Join(dirs[n], "keys", "control.key"), []byte(ctl), 0o600)
		writeTestKeys(t, dirs[n], n)
	}
	var lanOfB []netip.Prefix
	node := func(n string) Node {
		nd := Node{ID: n, Name: n, Role: roleMember, Address: addr[n], ControlAddress: netip.AddrFrom4([4]byte{100, 96, 255, addr[n].As4()[3]}),
			PublicKey: pubs[n], ControlKey: ctlPubs[n], SignKey: testSignPub(n), E2EKey: testE2EPub(n), Relays: []string{"R"}}
		if n == "B" {
			nd.Subnets = lanOfB
		}
		if n == "R" {
			nd.Role, nd.Relays = roleRelay, nil
		}
		return nd
	}
	config := func(n string, revision int64, rules []Rule) *Config {
		c := &Config{Schema: ConfigSchema, Revision: revision, Network: Network{ID: "oblab", Prefix: meshPrefix, ControlPrefix: controlPrefix, MTU: 1400},
			Self:  Self{ID: n, Name: n, Role: roleMember, Address: addr[n], ControlAddress: node(n).ControlAddress, Listen: Listen{UDP: 51820}},
			Rules: rules, Links: []Link{}}
		for _, o := range []string{"R", "A", "B"} {
			if o != n {
				c.Nodes = append(c.Nodes, node(o))
			}
		}
		if n == "R" {
			c.Self.Role, c.NextHops = roleRelay, map[string]string{}
			return c
		}
		relay := "10.99.1.1"
		if n == "B" {
			relay = "10.99.2.1"
		}
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

	icmpOnly := []Rule{{Proto: "icmp"}}
	write("R", config("R", 1, nil))
	write("A", config("A", 1, nil))
	write("B", config("B", 1, icmpOnly))
	procs := map[string]*exec.Cmd{}
	for _, n := range []string{"R", "A", "B"} {
		log, _ := os.Create(filepath.Join(dirs[n], "mesh.log"))
		cmd := exec.Command("ip", "netns", "exec", ns[n], self, "-test.run", "^TestNetnsNode$", "-test.v")
		cmd.Env = append(os.Environ(), "OBLAB_MESH_DIR="+dirs[n])
		cmd.Stdout, cmd.Stderr = log, log
		if err := cmd.Start(); err != nil {
			t.Fatal(err)
		}
		procs[n] = cmd
	}
	stopped := map[string]bool{}
	stop := func(n string) {
		if stopped[n] {
			return
		}
		stopped[n] = true
		procs[n].Process.Signal(os.Interrupt)
		done := make(chan error, 1)
		go func() { done <- procs[n].Wait() }()
		select {
		case <-done:
		case <-time.After(10 * time.Second):
			procs[n].Process.Kill()
			t.Errorf("%s did not stop on SIGINT", n)
		}
	}
	t.Cleanup(func() {
		for n := range procs {
			stop(n)
		}
		if t.Failed() {
			for n := range procs {
				raw, _ := os.ReadFile(filepath.Join(dirs[n], "mesh.log"))
				t.Logf("%s log:\n%s", n, raw)
			}
		}
	})
	for _, n := range []string{"R", "A", "B"} {
		waitFor(n+" ready", func() bool { s := status(n); return s.Ready && s.AppliedRevision == 1 && s.PID == procs[n].Process.Pid })
	}

	ping := func(from, to string) bool {
		_, err := in(from, "ping", "-c", "1", "-W", "2", addr[to].String())
		return err == nil
	}
	if !ping("A", "B") {
		t.Fatal("A cannot ping B through R")
	}
	if ping("B", "A") {
		t.Fatal("B pinged A, whose rules allow nothing")
	}
	if s := status("R"); s.Counters.Forwarded < 2 || s.Counters.DroppedACL != 0 {
		t.Fatalf("R counters %+v", s.Counters)
	}
	if s := status("A"); s.Counters.DroppedACL == 0 {
		t.Fatalf("A counters %+v: B's ping was not refused by A's rules", s.Counters)
	}

	// TCP to B is refused until B's rules open the port, online.
	server := exec.Command("ip", "netns", "exec", ns["B"], "python3", "-I", "-c", `
import socket
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("100.96.0.12", 7000)); s.listen()
while True:
    c, _ = s.accept(); c.sendall(b"hi"); c.close()
`)
	if err := server.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { server.Process.Kill(); server.Wait() })
	connect := func() bool {
		out, err := in("A", "python3", "-I", "-c", `
import socket
s = socket.create_connection(("100.96.0.12", 7000), timeout=2); print(s.recv(2).decode())`)
		return err == nil && strings.Contains(out, "hi")
	}
	time.Sleep(300 * time.Millisecond)
	if connect() {
		t.Fatal("A reached B:7000 without a rule")
	}
	handshakes := func(n string) map[string]string {
		out := map[string]string{}
		for _, p := range status(n).Peers {
			out[p.ID] = p.LastHandshakeUnixNano
		}
		return out
	}
	before := handshakes("R")
	write("B", config("B", 2, append(icmpOnly, Rule{Src: []netip.Prefix{host(addr["A"])}, Proto: "tcp", Ports: [][2]uint16{{7000, 7000}}})))
	waitFor("B applying revision 2", func() bool { return status("B").AppliedRevision == 2 })
	if !connect() {
		t.Fatal("A cannot reach B:7000 after B's rules opened it")
	}

	// R learns a new member online; its existing sessions stay.
	_, extra := newKey(t)
	_, extraCtl := newKey(t)
	rc := config("R", 2, nil)
	rc.Nodes = append(rc.Nodes, Node{ID: "C", Role: roleMember, Address: netip.MustParseAddr("100.96.0.13"),
		ControlAddress: netip.MustParseAddr("100.96.255.13"), PublicKey: extra, ControlKey: extraCtl, SignKey: testSignPub("C"), E2EKey: testE2EPub("C"), Relays: []string{"R"}})
	write("R", rc)
	waitFor("R applying revision 2", func() bool { return status("R").AppliedRevision == 2 })
	if len(status("R").Peers) != 3 {
		t.Fatalf("R peers %+v", status("R").Peers)
	}
	if after := handshakes("R"); after["A"] != before["A"] || after["B"] != before["B"] {
		t.Fatalf("R's sessions were renewed by the update: %v -> %v", before, after)
	}
	if !ping("A", "B") {
		t.Fatal("A cannot ping B after R's update")
	}

	// A bad config is refused and the old one keeps running.
	bad := config("B", 3, nil)
	bad.Self.Address = netip.MustParseAddr("10.0.0.1")
	raw, _ := json.Marshal(bad)
	path := filepath.Join(dirs["B"], "config.json")
	os.WriteFile(path+".tmp", raw, 0o600)
	os.Rename(path+".tmp", path)
	waitFor("B reporting revision 3", func() bool { s := status("B"); return s.ConfigRevision == 3 && s.Error != "" })
	if s := status("B"); s.AppliedRevision != 2 || !ping("A", "B") {
		t.Fatalf("B after a refused config: applied %d, error %q", s.AppliedRevision, s.Error)
	}

	// B becomes a gateway with address translation for a LAN on a dummy
	// link; A reaches it through R, and R forgets the member C again.
	sh("ip", "-n", ns["B"], "link", "add", "obldummy", "type", "dummy")
	sh("ip", "-n", ns["B"], "addr", "add", "10.98.0.1/24", "dev", "obldummy")
	sh("ip", "-n", ns["B"], "link", "set", "obldummy", "up")
	lanOfB = []netip.Prefix{netip.MustParsePrefix("10.98.0.0/24")}
	gw := config("B", 4, icmpOnly)
	gw.Self.Gateway = &Gateway{Subnets: lanOfB, Masquerade: true}
	write("B", gw)
	write("R", config("R", 3, nil))
	write("A", config("A", 2, nil))
	waitFor("the gateway revisions", func() bool {
		return status("B").AppliedRevision == 4 && status("R").AppliedRevision == 3 && status("A").AppliedRevision == 2
	})
	if out, _ := in("A", "ip", "route", "show", "10.98.0.0/24"); !strings.Contains(out, "dev obmesh0") {
		t.Fatalf("A has no route to B's LAN: %q", out)
	}
	if _, err := in("A", "ping", "-c", "1", "-W", "2", "10.98.0.1"); err != nil {
		t.Fatal("A cannot ping B's LAN")
	}
	if fwd, _ := in("B", "cat", "/proc/sys/net/ipv4/ip_forward"); strings.TrimSpace(fwd) != "1" {
		t.Fatalf("B's ip_forward is %q", fwd)
	}
	if nat, _ := in("B", "iptables", "-t", "nat", "-S"); !strings.Contains(nat, "-A POSTROUTING -j OBMESH-NAT") ||
		!strings.Contains(nat, "-A OBMESH-NAT -s 100.96.0.0/16 -d 10.98.0.0/24 -j MASQUERADE") {
		t.Fatalf("B's nat table: %s", nat)
	}
	if mss, _ := in("B", "iptables", "-t", "mangle", "-S"); !strings.Contains(mss, "-A FORWARD -j OBMESH-MSS") ||
		!strings.Contains(mss, "-A OBMESH-MSS -o obmesh0 -p tcp -m tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu") {
		t.Fatalf("B's mangle table: %s", mss)
	}
	if len(status("R").Peers) != 2 {
		t.Fatalf("R kept C: %+v", status("R").Peers)
	}

	// Firewall: the jump comes back after a flush, the chains go at exit.
	rules, _ := in("R", "iptables", "-S", "INPUT")
	if !strings.Contains(rules, "-A INPUT -j OBMESH-IN") {
		t.Fatalf("R's INPUT: %s", rules)
	}
	chain, _ := in("R", "iptables", "-S", "OBMESH-IN")
	if !strings.Contains(chain, "-i obmesh0 -j ACCEPT") || !strings.Contains(chain, "--dport 51820 -j ACCEPT") {
		t.Fatalf("OBMESH-IN: %s", chain)
	}
	in("R", "iptables", "-F", "INPUT")
	waitFor("the INPUT jump to come back", func() bool {
		rules, _ := in("R", "iptables", "-S", "INPUT")
		return strings.Contains(rules, "-A INPUT -j OBMESH-IN")
	})
	stop("R")
	if s := status("R"); s.Ready {
		t.Error("R's last status still says ready")
	}
	rules, _ = in("R", "iptables", "-S")
	if strings.Contains(rules, "OBMESH") {
		t.Fatalf("R left firewall rules: %s", rules)
	}
	if links, _ := in("R", "ip", "link", "show", "obmesh0"); !strings.Contains(links, "does not exist") {
		t.Fatalf("R left its interface: %s", links)
	}
	stop("B")
	if fwd, _ := in("B", "cat", "/proc/sys/net/ipv4/ip_forward"); strings.TrimSpace(fwd) != "0" {
		t.Fatalf("B left ip_forward at %q", fwd)
	}
	for _, table := range []string{"filter", "nat"} {
		if rules, _ := in("B", "iptables", "-t", table, "-S"); strings.Contains(rules, "OBMESH") {
			t.Fatalf("B left %s rules: %s", table, rules)
		}
	}
}
