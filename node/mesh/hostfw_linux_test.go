package mesh

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/netip"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"testing"
)

// fakeHost stands in for the commands linuxHost runs: just enough iptables,
// nft, firewall-cmd and busctl, with their state, to check what ends up where.
type fakeHost struct {
	t        *testing.T
	calls    [][]string
	ipt      map[string]map[string][]string // table → chain → rules ("-i obmesh0 -j ACCEPT")
	policy   map[string]string              // "filter INPUT" → DROP
	nft      []*fakeNFTChain
	handle   int
	fdOn     bool
	zones    map[string]string          // interface → zone
	ports    map[string]map[string]bool // zone → "51820/udp"
	defZone  string
	noBus    bool
	failNFT  bool
	listFail bool // nft -j list chains fails
	versionV string
}

type fakeNFTChain struct {
	c     nftChain
	rules []fakeNFTRule
}

type fakeNFTRule struct {
	handle        int
	text, comment string
}

// signsIn points where the signs of a firewall are read (ufw.conf, /proc,
// systemd's units) at an empty directory, not this machine's.
func signsIn(t *testing.T) string {
	dir := t.TempDir()
	conf, proc, units := ufwConf, procRoot, systemdDir
	ufwConf, procRoot, systemdDir = filepath.Join(dir, "ufw.conf"), filepath.Join(dir, "proc"), filepath.Join(dir, "systemd")
	t.Cleanup(func() { ufwConf, procRoot, systemdDir = conf, proc, units })
	return dir
}

// write puts a file under dir, its directories made.
func write(t *testing.T, dir, name, body string) {
	t.Helper()
	p := filepath.Join(dir, name)
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
}

func newFakeHost(t *testing.T) *fakeHost {
	t.Cleanup(func() { firewallSeen.Store(nil) })
	signsIn(t)
	return &fakeHost{t: t, ipt: map[string]map[string][]string{
		"filter": {"INPUT": nil, "FORWARD": nil, "OUTPUT": nil},
		"nat":    {"PREROUTING": nil, "POSTROUTING": nil}, "mangle": {"FORWARD": nil}},
		policy: map[string]string{}, zones: map[string]string{}, ports: map[string]map[string]bool{}, defZone: "public",
		versionV: "iptables v1.8.9 (nf_tables)"}
}

func (f *fakeHost) has(name string) bool {
	switch name {
	case "busctl":
		return !f.noBus
	case "iptables-legacy":
		return false
	}
	return true
}

func (f *fakeHost) exec(args ...string) (string, error) {
	f.calls = append(f.calls, args)
	fail := func() (string, error) { return "", fmt.Errorf("%s: failed", strings.Join(args, " ")) }
	switch args[0] {
	case "ip":
		return "default via 10.0.0.1 dev eth0 proto static\n", nil
	case "iptables":
		return f.iptables(args[1:])
	case "nft":
		if f.failNFT && args[1] == "insert" || f.listFail && slices.Equal(args[1:], []string{"-j", "list", "chains"}) {
			return fail()
		}
		return f.nftCmd(args[1:])
	case "busctl":
		if !f.fdOn {
			return fail()
		}
		i := slices.Index(args, "call")
		method, rest := args[i+4], args[i+6:]
		switch method {
		case "getZoneOfInterface":
			return "s " + strconv.Quote(f.zones[rest[0]]) + "\n", nil
		case "queryPort":
			return fmt.Sprintf("b %v\n", f.ports[rest[0]][rest[1]+"/"+rest[2]]), nil
		}
		return fail()
	case "firewall-cmd":
		if !f.fdOn {
			return fail()
		}
		opt := map[string]string{}
		for _, a := range args[1:] {
			k, v, _ := strings.Cut(a, "=")
			opt[k] = v
		}
		zone := opt["--zone"]
		switch {
		case args[1] == "--state":
			return "running\n", nil
		case args[1] == "--get-default-zone":
			return f.defZone + "\n", nil
		case opt["--get-zone-of-interface"] != "":
			if z := f.zones[opt["--get-zone-of-interface"]]; z != "" {
				return z + "\n", nil
			}
			return fail()
		case opt["--add-interface"] != "":
			if f.zones[opt["--add-interface"]] != "" {
				return fail()
			}
			f.zones[opt["--add-interface"]] = zone
		case opt["--remove-interface"] != "":
			delete(f.zones, opt["--remove-interface"])
		case opt["--query-port"] != "":
			if !f.ports[zone][opt["--query-port"]] {
				return fail()
			}
		case opt["--add-port"] != "":
			if f.ports[zone] == nil {
				f.ports[zone] = map[string]bool{}
			}
			f.ports[zone][opt["--add-port"]] = true
		case opt["--remove-port"] != "":
			delete(f.ports[zone], opt["--remove-port"])
		}
		return "success\n", nil
	}
	return fail()
}

func (f *fakeHost) iptables(args []string) (string, error) {
	if args[0] == "-V" {
		return f.versionV + "\n", nil
	}
	if args[0] == "-w" {
		args = args[1:]
	}
	table := "filter"
	if args[0] == "-t" {
		table, args = args[1], args[2:]
	}
	chains := f.ipt[table]
	err := errors.New("iptables: " + strings.Join(args, " "))
	if len(args) == 1 && args[0] == "-S" {
		var out []string
		for _, name := range []string{"INPUT", "FORWARD", "OUTPUT"} {
			out = append(out, "-P "+name+" "+cmp(f.policy[table+" "+name], "ACCEPT"))
		}
		for name := range chains {
			if !slices.Contains([]string{"INPUT", "FORWARD", "OUTPUT", "PREROUTING", "POSTROUTING"}, name) {
				out = append(out, "-N "+name)
			}
		}
		for name, rules := range chains {
			for _, r := range rules {
				out = append(out, "-A "+name+" "+r)
			}
		}
		return strings.Join(out, "\n") + "\n", nil
	}
	op, name, rest := args[0], args[1], strings.Join(args[2:], " ")
	rules, ok := chains[name]
	if !ok && op != "-N" {
		return "", err
	}
	switch op {
	case "-S":
		out := []string{"-N " + name}
		for _, r := range rules {
			out = append(out, "-A "+name+" "+r)
		}
		return strings.Join(out, "\n") + "\n", nil
	case "-N":
		if ok {
			return "", err
		}
		chains[name] = nil
	case "-F":
		chains[name] = nil
	case "-X":
		delete(chains, name)
	case "-A":
		chains[name] = append(rules, rest)
	case "-I":
		at, _ := strconv.Atoi(args[2])
		chains[name] = slices.Insert(rules, max(at, 1)-1, strings.Join(args[3:], " "))
	case "-D", "-C":
		i := slices.Index(rules, rest)
		if i < 0 {
			return "", err
		}
		if op == "-D" {
			chains[name] = slices.Delete(rules, i, i+1)
		}
	}
	return "", nil
}

