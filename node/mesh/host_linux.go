package mesh

import (
	"errors"
	"fmt"
	"net/netip"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"

	"golang.zx2c4.com/wireguard/tun"
)

const (
	chainIn     = "OBMESH-IN"
	chainFwd    = "OBMESH-FWD"
	chainNat    = "OBMESH-NAT"
	chainMSS    = "OBMESH-MSS"
	chainDocker = "OBMESH-DOCKER"
	dockerUser  = "DOCKER-USER"
	ipForward   = "/proc/sys/net/ipv4/ip_forward"
)

func createTUN(dir string, mtu int) (tun.Device, error) {
	return tun.CreateTUN("obmesh0", mtu)
}

func run(args ...string) error {
	path, err := lookCmd(args[0])
	if err != nil {
		return err
	}
	out, err := exec.Command(path, args[1:]...).CombinedOutput()
	if err != nil {
		return fmt.Errorf("%s: %v: %s", strings.Join(args, " "), err, strings.TrimSpace(string(out)))
	}
	return nil
}

// execFn runs a command and gives its standard output; the error carries
// what it printed on standard error. Tests put a fake or a network
// namespace in its place.
type execFn func(args ...string) (string, error)

// hostExec runs a command on this machine. Standard error is kept out of the
// output: iptables-nft warns there about legacy tables, no part of a listing.
func hostExec(args ...string) (string, error) {
	path, err := lookCmd(args[0])
	if err != nil {
		return "", err
	}
	cmd := exec.Command(path, args[1:]...)
	var stderr strings.Builder
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		return string(out), fmt.Errorf("%s: %v: %s", strings.Join(args, " "), err, strings.TrimSpace(stderr.String()+" "+string(out)))
	}
	return string(out), nil
}

func hostHas(name string) bool {
	_, err := lookCmd(name)
	return err == nil
}

// cmdDirs are where lookCmd looks for a program PATH does not have: a
// service's PATH may leave out /sbin and /bin (systemd's has neither), where
// some systems keep iptables, nft or ip. Tests put others.
var cmdDirs = []string{"/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin"}

// lookCmd finds a program by name, in PATH first and then in cmdDirs, and
// gives its absolute path: every command the host's network and firewall
// setup runs goes through it.
func lookCmd(name string) (string, error) {
	if strings.Contains(name, "/") {
		return name, nil
	}
	if p, err := exec.LookPath(name); err == nil {
		if abs, err := filepath.Abs(p); err == nil {
			return abs, nil
		}
	}
	for _, d := range cmdDirs {
		p := filepath.Join(d, name)
		if fi, err := os.Stat(p); err == nil && fi.Mode().IsRegular() && fi.Mode().Perm()&0o111 != 0 {
			return p, nil
		}
	}
	return "", fmt.Errorf("%s: not found in PATH or %s", name, strings.Join(cmdDirs, ":"))
}

type linuxHost struct {
	mu             sync.Mutex
	name           string
	x              execFn
	cur            hostConfig
	staleAddresses []netip.Addr
	up             bool
	dirty          bool
	iptables       bool
	chains         map[string]chain // installed, by name
	// docker is OBMESH-DOCKER as the applied config wants it, nil without
	// iptables; it goes in once DOCKER-USER exists, also when Docker starts
	// after this data plane.
	docker *chain
	// forwardWas is ip_forward before a gateway turned it on; empty when
	// this data plane did not change it. forwardDocker: Docker was there then.
	forwardWas    string
	forwardDocker bool
	// forwardFile is ip_forward's file; tests put another.
	forwardFile string
	// forwardState keeps forwardWas for a data plane started after this one
	// was killed: it puts ip_forward back. Empty: not kept.
	forwardState string
	// swept: chains a killed data plane left were taken out.
	swept bool
	fw    *hostFirewalls
}

type chain struct {
	table, parent string
	rules         [][]string
}

