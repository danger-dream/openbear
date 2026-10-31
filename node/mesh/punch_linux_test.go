package mesh

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/netip"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"

	"openbear/node/mesh/health"
)

// punchLab is a relay R on a "public" network behind a router W, and two
// members A and B each behind a NAT router like a home one: MASQUERADE out,
// and unsolicited packets to the router itself dropped. The link between W
// and R adds 20 ms each way, so the relayed path takes 80 ms round trip
// and the direct one next to nothing. Each node is a real mesh.Run in its
// own oblab-p namespace.
type punchLab struct {
	t     *testing.T
	ns    map[string]string
	dirs  map[string]string
	addr  map[string]netip.Addr
	procs map[string]*exec.Cmd
	env   []string
}

// The data planes a test starts with OBLAB_SIMULTANEOUS=1 punch the old
// way, both ends at once without opening.
func init() {
	if os.Getenv("OBLAB_SIMULTANEOUS") == "1" {
		health.SimultaneousPunch.Store(true)
	}
}

// punchNet is the networks of a punchLab. nat is the MASQUERADE option, ""
// or "--random-fully"; with sameNAT both members sit on one LAN behind N1.
// A plain router is a Linux box with MASQUERADE and nothing else: a packet
// to it that matches no flow reaches its own stack and leaves a flow of
// its own; and W counts for five more hops, as the internet between two
// homes has them.
type punchNet struct {
	nat            string
	sameNAT, plain bool
}

const punchUDP = 51820

func (l *punchLab) sh(args ...string) string {
	l.t.Helper()
	out, err := exec.Command(args[0], args[1:]...).CombinedOutput()
	if err != nil {
		l.t.Fatalf("%s: %v: %s", strings.Join(args, " "), err, out)
	}
	return string(out)
}

func (l *punchLab) in(name string, args ...string) string {
	l.t.Helper()
	return l.sh(append([]string{"ip", "netns", "exec", l.ns[name]}, args...)...)
}

