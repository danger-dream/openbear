package mesh

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/netip"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync/atomic"
)

// The host's own firewalls besides the OBMESH-* iptables chains. The mesh's
// rules (node rules, enforced by the data plane) decide what may come in over
// the mesh device, so the host firewall is told to let in everything from it
// to this machine, also what Docker forwards on to a container published on
// the mesh address, and the ports the data plane listens on:
//   - firewalld: the mesh device goes into the trusted zone at runtime, unless
//     the user put it in a zone; the listen ports open in the zone of the
//     default route's interface at runtime;
//   - nftables tables of the user's own (not iptables-nft's, firewalld's,
//     Docker's or another tool's, see otherTool): accept rules first in their
//     input and forward base chains that deny by default, marked by their
//     comment. Every base chain on a hook runs, so accepting in a table of
//     ours would not do.
// A reload of either drops what was added; heal puts it back. close takes out
// only what this data plane added.

const (
	nftMark     = "openbear-mesh"
	trustedZone = "trusted"
	fdName      = "org.fedoraproject.FirewallD1"
	fdPath      = "/org/fedoraproject/FirewallD1"
	slowEvery   = 12 // heals between checks that run firewall-cmd or look at legacy tables (1 minute)
)

// Where the signs of a firewall are read, besides its own program: tests put
// others.
var (
	ufwConf    = "/etc/ufw/ufw.conf"
	procRoot   = "/proc"
	systemdDir = "/etc/systemd/system"
)

type firewallReport struct {
	kinds  string
	issues []FirewallIssue
}

var firewallSeen atomic.Pointer[firewallReport]

func hostFirewall() string {
	if r := firewallSeen.Load(); r != nil {
		return r.kinds
	}
	return ""
}

func hostFirewallIssues() []FirewallIssue {
	if r := firewallSeen.Load(); r != nil {
		return r.issues
	}
	return nil
}

type firewallWant struct {
	addr          netip.Addr
	udp, tcp, tls int
	subnets       []netip.Prefix
}

// ports as firewalld names them: 51820/udp.
func (w firewallWant) ports() []string {
	var out []string
	if w.udp != 0 {
		out = append(out, strconv.Itoa(w.udp)+"/udp")
	}
	for _, p := range []int{w.tcp, w.tls} {
		if p != 0 {
			out = append(out, strconv.Itoa(p)+"/tcp")
		}
	}
	return out
}

type hostFirewalls struct {
	iface    string
	x        execFn
	has      func(string) bool
	iptables bool // the program found
	want     *firewallWant
	fd       firewalld
	nft      nftables
	// missing: firewalls that show signs of running whose program is not
	// found, so nothing could be let through; as last checked.
	missing []FirewallIssue
	// dockerErr is why OBMESH-DOCKER could not be written, "" when it was.
	dockerErr string
	heals     int
	mixed     []FirewallIssue // as last checked, every slowEvery heals
}

func newHostFirewalls(iface string, x execFn, has func(string) bool) *hostFirewalls {
	f := &hostFirewalls{iface: iface, x: x, has: has, iptables: has("iptables")}
	f.fd = firewalld{iface: iface, x: x, cli: has("firewall-cmd"), bus: has("busctl")}
	f.nft = nftables{iface: iface, x: x, present: has("nft")}
	return f
}

func (f *hostFirewalls) apply(w firewallWant) {
	f.want = &w
	f.fd.apply(w)
	f.nft.apply(w)
	f.mixed = f.mixedTables()
	f.missing = f.missingTools()
}

func (f *hostFirewalls) heal() []string {
	if f.want == nil {
		return nil
	}
	f.heals++
	slow := f.heals%slowEvery == 0
	fixed := f.fd.heal(*f.want, slow)
	fixed = append(fixed, f.nft.heal(*f.want)...)
	if slow {
		f.mixed = f.mixedTables()
		f.missing = f.missingTools()
	}
	return fixed
}

func (f *hostFirewalls) close() {
	f.fd.close()
	f.nft.close()
	f.want = nil
	f.missing = nil
	firewallSeen.Store(nil)
}