func newHost(name, dir string, _ tun.Device) hostNet {
	h := newLinuxHost(name, hostExec, hostHas)
	h.forwardState = filepath.Join(dir, "ip_forward.was")
	h.fw.fd.stateFile = filepath.Join(dir, "firewalld-owned.json")
	return h
}

// newLinuxHost runs its commands through x; has says whether a program is
// installed.
func newLinuxHost(name string, x execFn, has func(string) bool) *linuxHost {
	return &linuxHost{name: name, x: x, iptables: has("iptables"), chains: map[string]chain{}, forwardFile: ipForward, fw: newHostFirewalls(name, x, has)}
}

func (h *linuxHost) run(args ...string) error {
	_, err := h.x(args...)
	return err
}

func (h *linuxHost) apply(next hostConfig) (err error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	defer func() { h.dirty = err != nil }()
	first := !h.up || h.dirty
	run := h.run
	addr := next.Address.String()
	addressChanged := next.Address != h.cur.Address
	if first || addressChanged {
		if err := run("ip", "-4", "address", "replace", addr+"/32", "dev", h.name); err != nil {
			return err
		}
		if h.cur.Address.IsValid() && h.cur.Address != next.Address {
			h.staleAddresses = append(h.staleAddresses, h.cur.Address)
		}
		h.cur.Address, h.up = next.Address, true
	}
	for len(h.staleAddresses) > 0 {
		old := h.staleAddresses[0]
		if old != next.Address {
			if err := run("ip", "-4", "address", "del", old.String()+"/32", "dev", h.name); err != nil {
				out, queryErr := h.x("ip", "-4", "address", "show", "dev", h.name, "to", old.String()+"/32")
				if queryErr != nil || strings.TrimSpace(out) != "" {
					return err
				}
			}
		}
		h.staleAddresses = h.staleAddresses[1:]
	}
	if first || next.MTU != h.cur.MTU {
		if err := run("ip", "link", "set", "dev", h.name, "mtu", strconv.Itoa(next.MTU), "up"); err != nil {
			return err
		}
		h.cur.MTU = next.MTU
	}
	h.up = true
	for _, r := range next.Routes {
		if first || addressChanged || !slices.Contains(h.cur.Routes, r) {
			if err := run("ip", "-4", "route", "replace", r.String(), "dev", h.name, "src", addr); err != nil {
				return err
			}
			if !slices.Contains(h.cur.Routes, r) {
				h.cur.Routes = append(slices.Clone(h.cur.Routes), r)
			}
		}
	}
	for _, r := range slices.Clone(h.cur.Routes) {
		if !slices.Contains(next.Routes, r) {
			if err := run("ip", "-4", "route", "del", r.String(), "dev", h.name); err != nil {
				out, queryErr := h.x("ip", "-4", "route", "show", r.String(), "dev", h.name)
				if queryErr != nil || strings.TrimSpace(out) != "" {
					return err
				}
			}
			h.cur.Routes = slices.DeleteFunc(slices.Clone(h.cur.Routes), func(p netip.Prefix) bool { return p == r })
		}
	}
	h.cur.Address, h.cur.MTU, h.cur.Routes = next.Address, next.MTU, next.Routes

	if err := h.firewall(next); err != nil {
		return err
	}
	h.cur = next
	return nil
}