// newPunchLab builds the networks.
func newPunchLab(t *testing.T, net punchNet) *punchLab {
	nat, sameNAT := net.nat, net.sameNAT
	l := &punchLab{t: t, ns: map[string]string{"W": "oblab-pw", "R": "oblab-pr", "N1": "oblab-pn1", "N2": "oblab-pn2", "A": "oblab-pa", "B": "oblab-pb"},
		dirs: map[string]string{}, procs: map[string]*exec.Cmd{},
		addr: map[string]netip.Addr{"R": netip.MustParseAddr("100.96.0.1"), "A": netip.MustParseAddr("100.96.0.11"), "B": netip.MustParseAddr("100.96.0.12")}}
	if sameNAT {
		delete(l.ns, "N2")
	}
	cleanup := func() {
		for _, n := range l.ns {
			exec.Command("ip", "netns", "del", n).Run()
		}
	}
	cleanup()
	linksLeft := rootLinksLeft(t, "obp", "obmesh")
	t.Cleanup(func() {
		cleanup()
		if out, _ := exec.Command("ip", "netns", "list").Output(); bytes.Contains(out, []byte("oblab-p")) {
			t.Errorf("namespaces left behind: %s", out)
		}
		linksLeft()
	})
	for _, n := range l.ns {
		l.sh("ip", "netns", "add", n)
		l.sh("ip", "-n", n, "link", "set", "lo", "up")
	}
	veth := func(a, ifa, addrA, b, ifb, addrB string) {
		l.sh("ip", "link", "add", ifa, "netns", l.ns[a], "type", "veth", "peer", "name", ifb, "netns", l.ns[b])
		for _, x := range [][3]string{{a, ifa, addrA}, {b, ifb, addrB}} {
			if x[2] != "" {
				l.sh("ip", "-n", l.ns[x[0]], "addr", "add", x[2], "dev", x[1])
			}
			l.sh("ip", "-n", l.ns[x[0]], "link", "set", x[1], "up")
		}
	}
	l.in("W", "sysctl", "-qw", "net.ipv4.ip_forward=1")
	veth("W", "obpw0", "10.200.0.1/24", "R", "obpr0", "10.200.0.2/24")
	l.sh("ip", "-n", l.ns["R"], "route", "add", "default", "via", "10.200.0.1")
	for _, x := range [][2]string{{"W", "obpw0"}, {"R", "obpr0"}} {
		l.sh("ip", "netns", "exec", l.ns[x[0]], "tc", "qdisc", "add", "dev", x[1], "root", "netem", "delay", "20ms")
	}
	members := map[string]string{"A": "N1", "B": "N2"}
	if sameNAT {
		members["B"] = "N1"
	}
	for i, n := range []string{"N1", "N2"} {
		if l.ns[n] == "" {
			continue
		}
		wan := fmt.Sprintf("obpn%dw", i+1)
		veth("W", fmt.Sprintf("obpw%d", i+1), fmt.Sprintf("10.200.%d.1/24", i+1), n, wan, fmt.Sprintf("10.200.%d.2/24", i+1))
		l.sh("ip", "-n", l.ns[n], "route", "add", "default", "via", fmt.Sprintf("10.200.%d.1", i+1))
		l.in(n, "sysctl", "-qw", "net.ipv4.ip_forward=1")
		masq := []string{"iptables", "-t", "nat", "-A", "POSTROUTING", "-o", wan, "-j", "MASQUERADE"}
		if nat != "" {
			masq = append(masq, nat)
		}
		l.in(n, masq...)
		if !net.plain {
			l.in(n, "iptables", "-A", "INPUT", "-i", wan, "-m", "conntrack", "--ctstate", "NEW", "-j", "DROP")
		}
		l.sh("ip", "-n", l.ns[n], "link", "add", "obplan", "type", "bridge")
		l.sh("ip", "-n", l.ns[n], "addr", "add", fmt.Sprintf("192.168.%d.1/24", i+1), "dev", "obplan")
		l.sh("ip", "-n", l.ns[n], "link", "set", "obplan", "up")
	}
	if net.plain {
		for _, i := range []string{"obpw1", "obpw2"} {
			l.in("W", "iptables", "-t", "mangle", "-A", "PREROUTING", "-i", i, "-j", "TTL", "--ttl-dec", "5")
		}
	}
	host := 2
	for _, m := range []string{"A", "B"} {
		n := members[m]
		subnet := map[string]int{"N1": 1, "N2": 2}[n]
		port := "obpl" + strings.ToLower(m)
		veth(n, port, "", m, "obpeth", fmt.Sprintf("192.168.%d.%d/24", subnet, host))
		l.sh("ip", "-n", l.ns[n], "link", "set", port, "master", "obplan")
		l.sh("ip", "-n", l.ns[m], "route", "add", "default", "via", fmt.Sprintf("192.168.%d.1", subnet))
		host++
	}
	return l
}

