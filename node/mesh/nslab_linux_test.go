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

// nsLab is a set of oblab- namespaces wired by veth pairs, each node a real
// mesh.Run in its own. Everything it makes is removed when the test ends.
type nsLab struct {
	t      *testing.T
	prefix string // of namespace and link names
	ns     map[string]string
	dirs   map[string]string
	procs  map[string]*exec.Cmd
	nodes  map[string]Node
	env    []string
}

func newNSLab(t *testing.T, prefix string, names ...string) *nsLab {
	l := &nsLab{t: t, prefix: prefix, ns: map[string]string{}, dirs: map[string]string{}, procs: map[string]*exec.Cmd{}, nodes: map[string]Node{}}
	for _, n := range names {
		l.ns[n] = "oblab-" + prefix + strings.ToLower(n)
	}
	cleanup := func() {
		for _, n := range l.ns {
			exec.Command("ip", "netns", "del", n).Run()
		}
	}
	cleanup()
	t.Cleanup(func() {
		cleanup()
		if out, _ := exec.Command("ip", "netns", "list").Output(); bytes.Contains(out, []byte("oblab-"+prefix)) {
			t.Errorf("namespaces left behind: %s", out)
		}
		if out, _ := exec.Command("ip", "link", "show").Output(); bytes.Contains(out, []byte("ob"+prefix)) {
			t.Errorf("links left behind in the root namespace: %s", out)
		}
	})
	for _, n := range l.ns {
		l.sh("ip", "netns", "add", n)
		l.sh("ip", "-n", n, "link", "set", "lo", "up")
	}
	return l
}

func (l *nsLab) sh(args ...string) string {
	l.t.Helper()
	out, err := exec.Command(args[0], args[1:]...).CombinedOutput()
	if err != nil {
		l.t.Fatalf("%s: %v: %s", strings.Join(args, " "), err, out)
	}
	return string(out)
}

func (l *nsLab) in(name string, args ...string) string {
	l.t.Helper()
	return l.sh(append([]string{"ip", "netns", "exec", l.ns[name]}, args...)...)
}

// veth joins a and b; an address may be empty, and a name ends up with
// the lab's prefix.
func (l *nsLab) veth(a, ifa, addrA, b, ifb, addrB string) {
	l.t.Helper()
	ifa, ifb = "ob"+l.prefix+ifa, "ob"+l.prefix+ifb
	l.sh("ip", "link", "add", ifa, "netns", l.ns[a], "type", "veth", "peer", "name", ifb, "netns", l.ns[b])
	for _, x := range [][3]string{{a, ifa, addrA}, {b, ifb, addrB}} {
		if x[2] != "" {
			l.sh("ip", "-n", l.ns[x[0]], "addr", "add", x[2], "dev", x[1])
		}
		l.sh("ip", "-n", l.ns[x[0]], "link", "set", x[1], "up")
	}
}

func (l *nsLab) ifname(s string) string { return "ob" + l.prefix + s }

func (l *nsLab) route(n, via string) {
	l.sh("ip", "-n", l.ns[n], "route", "add", "default", "via", via)
}

func (l *nsLab) router(n string) { l.in(n, "sysctl", "-qw", "net.ipv4.ip_forward=1") }

// bridge makes a LAN in router n at addr (a.b.c.1/24).
func (l *nsLab) bridge(n, name, addr string) {
	br := l.ifname(name)
	l.sh("ip", "-n", l.ns[n], "link", "add", br, "type", "bridge")
	l.sh("ip", "-n", l.ns[n], "addr", "add", addr, "dev", br)
	l.sh("ip", "-n", l.ns[n], "link", "set", br, "up")
}

// onLAN puts host h on router n's bridge.
func (l *nsLab) onLAN(n, br, h, addr, gw string) {
	port := strings.ToLower(n + h)
	l.veth(n, port, "", h, "eth", addr)
	l.sh("ip", "-n", l.ns[n], "link", "set", l.ifname(port), "master", l.ifname(br))
	l.route(h, gw)
}

