//go:build linux

package node

import (
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"openbear/node/mesh/bypass"
)

// fakeHost answers proxyRun as a machine with the given outputs would; it
// records what was run, with stdin after a "<".
type fakeHost struct {
	out   map[string]string
	calls []string
}

func (f *fakeHost) run(stdin string, args ...string) (string, error) {
	call := strings.Join(args, " ")
	if stdin != "" {
		call += " < " + stdin
	}
	f.calls = append(f.calls, call)
	if out, ok := f.out[strings.Join(args, " ")]; ok {
		return out, nil
	}
	if args[0] == "nft" && args[1] == "-f" || strings.Contains(strings.Join(args, " "), "rule add") {
		return "", nil
	}
	return "", errors.New("exit status 1")
}

func withFakeHost(t *testing.T, out map[string]string) *fakeHost {
	t.Helper()
	f := &fakeHost{out: out}
	keepRun, keepNet, keepState := proxyRun, sysNet, bypass.StateFile
	t.Cleanup(func() { proxyRun, sysNet, bypass.StateFile = keepRun, keepNet, keepState; bypass.Forget() })
	proxyRun, sysNet, bypass.StateFile = f.run, t.TempDir(), filepath.Join(t.TempDir(), "openbear-bypass")
	return f
}

func TestDetectProxyByWhatItDoes(t *testing.T) {
	// OpenClash in fake-ip mode: marked traffic to a TUN, TCP redirected, DNS hijacked.
	withFakeHost(t, map[string]string{
		"ip -4 rule show":            "0:\tfrom all lookup local\n1888:\tfrom all fwmark 0x162 lookup 354\n32766:\tfrom all lookup main\n32767:\tfrom all lookup default\n",
		"ip -4 route show table 354": "default dev utun scope link \n",
		"nft list ruleset":           openClashRuleset,
	})
	os.MkdirAll(filepath.Join(sysNet, "utun"), 0o755)
	os.WriteFile(filepath.Join(sysNet, "utun", "tun_flags"), []byte("0x1001\n"), 0o644)
	if got := detectProxy(); !slices.Equal(got, []string{"redirect", "tun"}) {
		t.Fatalf("OpenClash: %v", got)
	}

	// Tailscale with an exit node: a rule to a table whose default route is its TUN, no proxy;
	// a UDP redirect (an NTP server) and a TCP one other machines' traffic meets are none either.
	withFakeHost(t, map[string]string{
		"ip -4 rule show":           "0:\tfrom all lookup local\n5210:\tfrom all fwmark 0x80000/0xff0000 lookup main\n5270:\tfrom all lookup 52\n32766:\tfrom all lookup main\n",
		"ip -4 route show table 52": "default dev tailscale0 \n",
		"nft list ruleset": "table ip nat {\n\tchain output {\n\t\ttype nat hook output priority -100; policy accept;\n\t\tudp dport 123 redirect to :123\n\t}\n" +
			"\tchain prerouting {\n\t\ttype nat hook prerouting priority dstnat; policy accept;\n\t\tip protocol tcp redirect to :3128\n\t}\n}\n",
	})
	os.MkdirAll(filepath.Join(sysNet, "tailscale0"), 0o755)
	os.WriteFile(filepath.Join(sysNet, "tailscale0", "tun_flags"), []byte("0x1001\n"), 0o644)
	if got := detectProxy(); len(got) != 0 {
		t.Fatalf("Tailscale, a UDP and a prerouting redirect taken for a proxy: %v", got)
	}

	// iptables: OUTPUT jumps to a chain that redirects TCP.
	withFakeHost(t, map[string]string{
		"iptables-save -t nat": "*nat\n:OUTPUT ACCEPT [0:0]\n:proxy_out - [0:0]\n-A PREROUTING -p tcp -j REDIRECT --to-ports 3128\n" +
			"-A OUTPUT -p tcp -j proxy_out\n-A proxy_out -d 192.168.0.0/16 -j RETURN\n-A proxy_out -p tcp -j REDIRECT --to-ports 7892\nCOMMIT\n",
	})
	if got := detectProxy(); !slices.Equal(got, []string{"redirect"}) {
		t.Fatalf("iptables: %v", got)
	}

	// wg-quick's full tunnel: a rule to a table through WireGuard, no proxy.
	withFakeHost(t, map[string]string{
		"ip -4 rule show":              "0:\tfrom all lookup local\n32764:\tfrom all lookup main suppress_prefixlength 0\n32765:\tnot from all fwmark 0xca6c lookup 51820\n32766:\tfrom all lookup main\n",
		"ip -4 route show table 51820": "default dev wg0 scope link \n",
		"nft list ruleset":             "\t\tudp dport 53 redirect to :5353\n",
	})
	os.MkdirAll(filepath.Join(sysNet, "wg0"), 0o755)
	if got := detectProxy(); len(got) != 0 {
		t.Fatalf("WireGuard taken for a proxy: %v", got)
	}

	// A TPROXY proxy (PassWall, ShellCrash): marked traffic delivered locally.
	withFakeHost(t, map[string]string{
		"ip -4 rule show":            "0:\tfrom all lookup local\n32765:\tfrom all fwmark 0x1 lookup 100\n",
		"ip -4 route show table 100": "local default dev lo scope host \n",
		"iptables-save -t nat":       "-A OUTPUT -p tcp -m tcp --dport 53 -j REDIRECT --to-ports 5353\n",
	})
	if got := detectProxy(); !slices.Equal(got, []string{"tproxy"}) {
		t.Fatalf("TPROXY: %v", got)
	}
}