func cmp(v, def string) string {
	if v == "" {
		return def
	}
	return v
}

func (f *fakeHost) chain(fam, table, name string) *fakeNFTChain {
	for _, c := range f.nft {
		if c.c.Family == fam && c.c.Table == table && c.c.Name == name {
			return c
		}
	}
	return nil
}

func (f *fakeHost) nftCmd(args []string) (string, error) {
	switch {
	case slices.Equal(args, []string{"-j", "list", "chains"}):
		items := []any{map[string]any{"metainfo": map[string]any{"version": "1.0.6"}}}
		for _, c := range f.nft {
			items = append(items, map[string]any{"chain": c.c})
		}
		raw, _ := json.Marshal(map[string]any{"nftables": items})
		return string(raw), nil
	case len(args) == 7 && args[2] == "list" && args[3] == "chain":
		c := f.chain(args[4], args[5], args[6])
		if c == nil {
			return "", errors.New("no such chain")
		}
		items := []any{map[string]any{"chain": c.c}}
		for _, r := range c.rules {
			rule := map[string]any{"family": c.c.Family, "table": c.c.Table, "chain": c.c.Name, "handle": r.handle, "expr": fakeExpr(r.text)}
			if r.comment != "" {
				rule["comment"] = r.comment
			}
			items = append(items, map[string]any{"rule": rule})
		}
		raw, _ := json.Marshal(map[string]any{"nftables": items})
		return string(raw), nil
	case args[0] == "insert" || args[0] == "add" && args[5] == "position":
		c := f.chain(args[2], args[3], args[4])
		body, at := args[5:], 0
		if args[0] == "add" {
			h, _ := strconv.Atoi(args[6])
			at = slices.IndexFunc(c.rules, func(r fakeNFTRule) bool { return r.handle == h }) + 1
			body = args[7:]
		}
		i := slices.Index(body, "comment")
		comment, _ := strconv.Unquote(body[i+1])
		f.handle++
		c.rules = slices.Insert(c.rules, at, fakeNFTRule{handle: f.handle, text: strings.Join(body[:i], " "), comment: comment})
		return "", nil
	case args[0] == "delete":
		c := f.chain(args[2], args[3], args[4])
		h, _ := strconv.Atoi(args[6])
		c.rules = slices.DeleteFunc(c.rules, func(r fakeNFTRule) bool { return r.handle == h })
		return "", nil
	}
	return "", errors.New("nft: " + strings.Join(args, " "))
}

// fakeExpr: the rule's expressions as nft -j gives them, roughly: counter and
// log statements, then a match unless the verdict comes next, the verdict.
func fakeExpr(text string) []any {
	var out []any
	w := strings.Fields(text)
	for len(w) > 0 && (w[0] == "counter" || w[0] == "log") {
		out = append(out, map[string]any{w[0]: map[string]any{}})
		w = w[1:]
	}
	if len(w) > 0 && (w[0] == "drop" || w[0] == "reject" || w[0] == "accept") {
		return append(out, map[string]any{w[0]: nil})
	}
	if len(w) == 0 {
		return out
	}
	match := map[string]any{"op": "=="}
	for _, word := range w {
		if strings.HasPrefix(word, "@") {
			match["right"] = word
		}
	}
	out = append([]any{map[string]any{"match": match}}, out...)
	if len(w) > 1 && (w[len(w)-2] == "jump" || w[len(w)-2] == "goto") {
		return append(out, map[string]any{w[len(w)-2]: map[string]any{"target": w[len(w)-1]}})
	}
	return append(out, map[string]any{w[len(w)-1]: nil})
}

func (f *fakeHost) addNFT(fam, table, name, hook, policy string, rules ...string) *fakeNFTChain {
	c := &fakeNFTChain{c: nftChain{Family: fam, Table: table, Name: name, Type: "filter", Hook: hook, Policy: policy}}
	for _, r := range rules {
		f.handle++
		c.rules = append(c.rules, fakeNFTRule{handle: f.handle, text: r})
	}
	f.nft = append(f.nft, c)
	return c
}

func texts(c *fakeNFTChain) []string {
	var out []string
	for _, r := range c.rules {
		out = append(out, r.text)
	}
	return out
}

func (f *fakeHost) ran(prefix ...string) bool {
	for _, c := range f.calls {
		if len(c) >= len(prefix) && slices.Equal(c[:len(prefix)], prefix) {
			return true
		}
	}
	return false
}

var fwConfig = hostConfig{Address: netip.MustParseAddr("100.96.0.13"), Prefix: netip.MustParsePrefix("100.96.0.0/16"), MTU: 1380, UDP: 51820, TCP: 443}

const (
	dockerIn  = "-i obmesh0 -m conntrack --ctorigdst 100.96.0.13 --ctdir ORIGINAL -j ACCEPT"
	dockerOut = "-o obmesh0 -m conntrack --ctorigdst 100.96.0.13 --ctdir REPLY -j ACCEPT"
	ufwDrop   = "-p tcp -m conntrack --ctorigdstport 18970 ! -s 192.0.2.42 -j DROP"
)