// missingTools: a firewall that shows signs of running while its program
// cannot be found is reported, not taken for no firewall. The signs are read
// without it: ufw's config, the legacy tables the kernel lists, a firewalld
// process, the nftables service enabled. Detail: "kind: program".
func (f *hostFirewalls) missingTools() []FirewallIssue {
	var out []FirewallIssue
	if !f.iptables {
		if ufwEnabled() == 1 {
			out = append(out, FirewallIssue{"firewall_tool_missing", "ufw: iptables"})
		} else if names, err := os.ReadFile(filepath.Join(procRoot, "net/ip_tables_names")); err == nil && strings.TrimSpace(string(names)) != "" {
			out = append(out, FirewallIssue{"firewall_tool_missing", "iptables: iptables"})
		}
	}
	if !f.fd.cli && processRunning("firewalld") {
		out = append(out, FirewallIssue{"firewall_tool_missing", "firewalld: firewall-cmd"})
	}
	if !f.nft.present && serviceEnabled("nftables.service") {
		out = append(out, FirewallIssue{"firewall_tool_missing", "nftables: nft"})
	}
	return out
}

// ufwEnabled reads ENABLED in ufw.conf: 1 yes, 0 no, -1 no such file or line.
func ufwEnabled() int {
	raw, err := os.ReadFile(ufwConf)
	if err != nil {
		return -1
	}
	for _, l := range strings.Split(string(raw), "\n") {
		if v, ok := strings.CutPrefix(strings.TrimSpace(l), "ENABLED="); ok {
			if strings.EqualFold(strings.Trim(v, `"' `), "yes") {
				return 1
			}
			return 0
		}
	}
	return -1
}

// processRunning: a process by that name (comm; for a script, its file name).
func processRunning(name string) bool {
	dirs, err := os.ReadDir(procRoot)
	if err != nil {
		return false
	}
	for _, d := range dirs {
		if d.Name()[0] < '0' || d.Name()[0] > '9' {
			continue
		}
		if comm, err := os.ReadFile(filepath.Join(procRoot, d.Name(), "comm")); err == nil && strings.TrimSpace(string(comm)) == name {
			return true
		}
	}
	return false
}

// serviceEnabled: systemd starts the unit at boot (a link in a .wants of
// /etc/systemd/system).
func serviceEnabled(unit string) bool {
	links, _ := filepath.Glob(filepath.Join(systemdDir, "*.wants", unit))
	return len(links) > 0
}

// report publishes what status.json says about the host firewall: the kinds
// found, comma-separated (ufw or iptables, docker, firewalld, nftables), and
// the issues.
func (f *hostFirewalls) report(iptables bool) {
	var kinds []string
	var issues []FirewallIssue
	if iptables {
		out, _ := f.x("iptables", "-w", "-S")
		kinds = append(kinds, iptablesKinds(out, f.fd.running, ufwEnabled())...)
	}
	// Found by its signs, its program missing.
	for _, m := range f.missing {
		if kind, _, _ := strings.Cut(m.Detail, ":"); !slices.Contains(kinds, kind) {
			kinds = append(kinds, kind)
		}
	}
	if f.fd.running {
		kinds = append(kinds, "firewalld")
	}
	if f.nft.found {
		kinds = append(kinds, "nftables")
	}
	if f.fd.zone != "" {
		issues = append(issues, FirewallIssue{"firewalld_zone", f.fd.zone})
	}
	if f.fd.err != "" {
		issues = append(issues, FirewallIssue{"firewalld_failed", f.fd.err})
	}
	if f.nft.err != "" {
		issues = append(issues, FirewallIssue{"nftables_failed", f.nft.err})
	}
	if f.dockerErr != "" {
		issues = append(issues, FirewallIssue{"docker_failed", f.dockerErr})
	}
	issues = append(issues, f.mixed...)
	issues = append(issues, f.missing...)
	firewallSeen.Store(&firewallReport{kinds: strings.Join(kinds, ","), issues: issues})
}