func TestBypassInstallsOnceAndComesOut(t *testing.T) {
	f := withFakeHost(t, map[string]string{"ip -4 rule show": "0:\tfrom all lookup local\n32766:\tfrom all lookup main\n"})
	if bypass.Active() {
		t.Fatal("active before")
	}
	if err := installBypass(); err != nil {
		t.Fatal(err)
	}
	joined := strings.Join(f.calls, "\n")
	for _, want := range []string{"nft -f - < table inet openbear_bypass\ndelete table inet openbear_bypass\n",
		"type route hook output priority -50", "meta skgid 52999 meta mark set 0x4f420000",
		"type nat hook output priority -110", "meta skgid 52999 meta nfproto ipv4 meta l4proto tcp dnat ip to ip daddr",
		"ip -4 rule add pref 100 fwmark 0x4f420000 lookup main", "ip -6 rule add pref 100 fwmark 0x4f420000 lookup main"} {
		if !strings.Contains(joined, want) {
			t.Fatalf("missing %q in\n%s", want, joined)
		}
	}
	if !bypass.Active() || !bypassInstalled() {
		t.Fatal("sockets not told")
	}
	// In place: nothing is loaded or added again.
	f.out["nft list table inet openbear_bypass"] = "table inet openbear_bypass {\n\tchain route_out {\n\t\tmeta skgid 52999 meta mark set 0x4f420000\n"
	f.out["ip -4 rule show"] += "100:\tfrom all fwmark 0x4f420000 lookup main\n"
	f.out["ip -6 rule show"] = "100:\tfrom all fwmark 0x4f420000 lookup main\n"
	f.calls = nil
	if err := installBypass(); err != nil || slices.ContainsFunc(f.calls, func(c string) bool { return strings.Contains(c, " -f ") || strings.Contains(c, "rule add") }) {
		t.Fatalf("again: %v %v", err, f.calls)
	}
	removeBypass()
	if bypass.Active() || !slices.Contains(f.calls, "nft delete table inet openbear_bypass") || !slices.Contains(f.calls, "ip -4 rule del pref 100 fwmark 0x4f420000 lookup main") {
		t.Fatalf("remove: %v", f.calls)
	}
}

// The rules as OpenClash leaves them (fw4, fake-ip): DNS hijacked, the machine's own TCP sent
// from nat_output on to openclash_output and redirected there, LAN TCP redirected in dstnat.
const openClashRuleset = `table inet fw4 {
	chain dstnat {
		type nat hook prerouting priority dstnat; policy accept;
		meta l4proto { tcp, udp } th dport 53 counter packets 27183 bytes 1587356 redirect to :53 comment "OpenClash DNS Hijack"
		ip protocol tcp counter packets 81546 bytes 4769808 jump openclash
	}
	chain nat_output {
		type nat hook output priority filter - 1; policy accept;
		meta skgid != 65534 meta l4proto { tcp, udp } th dport 53 ip daddr 127.0.0.1 counter packets 722 bytes 52676 redirect to :53 comment "OpenClash DNS Hijack"
		ip protocol tcp counter packets 16042 bytes 966368 jump openclash_output
	}
	chain openclash {
		ip protocol tcp counter packets 3692 bytes 214264 redirect to :7892
	}
	chain openclash_output {
		ip daddr @localnetwork counter return
		ip protocol tcp counter packets 22 bytes 1320 redirect to :7892
	}
}
table inet dnsmasq {
	chain prerouting {
		type nat hook prerouting priority dstnat + 5; policy accept;
		meta nfproto { ipv4, ipv6 } udp dport 53 counter packets 7244 bytes 516987 redirect to :53 comment "DNSMASQ HIJACK"
	}
}
`

// A step that fails takes out what the earlier ones put in place: no rule is left that the state
// file, which uninstalling and the watcher go by, does not account for.
func TestBypassThatFailsHalfwayComesOut(t *testing.T) {
	f := withFakeHost(t, map[string]string{})
	proxyRun = func(stdin string, args ...string) (string, error) {
		if strings.Join(args, " ") == "ip -4 rule add pref 100 fwmark 0x4f420000 lookup main" {
			f.calls = append(f.calls, strings.Join(args, " "))
			return "", errors.New("RTNETLINK answers: Operation not supported")
		}
		return f.run(stdin, args...)
	}
	if err := installBypass(); err == nil || bypassInstalled() || bypass.Active() {
		t.Fatalf("%v %v", err, f.calls)
	}
	if !slices.Contains(f.calls, "nft delete table inet openbear_bypass") {
		t.Fatalf("the table is left: %v", f.calls)
	}
	// Left behind without a state file (a node from before, a lost file): found all the same.
	f.out["nft list table inet openbear_bypass"] = "table inet openbear_bypass {\n"
	if !bypassLeft() {
		t.Fatal("a table left behind is not found")
	}
}

func TestBypassWithoutIPv6NAT(t *testing.T) {
	f := withFakeHost(t, nil)
	proxyRun = func(stdin string, args ...string) (string, error) {
		if stdin != "" && strings.Contains(stdin, "ip6 daddr") {
			return "", errors.New("nft: Error: Could not process rule: No such file or directory")
		}
		return f.run(stdin, args...)
	}
	if err := installBypass(); err != nil || !bypass.Active() {
		t.Fatalf("%v %v", err, f.calls)
	}
}