// start writes configs and runs the three data planes.
func (l *punchLab) start() {
	t := l.t
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	pubs, ctls := map[string]string{}, map[string]string{}
	for _, n := range []string{"R", "A", "B"} {
		l.dirs[n] = t.TempDir()
		wgPriv, wgPub := newKey(t)
		ctlPriv, ctlPub := newKey(t)
		pubs[n], ctls[n] = wgPub, ctlPub
		os.MkdirAll(filepath.Join(l.dirs[n], "keys"), 0o700)
		os.WriteFile(filepath.Join(l.dirs[n], "keys", "wg.key"), []byte(wgPriv), 0o600)
		os.WriteFile(filepath.Join(l.dirs[n], "keys", "control.key"), []byte(ctlPriv), 0o600)
		writeTestKeys(t, l.dirs[n], n)
	}
	node := func(n string) Node {
		nd := Node{ID: n, Name: n, Role: roleMember, Address: l.addr[n], ControlAddress: netip.AddrFrom4([4]byte{100, 96, 255, l.addr[n].As4()[3]}),
			PublicKey: pubs[n], ControlKey: ctls[n], SignKey: testSignPub(n), E2EKey: testE2EPub(n), Relays: []string{"R"}}
		if n == "R" {
			nd.Role, nd.Relays = roleRelay, nil
		}
		return nd
	}
	icmp := []Rule{{Proto: "icmp"}}
	for _, n := range []string{"R", "A", "B"} {
		c := &Config{Schema: ConfigSchema, Revision: 1, Network: Network{ID: "oblab", Prefix: meshPrefix, ControlPrefix: controlPrefix, MTU: 1400},
			Self:  Self{ID: n, Name: n, Role: roleMember, Address: l.addr[n], ControlAddress: node(n).ControlAddress, Listen: Listen{UDP: punchUDP}},
			Rules: icmp, Links: []Link{}, Health: &Health{ColdRetryMs: 10000}}
		for _, o := range []string{"R", "A", "B"} {
			if o != n {
				c.Nodes = append(c.Nodes, node(o))
			}
		}
		if n == "R" {
			c.Self.Role, c.NextHops = roleRelay, map[string]string{}
		} else {
			peer := map[string]string{"A": "B", "B": "A"}[n]
			c.Links = []Link{{Peer: peer, Kind: kindDirect, Punch: true}}
			c.Entry = &Entry{Mode: "fixed", Candidates: []Candidate{{ID: "c1", Relay: "R", Endpoint: fmt.Sprintf("udp://10.200.0.2:%d", punchUDP)}}}
			c.Reflectors = []string{fmt.Sprintf("10.200.0.2:%d", punchUDP)}
		}
		raw, err := json.Marshal(c)
		if err != nil {
			t.Fatal(err)
		}
		if err := Validate(raw); err != nil {
			t.Fatalf("%s: %v", n, err)
		}
		os.WriteFile(filepath.Join(l.dirs[n], "config.json"), raw, 0o600)
	}
	for _, n := range []string{"R", "A", "B"} {
		log, _ := os.Create(filepath.Join(l.dirs[n], "mesh.log"))
		cmd := exec.Command("ip", "netns", "exec", l.ns[n], self, "-test.run", "^TestNetnsNode$", "-test.v")
		cmd.Env = append(append(os.Environ(), "OBLAB_MESH_DIR="+l.dirs[n]), l.env...)
		cmd.Stdout, cmd.Stderr = log, log
		if err := cmd.Start(); err != nil {
			t.Fatal(err)
		}
		l.procs[n] = cmd
	}
	t.Cleanup(func() {
		for n, cmd := range l.procs {
			cmd.Process.Signal(os.Interrupt)
			done := make(chan error, 1)
			go func() { done <- cmd.Wait() }()
			select {
			case <-done:
			case <-time.After(10 * time.Second):
				cmd.Process.Kill()
				t.Errorf("%s did not stop on SIGINT", n)
			}
		}
		if t.Failed() {
			for _, n := range []string{"R", "A", "B"} {
				raw, _ := os.ReadFile(filepath.Join(l.dirs[n], "mesh.log"))
				st, _ := os.ReadFile(filepath.Join(l.dirs[n], "status.json"))
				t.Logf("%s log:\n%s\n%s status:\n%s", n, raw, n, st)
			}
		}
	})
	for _, n := range []string{"R", "A", "B"} {
		l.wait(n+" ready", 30*time.Second, func() bool { s := l.status(n); return s.Ready && s.AppliedRevision == 1 })
	}
}

func (l *punchLab) status(n string) Status {
	var s Status
	raw, _ := os.ReadFile(filepath.Join(l.dirs[n], "status.json"))
	json.Unmarshal(raw, &s)
	return s
}

// direct is n's view of its punched peer.
func (l *punchLab) direct(n string) DirectStatus {
	for _, p := range l.status(n).Peers {
		if p.Kind == kindDirect && p.Direct != nil {
			return *p.Direct
		}
	}
	return DirectStatus{}
}

func (l *punchLab) events(n, kind string) []string {
	var out []string
	for _, e := range l.status(n).Events {
		if e.Kind == kind {
			out = append(out, e.Detail)
		}
	}
	return out
}