// Docker after the data plane, a ufw reload that declares DOCKER-USER again
// (flushing it) and close: the jump is first whenever the chain exists, the
// user's rules stay, and close leaves only theirs.
func TestHostFirewallDockerUser(t *testing.T) {
	f := newFakeHost(t)
	f.fdOn = false
	h := newLinuxHost("obmesh0", f.exec, f.has)
	if err := h.apply(fwConfig); err != nil {
		t.Fatal(err)
	}
	if _, ok := f.ipt["filter"][chainDocker]; ok {
		t.Fatal("OBMESH-DOCKER made without DOCKER-USER")
	}
	if got := f.ipt["filter"]["INPUT"]; !slices.Equal(got, []string{"-j " + chainIn}) {
		t.Fatalf("INPUT %q", got)
	}
	if fixed := h.heal(); fixed != nil {
		t.Fatalf("nothing to heal, got %q", fixed)
	}

	// Docker starts: DOCKER-USER with ufw-docker's drop.
	f.ipt["filter"][dockerUser] = []string{ufwDrop, "-j RETURN"}
	f.ipt["filter"]["FORWARD"] = []string{"-j " + dockerUser}
	if fixed := h.heal(); !slices.Equal(fixed, []string{"OBMESH-DOCKER jump in DOCKER-USER"}) {
		t.Fatalf("heal after Docker started: %q", fixed)
	}
	if got := f.ipt["filter"][dockerUser]; !slices.Equal(got, []string{"-j " + chainDocker, ufwDrop, "-j RETURN"}) {
		t.Fatalf("DOCKER-USER %q", got)
	}
	if got := f.ipt["filter"][chainDocker]; !slices.Equal(got, []string{dockerIn, dockerOut}) {
		t.Fatalf("OBMESH-DOCKER %q", got)
	}
	if fixed := h.heal(); fixed != nil {
		t.Fatalf("healed again: %q", fixed)
	}
	if hostFirewall() != "docker" {
		t.Fatalf("kinds %q", hostFirewall())
	}

	// ufw reload: after.rules declares :DOCKER-USER, which empties it.
	f.ipt["filter"][dockerUser] = []string{ufwDrop, "-j RETURN"}
	if fixed := h.heal(); !slices.Equal(fixed, []string{"OBMESH-DOCKER jump in DOCKER-USER"}) {
		t.Fatalf("heal after ufw reload: %q", fixed)
	}
	if got := f.ipt["filter"][dockerUser]; got[0] != "-j "+chainDocker {
		t.Fatalf("DOCKER-USER after heal %q", got)
	}

	// A new address: the chain follows it.
	next := fwConfig
	next.Address = netip.MustParseAddr("100.96.0.14")
	if err := h.apply(next); err != nil {
		t.Fatal(err)
	}
	if got := f.ipt["filter"][chainDocker]; !strings.Contains(got[0], "--ctorigdst 100.96.0.14 ") || len(got) != 2 {
		t.Fatalf("OBMESH-DOCKER after the address changed %q", got)
	}

	h.close()
	if got := f.ipt["filter"][dockerUser]; !slices.Equal(got, []string{ufwDrop, "-j RETURN"}) {
		t.Fatalf("DOCKER-USER after close %q", got)
	}
	for _, name := range []string{chainDocker, chainIn} {
		if _, ok := f.ipt["filter"][name]; ok {
			t.Fatalf("%s left after close", name)
		}
	}
	if hostFirewall() != "" || hostFirewallIssues() != nil {
		t.Fatal("report left after close")
	}
}

// A gateway keeps its forwarding, translation and MSS chains as they were.
func TestHostFirewallGatewayChainsUnchanged(t *testing.T) {
	f := newFakeHost(t)
	f.ipt["filter"][dockerUser] = nil
	h := newLinuxHost("obmesh0", f.exec, f.has)
	h.forwardFile = filepath.Join(t.TempDir(), "ip_forward") // never this machine's
	os.WriteFile(h.forwardFile, []byte("0\n"), 0o644)
	c := fwConfig
	c.Gateway = &Gateway{Subnets: []netip.Prefix{netip.MustParsePrefix("192.168.10.0/24")}, Masquerade: true}
	if err := h.apply(c); err != nil {
		t.Fatal(err)
	}
	want := map[string][]string{
		"filter " + chainFwd:    {"-d 192.168.10.0/24 -i obmesh0 -j ACCEPT", "-s 192.168.10.0/24 -o obmesh0 -j ACCEPT"},
		"nat " + chainNat:       {"-s 100.96.0.0/16 -d 192.168.10.0/24 -j MASQUERADE"},
		"mangle " + chainMSS:    {"-o obmesh0 -p tcp -m tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu"},
		"filter " + chainIn:     {"-i obmesh0 -j ACCEPT", "-p udp -m udp --dport 51820 -j ACCEPT", "-p tcp -m tcp --dport 443 -j ACCEPT"},
		"filter " + chainDocker: {dockerIn, dockerOut},
	}
	for k, rules := range want {
		table, name, _ := strings.Cut(k, " ")
		if got := f.ipt[table][name]; !slices.Equal(got, rules) {
			t.Fatalf("%s: %q, want %q", k, got, rules)
		}
	}
	for k, parent := range map[string]string{"filter FORWARD": chainFwd, "nat POSTROUTING": chainNat, "mangle FORWARD": chainMSS, "filter " + dockerUser: chainDocker} {
		table, name, _ := strings.Cut(k, " ")
		if got := f.ipt[table][name]; len(got) == 0 || got[0] != "-j "+parent {
			t.Fatalf("%s does not jump to %s first: %q", k, parent, got)
		}
	}
	if raw, _ := os.ReadFile(h.forwardFile); string(raw) != "1" {
		t.Fatalf("ip_forward %q", raw)
	}
	h.close()
	if raw, _ := os.ReadFile(h.forwardFile); string(raw) != "0" {
		t.Fatalf("ip_forward after close %q", raw)
	}
}

