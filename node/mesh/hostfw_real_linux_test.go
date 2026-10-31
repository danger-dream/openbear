package mesh

import (
	"fmt"
	"net/netip"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

// Real packets through the host firewall, linuxHost doing what it does on a
// node. The mesh device is a veth named obmesh0 (the firewall goes by the
// name only); its other end, in oblab-fwg, stands for the gateway across the
// mesh at 100.96.0.10. oblab-fwc stands for a container whose port Docker
// publishes on the mesh address: DNAT 100.96.0.13:18970 → 172.31.0.2:18961.
// Port 18980 listens on the host side itself.
//
// TestHostFirewallNetnsIptables runs it in oblab- namespaces of this machine
// (OBLAB_NETNS=1). TestHostFirewallContainer runs inside a privileged test
// container (OBLAB_HOSTFW=firewalld or nftables, see hostfw_container.sh),
// the container's own network namespace being the host side.

const (
	fwPeer = "oblab-fwg"
	fwCtr  = "oblab-fwc"
	fwHost = "oblab-fwh"
	fwDNAT = 18970 // published on the mesh address, to the container
	fwIn   = 18980 // listening on the host side
)

type fwLab struct {
	t      *testing.T
	hostNS string // "" in a test container: the host side is where the test runs
	procs  []*exec.Cmd
}

// abs gives the command with the program's full path, as lookCmd finds it:
// with OBLAB_HOSTFW_SBIN, PATH has no sbin directory and the programs are
// only in one standing for /sbin, inside a namespace too.
func abs(args ...string) []string {
	if p, err := lookCmd(args[0]); err == nil {
		return append([]string{p}, args[1:]...)
	}
	return args
}

func command(args ...string) *exec.Cmd {
	args = abs(args...)
	return exec.Command(args[0], args[1:]...)
}

// x runs a command on the host side.
func (l *fwLab) x(args ...string) (string, error) {
	if l.hostNS != "" {
		args = append([]string{"ip", "netns", "exec", l.hostNS}, abs(args...)...)
	}
	return hostExec(args...)
}

func (l *fwLab) must(args ...string) string {
	l.t.Helper()
	out, err := l.x(args...)
	if err != nil {
		l.t.Fatal(err)
	}
	return out
}

func (l *fwLab) in(ns string, args ...string) string {
	l.t.Helper()
	out, err := hostExec(append([]string{"ip", "netns", "exec", ns}, abs(args...)...)...)
	if err != nil {
		l.t.Fatal(err)
	}
	return out
}

func (l *fwLab) bg(prefix []string, args ...string) {
	l.t.Helper()
	cmd := command(append(prefix, abs(args...)...)...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := cmd.Start(); err != nil {
		l.t.Fatal(err)
	}
	l.procs = append(l.procs, cmd)
}

func (l *fwLab) hostPrefix() []string {
	if l.hostNS != "" {
		return []string{"ip", "netns", "exec", l.hostNS}
	}
	return []string{"env"}
}

func newFWLab(t *testing.T, hostNS string) *fwLab {
	l := &fwLab{t: t, hostNS: hostNS}
	if dir := os.Getenv("OBLAB_HOSTFW_SBIN"); dir != "" {
		// As a node under systemd on a system keeping them in /sbin: not in PATH.
		dirs := cmdDirs
		cmdDirs = []string{dir}
		t.Cleanup(func() { cmdDirs = dirs })
		for _, name := range []string{"iptables", "nft", "ip"} {
			if p, err := exec.LookPath(name); err == nil {
				t.Fatalf("%s in PATH (%s): %s", name, os.Getenv("PATH"), p)
			}
		}
		t.Logf("PATH=%s; the programs only in %s", os.Getenv("PATH"), dir)
	}
	spaces := []string{fwPeer, fwCtr}
	if hostNS != "" {
		spaces = append(spaces, hostNS)
	}
	cleanup := func() {
		for _, p := range l.procs {
			syscall.Kill(-p.Process.Pid, syscall.SIGKILL)
			p.Wait()
		}
		l.procs = nil
		for _, n := range spaces {
			command("ip", "netns", "del", n).Run()
		}
		if hostNS == "" {
			command("ip", "link", "del", "obmesh0").Run()
			command("ip", "link", "del", "obd0").Run()
		}
	}
	cleanup()
	t.Cleanup(func() {
		cleanup()
		if out, _ := command("ip", "netns", "list").Output(); strings.Contains(string(out), "oblab-fw") {
			t.Errorf("namespaces left behind: %s", out)
		}
	})
	for _, n := range spaces {
		if _, err := hostExec("ip", "netns", "add", n); err != nil {
			t.Fatal(err)
		}
		l.in(n, "ip", "link", "set", "lo", "up")
	}
	// Never the live mesh device: the host side must have none.
	if _, err := l.x("ip", "link", "show", "obmesh0"); err == nil {
		t.Fatal("obmesh0 exists on the host side; refusing to run here")
	}
	l.must("ip", "link", "add", "obmesh0", "type", "veth", "peer", "name", "m0", "netns", fwPeer)
	l.in(fwPeer, "ip", "addr", "add", "100.96.0.10/32", "dev", "m0")
	l.in(fwPeer, "ip", "link", "set", "m0", "up")
	l.in(fwPeer, "ip", "route", "add", "100.96.0.0/16", "dev", "m0", "src", "100.96.0.10")
	l.must("ip", "link", "add", "obd0", "type", "veth", "peer", "name", "eth0", "netns", fwCtr)
	l.must("ip", "addr", "add", "172.31.0.1/24", "dev", "obd0")
	l.must("ip", "link", "set", "obd0", "up")
	l.in(fwCtr, "ip", "addr", "add", "172.31.0.2/24", "dev", "eth0")
	l.in(fwCtr, "ip", "link", "set", "eth0", "up")
	l.in(fwCtr, "ip", "route", "add", "default", "via", "172.31.0.1")
	l.must("sysctl", "-qw", "net.ipv4.ip_forward=1")
	l.bg([]string{"ip", "netns", "exec", fwCtr}, "socat", "TCP-LISTEN:18961,fork,reuseaddr", "SYSTEM:echo ok")
	l.bg(l.hostPrefix(), "socat", "TCP-LISTEN:"+strconv.Itoa(fwIn)+",fork,reuseaddr", "SYSTEM:echo ok")
	return l
}

// reach: a TCP connection from the gateway's side to the mesh address gets
// established and the far end's "ok" back.
func (l *fwLab) reach(port int) bool {
	out, err := hostExec("ip", "netns", "exec", fwPeer, abs("socat")[0], "-u", "TCP:100.96.0.13:"+strconv.Itoa(port)+",connect-timeout=2", "-")
	return err == nil && strings.TrimSpace(out) == "ok"
}

// expect checks both paths, a few tries for a listener still starting.
func (l *fwLab) expect(what string, in, dnat bool) {
	l.t.Helper()
	var gotIn, gotDNAT bool
	for try := 0; try < 3; try++ {
		gotIn, gotDNAT = l.reach(fwIn), l.reach(fwDNAT)
		if gotIn == in && gotDNAT == dnat {
			l.t.Logf("%s: to the host %v, to the container %v", what, gotIn, gotDNAT)
			return
		}
		if !in && !dnat {
			break // a refusal does not get better by waiting
		}
		time.Sleep(300 * time.Millisecond)
	}
	l.t.Fatalf("%s: to the host %v (want %v), to the container %v (want %v)", what, gotIn, in, gotDNAT, dnat)
}

// observe: the host path must be as wanted; the container path is logged
// (firewalld may let DNAT through by its own "ct status dnat accept").
func (l *fwLab) observe(what string, in bool) {
	l.t.Helper()
	gotIn, gotDNAT := l.reach(fwIn), l.reach(fwDNAT)
	l.t.Logf("%s: to the host %v, to the container %v", what, gotIn, gotDNAT)
	if gotIn != in {
		l.t.Fatalf("%s: to the host %v, want %v", what, gotIn, in)
	}
}

func (l *fwLab) host(has func(string) bool) *linuxHost {
	h := newLinuxHost("obmesh0", l.x, has)
	h.forwardFile = filepath.Join(l.t.TempDir(), "ip_forward")
	l.t.Cleanup(func() { firewallSeen.Store(nil) })
	return h
}

// healUntil runs heal rounds as the data plane does, until one fixes something.
func healUntil(t *testing.T, h *linuxHost, want string) []string {
	t.Helper()
	for i := 0; i < slowEvery; i++ {
		if fixed := h.heal(); len(fixed) > 0 {
			if !slices.ContainsFunc(fixed, func(s string) bool { return strings.Contains(s, want) }) {
				t.Fatalf("heal fixed %q, want %q among them", fixed, want)
			}
			t.Logf("heal: %q", fixed)
			return fixed
		}
	}
	t.Fatalf("heal fixed nothing, want %q", want)
	return nil
}

var fwHostConfig = hostConfig{Address: netip.MustParseAddr("100.96.0.13"), Prefix: netip.MustParsePrefix("100.96.0.0/16"), MTU: 1380, UDP: 51820, TCP: 8443}

// ufw-docker's after.rules, with the drop that only let one address reach
// the port: a ufw reload restores it, and :DOCKER-USER empties the chain.
const ufwDockerAfterRules = `*filter
:ufw-user-forward - [0:0]
:ufw-docker-logging-deny - [0:0]
:DOCKER-USER - [0:0]
-A DOCKER-USER -j ufw-user-forward
-A DOCKER-USER -p tcp -m conntrack --ctorigdstport 18970 ! -s 192.0.2.42 -j DROP
-A DOCKER-USER -j RETURN -s 10.0.0.0/8
-A DOCKER-USER -j RETURN -s 172.16.0.0/12
-A DOCKER-USER -j RETURN -s 192.168.0.0/16
-A DOCKER-USER -j ufw-docker-logging-deny -p tcp -m tcp --tcp-flags FIN,SYN,RST,ACK SYN -d 172.16.0.0/12
-A DOCKER-USER -j RETURN
-A ufw-docker-logging-deny -j DROP
COMMIT
`

func TestHostFirewallNetnsIptables(t *testing.T) {
	if os.Getenv("OBLAB_NETNS") == "" {
		t.Skip("OBLAB_NETNS=1 runs it in network namespaces (root)")
	}
	l := newFWLab(t, fwHost)
	// As ufw and Docker leave it: input dropped but for what is allowed,
	// forwarding dropped but for the published port.
	for _, r := range [][]string{
		{"-P", "INPUT", "DROP"}, {"-A", "INPUT", "-i", "lo", "-j", "ACCEPT"},
		{"-A", "INPUT", "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"},
		{"-P", "FORWARD", "DROP"},
		{"-A", "FORWARD", "-o", "obd0", "-d", "172.31.0.2/32", "-p", "tcp", "--dport", "18961", "-j", "ACCEPT"},
		{"-A", "FORWARD", "-o", "obd0", "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"},
		{"-A", "FORWARD", "-i", "obd0", "-j", "ACCEPT"},
		{"-t", "nat", "-A", "PREROUTING", "-d", "100.96.0.13/32", "-p", "tcp", "--dport", "18970", "-j", "DNAT", "--to-destination", "172.31.0.2:18961"},
	} {
		l.must(append([]string{"iptables", "-w"}, r...)...)
	}
	// The mesh address is not up before the data plane: as at boot.
	l.must("ip", "addr", "add", "100.96.0.13/32", "dev", "obmesh0")
	l.must("ip", "link", "set", "obmesh0", "up")
	l.must("ip", "route", "add", "100.96.0.0/16", "dev", "obmesh0", "src", "100.96.0.13")
	l.expect("before the data plane", false, true)

	// nft stays: it must leave iptables-nft's own tables to iptables. No
	// firewalld here, and busctl would ask this machine's bus.
	h := l.host(func(name string) bool { return hostHas(name) && name != "busctl" && name != "firewall-cmd" })
	if err := h.apply(fwHostConfig); err != nil {
		t.Fatal(err)
	}
	l.expect("data plane up, no Docker user rules yet", true, true)

	// Docker starts after it: DOCKER-USER, then ufw-docker's rules.
	l.must("iptables", "-w", "-N", "DOCKER-USER")
	l.must("iptables", "-w", "-I", "FORWARD", "1", "-j", "DOCKER-USER")
	rules := filepath.Join(t.TempDir(), "after.rules")
	os.WriteFile(rules, []byte(ufwDockerAfterRules), 0o644)
	l.must("iptables-restore", "-w", "-n", rules)
	l.expect("ufw-docker's DOCKER-USER, before heal", true, false)
	healUntil(t, h, "OBMESH-DOCKER jump in DOCKER-USER")
	l.expect("after heal", true, true)
	if k := hostFirewall(); !strings.Contains(k, "docker") || !strings.Contains(k, "ufw") {
		t.Fatalf("kinds %q", k)
	}

	// ufw reload: after.rules again, DOCKER-USER emptied and refilled.
	l.must("iptables-restore", "-w", "-n", rules)
	l.expect("after a ufw reload, before heal", true, false)
	healUntil(t, h, "OBMESH-DOCKER jump in DOCKER-USER")
	l.expect("after heal", true, true)
	t.Logf("DOCKER-USER:\n%s", l.must("iptables", "-w", "-S", "DOCKER-USER"))
	t.Logf("OBMESH-DOCKER (counters):\n%s", l.must("iptables", "-w", "-v", "-n", "-L", "OBMESH-DOCKER"))

	// Close takes out only its own.
	user := l.must("iptables", "-w", "-S", "DOCKER-USER")
	h.close()
	after := l.must("iptables", "-w", "-S")
	if strings.Contains(after, "OBMESH") {
		t.Fatalf("left after close:\n%s", after)
	}
	if got := l.must("iptables", "-w", "-S", "DOCKER-USER"); got != strings.Replace(user, "-A DOCKER-USER -j OBMESH-DOCKER\n", "", 1) {
		t.Fatalf("DOCKER-USER after close:\n%s\nbefore:\n%s", got, user)
	}
	l.expect("after close", false, false)
}

// A ban fail2ban put in holds over the mesh too: the mesh's jump goes after it.
func TestHostFirewallNetnsBanHolds(t *testing.T) {
	if os.Getenv("OBLAB_NETNS") == "" {
		t.Skip("OBLAB_NETNS=1 runs it in network namespaces (root)")
	}
	l := newFWLab(t, fwHost)
	for _, r := range [][]string{
		{"-P", "INPUT", "DROP"}, {"-A", "INPUT", "-i", "lo", "-j", "ACCEPT"},
		{"-A", "INPUT", "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"},
		// What fail2ban's iptables-multiport action leaves, the gateway's address banned.
		{"-N", "f2b-sshd"}, {"-A", "f2b-sshd", "-s", "100.96.0.10/32", "-j", "REJECT"}, {"-A", "f2b-sshd", "-j", "RETURN"},
		{"-I", "INPUT", "1", "-p", "tcp", "-m", "multiport", "--dports", strconv.Itoa(fwIn), "-j", "f2b-sshd"},
	} {
		l.must(append([]string{"iptables", "-w"}, r...)...)
	}
	l.must("ip", "addr", "add", "100.96.0.13/32", "dev", "obmesh0")
	l.must("ip", "link", "set", "obmesh0", "up")
	l.must("ip", "route", "add", "100.96.0.0/16", "dev", "obmesh0", "src", "100.96.0.13")
	h := l.host(func(name string) bool { return hostHas(name) && name != "busctl" && name != "firewall-cmd" })
	if err := h.apply(fwHostConfig); err != nil {
		t.Fatal(err)
	}
	t.Logf("INPUT:\n%s", l.must("iptables", "-w", "-S", "INPUT"))
	if l.reach(fwIn) {
		t.Fatal("the banned address got in over the mesh")
	}
	// Unbanned (fail2ban's unban deletes the rule): in again.
	l.must("iptables", "-w", "-D", "f2b-sshd", "-s", "100.96.0.10/32", "-j", "REJECT")
	l.observe("after the unban", true)
	h.close()
}

func TestHostFirewallContainer(t *testing.T) {
	kind := os.Getenv("OBLAB_HOSTFW")
	if kind == "" {
		t.Skip("runs inside a privileged test container: OBLAB_HOSTFW=firewalld|nftables")
	}
	if _, err := os.Stat("/.dockerenv"); err != nil {
		t.Fatal("not in a container: this test changes the firewall of where it runs")
	}
	switch kind {
	case "firewalld":
		fwFirewalld(t)
	case "nftables":
		fwNFTables(t)
	default:
		t.Fatalf("OBLAB_HOSTFW=%s", kind)
	}
}

func firewallCmd(t *testing.T, args ...string) string {
	t.Helper()
	out, err := hostExec(append([]string{"firewall-cmd"}, args...)...)
	if err != nil {
		t.Fatal(err)
	}
	return strings.TrimSpace(out)
}

func zoneOf(iface string) string {
	out, err := hostExec("firewall-cmd", "--get-zone-of-interface="+iface)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(out)
}

// startFirewalld (again): hostfw_container.sh started the first one.
func startFirewalld(t *testing.T) {
	t.Helper()
	cmd := command("firewalld", "--nofork", "--nopid")
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	go cmd.Wait()
	for i := 0; i < 100; i++ {
		if out, err := hostExec("firewall-cmd", "--state"); err == nil && strings.TrimSpace(out) == "running" {
			return
		}
		time.Sleep(200 * time.Millisecond)
	}
	t.Fatal("firewalld did not start")
}

func fwFirewalld(t *testing.T) {
	l := newFWLab(t, "")
	// Docker under firewalld: its bridges in a zone that accepts; the
	// published port by iptables-nft's DNAT.
	if !strings.Contains(firewallCmd(t, "--get-zones"), "docker") {
		firewallCmd(t, "--permanent", "--new-zone=docker")
		firewallCmd(t, "--permanent", "--zone=docker", "--set-target=ACCEPT")
	}
	firewallCmd(t, "--permanent", "--zone=docker", "--add-interface=obd0")
	firewallCmd(t, "--reload")
	l.must("iptables", "-w", "-t", "nat", "-A", "PREROUTING", "-d", "100.96.0.13/32", "-p", "tcp", "--dport", "18970", "-j", "DNAT", "--to-destination", "172.31.0.2:18961")
	l.must("ip", "addr", "add", "100.96.0.13/32", "dev", "obmesh0")
	l.must("ip", "link", "set", "obmesh0", "up")
	l.must("ip", "route", "add", "100.96.0.0/16", "dev", "obmesh0", "src", "100.96.0.13")
	public := firewallCmd(t, "--get-default-zone")
	// The user's, as users keep theirs: permanent, so reloads keep it too.
	firewallCmd(t, "--permanent", "--zone="+public, "--add-port=8443/tcp")
	firewallCmd(t, "--reload")
	l.observe("before the data plane (obmesh0 in the default zone "+public+")", false)

	noBus := os.Getenv("OBLAB_HOSTFW_NOBUS") != ""
	has := func(name string) bool { return hostHas(name) && !(noBus && name == "busctl") }
	t.Logf("busctl used: %v", has("busctl"))
	h := l.host(has)
	if err := h.apply(fwHostConfig); err != nil {
		t.Fatal(err)
	}
	if z := zoneOf("obmesh0"); z != "trusted" {
		t.Fatalf("obmesh0 in zone %q", z)
	}
	if out, err := hostExec("firewall-cmd", "--zone="+public, "--query-port=51820/udp"); err != nil {
		t.Fatalf("51820/udp not open in %s: %s %v", public, out, err)
	}
	l.expect("data plane up", true, true)
	if k := hostFirewall(); !strings.Contains(k, "firewalld") || len(hostFirewallIssues()) != 0 {
		t.Fatalf("report %q %v", k, hostFirewallIssues())
	}

	// firewall-cmd --reload: what was added at runtime.
	firewallCmd(t, "--reload")
	t.Logf("after --reload: obmesh0 in zone %q, 51820/udp open: %v", zoneOf("obmesh0"), func() bool {
		_, err := hostExec("firewall-cmd", "--zone="+public, "--query-port=51820/udp")
		return err == nil
	}())
	l.observe("after --reload, before heal", false)
	healUntil(t, h, "firewalld")
	l.expect("after heal", true, true)
	if z := zoneOf("obmesh0"); z != "trusted" {
		t.Fatalf("obmesh0 in zone %q after heal", z)
	}

	// A restart loses all runtime state too.
	command("pkill", "-TERM", "-f", "firewalld --nofork").Run()
	for i := 0; i < 50; i++ {
		if command("pgrep", "-f", "firewalld --nofork").Run() != nil {
			break
		}
		time.Sleep(200 * time.Millisecond)
	}
	startFirewalld(t)
	l.observe("after a firewalld restart, before heal", false)
	healUntil(t, h, "firewalld")
	l.expect("after heal", true, true)

	h.close()
	if z := zoneOf("obmesh0"); z != "" {
		t.Fatalf("obmesh0 still in zone %q after close", z)
	}
	if _, err := hostExec("firewall-cmd", "--zone="+public, "--query-port=51820/udp"); err == nil {
		t.Fatal("51820/udp still open after close")
	}
	if _, err := hostExec("firewall-cmd", "--zone="+public, "--query-port=8443/tcp"); err != nil {
		t.Fatal("close took the user's 8443/tcp")
	}
	l.observe("after close", false)

	// A zone the user put it in stays, and is reported.
	firewallCmd(t, "--zone=work", "--add-interface=obmesh0")
	h2 := l.host(has)
	if err := h2.apply(fwHostConfig); err != nil {
		t.Fatal(err)
	}
	if z := zoneOf("obmesh0"); z != "work" {
		t.Fatalf("the user's zone overridden: %q", z)
	}
	if got := hostFirewallIssues(); !slices.Contains(got, FirewallIssue{"firewalld_zone", "work"}) {
		t.Fatalf("issues %v", got)
	}
	l.observe("in the user's zone work", false)
	h2.close()
	if z := zoneOf("obmesh0"); z != "work" {
		t.Fatalf("close took the user's zone: %q", z)
	}

	// firewalld running (its process seen in /proc) but firewall-cmd not
	// found: reported, firewalld among the kinds, not taken for none.
	h3 := l.host(func(name string) bool { return name != "firewall-cmd" && has(name) })
	if err := h3.apply(fwHostConfig); err != nil {
		t.Fatal(err)
	}
	if k, got := hostFirewall(), hostFirewallIssues(); !strings.Contains(k, "firewalld") || !slices.Contains(got, FirewallIssue{"firewall_tool_missing", "firewalld: firewall-cmd"}) {
		t.Fatalf("firewall-cmd missing: %q %v", k, got)
	}
	t.Logf("firewall-cmd missing: %q %v", hostFirewall(), hostFirewallIssues())
	h3.close()
}

// The user's ruleset: input and forward dropped but for what it allows, the
// published port by its own DNAT. flush ruleset: a reload drops everything.
const userNFT = `flush ruleset
table inet filter {
	chain input {
		type filter hook input priority filter; policy drop;
		ct state established,related accept
		iifname "lo" accept
		tcp dport 22 accept
	}
	chain forward {
		type filter hook forward priority filter; policy drop;
		ct state established,related accept
		iifname "obd0" accept
	}
	chain output {
		type filter hook output priority filter; policy accept;
	}
}
table ip nat {
	chain prerouting {
		type nat hook prerouting priority dstnat; policy accept;
		ip daddr 100.96.0.13 tcp dport 18970 dnat to 172.31.0.2:18961
	}
}
`

func fwNFTables(t *testing.T) {
	ruleset := filepath.Join(t.TempDir(), "nftables.conf")
	os.WriteFile(ruleset, []byte(userNFT), 0o644)
	for _, withIptables := range []bool{false, true} {
		name := "nft only"
		if withIptables {
			name = "nft and iptables-nft"
		}
		t.Run(name, func(t *testing.T) {
			l := newFWLab(t, "")
			l.must("nft", "-f", ruleset)
			if withIptables {
				// iptables-nft's own table, dropping input too: OBMESH-IN's.
				l.must("iptables", "-w", "-P", "INPUT", "DROP")
				l.must("iptables", "-w", "-A", "INPUT", "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT")
			}
			l.must("ip", "addr", "add", "100.96.0.13/32", "dev", "obmesh0")
			l.must("ip", "link", "set", "obmesh0", "up")
			l.must("ip", "route", "add", "100.96.0.0/16", "dev", "obmesh0", "src", "100.96.0.13")
			l.expect("before the data plane", false, false)

			has := func(n string) bool { return hostHas(n) && (withIptables || n != "iptables") }
			h := l.host(has)
			if err := h.apply(fwHostConfig); err != nil {
				t.Fatal(err)
			}
			l.expect("data plane up", true, true)
			t.Logf("inet filter after apply:\n%s", l.must("nft", "-a", "list", "table", "inet", "filter"))
			if withIptables {
				if out := l.must("nft", "list", "table", "ip", "filter"); strings.Contains(out, nftMark) {
					t.Fatalf("rules of ours in iptables-nft's table:\n%s", out)
				}
			}
			if k := hostFirewall(); !strings.Contains(k, "nftables") {
				t.Fatalf("kinds %q", k)
			}

			// systemctl reload nftables: nft -f with flush ruleset.
			l.must("nft", "-f", ruleset)
			if withIptables {
				// iptables-nft's table went with the flush; as ufw would, put back.
				l.must("iptables", "-w", "-P", "INPUT", "DROP")
				l.must("iptables", "-w", "-A", "INPUT", "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT")
			}
			l.expect("after nft -f, before heal", false, false)
			healUntil(t, h, "nftables inet filter")
			l.expect("after heal", true, true)

			h.close()
			if out := l.must("nft", "list", "ruleset"); strings.Contains(out, nftMark) {
				t.Fatalf("left after close:\n%s", out)
			}
			if out := l.must("nft", "list", "chain", "inet", "filter", "input"); !strings.Contains(out, "tcp dport 22 accept") {
				t.Fatalf("the user's rules gone:\n%s", out)
			}
			l.expect("after close", false, false)
		})
	}
	t.Run("fail2ban", func(t *testing.T) { fwNFTablesFail2ban(t) })
}

// f2bNFT: fail2ban's nftables action as it sets itself up (inet f2b-table,
// chain f2b-chain at priority -1, policy accept, a reject for the set), the
// gateway's address banned or not.
func f2bNFT(banned bool) string {
	elements := ""
	if banned {
		elements = "elements = { 100.96.0.10 }"
	}
	return fmt.Sprintf(`table inet f2b-table {
	set addr-set-sshd {
		type ipv4_addr
		%s
	}
	chain f2b-chain {
		type filter hook input priority filter - 1; policy accept;
		tcp dport { 22, %d } ip saddr @addr-set-sshd reject with icmp port-unreachable
	}
}
`, elements, fwIn)
}

// fail2ban's table holds nothing of ours: the two rules an earlier version
// put there go on start, so its ban holds again; it is no firewall to
// report. With a table of the user's dropping by default, ours go there
// alone, and the connections get established.
func fwNFTablesFail2ban(t *testing.T) {
	dir := t.TempDir()
	only, both := filepath.Join(dir, "f2b.nft"), filepath.Join(dir, "both.nft")
	os.WriteFile(only, []byte("flush ruleset\n"+f2bNFT(true)), 0o644)
	os.WriteFile(both, []byte(userNFT+f2bNFT(false)), 0o644)
	l := newFWLab(t, "")
	l.must("nft", "-f", only)
	l.must("ip", "addr", "add", "100.96.0.13/32", "dev", "obmesh0")
	l.must("ip", "link", "set", "obmesh0", "up")
	l.must("ip", "route", "add", "100.96.0.0/16", "dev", "obmesh0", "src", "100.96.0.13")
	l.expect("fail2ban banning the gateway", false, false)
	// What dev.15 put in on Edge-Fra.
	l.must("nft", "insert", "rule", "inet", "f2b-table", "f2b-chain", "udp", "dport", "51997", "accept", "comment", `"openbear-mesh:71a71424"`)
	l.must("nft", "insert", "rule", "inet", "f2b-table", "f2b-chain", "iifname", `"obmesh0"`, "accept", "comment", `"openbear-mesh:71a71424"`)
	l.expect("an earlier version's rules in f2b-chain: the ban got round", true, false)

	has := func(n string) bool { return hostHas(n) && n != "iptables" }
	h := l.host(has)
	if err := h.apply(fwHostConfig); err != nil {
		t.Fatal(err)
	}
	if out := l.must("nft", "list", "ruleset"); strings.Contains(out, nftMark) {
		t.Fatalf("ours left or put in:\n%s", out)
	}
	t.Logf("f2b-chain after start:\n%s", l.must("nft", "-a", "list", "chain", "inet", "f2b-table", "f2b-chain"))
	l.expect("after start: the ban holds", false, false)
	if k := hostFirewall(); k != "" {
		t.Fatalf("kinds %q", k)
	}
	if fixed := h.heal(); fixed != nil {
		t.Fatalf("heal %q", fixed)
	}

	// The user's table dropping by default, fail2ban's beside it, nobody banned.
	l.must("nft", "-f", both)
	healUntil(t, h, "nftables inet filter")
	l.expect("with the user's table", true, true)
	if out := l.must("nft", "list", "table", "inet", "f2b-table"); strings.Contains(out, nftMark) {
		t.Fatalf("ours in fail2ban's table:\n%s", out)
	}
	if out := l.must("nft", "list", "chain", "inet", "filter", "input") + l.must("nft", "list", "chain", "inet", "filter", "forward"); strings.Count(out, nftMark) != len(nftRules("input", "obmesh0", firewallWant{addr: fwHostConfig.Address, udp: fwHostConfig.UDP, tcp: fwHostConfig.TCP, tls: fwHostConfig.TLS}))+2 {
		t.Fatalf("ours in the user's table:\n%s", out)
	}
	if k := hostFirewall(); k != "nftables" {
		t.Fatalf("kinds %q", k)
	}
	// fail2ban bans the gateway: ours in the user's table do not get round it.
	l.must("nft", "add", "element", "inet", "f2b-table", "addr-set-sshd", "{ 100.96.0.10 }")
	l.observe("fail2ban bans the gateway", false)
	if fixed := h.heal(); fixed != nil {
		t.Fatalf("heal %q", fixed)
	}
	h.close()
	if out := l.must("nft", "list", "ruleset"); strings.Contains(out, nftMark) {
		t.Fatalf("left after close:\n%s", out)
	}
}