// node registers a mesh node with its keys; i numbers its addresses.
func (l *nsLab) node(id, role string, i byte, relays ...string) {
	l.dirs[id] = l.t.TempDir()
	wgPriv, wgPub := newKey(l.t)
	ctlPriv, ctlPub := newKey(l.t)
	os.MkdirAll(filepath.Join(l.dirs[id], "keys"), 0o700)
	os.WriteFile(filepath.Join(l.dirs[id], "keys", "wg.key"), []byte(wgPriv), 0o600)
	os.WriteFile(filepath.Join(l.dirs[id], "keys", "control.key"), []byte(ctlPriv), 0o600)
	writeTestKeys(l.t, l.dirs[id], id)
	l.nodes[id] = Node{ID: id, Name: id, Role: role, Address: netip.AddrFrom4([4]byte{100, 96, 0, i}), ControlAddress: netip.AddrFrom4([4]byte{100, 96, 255, i}),
		PublicKey: wgPub, ControlKey: ctlPub, SignKey: testSignPub(id), E2EKey: testE2EPub(id), Relays: relays}
}

// config is the start of id's config: every other node, ICMP allowed.
func (l *nsLab) config(id string, listen Listen) *Config {
	n := l.nodes[id]
	c := &Config{Schema: ConfigSchema, Revision: 1, Network: Network{ID: "oblab", Prefix: meshPrefix, ControlPrefix: controlPrefix, MTU: 1400},
		Self:  Self{ID: id, Name: id, Role: n.Role, Address: n.Address, ControlAddress: n.ControlAddress, Listen: listen},
		Rules: []Rule{{Proto: "icmp"}}, Links: []Link{}, Health: &Health{ColdRetryMs: 10000}}
	for oid, o := range l.nodes {
		if oid != id {
			c.Nodes = append(c.Nodes, o)
		}
	}
	return c
}

// run writes the configs and starts every node in them.
func (l *nsLab) run(configs map[string]*Config) {
	t := l.t
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	fillRelayLinks(configs)
	for id, c := range configs {
		raw, err := json.Marshal(c)
		if err != nil {
			t.Fatal(err)
		}
		if err := Validate(raw); err != nil {
			t.Fatalf("%s: %v", id, err)
		}
		os.WriteFile(filepath.Join(l.dirs[id], "config.json"), raw, 0o600)
	}
	for id := range configs {
		l.startNode(id, self)
	}
	t.Cleanup(func() {
		for id := range l.procs {
			l.stopNode(id)
		}
		if t.Failed() {
			for id := range configs {
				raw, _ := os.ReadFile(filepath.Join(l.dirs[id], "mesh.log"))
				st, _ := os.ReadFile(filepath.Join(l.dirs[id], "status.json"))
				t.Logf("%s log:\n%s\n%s status:\n%s", id, tail(raw, 6000), id, st)
			}
		}
	})
}

// fillRelayLinks gives each relay config without relayLinks the links
// between relays the configs name, as the controller writes them.
func fillRelayLinks(configs map[string]*Config) {
	pairs := map[[2]string]bool{}
	for id, c := range configs {
		if !c.isRelay() {
			continue
		}
		for _, k := range c.Links {
			if k.Kind == kindRelay {
				a, b := id, k.Peer
				if b < a {
					a, b = b, a
				}
				pairs[[2]string{a, b}] = true
			}
		}
	}
	var links [][2]string
	for p := range pairs {
		links = append(links, p)
	}
	slices.SortFunc(links, func(x, y [2]string) int { return strings.Compare(x[0]+"\x00"+x[1], y[0]+"\x00"+y[1]) })
	for _, c := range configs {
		if c.isRelay() && len(c.RelayLinks) == 0 {
			c.RelayLinks = links
		}
	}
}

func tail(b []byte, n int) []byte {
	if len(b) > n {
		return b[len(b)-n:]
	}
	return b
}