func (h *linuxHost) firewall(next hostConfig) error {
	want := map[string]chain{}
	in := chain{table: "filter", parent: "INPUT", rules: [][]string{{"-i", h.name, "-j", "ACCEPT"}}}
	if next.UDP != 0 {
		in.rules = append(in.rules, []string{"-p", "udp", "-m", "udp", "--dport", strconv.Itoa(next.UDP), "-j", "ACCEPT"})
	}
	for _, p := range []int{next.TCP, next.TLS} {
		if p != 0 {
			in.rules = append(in.rules, []string{"-p", "tcp", "-m", "tcp", "--dport", strconv.Itoa(p), "-j", "ACCEPT"})
		}
	}
	want[chainIn] = in
	if h.iptables {
		// Docker forwards what comes in for a port published on the mesh
		// address; DOCKER-USER is where users (ufw-docker) drop such traffic.
		// The mesh's own rules have let it in already: the connection and its
		// replies go through, nothing else.
		a := next.Address.String()
		d := chain{table: "filter", parent: dockerUser, rules: [][]string{
			{"-i", h.name, "-m", "conntrack", "--ctorigdst", a, "--ctdir", "ORIGINAL", "-j", "ACCEPT"},
			{"-o", h.name, "-m", "conntrack", "--ctorigdst", a, "--ctdir", "REPLY", "-j", "ACCEPT"}}}
		h.docker = &d
		if h.exists("filter", dockerUser) {
			want[chainDocker] = d
		}
	}
	if g := next.Gateway; g != nil {
		fwd := chain{table: "filter", parent: "FORWARD"}
		nat := chain{table: "nat", parent: "POSTROUTING"}
		for _, s := range g.Subnets {
			fwd.rules = append(fwd.rules, []string{"-d", s.String(), "-i", h.name, "-j", "ACCEPT"}, []string{"-s", s.String(), "-o", h.name, "-j", "ACCEPT"})
			nat.rules = append(nat.rules, []string{"-s", next.Prefix.String(), "-d", s.String(), "-j", "MASQUERADE"})
		}
		want[chainFwd] = fwd
		// The mesh device's MTU leaves room for the end-to-end header, so TCP
		// a gateway forwards into the mesh is held to segments that fit it.
		want[chainMSS] = chain{table: "mangle", parent: "FORWARD", rules: [][]string{
			{"-o", h.name, "-p", "tcp", "-m", "tcp", "--tcp-flags", "SYN,RST", "SYN", "-j", "TCPMSS", "--clamp-mss-to-pmtu"}}}
		if g.Masquerade {
			want[chainNat] = nat
		}
		if err := h.forwarding(true); err != nil {
			return err
		}
	}
	// firewalld and nftables: never fatal, what does not go through is reported.
	w := firewallWant{addr: next.Address, udp: next.UDP, tcp: next.TCP, tls: next.TLS}
	if next.Gateway != nil {
		w.subnets = slices.Clone(next.Gateway.Subnets)
	}
	h.fw.apply(w)
	defer h.fw.report(h.iptables)
	if !h.iptables {
		if next.Gateway != nil {
			return errors.New("a gateway needs iptables")
		}
		return nil
	}
	if !h.swept {
		// A data plane that was killed left its chains: those no longer wanted
		// (a gateway's forwarding and translation) go.
		for name, c := range ownChains {
			if _, ok := want[name]; !ok && h.exists(c.table, name) {
				h.removeChain(name, c)
			}
		}
		h.swept = true
	}
	for name, c := range want {
		if old, ok := h.chains[name]; ok && slices.EqualFunc(old.rules, c.rules, slices.Equal) {
			continue
		}
		h.chains[name] = chain{table: c.table, parent: c.parent}
		if err := h.writeChain(name, c); err != nil {
			if name == chainDocker {
				// Containers only: the mesh itself is not held up by it.
				h.fw.dockerErr = err.Error()
				continue
			}
			return err
		}
		if name == chainDocker {
			h.fw.dockerErr = ""
		}
		h.chains[name] = c
	}
	for name, c := range h.chains {
		if _, ok := want[name]; !ok {
			h.removeChain(name, c)
			delete(h.chains, name)
		}
	}
	if next.Gateway == nil {
		h.forwarding(false)
	}
	return nil
}

// ownChains: where each chain of this data plane hangs.
var ownChains = map[string]chain{
	chainIn: {table: "filter", parent: "INPUT"}, chainDocker: {table: "filter", parent: dockerUser},
	chainFwd: {table: "filter", parent: "FORWARD"}, chainNat: {table: "nat", parent: "POSTROUTING"}, chainMSS: {table: "mangle", parent: "FORWARD"}}