// iptablesKinds reads iptables -S of the filter table: ufw by its chains,
// unless ufw.conf says it is off (ufw disable leaves them, empty), or by
// ufw.conf alone; docker by DOCKER-USER; iptables for rules of the user's own
// in INPUT (Docker sets FORWARD's policy itself; firewalld's own rules are
// firewalld). ufwConf as ufwEnabled reads it.
func iptablesKinds(rules string, firewalld bool, ufwConf int) []string {
	var ufw, docker, own bool
	for _, l := range strings.Split(rules, "\n") {
		switch {
		case strings.HasPrefix(l, "-N ufw-"):
			ufw = true
		case l == "-N "+dockerUser:
			docker = true
		case strings.HasPrefix(l, "-A INPUT -j ufw-"):
		case l == "-P INPUT DROP" || strings.HasPrefix(l, "-A INPUT ") && l != "-A INPUT -j "+chainIn:
			own = true
		}
	}
	ufw = ufw && ufwConf != 0 || ufwConf == 1
	var out []string
	if ufw {
		out = append(out, "ufw")
	} else if own && !firewalld {
		out = append(out, "iptables")
	}
	if docker {
		out = append(out, "docker")
	}
	return out
}

// mixedTables finds rules in the iptables backend this data plane does not
// write to: both run, and the other one's drops still drop.
func (f *hostFirewalls) mixedTables() []FirewallIssue {
	if !f.has("iptables") {
		return nil
	}
	v, _ := f.x("iptables", "-V")
	switch {
	case strings.Contains(v, "nf_tables"):
		// Legacy tables exist only once something loaded them; asking
		// iptables-legacy would load them itself.
		names, err := os.ReadFile(filepath.Join(procRoot, "net/ip_tables_names"))
		if err != nil || !strings.Contains(string(names), "filter") || !f.has("iptables-legacy") {
			return nil
		}
		out, err := f.x("iptables-legacy", "-w", "-S")
		if err == nil && legacyRules(out) {
			return []FirewallIssue{{"iptables_legacy", "iptables-legacy"}}
		}
	case strings.Contains(v, "legacy"):
		if !f.nft.present {
			return nil
		}
		cs, _ := f.nft.chains()
		for _, c := range cs {
			if iptablesTable(c) && (c.Hook == "input" || c.Hook == "forward") {
				return []FirewallIssue{{"iptables_nft", c.Family + " " + c.Table + " " + c.Name}}
			}
		}
	}
	return nil
}

// legacyRules: anything in iptables -S beyond ACCEPT policies.
func legacyRules(out string) bool {
	for _, l := range strings.Split(strings.TrimSpace(out), "\n") {
		if l != "" && !(strings.HasPrefix(l, "-P ") && strings.HasSuffix(l, " ACCEPT")) {
			return true
		}
	}
	return false
}

// ----- firewalld -----

type firewalld struct {
	iface     string
	x         execFn
	cli, bus  bool // firewall-cmd, busctl
	running   bool
	bound     bool              // the mesh device put in trusted by this data plane
	zone      string            // the zone the user put it in: left alone, reported
	ports     map[string]string // port → zone this data plane opened it in
	stateFile string
	loaded    bool
	err       string
}

type firewalldOwnership struct {
	Interface string            `json:"interface"`
	Bound     bool              `json:"bound"`
	Ports     map[string]string `json:"ports"`
}

func (d *firewalld) load() bool {
	if d.loaded || d.stateFile == "" {
		return true
	}
	raw, err := os.ReadFile(d.stateFile)
	if os.IsNotExist(err) {
		d.loaded = true
		return true
	}
	var state firewalldOwnership
	if err != nil {
		d.err = err.Error()
		return false
	}
	if err = json.Unmarshal(raw, &state); err != nil || state.Interface != d.iface {
		d.err = "invalid firewalld ownership state"
		return false
	}
	d.bound, d.ports, d.loaded = state.Bound, state.Ports, true
	return true
}

func (d *firewalld) save() bool {
	if d.stateFile == "" {
		return true
	}
	if err := writeAtomic(d.stateFile, firewalldOwnership{d.iface, d.bound, d.ports}); err != nil {
		d.err = err.Error()
		return false
	}
	return true
}

// call asks firewalld over D-Bus with busctl, never starting it: firewalld
// can be D-Bus activated, and a firewall the user stopped stays stopped.
// ok false when it does not run (or busctl cannot ask).
func (d *firewalld) call(iface, method, sig string, args ...string) (string, bool) {
	out, err := d.x(append([]string{"busctl", "--system", "--auto-start=no", "call", fdName, fdPath, fdName + "." + iface, method, sig}, args...)...)
	if err != nil {
		return err.Error(), false
	}
	return strings.TrimSpace(out), true
}

