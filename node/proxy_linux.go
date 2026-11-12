//go:build linux

package node

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"time"

	"openbear/node/mesh/bypass"
)

// The node keeps package bypass's rules in place while a transparent proxy
// takes over this machine's own traffic, so the mesh data plane and the node's
// measurements see the real line. A proxy shows by what it does, whatever it
// is: a routing rule leading to a table whose default route is a TUN device
// (tun), one leading to a table that delivers everything locally (tproxy), or
// a NAT rule redirecting TCP to a local port (redirect). Checked every
// proxyEvery; the rules go only after proxyMisses checks in a row found none,
// so a proxy restarting does not take them away.

const (
	proxyEvery  = 30 * time.Second
	proxyMisses = 10
)

// proxyRun runs a command and gives its standard output; tests put others.
var proxyRun = func(stdin string, args ...string) (string, error) {
	path, err := exec.LookPath(args[0])
	if err != nil {
		for _, dir := range []string{"/usr/sbin", "/sbin", "/usr/bin", "/bin"} {
			if _, e := os.Stat(filepath.Join(dir, args[0])); e == nil {
				path, err = filepath.Join(dir, args[0]), nil
				break
			}
		}
	}
	if err != nil {
		return "", err
	}
	cmd := exec.Command(path, args[1:]...)
	if stdin != "" {
		cmd.Stdin = strings.NewReader(stdin)
	}
	var stderr strings.Builder
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		return string(out), fmt.Errorf("%s: %s", args[0], strings.TrimSpace(stderr.String()))
	}
	return string(out), nil
}

// watchProxy runs only as the installed service: a test or a manual run never
// touches the machine's routing.
func (a *Agent) watchProxy(ctx context.Context) {
	if !inService() {
		return
	}
	misses := 0
	tick := time.NewTicker(proxyEvery)
	defer tick.Stop()
	for {
		st := &ProxyStatus{Kinds: detectProxy()}
		switch {
		case len(st.Kinds) > 0:
			misses = 0
			if err := installBypass(); err != nil {
				st.Error = err.Error()
			}
		case bypassInstalled():
			if misses++; misses >= proxyMisses {
				removeBypass()
			}
		}
		st.Active = bypassInstalled()
		if old := proxyNow.Swap(st); old == nil || old.Active != st.Active || old.Error != st.Error {
			a.Logger.Printf("transparent proxy %v, bypass active %v %s", st.Kinds, st.Active, st.Error)
		}
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
	}
}

func detectProxy() []string {
	var kinds []string
	rules, _ := proxyRun("", "ip", "-4", "rule", "show")
	for _, table := range ruleTables(rules) {
		routes, _ := proxyRun("", "ip", "-4", "route", "show", "table", table)
		for _, line := range strings.Split(routes, "\n") {
			f := strings.Fields(line)
			switch {
			case len(f) >= 3 && f[0] == "default" && tunDevice(fieldAfter(f, "dev")):
				kinds = append(kinds, "tun")
			case len(f) >= 4 && f[0] == "local" && (f[1] == "default" || f[1] == "0.0.0.0/0") && fieldAfter(f, "dev") == "lo":
				kinds = append(kinds, "tproxy")
			}
		}
	}
	ruleset, _ := proxyRun("", "nft", "list", "ruleset")
	saved, _ := proxyRun("", "iptables-save", "-t", "nat")
	if nftRedirectsOutput(ruleset) || iptablesRedirectsOutput(saved) {
		kinds = append(kinds, "redirect")
	}
	slices.Sort(kinds)
	return slices.Compact(kinds)
}

// ruleTables are the tables routing rules lead to, besides the system's and
// ours.
func ruleTables(rules string) []string {
	var out []string
	for _, line := range strings.Split(rules, "\n") {
		if strings.HasPrefix(line, strconv.Itoa(bypass.RulePref)+":") {
			continue
		}
		f := strings.Fields(line)
		t := fieldAfter(f, "lookup")
		if t == "" {
			t = fieldAfter(f, "table")
		}
		if t != "" && !slices.Contains([]string{"local", "main", "default", "253", "254", "255"}, t) && !slices.Contains(out, t) {
			out = append(out, t)
		}
	}
	return out
}