// forwarding turns IPv4 forwarding on for a gateway, and back off only if
// it was off before this data plane turned it on.
func (h *linuxHost) forwarding(on bool) error {
	if h.forwardWas == "" && h.forwardState != "" {
		if raw, err := os.ReadFile(h.forwardState); err == nil {
			if f := strings.Fields(string(raw)); len(f) > 0 {
				h.forwardWas, h.forwardDocker = f[0], len(f) > 1 && f[1] == "docker"
			}
		}
	}
	if on {
		raw, err := os.ReadFile(h.forwardFile)
		if err != nil {
			return err
		}
		if was := strings.TrimSpace(string(raw)); was != "1" {
			if err = os.WriteFile(h.forwardFile, []byte("1"), 0o644); err != nil {
				return fmt.Errorf("enable IP forwarding: %w", err)
			}
			if h.forwardWas == "" {
				h.forwardWas, h.forwardDocker = was, h.exists("filter", dockerUser)
				if h.forwardState != "" {
					state := was
					if h.forwardDocker {
						state += " docker"
					}
					os.WriteFile(h.forwardState, []byte(state), 0o600)
				}
			}
		}
		return nil
	}
	// Docker started while the gateway was on found forwarding on and left it to
	// others: its containers need it, so it stays on.
	if h.forwardWas != "" && (h.forwardDocker || !h.exists("filter", dockerUser)) {
		os.WriteFile(h.forwardFile, []byte(h.forwardWas), 0o644)
	}
	h.forwardWas, h.forwardDocker = "", false
	if h.forwardState != "" {
		os.Remove(h.forwardState)
	}
	return nil
}

func (h *linuxHost) iptablesCmd(table string, args ...string) error {
	return h.run(append([]string{"iptables", "-w", "-t", table}, args...)...)
}

// exists reports whether the table has the chain.
func (h *linuxHost) exists(table, name string) bool {
	_, err := h.x("iptables", "-w", "-t", table, "-S", name)
	return err == nil
}

func (h *linuxHost) writeChain(name string, c chain) error {
	h.iptablesCmd(c.table, "-N", name)
	if err := h.iptablesCmd(c.table, "-F", name); err != nil {
		return err
	}
	for _, r := range c.rules {
		if err := h.iptablesCmd(c.table, append([]string{"-A", name}, r...)...); err != nil {
			return err
		}
	}
	h.ensureJump(name, c)
	return nil
}

func (h *linuxHost) removeChain(name string, c chain) {
	for h.iptablesCmd(c.table, "-D", c.parent, "-j", name) == nil {
	}
	h.iptablesCmd(c.table, "-F", name)
	h.iptablesCmd(c.table, "-X", name)
}

// ensureJump keeps the jump in the parent chain where jumpAt puts it; true
// when it had to and could (a parent chain that is gone, DOCKER-USER, takes
// no jump).
func (h *linuxHost) ensureJump(name string, c chain) bool {
	out, _ := h.x("iptables", "-w", "-t", c.table, "-S", c.parent)
	lines := strings.Split(strings.TrimSpace(out), "\n")
	jump := "-A " + c.parent + " -j " + name
	var others []string
	at := -1
	for i, l := range lines[1:] {
		if l == jump {
			if at >= 0 {
				at = -2 // twice
			} else if at == -1 {
				at = i
			}
			continue
		}
		others = append(others, l)
	}
	pos := jumpAt(others)
	if at == pos {
		return false
	}
	for h.iptablesCmd(c.table, "-D", c.parent, "-j", name) == nil {
	}
	return h.iptablesCmd(c.table, "-I", c.parent, strconv.Itoa(pos+1), "-j", name) == nil
}

// banChains: chains of the tools that ban addresses (fail2ban, CrowdSec,
// sshguard), by the names they give them.
var banChains = []string{"f2b-", "fail2ban", "crowdsec", "sshguard"}

