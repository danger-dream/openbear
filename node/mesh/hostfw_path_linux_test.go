package mesh

import (
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

// sbinOnly makes PATH an empty directory and cmdDirs one that stands for
// /sbin, as a service started by systemd may see it: what it returns.
func sbinOnly(t *testing.T) string {
	t.Setenv("PATH", t.TempDir())
	sbin := filepath.Join(t.TempDir(), "sbin")
	if err := os.Mkdir(sbin, 0o755); err != nil {
		t.Fatal(err)
	}
	dirs := cmdDirs
	cmdDirs = []string{sbin}
	t.Cleanup(func() { cmdDirs = dirs })
	return sbin
}

func TestLookCmd(t *testing.T) {
	sbin := sbinOnly(t)
	write(t, sbin, "iptables", "#!/bin/sh\n")
	if err := os.WriteFile(filepath.Join(sbin, "nft"), []byte("#!/bin/sh\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if p, err := lookCmd("iptables"); err != nil || p != filepath.Join(sbin, "iptables") {
		t.Fatalf("iptables: %q %v", p, err)
	}
	// Not executable, not there, a directory: not found, and said where it looked.
	os.Mkdir(filepath.Join(sbin, "ip"), 0o755)
	for _, name := range []string{"nft", "firewall-cmd", "ip"} {
		if p, err := lookCmd(name); err == nil || !strings.Contains(err.Error(), sbin) {
			t.Fatalf("%s: %q %v", name, p, err)
		}
	}
	if hostHas("nft") || !hostHas("iptables") {
		t.Fatal("hostHas")
	}
	// PATH first.
	bin := filepath.Join(t.TempDir(), "bin")
	write(t, bin, "iptables", "#!/bin/sh\n")
	t.Setenv("PATH", bin)
	if p, _ := lookCmd("iptables"); p != filepath.Join(bin, "iptables") {
		t.Fatalf("PATH first: %q", p)
	}
	// A path is taken as it is.
	if p, _ := lookCmd("/usr/sbin/whatever"); p != "/usr/sbin/whatever" {
		t.Fatal(p)
	}
	// hostExec runs it by its full path, and says what was not found.
	write(t, sbin, "echoer", "#!/bin/sh\necho \"$0 $*\"\n")
	if out, err := hostExec("echoer", "a", "b"); err != nil || strings.TrimSpace(out) != filepath.Join(sbin, "echoer")+" a b" {
		t.Fatalf("%q %v", out, err)
	}
	if _, err := hostExec("nft", "list", "ruleset"); err == nil || !strings.Contains(err.Error(), "nft: not found") {
		t.Fatal(err)
	}
}

// fakeTools answers as iptables (ufw and Docker there), ip, nft (a ruleset
// of the user's) and firewall-cmd (firewalld running) would, and writes
// down how it was called: $0 is the path it was run by.
const fakeTools = `#!/bin/sh
printf '%s\n' "$0 $*" >> "${0%/*}/calls.log"
case "${0##*/} $*" in
"iptables -w -S") printf -- '-P INPUT DROP\n-N ufw-user-input\n-N DOCKER-USER\n-A INPUT -j ufw-before-input\n' ;;
"iptables -w -t filter -S DOCKER-USER") printf -- '-N DOCKER-USER\n' ;;
"iptables -w -t filter -S "*) printf -- '-P INPUT DROP\n' ;;
"iptables -w -t "*" -D "*) exit 1 ;;
"iptables -V") echo 'iptables v1.8.9 (nf_tables)' ;;
"ip -4 route show default") echo 'default via 10.0.0.1 dev eth0' ;;
"firewall-cmd --state") echo running ;;
"firewall-cmd --get-zone-of-interface=eth0") echo public ;;
"firewall-cmd --get-zone-of-interface="*) echo 'no zone' ; exit 2 ;;
"firewall-cmd --zone="*" --query-port="*) exit 1 ;;
"nft -j list chains") echo '{"nftables":[{"chain":{"family":"inet","table":"filter","name":"input","type":"filter","hook":"input","policy":"drop"}}]}' ;;
"nft -j -a list chain inet filter input") echo '{"nftables":[{"rule":{"handle":4}}]}' ;;
esac
exit 0
`

// With no sbin directory in PATH and the programs only in one that stands
// for /sbin, linuxHost as a node runs it (hostExec, hostHas) finds them all,
// runs each by its full path, and lets the mesh through every firewall:
// OBMESH-IN and OBMESH-DOCKER, nftables, firewalld. Never ufw itself.
func TestHostFirewallWithoutSbinInPath(t *testing.T) {
	sbin := sbinOnly(t)
	t.Cleanup(func() { firewallSeen.Store(nil) })
	signs := signsIn(t)
	write(t, signs, "ufw.conf", "ENABLED=yes\n")
	for _, name := range []string{"iptables", "ip", "nft", "firewall-cmd"} {
		write(t, sbin, name, fakeTools)
	}
	for _, name := range []string{"iptables", "nft", "ip", "firewall-cmd"} {
		if _, err := exec.LookPath(name); err == nil {
			t.Fatalf("%s found in PATH", name)
		}
	}
	h := newLinuxHost("obmesh0", hostExec, hostHas)
	h.forwardFile = filepath.Join(t.TempDir(), "ip_forward")
	if err := h.apply(fwHostConfig); err != nil {
		t.Fatal(err)
	}
	raw, _ := os.ReadFile(filepath.Join(sbin, "calls.log"))
	calls := strings.Split(strings.TrimSpace(string(raw)), "\n")
	for _, c := range calls {
		if !strings.HasPrefix(c, sbin+"/") {
			t.Fatalf("not run by its full path: %q", c)
		}
	}
	ran := func(prefix string) {
		t.Helper()
		if !slices.ContainsFunc(calls, func(c string) bool { return strings.HasPrefix(c, sbin+"/"+prefix) }) {
			t.Fatalf("did not run %q; ran:\n%s", prefix, raw)
		}
	}
	ran("ip -4 address replace 100.96.0.13/32 dev obmesh0")
	ran("iptables -w -t filter -A OBMESH-IN -i obmesh0 -j ACCEPT")
	ran("iptables -w -t filter -I INPUT 1 -j OBMESH-IN")
	ran("iptables -w -t filter -A OBMESH-DOCKER -i obmesh0 -m conntrack --ctorigdst 100.96.0.13 --ctdir ORIGINAL -j ACCEPT")
	ran("iptables -w -t filter -I DOCKER-USER 1 -j OBMESH-DOCKER")
	ran(`nft insert rule inet filter input iifname "obmesh0" accept comment "openbear-mesh:`)
	ran("firewall-cmd --zone=trusted --add-interface=obmesh0")
	ran("firewall-cmd --zone=public --add-port=51820/udp")
	if slices.ContainsFunc(calls, func(c string) bool { return strings.Contains(c, "/ufw ") }) {
		t.Fatal("ran ufw")
	}
	if k := hostFirewall(); k != "ufw,docker,firewalld,nftables" {
		t.Fatalf("kinds %q", k)
	}
	if got := hostFirewallIssues(); len(got) != 0 {
		t.Fatalf("issues %v", got)
	}
}

// A firewall that shows its signs while its program is nowhere to be found
// is reported, not taken for none; without signs nothing is.
func TestFirewallToolMissing(t *testing.T) {
	f := newFakeHost(t)
	gone := []string{"iptables", "firewall-cmd", "nft"}
	has := func(name string) bool { return f.has(name) && !slices.Contains(gone, name) }
	signs := filepath.Dir(ufwConf)

	h := newLinuxHost("obmesh0", f.exec, has)
	h.forwardFile = filepath.Join(t.TempDir(), "ip_forward")
	if err := h.apply(fwHostConfig); err != nil {
		t.Fatal(err)
	}
	if k, issues := hostFirewall(), hostFirewallIssues(); k != "" || len(issues) != 0 {
		t.Fatalf("no signs: %q %v", k, issues)
	}

	write(t, signs, "ufw.conf", "ENABLED=yes\n")
	write(t, signs, "proc/1234/comm", "firewalld\n")
	write(t, signs, "proc/99/comm", "sshd\n")
	write(t, signs, "systemd/sysinit.target.wants/nftables.service", "")
	if err := h.apply(fwHostConfig); err != nil {
		t.Fatal(err)
	}
	want := []FirewallIssue{{"firewall_tool_missing", "ufw: iptables"}, {"firewall_tool_missing", "firewalld: firewall-cmd"}, {"firewall_tool_missing", "nftables: nft"}}
	if k, issues := hostFirewall(), hostFirewallIssues(); k != "ufw,firewalld,nftables" || !slices.Equal(issues, want) {
		t.Fatalf("%q %v", k, issues)
	}
	for _, c := range f.calls {
		if slices.Contains(gone, c[0]) {
			t.Fatalf("ran %v", c)
		}
	}

	// iptables without ufw: the legacy tables the kernel lists.
	os.Remove(ufwConf)
	write(t, signs, "proc/net/ip_tables_names", "filter\nnat\n")
	h.heal()
	for range slowEvery {
		h.heal() // a slow round looks again
	}
	if issues := hostFirewallIssues(); !slices.Contains(issues, FirewallIssue{"firewall_tool_missing", "iptables: iptables"}) {
		t.Fatalf("%v", issues)
	}
	h.close()
	if hostFirewall() != "" || hostFirewallIssues() != nil {
		t.Fatal("report after close")
	}
}

// nft there but not answering is reported, not taken for no ruleset.
func TestNFTListFailureReported(t *testing.T) {
	f := newFakeHost(t)
	f.listFail = true
	h := newLinuxHost("obmesh0", f.exec, f.has)
	h.forwardFile = filepath.Join(t.TempDir(), "ip_forward")
	if err := h.apply(fwHostConfig); err != nil {
		t.Fatal(err)
	}
	issues := hostFirewallIssues()
	if !slices.ContainsFunc(issues, func(i FirewallIssue) bool {
		return i.Code == "nftables_failed" && strings.Contains(i.Detail, "nft -j list chains")
	}) {
		t.Fatalf("%v", issues)
	}
	// Answering again: the issue goes with the next round.
	f.listFail = false
	h.heal()
	if slices.ContainsFunc(hostFirewallIssues(), func(i FirewallIssue) bool { return i.Code == "nftables_failed" }) {
		t.Fatalf("%v", hostFirewallIssues())
	}
}