func fieldAfter(f []string, key string) string {
	if i := slices.Index(f, key); i >= 0 && i+1 < len(f) {
		return f[i+1]
	}
	return ""
}

// sysNet is where tunDevice looks; tests point it elsewhere.
var sysNet = "/sys/class/net"

// vpnTUNs name the TUN devices of VPNs, whose routes are the user's choice of
// way out rather than a proxy taking traffic over: Tailscale (an exit node),
// WireGuard in user space, ZeroTier.
var vpnTUNs = []string{"tailscale", "wg", "zt"}

// tunDevice: a TUN/TAP device, as proxies use; not WireGuard's kernel devices
// (no tun_flags) nor a VPN's.
func tunDevice(name string) bool {
	if name == "" || slices.ContainsFunc(vpnTUNs, func(p string) bool { return strings.HasPrefix(name, p) }) {
		return false
	}
	_, err := os.Stat(filepath.Join(sysNet, name, "tun_flags"))
	return err == nil
}

// redirectsTCP: a NAT rule redirecting TCP to a local port, other than for DNS
// (port 53), which proxies take on their own.
func redirectsTCP(rule, redirect, tcp, dns string) bool {
	return strings.Contains(rule, redirect) && strings.Contains(rule, tcp) && !strings.Contains(rule, dns)
}

// nftRedirectsOutput: a chain on the output hook, or one it jumps to, redirects
// this machine's own TCP. Prerouting redirects only take what other machines
// send through it.
func nftRedirectsOutput(ruleset string) bool {
	type chainKey struct{ table, chain string }
	rules := map[chainKey][]string{}
	var output []chainKey
	var table string
	var cur *chainKey
	for _, line := range strings.Split(ruleset, "\n") {
		switch {
		case strings.HasPrefix(line, "table "):
			table, cur = strings.TrimSuffix(strings.TrimSpace(line), " {"), nil
		case strings.HasPrefix(line, "\tchain "):
			k := chainKey{table, strings.Fields(line)[1]}
			cur = &k
		case line == "\t}":
			cur = nil
		case cur != nil:
			if strings.Contains(line, "type nat hook output") {
				output = append(output, *cur)
			}
			rules[*cur] = append(rules[*cur], line)
		}
	}
	seen := map[chainKey]bool{}
	for len(output) > 0 {
		k := output[0]
		output = output[1:]
		if seen[k] {
			continue
		}
		seen[k] = true
		for _, rule := range rules[k] {
			if redirectsTCP(rule, "redirect to", "tcp", "dport 53") {
				return true
			}
			f := strings.Fields(rule)
			for i := 0; i+1 < len(f); i++ {
				if f[i] == "jump" || f[i] == "goto" {
					output = append(output, chainKey{k.table, strings.TrimRight(f[i+1], ",}")})
				}
			}
		}
	}
	return false
}

// iptablesRedirectsOutput is nftRedirectsOutput for iptables-save -t nat: OUTPUT
// and the chains it jumps to.
func iptablesRedirectsOutput(saved string) bool {
	rules := map[string][]string{}
	for _, line := range strings.Split(saved, "\n") {
		if f := strings.Fields(line); len(f) > 1 && f[0] == "-A" {
			rules[f[1]] = append(rules[f[1]], line)
		}
	}
	seen := map[string]bool{}
	for next := []string{"OUTPUT"}; len(next) > 0; {
		chain := next[0]
		next = next[1:]
		if seen[chain] {
			continue
		}
		seen[chain] = true
		for _, rule := range rules[chain] {
			if redirectsTCP(rule, "-j REDIRECT", "-p tcp", "--dport 53") {
				return true
			}
			f := strings.Fields(rule)
			for i := 0; i+1 < len(f); i++ {
				if f[i] == "-j" || f[i] == "-g" {
					next = append(next, f[i+1])
				}
			}
		}
	}
	return false
}