// From the 10-30 check: Docker installed while the gateway was on found forwarding on
// and did not turn it on itself; putting it back to 0 cut its containers off.
func TestForwardingStaysOnForDockerStartedMeanwhile(t *testing.T) {
	f := newFakeHost(t)
	h := newLinuxHost("obmesh0", f.exec, f.has)
	dir := t.TempDir()
	h.forwardFile, h.forwardState = filepath.Join(dir, "ip_forward"), filepath.Join(dir, "ip_forward.was")
	os.WriteFile(h.forwardFile, []byte("0\n"), 0o644)
	if err := h.forwarding(true); err != nil {
		t.Fatal(err)
	}
	f.ipt["filter"][dockerUser] = nil
	h.forwarding(false)
	if raw, _ := os.ReadFile(h.forwardFile); string(raw) != "1" {
		t.Fatalf("ip_forward %q, want it left on for Docker", raw)
	}
	if _, err := os.Stat(h.forwardState); !os.IsNotExist(err) {
		t.Fatal("state left behind")
	}
}

func TestIptablesKinds(t *testing.T) {
	for _, c := range []struct {
		rules     string
		firewalld bool
		ufwConf   int // ufw.conf ENABLED: 1 yes, 0 no, -1 none
		want      []string
	}{
		{"-P INPUT ACCEPT\n-P FORWARD ACCEPT\n-A INPUT -j OBMESH-IN", false, -1, nil},
		{"-P INPUT DROP\n-N ufw-user-input\n-N DOCKER-USER\n-A INPUT -j ufw-before-input", false, -1, []string{"ufw", "docker"}},
		{"-P INPUT DROP\n-N ufw-user-input\n-N DOCKER-USER\n-A INPUT -j ufw-before-input", false, 1, []string{"ufw", "docker"}},
		// ufw disable leaves its chains, empty, and the jumps to them: neither ufw nor rules of the user's.
		{"-P INPUT ACCEPT\n-N ufw-user-input\n-A INPUT -j ufw-before-input", false, 0, nil},
		// Enabled but its chains not (yet) there: ufw all the same.
		{"-P INPUT ACCEPT", false, 1, []string{"ufw"}},
		{"-P INPUT ACCEPT\n-A INPUT -p tcp --dport 22 -j ACCEPT\n-A INPUT -j DROP", false, -1, []string{"iptables"}},
		{"-P INPUT ACCEPT\n-P FORWARD DROP\n-N DOCKER-USER", false, -1, []string{"docker"}},
		{"-P INPUT ACCEPT\n-A INPUT -j INPUT_direct", true, -1, nil},
	} {
		if got := iptablesKinds(c.rules, c.firewalld, c.ufwConf); !slices.Equal(got, c.want) {
			t.Errorf("%q ufw.conf %d: %q, want %q", c.rules, c.ufwConf, got, c.want)
		}
	}
	dir := signsIn(t)
	for body, want := range map[string]int{"ENABLED=yes\n": 1, "# x\nENABLED=no\nLOGLEVEL=low\n": 0, "ENABLED=\"yes\"": 1, "LOGLEVEL=low\n": -1} {
		write(t, dir, "ufw.conf", body)
		if got := ufwEnabled(); got != want {
			t.Errorf("ufw.conf %q: %d, want %d", body, got, want)
		}
	}
	os.Remove(ufwConf)
	if ufwEnabled() != -1 {
		t.Error("no ufw.conf")
	}
	if legacyRules("-P INPUT ACCEPT\n-P FORWARD ACCEPT\n-P OUTPUT ACCEPT\n") || !legacyRules("-P INPUT DROP\n") || !legacyRules("-P INPUT ACCEPT\n-A INPUT -j DROP\n") {
		t.Fatal("legacyRules")
	}
}

// firewalld: the device into trusted at runtime, the listen ports into the
// public interface's zone; back after a reload; a zone the user chose stays
// and is reported; close undoes only this data plane's.
func TestHostFirewallFirewalld(t *testing.T) {
	for _, bus := range []bool{true, false} {
		t.Run(fmt.Sprintf("busctl=%v", bus), func(t *testing.T) {
			f := newFakeHost(t)
			f.fdOn, f.noBus = true, !bus
			f.ports["public"] = map[string]bool{"443/tcp": true} // the user's
			h := newLinuxHost("obmesh0", f.exec, f.has)
			if err := h.apply(fwConfig); err != nil {
				t.Fatal(err)
			}
			if f.zones["obmesh0"] != "trusted" || !f.ports["public"]["51820/udp"] {
				t.Fatalf("zones %v ports %v", f.zones, f.ports)
			}
			if hostFirewall() != "firewalld" || len(hostFirewallIssues()) != 0 {
				t.Fatalf("report %q %v", hostFirewall(), hostFirewallIssues())
			}
			if bus {
				for _, c := range f.calls {
					if c[0] == "busctl" && !slices.Contains(c, "--auto-start=no") {
						t.Fatalf("busctl may start firewalld: %q", c)
					}
				}
			}

			// firewall-cmd --reload: runtime changes gone.
			f.zones, f.ports = map[string]string{}, map[string]map[string]bool{"public": {"443/tcp": true}}
			heal := func() []string {
				for i := 0; i < slowEvery; i++ {
					if fixed := h.heal(); fixed != nil {
						return fixed
					}
				}
				return nil
			}
			if fixed := heal(); !slices.Equal(fixed, []string{"firewalld obmesh0 in zone trusted", "firewalld port 51820/udp in zone public"}) {
				t.Fatalf("heal after reload: %q", fixed)
			}
			if fixed := heal(); fixed != nil {
				t.Fatalf("healed again: %q", fixed)
			}

			h.close()
			if f.zones["obmesh0"] != "" || f.ports["public"]["51820/udp"] || !f.ports["public"]["443/tcp"] {
				t.Fatalf("after close: zones %v ports %v", f.zones, f.ports)
			}
		})
	}
}