func (l *nsLab) startNode(id, self string) {
	log, _ := os.OpenFile(filepath.Join(l.dirs[id], "mesh.log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	cmd := exec.Command("ip", "netns", "exec", l.ns[id], self, "-test.run", "^TestNetnsNode$", "-test.v")
	cmd.Env = append(append(os.Environ(), "OBLAB_MESH_DIR="+l.dirs[id]), l.env...)
	cmd.Stdout, cmd.Stderr = log, log
	if err := cmd.Start(); err != nil {
		l.t.Fatal(err)
	}
	l.procs[id] = cmd
}

func (l *nsLab) stopNode(id string) {
	cmd := l.procs[id]
	if cmd == nil {
		return
	}
	delete(l.procs, id)
	cmd.Process.Signal(os.Interrupt)
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case <-done:
	case <-time.After(10 * time.Second):
		cmd.Process.Kill()
		<-done
		l.t.Errorf("%s did not stop on SIGINT", id)
	}
}

func (l *nsLab) status(id string) Status {
	var s Status
	raw, _ := os.ReadFile(filepath.Join(l.dirs[id], "status.json"))
	json.Unmarshal(raw, &s)
	return s
}

func (l *nsLab) wait(what string, within time.Duration, ok func() bool) time.Duration {
	l.t.Helper()
	start := time.Now()
	for !ok() {
		if time.Since(start) > within {
			l.dump(what)
			l.t.Fatalf("timed out after %v waiting for %s", within, what)
		}
		time.Sleep(100 * time.Millisecond)
	}
	return time.Since(start)
}

func (l *nsLab) peer(id, peer string) PeerStatus {
	for _, p := range l.status(id).Peers {
		if p.ID == peer {
			return p
		}
	}
	return PeerStatus{}
}

func (l *nsLab) events(id, kind string) []string {
	var out []string
	for _, e := range l.status(id).Events {
		if e.Kind == kind {
			out = append(out, e.Detail)
		}
	}
	return out
}

func (l *nsLab) candidate(id, cand string) CandidateHealth {
	if e := l.status(id).Entry; e != nil {
		for _, c := range e.Candidates {
			if c.ID == cand {
				return c
			}
		}
	}
	return CandidateHealth{}
}

func udpEndpoint(addr string, port int) string { return fmt.Sprintf("udp://%s:%d", addr, port) }

// dump saves, under $OBLAB_DUMP, what every node and namespace had when a
// wait for what failed: each node's live status.json and log, and each
// namespace's routes, addresses, firewall and connection tracking.
func (l *nsLab) dump(what string) {
	root := os.Getenv("OBLAB_DUMP")
	if root == "" {
		return
	}
	dir := filepath.Join(root, fmt.Sprintf("%s-%d", l.t.Name(), time.Now().UnixMilli()))
	dir = strings.ReplaceAll(dir, " ", "_")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return
	}
	os.WriteFile(filepath.Join(dir, "what"), []byte(what+"\n"), 0o644)
	for id, d := range l.dirs {
		for _, f := range []string{"status.json", "mesh.log", "config.json"} {
			if b, err := os.ReadFile(filepath.Join(d, f)); err == nil {
				os.WriteFile(filepath.Join(dir, id+"."+f), b, 0o644)
			}
		}
	}
	for n, ns := range l.ns {
		var out bytes.Buffer
		for _, c := range [][]string{{"ip", "-d", "addr"}, {"ip", "route", "show", "table", "all"}, {"ip", "rule"}, {"iptables-save"}, {"conntrack", "-L"}, {"ss", "-uanp"}} {
			b, _ := exec.Command("ip", append([]string{"netns", "exec", ns}, c...)...).CombinedOutput()
			fmt.Fprintf(&out, "### %s\n%s\n", strings.Join(c, " "), b)
		}
		os.WriteFile(filepath.Join(dir, n+".ns.txt"), out.Bytes(), 0o644)
	}
	l.t.Logf("dumped to %s", dir)
}