// state: whether firewalld runs, and the zone the interface is in ("" for
// none). busctl when there, else firewall-cmd.
func (d *firewalld) state(ifname string) (running bool, zone string) {
	if d.bus {
		out, ok := d.call("zone", "getZoneOfInterface", "s", ifname)
		if !ok {
			return false, ""
		}
		return true, busString(out)
	}
	if !d.cli {
		return false, ""
	}
	if out, err := d.x("firewall-cmd", "--state"); err != nil || strings.TrimSpace(out) != "running" {
		return false, ""
	}
	out, err := d.x("firewall-cmd", "--get-zone-of-interface="+ifname)
	if err != nil {
		return true, "" // "no zone", exit code 2
	}
	return true, strings.TrimSpace(out)
}

// busString reads busctl's `s "trusted"`.
func busString(out string) string {
	if s, ok := strings.CutPrefix(out, "s "); ok {
		if v, err := strconv.Unquote(s); err == nil {
			return v
		}
	}
	return ""
}

func (d *firewalld) apply(w firewallWant) {
	d.err = ""
	d.running, d.zone = false, ""
	if !d.cli || !d.load() {
		return
	}
	running, zone := d.state(d.iface)
	d.running = running
	if !running {
		return
	}
	d.bind(zone)
	d.openPorts(w)
}

// bind puts the mesh device in trusted when it is in no zone; a zone the
// user chose is left alone.
func (d *firewalld) bind(zone string) bool {
	switch zone {
	case trustedZone:
		return false
	case "":
		d.bound = true
		if !d.save() {
			d.bound = false
			return false
		}
		if _, err := d.x("firewall-cmd", "--zone="+trustedZone, "--add-interface="+d.iface); err != nil {
			d.err = err.Error()
			return false
		}
		d.bound, d.zone = true, ""
		return true
	default:
		d.bound, d.zone = false, zone
		d.save()
		return false
	}
}

// publicZone is the zone of the default route's interface, else the
// default zone: where the listen ports are reached from.
func (d *firewalld) publicZone() string {
	out, _ := d.x("ip", "-4", "route", "show", "default")
	if f := strings.Fields(out); len(f) > 0 {
		if i := slices.Index(f, "dev"); i >= 0 && i+1 < len(f) {
			if _, zone := d.state(f[i+1]); zone != "" {
				return zone
			}
		}
	}
	out, err := d.x("firewall-cmd", "--get-default-zone")
	if err != nil {
		return ""
	}
	return strings.TrimSpace(out)
}

func (d *firewalld) openPorts(w firewallWant) {
	want := w.ports()
	zone := d.publicZone()
	for p, previous := range d.ports {
		if !slices.Contains(want, p) || zone != "" && previous != zone {
			if _, err := d.x("firewall-cmd", "--zone="+previous, "--remove-port="+p); err != nil {
				d.err = err.Error()
				continue
			}
			delete(d.ports, p)
			if !d.save() {
				return
			}
		}
	}
	if len(want) == 0 {
		return
	}
	if zone == "" || zone == trustedZone {
		return
	}
	for _, p := range want {
		if previous, ok := d.ports[p]; ok {
			if previous != zone || d.open(zone, p) {
				continue
			}
		}
		if _, err := d.x("firewall-cmd", "--zone="+zone, "--query-port="+p); err == nil {
			continue // open already, by the user
		}
		if d.ports == nil {
			d.ports = map[string]string{}
		}
		d.ports[p] = zone
		if !d.save() {
			delete(d.ports, p)
			return
		}
		if _, err := d.x("firewall-cmd", "--zone="+zone, "--add-port="+p); err != nil {
			d.err = err.Error()
		}
	}
}

// heal: a reload or restart drops runtime changes. busctl asks cheaply every
// time; without it firewall-cmd is asked only on slow rounds.
func (d *firewalld) heal(w firewallWant, slow bool) []string {
	if !d.cli || !d.bus && !slow || !d.load() {
		return nil
	}
	running, zone := d.state(d.iface)
	if !running {
		d.running, d.zone = false, ""
		return nil
	}
	var fixed []string
	if !d.running {
		// Started after this data plane: as on apply.
		d.running = true
		if d.bind(zone) {
			fixed = append(fixed, "firewalld "+d.iface+" in zone "+trustedZone)
		}
		d.openPorts(w)
		return fixed
	}
	// In no zone: the reload dropped it, or the user took it out of theirs.
	if d.bind(zone) {
		fixed = append(fixed, "firewalld "+d.iface+" in zone "+trustedZone)
	} else if zone == trustedZone {
		d.zone = ""
	}
	for p, z := range d.ports {
		if d.open(z, p) {
			continue
		}
		if _, err := d.x("firewall-cmd", "--zone="+z, "--add-port="+p); err == nil {
			fixed = append(fixed, "firewalld port "+p+" in zone "+z)
		}
	}
	return fixed
}