func banTarget(target string) bool {
	target = strings.ToLower(target)
	return slices.ContainsFunc(banChains, func(p string) bool { return strings.HasPrefix(target, p) })
}

// jumpAt: how many of the parent's rules ("-A ..." lines, without ours) go
// before the jump. The mesh device and the listen ports are let in after
// what bans addresses, so a ban holds over the mesh too: after the last
// jump to a ban tool's chain or drop by an ipset, and after ufw's own
// checks and user rules (ufw-before-input, ufw-before-forward and
// ufw-user-forward, where "ufw deny" and fail2ban's ufw action put theirs).
// Nothing past a rule that drops, rejects, accepts or returns whatever
// reaches it counts: the jump would not be reached.
func jumpAt(rules []string) int {
	at := 0
	for i, l := range rules {
		f := strings.Fields(l)
		target := ""
		for j := 0; j+1 < len(f); j++ {
			if f[j] == "-j" || f[j] == "-g" {
				target = f[j+1]
			}
		}
		switch {
		case banTarget(target),
			strings.Contains(l, "--match-set") && (target == "DROP" || target == "REJECT"),
			strings.HasPrefix(target, "ufw-before-") && target != "ufw-before-logging-input" && target != "ufw-before-logging-forward" || target == "ufw-user-forward":
			at = i + 1
		case len(f) > 3 && f[2] == "-j" && (target == "DROP" || target == "REJECT" || target == "ACCEPT" || target == "RETURN"):
			return at
		}
	}
	return at
}

// intact reports whether the chain still holds exactly its rules.
func (h *linuxHost) intact(name string, c chain) bool {
	out, err := h.x("iptables", "-w", "-t", c.table, "-S", name)
	if err != nil || len(strings.Split(strings.TrimSpace(out), "\n")) != len(c.rules)+1 {
		return false
	}
	for _, r := range c.rules {
		if h.iptablesCmd(c.table, append([]string{"-C", name}, r...)...) != nil {
			return false
		}
	}
	return true
}

// heal puts back what a firewall reload (ufw, firewalld, nftables, Docker)
// removed, and adds OBMESH-DOCKER once Docker made DOCKER-USER.
func (h *linuxHost) heal() []string {
	h.mu.Lock()
	defer h.mu.Unlock()
	if !h.up {
		return nil
	}
	var fixed []string
	if h.docker != nil {
		if _, ok := h.chains[chainDocker]; !ok && h.exists("filter", dockerUser) {
			if err := h.writeChain(chainDocker, *h.docker); err != nil {
				h.fw.dockerErr = err.Error()
			} else {
				h.fw.dockerErr = ""
				h.chains[chainDocker] = *h.docker
				fixed = append(fixed, chainDocker+" jump in "+dockerUser)
			}
		}
	}
	for name, c := range h.chains {
		if !h.intact(name, c) {
			if h.writeChain(name, c) == nil {
				fixed = append(fixed, name+" rules")
			}
		} else if h.ensureJump(name, c) {
			fixed = append(fixed, name+" jump in "+c.parent)
		}
	}
	fixed = append(fixed, h.fw.heal()...)
	h.fw.report(h.iptables)
	return fixed
}

func (h *linuxHost) ready() bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.up
}

func (h *linuxHost) close() {
	h.mu.Lock()
	defer h.mu.Unlock()
	for name, c := range h.chains {
		h.removeChain(name, c)
	}
	h.chains = map[string]chain{}
	h.docker = nil
	h.fw.close()
	h.forwarding(false)
}

func residentBytes() uint64 {
	raw, err := os.ReadFile("/proc/self/statm")
	if err != nil {
		return 0
	}
	f := strings.Fields(string(raw))
	if len(f) < 2 {
		return 0
	}
	pages, _ := strconv.ParseUint(f[1], 10, 64)
	return pages * uint64(os.Getpagesize())
}