// bypassScript loads the table in one transaction, replacing an older one.
// The mark goes on late in the output route hook, after the proxies' marks
// (mangle, -150); the TCP hold comes before NAT redirects (-100, fw4's -1):
// once a connection has a destination NAT, later NAT chains pass it by.
func bypassScript(ipv6 bool) string {
	hold := "\t\tmeta skgid " + strconv.Itoa(bypass.GID) + " meta nfproto ipv4 meta l4proto tcp dnat ip to ip daddr\n"
	if ipv6 {
		hold += "\t\tmeta skgid " + strconv.Itoa(bypass.GID) + " meta nfproto ipv6 meta l4proto tcp dnat ip6 to ip6 daddr\n"
	}
	return "table inet " + bypass.Table + "\ndelete table inet " + bypass.Table + "\ntable inet " + bypass.Table + " {\n" +
		"\tchain route_out {\n\t\ttype route hook output priority -50; policy accept;\n" +
		"\t\tmeta skgid " + strconv.Itoa(bypass.GID) + " meta mark set " + fmt.Sprintf("0x%08x", bypass.Mark) + "\n\t}\n" +
		"\tchain hold_tcp {\n\t\ttype nat hook output priority -110; policy accept;\n" + hold + "\t}\n}\n"
}

var markRule = []string{"pref", strconv.Itoa(bypass.RulePref), "fwmark", fmt.Sprintf("0x%x", bypass.Mark), "lookup", "main"}

// installBypass puts in place what is missing: the table, the rules, then the
// state file that tells the sockets to use them. A step that fails takes the
// earlier ones out again, so nothing is left in place that the state file does
// not account for.
func installBypass() (err error) {
	defer func() {
		if err != nil {
			removeBypass()
		}
	}()
	if table, err := proxyRun("", "nft", "list", "table", "inet", bypass.Table); err != nil || !strings.Contains(table, "skgid "+strconv.Itoa(bypass.GID)) {
		// A kernel without IPv6 NAT refuses the IPv6 hold; IPv4 is what the mesh needs.
		if _, err = proxyRun(bypassScript(true), "nft", "-f", "-"); err != nil {
			if _, err = proxyRun(bypassScript(false), "nft", "-f", "-"); err != nil {
				return fmt.Errorf("nftables: %w", err)
			}
		}
	}
	for _, family := range []string{"-4", "-6"} {
		if out, err := proxyRun("", "ip", family, "rule", "show"); err == nil && hasMarkRule(out) {
			continue
		}
		if _, err := proxyRun("", append([]string{"ip", family, "rule", "add"}, markRule...)...); err != nil && family == "-4" {
			return fmt.Errorf("ip rule: %w", err)
		}
	}
	if _, err := os.Stat(bypass.StateFile); err != nil {
		data, _ := json.Marshal(map[string]any{"mark": bypass.Mark, "gid": bypass.GID, "since": time.Now().Unix()})
		os.MkdirAll(filepath.Dir(bypass.StateFile), 0o755)
		if err := os.WriteFile(bypass.StateFile, data, 0o644); err != nil {
			return err
		}
		bypass.Forget()
	}
	return nil
}

func hasMarkRule(rules string) bool {
	for _, line := range strings.Split(rules, "\n") {
		f := strings.Fields(line)
		if len(f) > 0 && f[0] == strconv.Itoa(bypass.RulePref)+":" && fieldAfter(f, "fwmark") == markRule[3] && fieldAfter(f, "lookup") == "main" {
			return true
		}
	}
	return false
}

func bypassInstalled() bool {
	_, err := os.Stat(bypass.StateFile)
	return err == nil
}

// bypassLeft: the table or the rule is there though the state file is not (it was lost, or a
// node from before wrote none).
func bypassLeft() bool {
	if _, err := proxyRun("", "nft", "list", "table", "inet", bypass.Table); err == nil {
		return true
	}
	out, err := proxyRun("", "ip", "-4", "rule", "show")
	return err == nil && hasMarkRule(out)
}

// removeBypass takes everything out again, the state file first so no socket
// asks for rules that are going.
func removeBypass() {
	os.Remove(bypass.StateFile)
	bypass.Forget()
	proxyRun("", "nft", "delete", "table", "inet", bypass.Table)
	for _, family := range []string{"-4", "-6"} {
		for range 4 {
			if _, err := proxyRun("", append([]string{"ip", family, "rule", "del"}, markRule...)...); err != nil {
				break
			}
		}
	}
}