func (d *firewalld) open(zone, port string) bool {
	if d.bus {
		num, proto, _ := strings.Cut(port, "/")
		out, ok := d.call("zone", "queryPort", "sss", zone, num, proto)
		return ok && out == "b true"
	}
	_, err := d.x("firewall-cmd", "--zone="+zone, "--query-port="+port)
	return err == nil
}

func (d *firewalld) close() {
	if !d.load() {
		return
	}
	if d.bound {
		if running, zone := d.state(d.iface); running && zone == trustedZone {
			if _, err := d.x("firewall-cmd", "--zone="+trustedZone, "--remove-interface="+d.iface); err == nil {
				d.bound = false
			}
		}
	}
	for p, zone := range d.ports {
		if _, err := d.x("firewall-cmd", "--zone="+zone, "--remove-port="+p); err == nil {
			delete(d.ports, p)
		}
	}
	d.running, d.zone = false, ""
	d.save()
}

// ----- nftables -----

type nftables struct {
	iface   string
	x       execFn
	present bool
	found   bool   // the user has base chains of their own on input or forward that deny by default
	err     string // the last insert that failed
	applied bool
}

type nftChain struct {
	Family string `json:"family"`
	Table  string `json:"table"`
	Name   string `json:"name"`
	Type   string `json:"type"`
	Hook   string `json:"hook"`
	Policy string `json:"policy"`
}

type nftRule struct {
	Handle  int                          `json:"handle"`
	Comment string                       `json:"comment"`
	Expr    []map[string]json.RawMessage `json:"expr"`
}

func (r nftRule) ours() bool { return strings.HasPrefix(r.Comment, nftMark) }

// denies: a rule that drops or rejects whatever reaches it, counted or
// logged at most: no match, no limit.
func (r nftRule) denies() bool {
	if len(r.Expr) == 0 {
		return false
	}
	for i, e := range r.Expr {
		if len(e) != 1 {
			return false
		}
		for k := range e {
			last := i == len(r.Expr)-1
			if last && k != "drop" && k != "reject" || !last && k != "counter" && k != "log" {
				return false
			}
		}
	}
	return true
}

// bans: a rule that drops or rejects addresses in a set (fail2ban 0.10's in
// the user's table, a blocklist of the user's), or jumps to a ban tool's chain.
func (r nftRule) bans() bool {
	if len(r.Expr) == 0 {
		return false
	}
	var set bool
	for _, e := range r.Expr {
		if raw, ok := e["match"]; ok {
			var m struct {
				Right json.RawMessage `json:"right"`
			}
			var right string
			if json.Unmarshal(raw, &m) == nil && json.Unmarshal(m.Right, &right) == nil && strings.HasPrefix(right, "@") {
				set = true
			}
		}
		for _, k := range []string{"jump", "goto"} {
			if raw, ok := e[k]; ok {
				var v struct {
					Target string `json:"target"`
				}
				if json.Unmarshal(raw, &v) == nil && banTarget(v.Target) {
					return true
				}
			}
		}
	}
	last := r.Expr[len(r.Expr)-1]
	_, drop := last["drop"]
	_, reject := last["reject"]
	return set && (drop || reject)
}

// banEnd: how many of the chain's rules, ours not counted, go before ours,
// as jumpAt for iptables: up to the last that bans, none past one that
// denies whatever reaches it.
func banEnd(rules []nftRule) int {
	end, n := 0, 0
	for _, r := range rules {
		if r.ours() {
			continue
		}
		n++
		if r.bans() {
			end = n
		} else if r.denies() {
			break
		}
	}
	return end
}

func (n *nftables) chains() ([]nftChain, error) {
	out, err := n.x("nft", "-j", "list", "chains")
	if err != nil {
		return nil, err
	}
	return parseNFTChains(out), nil
}