func (l *punchLab) wait(what string, within time.Duration, ok func() bool) time.Duration {
	l.t.Helper()
	start := time.Now()
	for !ok() {
		if time.Since(start) > within {
			l.t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(100 * time.Millisecond)
	}
	return time.Since(start)
}

type pingReply struct {
	seq, ttl int
	at       time.Time
	rtt      float64 // ms
}

type pingRun struct {
	cmd     *exec.Cmd
	out     bytes.Buffer
	started time.Time
	every   time.Duration
}

// pinger pings every 50 ms from A to B until stopped.
func (l *punchLab) pinger() *pingRun {
	p := &pingRun{cmd: exec.Command("ip", "netns", "exec", l.ns["A"], "ping", "-D", "-n", "-i", "0.05", l.addr["B"].String()), every: 50 * time.Millisecond}
	p.cmd.Stdout = &p.out
	p.started = time.Now()
	if err := p.cmd.Start(); err != nil {
		l.t.Fatal(err)
	}
	l.t.Cleanup(func() { p.cmd.Process.Kill(); p.cmd.Wait() })
	return p
}

var pingLine = regexp.MustCompile(`^\[(\d+)\.(\d+)\] .* icmp_seq=(\d+) ttl=(\d+) time=([\d.]+) ms`)

// stop ends the run and returns its replies and how many were sent.
func (p *pingRun) stop() ([]pingReply, int) {
	p.cmd.Process.Signal(os.Interrupt)
	p.cmd.Wait()
	var out []pingReply
	for _, line := range strings.Split(p.out.String(), "\n") {
		m := pingLine.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		sec, _ := strconv.ParseInt(m[1], 10, 64)
		frac, _ := strconv.ParseFloat("0."+m[2], 64)
		seq, _ := strconv.Atoi(m[3])
		ttl, _ := strconv.Atoi(m[4])
		rtt, _ := strconv.ParseFloat(m[5], 64)
		out = append(out, pingReply{seq: seq, ttl: ttl, rtt: rtt, at: time.Unix(sec, int64(frac*1e9))})
	}
	sent := 0
	if len(out) > 0 {
		sent = out[len(out)-1].seq
	}
	return out, sent
}

// longestGap is the longest run of sequence numbers without a reply, as time.
func longestGap(r []pingReply, every time.Duration) time.Duration {
	gap := 0
	for i := 1; i < len(r); i++ {
		gap = max(gap, r[i].seq-r[i-1].seq-1)
	}
	return time.Duration(gap) * every
}

func median(v []float64) float64 {
	if len(v) == 0 {
		return 0
	}
	v = slices.Clone(v)
	slices.Sort(v)
	return v[len(v)/2]
}

// byPath splits the replies into those through the relay, which lowers the
// TTL by one, and the direct ones.
// directRTT tells the paths apart: the relay's has netem delays on the way
// (70 ms and more), the direct one none. TTL cannot: what the relay carries
// is end to end, its TTL as the sender set it.
const directRTT = 35

func byPath(r []pingReply) (relayed, direct []float64) {
	for _, x := range r {
		if x.rtt < directRTT {
			direct = append(direct, x.rtt)
		} else {
			relayed = append(relayed, x.rtt)
		}
	}
	return
}

var pingTime = regexp.MustCompile(`time=([\d.]+) ms`)

// directPings: ping's output has replies, all over the direct path.
func directPings(out string) bool {
	m := pingTime.FindAllStringSubmatch(out, -1)
	for _, x := range m {
		if rtt, _ := strconv.ParseFloat(x[1], 64); rtt >= directRTT {
			return false
		}
	}
	return len(m) > 0
}

func (l *punchLab) bothDirect(via string) func() bool {
	return func() bool {
		a, b := l.direct("A"), l.direct("B")
		return a.State == "direct" && b.State == "direct" && a.Via == via && b.Via == via
	}
}

// TestPunch needs root. OBLAB_PUNCH=1 runs every case; each builds its own
// namespaces and removes them.
func TestPunch(t *testing.T) {
	if os.Getenv("OBLAB_PUNCH") != "1" || os.Getuid() != 0 {
		t.Skip("set OBLAB_PUNCH=1 and run as root")
	}
	t.Run("nat", func(t *testing.T) {
		l := newPunchLab(t, punchNet{})
		l.start()
		ping := l.pinger()
		took := l.wait("direct on both members", 40*time.Second, l.bothDirect("public"))
		time.Sleep(3 * time.Second)
		replies, sent := ping.stop()
		relayed, direct := byPath(replies)
		gap := longestGap(replies, ping.every)
		t.Logf("direct %v after ready; %d/%d replies, relayed %d (median %.1f ms), direct %d (median %.2f ms), longest gap %v; A %+v; B %+v",
			took, len(replies), sent, len(relayed), median(relayed), len(direct), median(direct), gap, l.direct("A"), l.direct("B"))
		if len(relayed) == 0 || len(direct) == 0 || median(relayed) < 70 || median(direct) > 10 {
			t.Fatal("business did not move from the relay to the direct path")
		}
		if gap > 500*time.Millisecond {
			t.Fatalf("business stopped for %v while moving", gap)
		}
		t.Logf("events A %v %v", l.events("A", "direct.up"), l.events("A", "direct.failed"))
	})

	t.Run("symmetric", func(t *testing.T) {
		l := newPunchLab(t, punchNet{nat: "--random-fully"})
		l.start()
		l.wait("a failed attempt", 40*time.Second, func() bool {
			d := l.direct("A")
			return d.State == "relayed" && d.Attempts >= 1 && d.LastError == "punch timeout"
		})
		ping := l.pinger()
		time.Sleep(10 * time.Second)
		replies, sent := ping.stop()
		relayed, direct := byPath(replies)
		t.Logf("%d/%d replies, relayed %d (median %.1f ms), direct %d; A %+v; B %+v; failed %v",
			len(replies), sent, len(relayed), median(relayed), len(direct), l.direct("A"), l.direct("B"), l.events("A", "direct.failed"))
		if len(direct) != 0 || len(replies) < sent-2 {
			t.Fatal("business through the relay was disturbed")
		}
		if d := l.direct("B"); d.State == "direct" {
			t.Fatalf("B is direct behind a symmetric NAT: %+v", d)
		}
	})

	t.Run("cut", func(t *testing.T) {
		l := newPunchLab(t, punchNet{})
		l.start()
		l.wait("direct on both members", 40*time.Second, l.bothDirect("public"))
		ping := l.pinger()
		time.Sleep(2 * time.Second)
		rules := [][]string{{"-s", "10.200.1.0/24", "-d", "10.200.2.0/24"}, {"-s", "10.200.2.0/24", "-d", "10.200.1.0/24"}}
		for _, r := range rules {
			l.in("W", append(append([]string{"iptables", "-I", "FORWARD"}, r...), "-p", "udp", "-j", "DROP")...)
		}
		cut := time.Now()
		back := l.wait("both members back on the relay", 20*time.Second, func() bool {
			a, b := l.direct("A"), l.direct("B")
			return a.State != "direct" && b.State != "direct"
		})
		time.Sleep(3 * time.Second)
		replies, sent := ping.stop()
		var after []pingReply
		for _, r := range replies {
			if r.at.After(cut) {
				after = append(after, r)
			}
		}
		relayed, _ := byPath(after)
		gap := longestGap(replies, ping.every)
		t.Logf("relayed %v after the cut; %d/%d replies, %d relayed after it, longest gap %v; A %+v; down %v",
			back, len(replies), sent, len(relayed), gap, l.direct("A"), l.events("A", "direct.down"))
		if len(relayed) == 0 || gap > 8*time.Second {
			for _, n := range []string{"A", "B", "R"} {
				s := l.status(n)
				t.Logf("%s: counters %+v e2e %+v", n, s.Counters, s.E2E)
				for _, p := range s.Peers {
					t.Logf("%s peer %+v", n, p)
				}
			}
			t.Fatal("business did not return to the relay")
		}
		for _, r := range rules {
			l.in("W", append(append([]string{"iptables", "-D", "FORWARD"}, r...), "-p", "udp", "-j", "DROP")...)
		}
		again := l.wait("direct again after the cut ends", 90*time.Second, l.bothDirect("public"))
		out := l.in("A", "ping", "-c", "3", "-i", "0.2", l.addr["B"].String())
		t.Logf("direct again %v after the cut ended; A %+v\n%s", again, l.direct("A"), out)
		if !directPings(out) {
			t.Fatal("not direct after the cut ended")
		}
	})

	// Two plain Linux NATs and both members starting together: the old way,
	// both ends sending at once, must fail every time; opening first must
	// get through.
	t.Run("race", func(t *testing.T) {
		for _, old := range []bool{true, false} {
			for trial := 1; trial <= 3; trial++ {
				l := newPunchLab(t, punchNet{plain: true})
				if old {
					l.env = []string{"OBLAB_SIMULTANEOUS=1"}
				}
				l.start()
				start := time.Now()
				var took time.Duration
				var punching time.Time
				dumped := false
				for time.Since(start) < 45*time.Second && took == 0 {
					if l.bothDirect("public")() {
						took = time.Since(start)
					}
					if punching.IsZero() && l.direct("B").State == "punching" {
						punching = time.Now()
					}
					if !dumped && !punching.IsZero() && time.Since(punching) > 4*time.Second {
						// What each NAT holds between the two public addresses.
						for _, n := range []string{"N1", "N2"} {
							out, _ := exec.Command("ip", "netns", "exec", l.ns[n], "conntrack", "-L", "-p", "udp").CombinedOutput()
							var flows []string
							for _, line := range strings.Split(string(out), "\n") {
								if strings.Contains(line, "10.200.1.2") && strings.Contains(line, "10.200.2.2") {
									flows = append(flows, strings.Join(strings.Fields(line), " "))
								}
							}
							t.Logf("old=%v trial %d: %s flows:\n%s", old, trial, n, strings.Join(flows, "\n"))
						}
						dumped = true
					}
					time.Sleep(100 * time.Millisecond)
				}
				a, b := l.direct("A"), l.direct("B")
				t.Logf("old=%v trial %d: direct after %v (0: not within 45 s); A %+v; B %+v; failed %v", old, trial, took, a, b, l.events("A", "direct.failed"))
				if old && took != 0 {
					t.Errorf("old=%v trial %d: punched at once through two plain NATs", old, trial)
				}
				if !old {
					if took == 0 {
						t.Fatalf("trial %d: opening first did not get through", trial)
					}
					out := l.in("A", "ping", "-c", "3", "-i", "0.2", l.addr["B"].String())
					if !directPings(out) {
						t.Fatalf("trial %d: not direct\n%s", trial, out)
					}
				}
				for n := range l.procs {
					l.procs[n].Process.Signal(os.Interrupt)
				}
				for n, cmd := range l.procs {
					cmd.Wait()
					delete(l.procs, n)
				}
				for _, n := range l.ns {
					exec.Command("ip", "netns", "del", n).Run()
				}
			}
		}
	})

	t.Run("samenat", func(t *testing.T) {
		l := newPunchLab(t, punchNet{sameNAT: true})
		l.start()
		took := l.wait("direct over the LAN", 40*time.Second, l.bothDirect("lan"))
		out := l.in("A", "ping", "-c", "5", "-i", "0.2", l.addr["B"].String())
		t.Logf("direct over the LAN %v after ready; A %+v; B %+v\n%s", took, l.direct("A"), l.direct("B"), out)
		if !directPings(out) {
			t.Fatal("not direct")
		}
		for _, p := range l.status("A").Peers {
			if p.Kind == kindDirect && !strings.HasPrefix(p.Endpoint, "192.168.1.") {
				t.Fatalf("direct path is %s, not the LAN", p.Endpoint)
			}
		}
	})
}