func TestHostFirewallFirewalldUserZone(t *testing.T) {
	f := newFakeHost(t)
	f.fdOn = true
	f.zones["obmesh0"] = "work"
	h := newLinuxHost("obmesh0", f.exec, f.has)
	if err := h.apply(fwConfig); err != nil {
		t.Fatal(err)
	}
	if f.zones["obmesh0"] != "work" || f.ran("firewall-cmd", "--zone=trusted", "--add-interface=obmesh0") {
		t.Fatal("the user's zone was overridden")
	}
	if got := hostFirewallIssues(); !slices.Equal(got, []FirewallIssue{{"firewalld_zone", "work"}}) {
		t.Fatalf("issues %v", got)
	}
	h.close()
	if f.zones["obmesh0"] != "work" {
		t.Fatal("close took the user's zone away")
	}
}

// firewalld stopped: busctl must not start it, and nothing is changed.
func TestHostFirewallFirewalldStopped(t *testing.T) {
	f := newFakeHost(t)
	h := newLinuxHost("obmesh0", f.exec, f.has)
	if err := h.apply(fwConfig); err != nil {
		t.Fatal(err)
	}
	if f.ran("firewall-cmd") {
		t.Fatalf("firewall-cmd run with firewalld stopped: %q", f.calls)
	}
	// Started later: heal binds.
	f.fdOn = true
	if fixed := h.heal(); !slices.Equal(fixed, []string{"firewalld obmesh0 in zone trusted"}) {
		t.Fatalf("heal after firewalld started: %q", fixed)
	}
}

func TestLookedNFTChain(t *testing.T) {
	for _, c := range []struct {
		chain nftChain
		own   bool
	}{
		{nftChain{"inet", "f2b-table", "f2b-chain", "filter", "input", "accept"}, true}, // looked at, for ours to go
		{nftChain{"inet", "filter", "input", "filter", "input", "drop"}, true},
		{nftChain{"ip", "filter", "forward", "filter", "forward", "drop"}, true},  // lower case: the user's
		{nftChain{"ip", "filter", "INPUT", "filter", "input", "accept"}, false},   // iptables-nft
		{nftChain{"ip", "filter", "FORWARD", "filter", "forward", "drop"}, false}, // iptables-nft
		{nftChain{"inet", "firewalld", "filter_INPUT", "filter", "input", "accept"}, false},
		{nftChain{"ip", "docker-bridges", "filter-FORWARD", "filter", "forward", "accept"}, false},
		{nftChain{"inet", "filter", "output", "filter", "output", "drop"}, false},
		{nftChain{"ip6", "filter", "input", "filter", "input", "drop"}, false},
		{nftChain{"ip", "nat", "prerouting", "nat", "prerouting", "accept"}, false},
		{nftChain{"inet", "filter", "input-nat", "nat", "input", "accept"}, false},
	} {
		if looked(c.chain) != c.own {
			t.Errorf("%+v: looked %v", c.chain, !c.own)
		}
	}
	for _, c := range []struct {
		chain nftChain
		other bool
	}{
		{nftChain{"inet", "f2b-table", "f2b-chain", "filter", "input", "accept"}, true},
		{nftChain{"inet", "mine", "f2b-sshd", "filter", "input", "accept"}, true}, // fail2ban's chain, a table named otherwise
		{nftChain{"ip", "sshguard", "blacklist", "filter", "input", "accept"}, true},
		{nftChain{"ip", "crowdsec", "crowdsec-chain", "filter", "input", "accept"}, true},
		{nftChain{"ip6", "crowdsec6", "crowdsec6-chain", "filter", "input", "accept"}, true},
		{nftChain{"inet", "filter", "input", "filter", "input", "drop"}, false},
	} {
		if otherTool(c.chain) != c.other {
			t.Errorf("%+v: other tool's %v", c.chain, !c.other)
		}
	}
}

// Only a chain that denies by default gets our rules and counts as a
// firewall: drop policy, or a last unconditional drop or reject; not an
// accepting one, not one rejecting only what it picks out, not fail2ban's.
func TestNFTDefaultDeny(t *testing.T) {
	for _, c := range []struct {
		name, table, policy string
		rules               []string
		want                bool
	}{
		{"policy accept", "filter", "accept", []string{"ct state established,related accept", "tcp dport 22 accept"}, false},
		{"only a picked reject", "filter", "accept", []string{"ip saddr @blocked reject", "tcp dport 25 drop"}, false},
		{"last unconditional drop", "filter", "accept", []string{"ct state established,related accept", "tcp dport 22 accept", "counter drop"}, true},
		{"last unconditional reject", "filter", "accept", []string{"tcp dport 22 accept", "log reject"}, true},
		{"policy drop", "filter", "drop", []string{"tcp dport 22 accept"}, true},
		{"policy drop, empty", "filter", "drop", nil, true},
		{"fail2ban's table", "f2b-table", "accept", []string{"tcp dport 22 ip saddr @addr-set-sshd reject"}, false},
		{"fail2ban's table, a drop policy all the same", "f2b-table", "drop", nil, false},
	} {
		t.Run(c.name, func(t *testing.T) {
			f := newFakeHost(t)
			ch := f.addNFT("inet", c.table, "input", "input", c.policy, c.rules...)
			h := newLinuxHost("obmesh0", f.exec, f.has)
			if err := h.apply(fwConfig); err != nil {
				t.Fatal(err)
			}
			got := texts(ch)
			if c.want {
				if !slices.Equal(got[3:], c.rules) || got[0] != `iifname "obmesh0" accept` || hostFirewall() != "nftables" {
					t.Fatalf("%q, kinds %q", got, hostFirewall())
				}
			} else if !slices.Equal(got, c.rules) || hostFirewall() != "" || f.ran("nft", "insert") {
				t.Fatalf("%q, kinds %q", got, hostFirewall())
			}
		})
	}
	r := func(expr string) nftRule {
		return parseNFTRules(`{"nftables":[{"rule":{"handle":1,"expr":` + expr + `}}]}`)[0]
	}
	for expr, want := range map[string]bool{
		`[{"drop": null}]`: true,
		`[{"counter": {"packets": 3, "bytes": 180}}, {"reject": {"type": "icmpx", "expr": "admin-prohibited"}}]`: true,
		`[{"log": {"prefix": "x "}}, {"counter": null}, {"drop": null}]`:                                         true,
		`[{"match": {"op": "==", "left": {"meta": {"key": "iifname"}}, "right": "eth0"}}, {"drop": null}]`:       false,
		`[{"limit": {"rate": 10, "per": "second"}}, {"drop": null}]`:                                             false,
		`[{"counter": null}]`:         false,
		`[{"jump": {"target": "x"}}]`: false,
		`[]`:                          false,
	} {
		if r(expr).denies() != want {
			t.Errorf("%s: denies %v", expr, !want)
		}
	}
}