func parseNFTChains(out string) []nftChain {
	var doc struct {
		Items []struct {
			Chain *nftChain `json:"chain"`
		} `json:"nftables"`
	}
	if json.Unmarshal([]byte(out), &doc) != nil {
		return nil
	}
	var cs []nftChain
	for _, it := range doc.Items {
		if it.Chain != nil {
			cs = append(cs, *it.Chain)
		}
	}
	return cs
}

func parseNFTRules(out string) []nftRule {
	var doc struct {
		Items []struct {
			Rule *nftRule `json:"rule"`
		} `json:"nftables"`
	}
	if json.Unmarshal([]byte(out), &doc) != nil {
		return nil
	}
	var rs []nftRule
	for _, it := range doc.Items {
		if it.Rule != nil {
			rs = append(rs, *it.Rule)
		}
	}
	return rs
}

var iptablesTables = []string{"filter", "nat", "mangle", "raw", "security"}
var iptablesChains = []string{"INPUT", "FORWARD", "OUTPUT", "PREROUTING", "POSTROUTING"}

// iptablesTable: a table iptables-nft made (its tables and upper-case base
// chains); iptables itself handles those.
func iptablesTable(c nftChain) bool {
	return (c.Family == "ip" || c.Family == "ip6") && slices.Contains(iptablesTables, c.Table) && slices.Contains(iptablesChains, c.Name)
}

// looked: an IPv4 filter base chain on input or forward, not iptables-nft's,
// not firewalld's (its zone is used), not Docker's (it lets in the ports it
// publishes itself): where rules of ours may be, those of earlier versions
// too, and are looked for.
func looked(c nftChain) bool {
	return (c.Family == "ip" || c.Family == "inet") && c.Type == "filter" && (c.Hook == "input" || c.Hook == "forward") &&
		!iptablesTable(c) && !strings.HasPrefix(c.Table, "firewalld") && !strings.HasPrefix(c.Table, "docker")
}

// Tables other tools make and manage by their default names, blocking what
// they decided to, not a firewall of the user's: fail2ban's nftables action
// (inet f2b-table, chain f2b-chain; fail2ban 0.11 to 1.x), sshguard's
// (ip/ip6 sshguard), the CrowdSec firewall bouncer's (ip crowdsec, ip6
// crowdsec6), Podman's netavark, libvirt's networks, kube-proxy, Incus, LXD.
// fail2ban 0.10 puts its set and rules in the user's inet filter table
// instead: the user's chain, judged as such.
var otherTables = []string{"f2b-table", "sshguard", "crowdsec", "crowdsec6", "netavark", "libvirt_network", "kube-proxy", "incus", "lxd"}

func otherTool(c nftChain) bool {
	return slices.Contains(otherTables, c.Table) || strings.HasPrefix(c.Name, "f2b-")
}

// deniesByDefault: what nothing in the chain accepts is dropped, by its
// policy or a last rule (not ours) dropping or rejecting unconditionally. A
// chain that lets through all but what it picks out (fail2ban's bans) needs
// nothing of ours, and ours there would get round what it picks out.
func deniesByDefault(c nftChain, rules []nftRule) bool {
	if c.Policy == "drop" {
		return true
	}
	for i := len(rules) - 1; i >= 0; i-- {
		if !rules[i].ours() {
			return rules[i].denies()
		}
	}
	return false
}

// nftRules: what goes first in a chain on the hook, as nft arguments; the
// same as OBMESH-IN and OBMESH-DOCKER.
func nftRules(hook, iface string, w firewallWant) [][]string {
	in := []string{"iifname", strconv.Quote(iface)}
	if hook == "forward" {
		a := w.addr.String()
		rules := [][]string{
			append(in, "ct", "original", "ip", "daddr", a, "ct", "direction", "original", "accept"),
			{"oifname", strconv.Quote(iface), "ct", "original", "ip", "daddr", a, "ct", "direction", "reply", "accept"}}
		for _, subnet := range w.subnets {
			rules = append(rules, []string{"iifname", strconv.Quote(iface), "ip", "daddr", subnet.String(), "accept"},
				[]string{"oifname", strconv.Quote(iface), "ip", "saddr", subnet.String(), "accept"})
		}
		return rules
	}
	rules := [][]string{append(in, "accept")}
	if w.udp != 0 {
		rules = append(rules, []string{"udp", "dport", strconv.Itoa(w.udp), "accept"})
	}
	for _, p := range []int{w.tcp, w.tls} {
		if p != 0 {
			rules = append(rules, []string{"tcp", "dport", strconv.Itoa(p), "accept"})
		}
	}
	return rules
}