// Edge-Fra: ufw over iptables-nft, Docker, fail2ban's table, the rules
// an earlier version put in fail2ban's chain. On start they go, nothing
// goes in, and the kinds are ufw and docker.
func TestNFTOursLeftInOtherChains(t *testing.T) {
	f := newFakeHost(t)
	f.ipt["filter"]["ufw-user-input"] = nil
	f.ipt["filter"]["DOCKER-USER"] = nil
	f.policy["filter INPUT"] = "DROP"
	f.addNFT("ip", "filter", "INPUT", "input", "drop", "counter")
	f2b := f.addNFT("inet", "f2b-table", "f2b-chain", "input", "accept", "tcp dport 22 ip saddr @addr-set-sshd reject")
	open := f.addNFT("inet", "mine", "input", "input", "accept", "ip saddr 10.0.0.0/8 tcp dport 25 reject")
	old := []fakeNFTRule{{handle: 900, text: `iifname "obmesh0" accept`, comment: "openbear-mesh:71a71424"}, {handle: 901, text: "udp dport 51997 accept", comment: "openbear-mesh:71a71424"}}
	f2b.rules = append(slices.Clone(old), f2b.rules...)
	open.rules = append(slices.Clone(old), open.rules...)
	h := newLinuxHost("obmesh0", f.exec, f.has)
	if err := h.apply(fwConfig); err != nil {
		t.Fatal(err)
	}
	if got := texts(f2b); !slices.Equal(got, []string{"tcp dport 22 ip saddr @addr-set-sshd reject"}) {
		t.Fatalf("f2b-chain %q", got)
	}
	if got := texts(open); !slices.Equal(got, []string{"ip saddr 10.0.0.0/8 tcp dport 25 reject"}) {
		t.Fatalf("accepting chain %q", got)
	}
	if f.ran("nft", "insert") {
		t.Fatal("inserted")
	}
	if k := hostFirewall(); k != "ufw,docker" {
		t.Fatalf("kinds %q", k)
	}
	if fixed := h.heal(); fixed != nil {
		t.Fatalf("heal %q", fixed)
	}
	// A chain that no longer denies by default: ours go on the next heal.
	f.calls = nil
	mine := f.addNFT("inet", "mine2", "input", "input", "drop")
	h.heal()
	if len(mine.rules) != 3 {
		t.Fatalf("%q", texts(mine))
	}
	mine.c.Policy = "accept"
	if fixed := h.heal(); !slices.Equal(fixed, []string{"nftables inet mine2 input (out)"}) || len(mine.rules) != 0 || hostFirewall() != "ufw,docker" {
		t.Fatalf("%q %q %q", fixed, texts(mine), hostFirewall())
	}
}

// nftables: rules first in the user's input and forward chains, marked;
// back after a reload; not in iptables-nft's table or a chain that drops
// nothing; deleted by handle on close.
func TestHostFirewallNFTables(t *testing.T) {
	f := newFakeHost(t)
	in := f.addNFT("inet", "filter", "input", "input", "drop", "ct state established,related accept", `tcp dport 22 accept`)
	fwd := f.addNFT("inet", "filter", "forward", "forward", "drop")
	open := f.addNFT("inet", "other", "input", "input", "accept")
	ipt := f.addNFT("ip", "filter", "INPUT", "input", "accept", "counter")
	h := newLinuxHost("obmesh0", f.exec, f.has)
	if err := h.apply(fwConfig); err != nil {
		t.Fatal(err)
	}
	wantIn := []string{`iifname "obmesh0" accept`, "udp dport 51820 accept", "tcp dport 443 accept", "ct state established,related accept", "tcp dport 22 accept"}
	wantFwd := []string{`iifname "obmesh0" ct original ip daddr 100.96.0.13 ct direction original accept`,
		`oifname "obmesh0" ct original ip daddr 100.96.0.13 ct direction reply accept`}
	if got := texts(in); !slices.Equal(got, wantIn) {
		t.Fatalf("input %q", got)
	}
	if got := texts(fwd); !slices.Equal(got, wantFwd) {
		t.Fatalf("forward %q", got)
	}
	if len(open.rules) != 0 || len(ipt.rules) != 1 {
		t.Fatalf("rules where none belong: %q %q", texts(open), texts(ipt))
	}
	if !strings.HasPrefix(in.rules[0].comment, nftMark+":") || in.rules[3].comment != "" {
		t.Fatalf("comments %+v", in.rules)
	}
	if hostFirewall() != "nftables" {
		t.Fatalf("kinds %q", hostFirewall())
	}
	if fixed := h.heal(); fixed != nil {
		t.Fatalf("nothing to heal, got %q", fixed)
	}

	// nft -f /etc/nftables.conf: flush ruleset, the user's rules again.
	in.rules = in.rules[3:]
	fwd.rules = nil
	if fixed := h.heal(); !slices.Equal(fixed, []string{"nftables inet filter input", "nftables inet filter forward"}) {
		t.Fatalf("heal after reload: %q", fixed)
	}
	if got := texts(in); !slices.Equal(got, wantIn) {
		t.Fatalf("input after heal %q", got)
	}

	// Ours pushed down by a rule inserted above them: first again.
	f.handle++
	in.rules = slices.Insert(in.rules, 0, fakeNFTRule{handle: f.handle, text: "ip saddr 1.2.3.4 drop"})
	if fixed := h.heal(); !slices.Equal(fixed, []string{"nftables inet filter input"}) || texts(in)[3] != "ip saddr 1.2.3.4 drop" || texts(in)[0] != wantIn[0] {
		t.Fatalf("heal after an insert: %q %q", fixed, texts(in))
	}

	h.close()
	if got := texts(in); !slices.Equal(got, []string{"ip saddr 1.2.3.4 drop", "ct state established,related accept", "tcp dport 22 accept"}) {
		t.Fatalf("input after close %q", got)
	}
	if len(fwd.rules) != 0 {
		t.Fatalf("forward after close %q", texts(fwd))
	}
}