// nftComment marks the rules, with a digest of them: a chain whose first
// rules carry it holds them as wanted.
func nftComment(rules [][]string) string {
	h := sha256.New()
	for _, r := range rules {
		h.Write([]byte(strings.Join(r, " ") + "\n"))
	}
	return nftMark + ":" + hex.EncodeToString(h.Sum(nil))[:8]
}

func (n *nftables) apply(w firewallWant) {
	n.err = ""
	n.sync(w)
	n.applied = true
}

func (n *nftables) heal(w firewallWant) []string {
	if !n.applied {
		return nil
	}
	return n.sync(w)
}

// sync puts the rules in each of the user's chains that deny by default,
// first but for the rules that ban (banEnd), and takes ours out of every
// other looked-at chain (rules an earlier version put in a chain that lets
// through all but what it picks out, or in another tool's): the chains it
// had to change. Run by apply, so on start too.
func (n *nftables) sync(w firewallWant) []string {
	n.found, n.err = false, ""
	if !n.present {
		return nil
	}
	cs, err := n.chains()
	if err != nil {
		// nft there but not answering: not taken for no ruleset.
		n.err = err.Error()
		return nil
	}
	var fixed []string
	for _, c := range cs {
		if !looked(c) {
			continue
		}
		out, err := n.x("nft", "-j", "-a", "list", "chain", c.Family, c.Table, c.Name)
		if err != nil {
			continue
		}
		rules := parseNFTRules(out)
		if otherTool(c) || !deniesByDefault(c, rules) {
			if slices.ContainsFunc(rules, nftRule.ours) {
				n.remove(c, rules)
				fixed = append(fixed, "nftables "+c.Family+" "+c.Table+" "+c.Name+" (out)")
			}
			continue
		}
		want := nftRules(c.Hook, n.iface, w)
		mark := nftComment(want)
		end := banEnd(rules)
		held := len(rules) >= end+len(want)
		for i, r := range rules {
			if r.ours() != (i >= end && i < end+len(want)) || r.ours() && r.Comment != mark {
				held = false
			}
		}
		n.found = true
		if held {
			continue
		}
		n.remove(c, rules)
		where := []string{"nft", "insert", "rule", c.Family, c.Table, c.Name}
		if end > 0 {
			// Added right after the last rule that bans, each in front of the one added before.
			seen := 0
			for _, r := range rules {
				if !r.ours() {
					if seen++; seen == end {
						where = []string{"nft", "add", "rule", c.Family, c.Table, c.Name, "position", strconv.Itoa(r.Handle)}
						break
					}
				}
			}
		}
		failed := false
		for i := len(want) - 1; i >= 0; i-- {
			args := append(slices.Clone(where), want[i]...)
			if _, err := n.x(append(args, "comment", strconv.Quote(mark))...); err != nil {
				n.err = c.Family + " " + c.Table + " " + c.Name + ": " + err.Error()
				failed = true
				break
			}
		}
		if failed {
			// Partly in, it would be put back every heal: none rather.
			if out, err := n.x("nft", "-j", "-a", "list", "chain", c.Family, c.Table, c.Name); err == nil {
				n.remove(c, parseNFTRules(out))
			}
			continue
		}
		fixed = append(fixed, "nftables "+c.Family+" "+c.Table+" "+c.Name)
	}
	return fixed
}

// remove takes this data plane's rules out of a chain, by their handles.
func (n *nftables) remove(c nftChain, rules []nftRule) {
	for _, r := range rules {
		if r.ours() {
			n.x("nft", "delete", "rule", c.Family, c.Table, c.Name, "handle", strconv.Itoa(r.Handle))
		}
	}
}

func (n *nftables) close() {
	if n.present && n.applied {
		cs, _ := n.chains()
		for _, c := range cs {
			if !looked(c) {
				continue
			}
			if out, err := n.x("nft", "-j", "-a", "list", "chain", c.Family, c.Table, c.Name); err == nil {
				n.remove(c, parseNFTRules(out))
			}
		}
	}
	n.applied, n.found, n.err = false, false, ""
}