// An insert that fails leaves none of ours (no half set put back every
// heal) and is reported.
func TestHostFirewallNFTablesInsertFails(t *testing.T) {
	f := newFakeHost(t)
	in := f.addNFT("inet", "filter", "input", "input", "drop")
	f.failNFT = true
	h := newLinuxHost("obmesh0", f.exec, f.has)
	if err := h.apply(fwConfig); err != nil {
		t.Fatal(err)
	}
	if len(in.rules) != 0 {
		t.Fatalf("rules %q", texts(in))
	}
	issues := hostFirewallIssues()
	if len(issues) != 1 || issues[0].Code != "nftables_failed" || !strings.HasPrefix(issues[0].Detail, "inet filter input: ") {
		t.Fatalf("issues %v", issues)
	}
}

func TestParseNFT(t *testing.T) {
	chains := parseNFTChains(`{"nftables": [{"metainfo": {"version": "1.0.6", "json_schema_version": 1}}, {"table": {"family": "inet", "name": "filter", "handle": 1}},
	 {"chain": {"family": "inet", "table": "filter", "name": "input", "handle": 1, "type": "filter", "hook": "input", "prio": 0, "policy": "drop"}},
	 {"chain": {"family": "inet", "table": "filter", "name": "mine", "handle": 4}}]}`)
	if len(chains) != 2 || chains[0] != (nftChain{"inet", "filter", "input", "filter", "input", "drop"}) || chains[1].Hook != "" {
		t.Fatalf("chains %+v", chains)
	}
	rules := parseNFTRules(`{"nftables": [{"chain": {"family": "inet", "table": "filter", "name": "input", "handle": 1}},
	 {"rule": {"family": "inet", "table": "filter", "chain": "input", "handle": 7, "comment": "openbear-mesh:0123abcd", "expr": [{"accept": null}]}},
	 {"rule": {"family": "inet", "table": "filter", "chain": "input", "handle": 3, "expr": [{"drop": null}]}}]}`)
	if len(rules) != 2 || rules[0].Handle != 7 || !rules[0].ours() || rules[1].Handle != 3 || rules[1].ours() || !rules[1].denies() || rules[0].denies() {
		t.Fatalf("rules %+v", rules)
	}
	a := nftComment(nftRules("input", "obmesh0", firewallWant{addr: fwConfig.Address, udp: 1}))
	b := nftComment(nftRules("input", "obmesh0", firewallWant{addr: fwConfig.Address, udp: 2}))
	if a == b || len(a) != len(nftMark)+9 {
		t.Fatalf("comments %q %q", a, b)
	}
}

func TestJumpAtGoesAfterWhatBans(t *testing.T) {
	for _, c := range []struct {
		rules []string
		want  int
	}{
		{nil, 0},
		{[]string{"-A INPUT -i lo -j ACCEPT", "-A INPUT -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT"}, 0},
		// fail2ban's iptables actions, a CrowdSec bouncer's chain and its ipset, sshguard.
		{[]string{"-A INPUT -p tcp -m multiport --dports 22 -j f2b-sshd", "-A INPUT -j ufw-before-logging-input"}, 1},
		{[]string{"-A INPUT -j CROWDSEC_CHAIN", "-A INPUT -i lo -j ACCEPT"}, 1},
		{[]string{"-A INPUT -i lo -j ACCEPT", "-A INPUT -m set --match-set crowdsec-blacklists-0 src -j DROP"}, 2},
		{[]string{"-A INPUT -j sshguard", "-A INPUT -p tcp --dport 22 -j ACCEPT", "-A INPUT -j DROP"}, 1},
		// ufw: after its own checks and user rules (where "ufw deny" and fail2ban's ufw action go).
		{[]string{"-A INPUT -p tcp -m multiport --dports 22 -j f2b-sshd", "-A INPUT -j ufw-before-logging-input", "-A INPUT -j ufw-before-input",
			"-A INPUT -j ufw-after-input", "-A INPUT -j ufw-after-logging-input", "-A INPUT -j ufw-reject-input", "-A INPUT -j ufw-track-input"}, 3},
		{[]string{"-A DOCKER-USER -j ufw-user-forward", "-A DOCKER-USER -p tcp -m conntrack --ctorigdstport 18970 ! -s 192.0.2.42 -j DROP", "-A DOCKER-USER -j RETURN"}, 1},
		// Past what drops everything nothing is reached.
		{[]string{"-A INPUT -j DROP", "-A INPUT -j f2b-sshd"}, 0},
		{[]string{"-A INPUT -j REJECT --reject-with icmp-port-unreachable", "-A INPUT -j f2b-sshd"}, 0},
	} {
		if got := jumpAt(c.rules); got != c.want {
			t.Errorf("jumpAt(%q) = %d, want %d", c.rules, got, c.want)
		}
	}
}

func TestHostFirewallMeshAfterBans(t *testing.T) {
	f := newFakeHost(t)
	f.fdOn = false
	f.policy["filter INPUT"] = "DROP"
	f.ipt["filter"]["INPUT"] = []string{"-p tcp -m multiport --dports 22 -j f2b-sshd", "-j ufw-before-logging-input", "-j ufw-before-input", "-j ufw-after-input"}
	h := newLinuxHost("obmesh0", f.exec, f.has)
	if err := h.apply(fwConfig); err != nil {
		t.Fatal(err)
	}
	want := []string{"-p tcp -m multiport --dports 22 -j f2b-sshd", "-j ufw-before-logging-input", "-j ufw-before-input", "-j " + chainIn, "-j ufw-after-input"}
	if got := f.ipt["filter"]["INPUT"]; !slices.Equal(got, want) {
		t.Fatalf("INPUT %q", got)
	}
	if fixed := h.heal(); fixed != nil {
		t.Fatalf("nothing to heal, got %q", fixed)
	}
	// fail2ban starting another jail puts its jump first: ours stays after the bans, nothing to move.
	f.ipt["filter"]["INPUT"] = append([]string{"-p tcp -m multiport --dports 80 -j f2b-nginx"}, f.ipt["filter"]["INPUT"]...)
	if fixed := h.heal(); fixed != nil {
		t.Fatalf("moved for a new ban: %q", fixed)
	}
	// An older node's jump, first of all: moved after the bans.
	f.ipt["filter"]["INPUT"] = []string{"-j " + chainIn, "-p tcp -m multiport --dports 22 -j f2b-sshd"}
	if fixed := h.heal(); !slices.Equal(fixed, []string{chainIn + " jump in INPUT"}) {
		t.Fatalf("heal %q", fixed)
	}
	if got := f.ipt["filter"]["INPUT"]; !slices.Equal(got, []string{"-p tcp -m multiport --dports 22 -j f2b-sshd", "-j " + chainIn}) {
		t.Fatalf("INPUT after heal %q", got)
	}
}

func TestNFTablesMeshAfterBans(t *testing.T) {
	f := newFakeHost(t)
	// fail2ban 0.10's set in the user's own table, a blocklist of the user's, then what is allowed.
	in := f.addNFT("inet", "filter", "input", "input", "drop", "ip saddr @f2b-sshd reject", "ip saddr @blocklist drop", "ct state established,related accept")
	h := newLinuxHost("obmesh0", f.exec, f.has)
	if err := h.apply(fwConfig); err != nil {
		t.Fatal(err)
	}
	want := []string{"ip saddr @f2b-sshd reject", "ip saddr @blocklist drop", `iifname "obmesh0" accept`, "udp dport 51820 accept", "tcp dport 443 accept", "ct state established,related accept"}
	if got := texts(in); !slices.Equal(got, want) {
		t.Fatalf("input %q", got)
	}
	if fixed := h.heal(); fixed != nil {
		t.Fatalf("nothing to heal, got %q", fixed)
	}
	// A jump to a ban tool's chain inserted first: ours already come after every ban.
	f.handle++
	in.rules = slices.Insert(in.rules, 0, fakeNFTRule{handle: f.handle, text: "jump crowdsec-chain"})
	if fixed := h.heal(); fixed != nil || texts(in)[3] != `iifname "obmesh0" accept` {
		t.Fatalf("heal after a ban jump: %q %q", fixed, texts(in))
	}
	// A ban put below ours: ours move after it.
	f.handle++
	in.rules = slices.Insert(in.rules, 6, fakeNFTRule{handle: f.handle, text: "ip saddr @late drop"})
	if fixed := h.heal(); !slices.Equal(fixed, []string{"nftables inet filter input"}) || texts(in)[3] != "ip saddr @late drop" || texts(in)[4] != `iifname "obmesh0" accept` {
		t.Fatalf("heal after a later ban: %q %q", fixed, texts(in))
	}
}

// A data plane killed while it was a gateway: the next one takes out the
// forwarding and translation it left and puts ip_forward back.
func TestHostFirewallKilledGatewayLeavesNothing(t *testing.T) {
	f := newFakeHost(t)
	dir := t.TempDir()
	start := func() *linuxHost {
		h := newLinuxHost("obmesh0", f.exec, f.has)
		h.forwardFile, h.forwardState = filepath.Join(dir, "ip_forward"), filepath.Join(dir, "ip_forward.was")
		return h
	}
	os.WriteFile(filepath.Join(dir, "ip_forward"), []byte("0\n"), 0o644)
	gw := fwConfig
	gw.Gateway = &Gateway{Subnets: []netip.Prefix{netip.MustParsePrefix("192.168.10.0/24")}, Masquerade: true}
	if err := start().apply(gw); err != nil { // and then killed: no close
		t.Fatal(err)
	}
	if raw, _ := os.ReadFile(filepath.Join(dir, "ip_forward")); strings.TrimSpace(string(raw)) != "1" {
		t.Fatalf("ip_forward %q", raw)
	}
	h := start()
	if err := h.apply(fwConfig); err != nil {
		t.Fatal(err)
	}
	for k, name := range map[string]string{"filter": chainFwd, "nat": chainNat, "mangle": chainMSS} {
		if _, ok := f.ipt[k][name]; ok {
			t.Fatalf("%s %s left", k, name)
		}
	}
	for k, parent := range map[string]string{"filter": "FORWARD", "nat": "POSTROUTING", "mangle": "FORWARD"} {
		if got := f.ipt[k][parent]; len(got) != 0 {
			t.Fatalf("%s %s still jumps: %q", k, parent, got)
		}
	}
	if got := f.ipt["filter"]["INPUT"]; !slices.Equal(got, []string{"-j " + chainIn}) {
		t.Fatalf("INPUT %q", got)
	}
	if raw, _ := os.ReadFile(filepath.Join(dir, "ip_forward")); strings.TrimSpace(string(raw)) != "0" {
		t.Fatalf("ip_forward not put back: %q", raw)
	}
	if _, err := os.Stat(h.forwardState); !os.IsNotExist(err) {
		t.Fatal("state file left:", err)
	}
}
