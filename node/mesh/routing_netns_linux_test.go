package mesh

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"net/netip"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

// Relay routing in network namespaces: every relay a real data plane
// (TestNetnsNode) in its own oblab-rt- namespace, every relay link its own
// veth pair with netem delays, faults made with netem on both ends of a
// link, and business traffic measured with ping over obmesh0.
// OBLAB_NETNS=1 OBLAB_ROUTING=1 enables it; it needs root.

type rtLink struct {
	a, b    string  // a dials b
	rttMs   float64 // netem adds half on each end
	devA    string
	devB    string
	addrA   string
	addrB   string
	current string // the netem arguments in place
	tcp     bool   // the relay link runs over TCP, else UDP
}

type rtLab struct {
	t     *testing.T
	ids   []string
	links []*rtLink
	ns    map[string]string
	dirs  map[string]string
	addr  map[string]netip.Addr
	procs map[string]*exec.Cmd
	keys  map[string][4]string // wg priv, wg pub, control priv, control pub
	hops  map[string]map[string]string
}

func rtSkip(t *testing.T) {
	if os.Getenv("OBLAB_NETNS") != "1" || os.Getenv("OBLAB_ROUTING") != "1" || os.Getuid() != 0 {
		t.Skip("set OBLAB_NETNS=1 OBLAB_ROUTING=1 and run as root")
	}
}

func rtSh(t *testing.T, args ...string) string {
	t.Helper()
	out, err := exec.Command(args[0], args[1:]...).CombinedOutput()
	if err != nil {
		t.Fatalf("%s: %v: %s", strings.Join(args, " "), err, out)
	}
	return string(out)
}

// newRTLab builds the namespaces and links: links are "A-B:rtt", A dialing,
// or "A-B:rtt:tcp" for a link over TCP;
// hops are each relay's configured next hops, "A>C": "B".
func newRTLab(t *testing.T, ids []string, links []string, hops map[string]string) *rtLab {
	l := &rtLab{t: t, ids: ids, ns: map[string]string{}, dirs: map[string]string{}, addr: map[string]netip.Addr{}, procs: map[string]*exec.Cmd{},
		keys: map[string][4]string{}, hops: map[string]map[string]string{}}
	for i, id := range ids {
		l.ns[id] = "oblab-rt-" + strings.ToLower(id)
		l.addr[id] = netip.AddrFrom4([4]byte{100, 96, 0, byte(1 + i)})
	}
	cleanup := func() {
		for _, n := range l.ns {
			exec.Command("ip", "netns", "del", n).Run()
		}
	}
	cleanup()
	// This machine may run a mesh node of its own.
	hostMesh, _ := exec.Command("ip", "link", "show").Output()
	hadMesh := bytes.Contains(hostMesh, []byte("obmesh"))
	t.Cleanup(func() {
		l.stop()
		cleanup()
		if out, _ := exec.Command("ip", "netns", "list").Output(); bytes.Contains(out, []byte("oblab-rt")) {
			t.Errorf("namespaces left behind: %s", out)
		}
		if out, _ := exec.Command("ip", "link", "show").Output(); bytes.Contains(out, []byte("oblrt")) || (!hadMesh && bytes.Contains(out, []byte("obmesh"))) {
			t.Errorf("links left behind in the root namespace: %s", out)
		}
	})
	for _, id := range ids {
		rtSh(t, "ip", "netns", "add", l.ns[id])
		rtSh(t, "ip", "-n", l.ns[id], "link", "set", "lo", "up")
	}
	for i, s := range links {
		pair, rtt, _ := strings.Cut(s, ":")
		rtt, transport, _ := strings.Cut(rtt, ":")
		a, b, _ := strings.Cut(pair, "-")
		v, _ := strconv.ParseFloat(rtt, 64)
		k := &rtLink{a: a, b: b, rttMs: v, devA: fmt.Sprintf("oblrt%da", i), devB: fmt.Sprintf("oblrt%db", i),
			addrA: fmt.Sprintf("10.77.%d.1", i), addrB: fmt.Sprintf("10.77.%d.2", i), tcp: transport == "tcp"}
		rtSh(t, "ip", "link", "add", k.devA, "netns", l.ns[a], "type", "veth", "peer", "name", k.devB, "netns", l.ns[b])
		for _, end := range [][3]string{{a, k.devA, k.addrA}, {b, k.devB, k.addrB}} {
			rtSh(t, "ip", "-n", l.ns[end[0]], "addr", "add", end[2]+"/24", "dev", end[1])
			rtSh(t, "ip", "-n", l.ns[end[0]], "link", "set", end[1], "up")
		}
		l.links = append(l.links, k)
		l.netem(k, "")
	}
	for k, v := range hops {
		from, to, _ := strings.Cut(k, ">")
		if l.hops[from] == nil {
			l.hops[from] = map[string]string{}
		}
		l.hops[from][to] = v
	}
	for _, id := range ids {
		l.dirs[id] = t.TempDir()
		wgPriv, wgPub := newKey(t)
		ctlPriv, ctlPub := newKey(t)
		l.keys[id] = [4]string{wgPriv, wgPub, ctlPriv, ctlPub}
		os.MkdirAll(filepath.Join(l.dirs[id], "keys"), 0o700)
		os.WriteFile(filepath.Join(l.dirs[id], "keys", "wg.key"), []byte(wgPriv), 0o600)
		os.WriteFile(filepath.Join(l.dirs[id], "keys", "control.key"), []byte(ctlPriv), 0o600)
		writeTestKeys(t, l.dirs[id], id)
	}
	return l
}

func (l *rtLab) link(a, b string) *rtLink {
	for _, k := range l.links {
		if (k.a == a && k.b == b) || (k.a == b && k.b == a) {
			return k
		}
	}
	l.t.Fatalf("no link %s-%s", a, b)
	return nil
}

// netem sets both ends of k to its delay plus extra arguments: "" restores
// the line, "loss 100%" cuts it. extraMs adds to the round trip.
func (l *rtLab) netem(k *rtLink, extra string) { l.netemDelay(k, 0, extra) }

func (l *rtLab) netemDelay(k *rtLink, extraMs float64, extra string) {
	args := fmt.Sprintf("delay %.3fms %s", (k.rttMs+extraMs)/2, extra)
	verb := "add"
	if k.current != "" {
		verb = "change"
	}
	for _, end := range [][2]string{{k.a, k.devA}, {k.b, k.devB}} {
		rtSh(l.t, append([]string{"ip", "netns", "exec", l.ns[end[0]], "tc", "qdisc", verb, "dev", end[1], "root", "netem", "limit", "10000"}, strings.Fields(args)...)...)
	}
	k.current = args
}

func (l *rtLab) config(id string) *Config {
	ctl := func(x string) netip.Addr { return netip.AddrFrom4([4]byte{100, 96, 255, l.addr[x].As4()[3]}) }
	c := &Config{Schema: ConfigSchema, Revision: 1, Network: Network{ID: "oblab-rt", Prefix: meshPrefix, ControlPrefix: controlPrefix, MTU: 1400},
		Self:  Self{ID: id, Name: id, Role: roleRelay, Address: l.addr[id], ControlAddress: ctl(id), Listen: Listen{UDP: 51820, TCP: 51821}},
		Rules: []Rule{{Proto: "icmp"}}, Links: []Link{}, NextHops: map[string]string{}, Routing: &Routing{Mode: "dynamic"}}
	for _, o := range l.ids {
		if o != id {
			c.Nodes = append(c.Nodes, Node{ID: o, Name: o, Role: roleRelay, Address: l.addr[o], ControlAddress: ctl(o), PublicKey: l.keys[o][1], ControlKey: l.keys[o][3],
				SignKey: testSignPub(o), E2EKey: testE2EPub(o)})
		}
	}
	for _, k := range l.links {
		switch id {
		case k.a:
			endpoint := "udp://" + k.addrB + ":51820"
			if k.tcp {
				endpoint = "tcp://" + k.addrB + ":51821"
			}
			c.Links = append(c.Links, Link{Peer: k.b, Kind: kindRelay, Dial: true, Endpoint: endpoint})
		case k.b:
			c.Links = append(c.Links, Link{Peer: k.a, Kind: kindRelay})
		}
	}
	for to, via := range l.hops[id] {
		c.NextHops[to] = via
	}
	for _, k := range l.links {
		c.RelayLinks = append(c.RelayLinks, [2]string{k.a, k.b})
	}
	return c
}

func (l *rtLab) start() {
	self, err := os.Executable()
	if err != nil {
		l.t.Fatal(err)
	}
	for _, id := range l.ids {
		raw, _ := json.Marshal(l.config(id))
		if err := Validate(raw); err != nil {
			l.t.Fatalf("%s: %v", id, err)
		}
		os.WriteFile(filepath.Join(l.dirs[id], "config.json"), raw, 0o600)
		log, _ := os.Create(filepath.Join(l.dirs[id], "mesh.log"))
		cmd := exec.Command("ip", "netns", "exec", l.ns[id], self, "-test.run", "^TestNetnsNode$", "-test.v")
		cmd.Env = append(os.Environ(), "OBLAB_MESH_DIR="+l.dirs[id])
		cmd.Stdout, cmd.Stderr = log, log
		if err := cmd.Start(); err != nil {
			l.t.Fatal(err)
		}
		l.procs[id] = cmd
	}
	l.t.Cleanup(func() {
		if l.t.Failed() {
			for _, id := range l.ids {
				s := l.status(id)
				raw, _ := json.Marshal(s.Routing)
				l.t.Logf("%s routing %s counters %+v events %+v", id, raw, s.Counters, s.Events)
			}
		}
	})
}

func (l *rtLab) stop() {
	for id, cmd := range l.procs {
		cmd.Process.Signal(os.Interrupt)
		done := make(chan error, 1)
		go func() { done <- cmd.Wait() }()
		select {
		case <-done:
		case <-time.After(10 * time.Second):
			cmd.Process.Kill()
			l.t.Errorf("%s did not stop on SIGINT", id)
		}
		delete(l.procs, id)
	}
}

func (l *rtLab) status(id string) Status {
	var s Status
	raw, _ := os.ReadFile(filepath.Join(l.dirs[id], "status.json"))
	json.Unmarshal(raw, &s)
	return s
}

func (l *rtLab) route(id, dest string) (RelayRoute, bool) {
	s := l.status(id)
	if s.Routing == nil {
		return RelayRoute{}, false
	}
	for _, r := range s.Routing.Routes {
		if r.Dest == dest {
			return r, true
		}
	}
	return RelayRoute{}, false
}

func (l *rtLab) waitFor(what string, within time.Duration, ok func() bool) {
	l.t.Helper()
	for deadline := time.Now().Add(within); !ok(); time.Sleep(250 * time.Millisecond) {
		if time.Now().After(deadline) {
			l.t.Fatalf("timed out waiting for %s", what)
		}
	}
}

// paths waits until every listed route has the path given, "H>S>T".
func (l *rtLab) paths(within time.Duration, want ...string) {
	l.t.Helper()
	l.waitFor("paths "+strings.Join(want, " "), within, func() bool {
		for _, w := range want {
			p := strings.Split(w, ">")
			r, ok := l.route(p[0], p[len(p)-1])
			if !ok || !slices.Equal(r.Path, p) {
				return false
			}
		}
		return true
	})
}

type pingSample struct {
	seq   int
	sent  time.Time
	ok    bool
	rtt   float64
	ttl   int
	class string
}

type pinger struct {
	cmd      *exec.Cmd
	mu       sync.Mutex
	replies  map[int]pingSample
	maxSeq   int
	interval time.Duration
	done     chan struct{}
}

var rtPingLine = regexp.MustCompile(`^\[(\d+\.\d+)\] \d+ bytes from [0-9.]+: icmp_seq=(\d+) ttl=(\d+) time=([0-9.]+) ms`)

// ping runs from src to dst every interval with the given IP TTL.
func (l *rtLab) ping(src, dst string, interval time.Duration, ttl int) *pinger {
	p := &pinger{replies: map[int]pingSample{}, interval: interval, done: make(chan struct{})}
	p.cmd = exec.Command("ip", "netns", "exec", l.ns[src], "ping", "-n", "-D", "-i", fmt.Sprintf("%.3f", interval.Seconds()), "-W", "1", "-t", strconv.Itoa(ttl), l.addr[dst].String())
	out, _ := p.cmd.StdoutPipe()
	if err := p.cmd.Start(); err != nil {
		l.t.Fatal(err)
	}
	go func() {
		defer close(p.done)
		sc := bufio.NewScanner(out)
		for sc.Scan() {
			m := rtPingLine.FindStringSubmatch(sc.Text())
			if m == nil {
				continue
			}
			ts, _ := strconv.ParseFloat(m[1], 64)
			seq, _ := strconv.Atoi(m[2])
			ttl, _ := strconv.Atoi(m[3])
			rtt, _ := strconv.ParseFloat(m[4], 64)
			at := time.Unix(0, int64(ts*1e9)).Add(-time.Duration(rtt * float64(time.Millisecond)))
			p.mu.Lock()
			p.replies[seq] = pingSample{seq: seq, sent: at, ok: true, rtt: rtt, ttl: ttl}
			p.maxSeq = max(p.maxSeq, seq)
			p.mu.Unlock()
		}
	}()
	l.t.Cleanup(p.stop)
	return p
}

func (p *pinger) stop() {
	if p.cmd.Process != nil && p.cmd.ProcessState == nil {
		p.cmd.Process.Signal(os.Interrupt)
		p.cmd.Wait()
		<-p.done
	}
}

// samples is every probe sent, lost ones with a send time between their
// neighbors', classified by classify.
func (p *pinger) samples(classify func(pingSample) string) []pingSample {
	p.stop()
	p.mu.Lock()
	defer p.mu.Unlock()
	var out []pingSample
	for seq := 1; seq <= p.maxSeq; seq++ {
		s, ok := p.replies[seq]
		if !ok {
			s = pingSample{seq: seq}
			for d := 1; d <= p.maxSeq; d++ {
				if r, ok := p.replies[seq-d]; ok {
					s.sent = r.sent.Add(time.Duration(d) * p.interval)
					break
				}
			}
		}
		if s.ok {
			s.class = classify(s)
		} else {
			s.class = "lost"
		}
		out = append(out, s)
	}
	return out
}

// pathRun sums up one flow over a fault from start to end: when traffic
// first left the usual path and first took the detour, the time lost, the
// path changes seen, and when it came back for good.
type pathRun struct {
	away, detour, back time.Duration // from the fault's start, start and end
	lost               time.Duration
	changes            int
	other              time.Duration // replies on neither path: mixed directions
	timeline           []string
}

func summarize(all []pingSample, usual, detour string, interval time.Duration, start, end time.Time) pathRun {
	r := pathRun{away: -1, detour: -1, back: -1}
	shown, path := "", ""
	for _, s := range all {
		if s.sent.Before(start.Add(-10 * time.Second)) {
			continue
		}
		switch s.class {
		case "lost":
			r.lost += interval
		case "other":
			r.other += interval
		default:
			// Changes between paths; lost and mixed replies in between do
			// not count.
			if path != "" && s.class != path {
				r.changes++
			}
			path = s.class
		}
		if !s.sent.Before(start) && r.away < 0 && s.class != usual {
			r.away = s.sent.Sub(start)
		}
		if !s.sent.Before(start) && r.detour < 0 && s.class == detour {
			r.detour = s.sent.Sub(start)
		}
		if s.class != shown {
			r.timeline = append(r.timeline, fmt.Sprintf("%+.1fs %s", s.sent.Sub(start).Seconds(), s.class))
			shown = s.class
		}
	}
	// Back for good: the first sample from which every later one is usual.
	for i := len(all) - 1; i >= 0; i-- {
		if all[i].class != usual {
			if i+1 < len(all) && all[i+1].sent.After(end) {
				r.back = all[i+1].sent.Sub(end)
			}
			break
		}
	}
	return r
}

func (r pathRun) String() string {
	d := func(v time.Duration) string {
		if v < 0 {
			return "-"
		}
		return fmt.Sprintf("%.1fs", v.Seconds())
	}
	return fmt.Sprintf("left the usual path %s after the fault, on the detour %s after, back %s after it ended; lost %s, mixed %s, %d path changes; %s",
		d(r.away), d(r.detour), d(r.back), d(r.lost), d(r.other), r.changes, strings.Join(r.timeline, ", "))
}

// TestRoutingNetnsFaults replays sim.py's three faults on the home-TOY link
// of its topology: H home, T TOY, S Singapore, L Los Angeles, with the
// round trips of the real data; H-T, the punched link there, is dialed.
func TestRoutingNetnsFaults(t *testing.T) {
	rtSkip(t)
	for _, c := range []struct {
		name  string
		fault func(l *rtLab, k *rtLink, start time.Time) time.Time // returns the fault's end
		after time.Duration                                        // watched after the fault
	}{
		{"outage", func(l *rtLab, k *rtLink, start time.Time) time.Time {
			l.netem(k, "loss 100%")
			time.Sleep(time.Until(start.Add(30 * time.Second)))
			l.netem(k, "")
			return time.Now()
		}, 45 * time.Second},
		{"flap", func(l *rtLab, k *rtLink, start time.Time) time.Time {
			for i := 0; i < 30; i += 4 {
				if (i/4)%2 == 0 {
					l.netem(k, "loss 100%")
				} else {
					l.netem(k, "")
				}
				time.Sleep(time.Until(start.Add(time.Duration(i+4) * time.Second)))
			}
			l.netem(k, "")
			return time.Now()
		}, 100 * time.Second},
		{"degrade", func(l *rtLab, k *rtLink, start time.Time) time.Time {
			// +40 ms ±15 and 30 % lost on the round trip, as sim.py.
			l.netemDelay(k, 40, "7.5ms loss 16%")
			time.Sleep(time.Until(start.Add(40 * time.Second)))
			l.netem(k, "")
			return time.Now()
		}, 60 * time.Second},
	} {
		t.Run(c.name, func(t *testing.T) {
			l := newRTLab(t, []string{"H", "T", "S", "L"},
				[]string{"H-T:2.5", "H-S:79.4", "H-L:202.7", "L-S:164", "T-S:74.9"},
				map[string]string{"H>T": "S", "H>S": "S", "H>L": "L", "T>H": "S", "T>S": "S", "T>L": "S", "S>H": "H", "S>T": "T", "S>L": "L", "L>H": "H", "L>S": "S", "L>T": "S"})
			l.start()
			l.paths(60*time.Second, "H>T", "T>H")
			time.Sleep(15 * time.Second) // past the warm-up, as sim.py's first 15 s
			before, _ := l.route("H", "T")
			forwardedS := l.status("S").Counters.Forwarded
			p := l.ping("H", "T", 100*time.Millisecond, 64)
			time.Sleep(10 * time.Second)
			start := time.Now()
			var mid []string
			var midMu sync.Mutex
			stopMid := make(chan struct{})
			go func() {
				// What H and T route by while the fault lasts.
				for {
					select {
					case <-stopMid:
						return
					case <-time.After(5 * time.Second):
					}
					h, _ := l.route("H", "T")
					x, _ := l.route("T", "H")
					midMu.Lock()
					mid = append(mid, fmt.Sprintf("%+.0fs H:%s T:%s", time.Since(start).Seconds(), strings.Join(h.Path, ">"), strings.Join(x.Path, ">")))
					midMu.Unlock()
				}
			}()
			end := c.fault(l, l.link("H", "T"), start)
			time.Sleep(c.after)
			close(stopMid)
			after, _ := l.route("H", "T")
			// The round trip tells the path: H and T talk end to end, so
			// S forwards the outer packet and the reply's TTL is 64 either way.
			all := p.samples(func(s pingSample) string {
				switch {
				case s.rtt < 30 && s.ttl == 64:
					return "direct"
				case s.rtt > 140 && s.rtt < 240 && s.ttl == 64:
					return "via-S"
				}
				return "other"
			})
			r := summarize(all, "direct", "via-S", 100*time.Millisecond, start, end)
			switches := after.Switches - before.Switches
			midMu.Lock()
			t.Logf("%s: H's route to T switched %d times; ping H>T: %s", c.name, switches, r)
			t.Logf("%s: routes during the fault: %s", c.name, strings.Join(mid, "; "))
			midMu.Unlock()
			if fs := l.status("S").Counters.Forwarded - forwardedS; r.detour >= 0 && fs == 0 {
				t.Errorf("S forwarded nothing while traffic took the detour")
			}
			for _, id := range l.ids {
				if n := l.status(id).Counters.DroppedTTL; n != 0 {
					t.Errorf("%s dropped %d packets for their TTL", id, n)
				}
			}
			if !slices.Equal(after.Path, []string{"H", "T"}) || r.back < 0 {
				t.Fatalf("not back on the direct path: %v", after.Path)
			}
			switch c.name {
			case "outage":
				// result.txt: 2 switches, 3 s lost, away at +3 s, back at +13 s.
				if switches != 2 || r.detour < 0 || r.detour > 5*time.Second || r.lost > 5*time.Second || r.back > 30*time.Second {
					t.Fatalf("outage: %d switches, %s", switches, r)
				}
			case "flap":
				// result.txt: 2 switches, 3 s lost, back at +63 s.
				if switches > 2 {
					t.Fatalf("flap: %d switches, %s", switches, r)
				}
			case "degrade":
				// result.txt: 2 switches, 4 s lost, away at +11 s, back at +8 s.
				if switches > 2 || r.detour < 0 || r.detour > 40*time.Second {
					t.Fatalf("degrade: %d switches, %s", switches, r)
				}
			}
		})
	}
}

// TestRoutingNetnsNoLoops runs four relays in a ring with one diagonal,
// every link with its own round trip so that a ping's round trip tells its
// path. Links fail, flap and return while pings cross the mesh in both
// diagonals: they go end to end, in an outer packet with a TTL of 64 that
// each relay lowers, and a forwarding loop would drop them for it.
func TestRoutingNetnsNoLoops(t *testing.T) {
	rtSkip(t)
	l := newRTLab(t, []string{"A", "B", "C", "D"},
		[]string{"A-B:4", "B-C:8", "C-D:16", "A-D:32", "A-C:100"},
		map[string]string{"A>B": "B", "A>C": "B", "A>D": "D", "B>A": "A", "B>C": "C", "B>D": "C", "C>A": "B", "C>B": "B", "C>D": "D", "D>A": "A", "D>B": "C", "D>C": "C"})
	l.start()
	l.paths(60*time.Second, "A>B>C", "C>B>A", "B>C>D", "D>C>B")
	rtt := map[string]float64{}
	for _, k := range l.links {
		rtt[k.a+k.b], rtt[k.b+k.a] = k.rttMs, k.rttMs
	}
	// Every simple path between two relays, with its one-way time.
	var simple func(path []string, dst string, out *[][]string)
	simple = func(path []string, dst string, out *[][]string) {
		x := path[len(path)-1]
		if x == dst {
			*out = append(*out, slices.Clone(path))
			return
		}
		for _, y := range l.ids {
			if _, ok := rtt[x+y]; ok && !slices.Contains(path, y) {
				simple(append(path, y), dst, out)
			}
		}
	}
	oneWay := func(p []string) float64 {
		v := 0.0
		for i := 0; i+1 < len(p); i++ {
			v += rtt[p[i]+p[i+1]] / 2
		}
		return v
	}
	flows := [][2]string{{"A", "C"}, {"C", "A"}, {"B", "D"}, {"D", "B"}}
	pingers := map[[2]string]*pinger{}
	for _, f := range flows {
		pingers[f] = l.ping(f[0], f[1], 50*time.Millisecond, 6)
	}
	start := time.Now()
	phase := func(what string, d time.Duration) {
		t.Logf("%+.0fs %s", time.Since(start).Seconds(), what)
		time.Sleep(d)
	}
	phase("all links up", 10*time.Second)
	l.netem(l.link("B", "C"), "loss 100%")
	phase("B-C cut", 20*time.Second)
	l.netem(l.link("B", "C"), "")
	phase("B-C back", 40*time.Second)
	for i := 0; i < 8; i++ {
		if i%2 == 0 {
			l.netem(l.link("C", "D"), "loss 100%")
		} else {
			l.netem(l.link("C", "D"), "")
		}
		phase(fmt.Sprintf("C-D flapping (%d)", i), 3*time.Second)
	}
	l.netem(l.link("C", "D"), "")
	phase("C-D back", 0)
	// The flapping suppressed C-D; until it is reused (penalty below 750,
	// about a minute) A would have no path through D.
	linkUp := func(a, b string) bool {
		s := l.status(a)
		if s.Routing == nil {
			return false
		}
		for _, k := range s.Routing.Links {
			if k.Peer == b {
				return k.State == "up"
			}
		}
		return false
	}
	l.waitFor("C-D reused", 120*time.Second, func() bool { return linkUp("C", "D") && linkUp("D", "C") })
	phase("C-D reused", 10*time.Second)
	l.netem(l.link("A", "B"), "loss 100%")
	l.netem(l.link("A", "C"), "loss 100%")
	phase("A-B and A-C cut: A only through D", 0)
	l.paths(10*time.Second, "A>D>C", "C>D>A", "B>C>D>A")
	phase("A routed through D", 15*time.Second)
	l.netem(l.link("A", "B"), "")
	l.netem(l.link("A", "C"), "")
	phase("A-B and A-C back", 60*time.Second)
	l.paths(30*time.Second, "A>B>C", "C>B>A", "B>C>D", "D>C>B")

	for _, f := range flows {
		var paths [][]string
		simple([]string{f[0]}, f[1], &paths)
		all := pingers[f].samples(func(s pingSample) string {
			// The forward and return paths whose times add up to the round
			// trip. End to end, relays lower only the outer packet's TTL:
			// the reply's says nothing of the return path.
			best, bestErr := "other", math.Inf(1)
			for _, there := range paths {
				for _, back := range paths {
					e := math.Abs(oneWay(there) + oneWay(back) - s.rtt)
					if e < bestErr && e < 3+0.1*s.rtt {
						back := slices.Clone(back)
						slices.Reverse(back)
						best, bestErr = strings.Join(there, ">"), e
						if !slices.Equal(there, back) {
							best = strings.Join(there, ">") + " back " + strings.Join(back, ">")
						}
					}
				}
			}
			return best
		})
		count := map[string]int{}
		for _, s := range all {
			count[s.class]++
		}
		var keys []string
		for k := range count {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		var parts []string
		for _, k := range keys {
			parts = append(parts, fmt.Sprintf("%s %d", k, count[k]))
		}
		r := summarize(all, "", "", 50*time.Millisecond, start, start)
		t.Logf("%s>%s: %d pings: %s; %s", f[0], f[1], len(all), strings.Join(parts, ", "), strings.Join(r.timeline, ", "))
	}
	for _, id := range l.ids {
		s := l.status(id)
		if s.Counters.DroppedTTL != 0 {
			t.Errorf("%s dropped %d packets for their TTL", id, s.Counters.DroppedTTL)
		}
		var changes []string
		for _, e := range s.Events {
			if e.Kind == "route.changed" || e.Kind == "link.suppressed" || e.Kind == "link.reused" {
				changes = append(changes, fmt.Sprintf("%+.1fs %s %s", time.UnixMilli(e.At).Sub(start).Seconds(), e.Kind, e.Detail))
			}
		}
		t.Logf("%s: dropped for TTL %d, no route %d; %s", id, s.Counters.DroppedTTL, s.Counters.DroppedNoRoute, strings.Join(changes, "; "))
	}
}

// TestRoutingNetnsTCPNoise is the flapping seen on the relays' long TCP
// links: T (TOY) and H (home) 2.5 ms apart, B (Berlin) 159 ms from T and
// 175 ms from H, both over TCP, both losing about 1 % of packets each way.
// TCP retransmits the lost segments, and probes behind them come late or
// not in time. For OBLAB_NOISE_MINUTES (default 10) no relay's route may
// move more than twice (away and back once); before round trips were
// taken as medians and routes damped, T's and B's to each other went back
// and forth 26 times in six minutes. Then T-B is cut for 30 s and T must
// still detour within 5 s and come back once it is restored.
func TestRoutingNetnsTCPNoise(t *testing.T) {
	rtSkip(t)
	minutes := 10.0
	if v, err := strconv.ParseFloat(os.Getenv("OBLAB_NOISE_MINUTES"), 64); err == nil && v > 0 {
		minutes = v
	}
	l := newRTLab(t, []string{"T", "H", "B"}, []string{"T-H:2.5", "T-B:159:tcp", "H-B:175:tcp"},
		map[string]string{"T>H": "H", "T>B": "B", "H>T": "T", "H>B": "B", "B>T": "T", "B>H": "H"})
	l.start()
	l.paths(60*time.Second, "T>B", "B>T", "T>H", "H>T", "H>B", "B>H")
	time.Sleep(15 * time.Second)
	for _, k := range []*rtLink{l.link("T", "B"), l.link("H", "B")} {
		l.netem(k, "loss 1%")
	}
	start := time.Now()
	switches := func() map[string]int {
		n := map[string]int{}
		for _, id := range l.ids {
			if s := l.status(id); s.Routing != nil {
				for _, r := range s.Routing.Routes {
					n[id+">"+r.Dest] = r.Switches
				}
			}
		}
		return n
	}
	before := switches()
	// Each relay's links and routes every second, to show what moved them.
	snap := map[string][]string{}
	var peakCost, peakJitter, peakLoss float64
	for deadline := start.Add(time.Duration(minutes * float64(time.Minute))); time.Now().Before(deadline); time.Sleep(time.Second) {
		for _, id := range l.ids {
			s := l.status(id)
			if s.Routing == nil {
				continue
			}
			var parts []string
			for _, k := range s.Routing.Links {
				c := -1.0
				if k.Cost != nil {
					c = *k.Cost
					if id == "B" || k.Peer == "B" { // the TCP links
						peakCost, peakJitter, peakLoss = max(peakCost, c), max(peakJitter, k.JitterMs), max(peakLoss, k.Loss)
					}
				}
				parts = append(parts, fmt.Sprintf("%s %s rtt %.1f j %.1f loss %.3f cost %.1f", k.Peer, k.State, k.RTTMs, k.JitterMs, k.Loss, c))
			}
			for _, r := range s.Routing.Routes {
				parts = append(parts, fmt.Sprintf("%s via %s", r.Dest, strings.Join(r.Path, ">")))
			}
			snap[id] = append(snap[id], fmt.Sprintf("%+.0fs %s", time.Since(start).Seconds(), strings.Join(parts, "; ")))
		}
	}
	moved, most := 0, 0
	for k, v := range switches() {
		moved += v - before[k]
		most = max(most, v-before[k])
	}
	var changes []string
	for _, id := range l.ids {
		for _, e := range l.status(id).Events {
			if e.Kind == "route.changed" && time.UnixMilli(e.At).After(start) {
				changes = append(changes, fmt.Sprintf("%s %+.0fs %s", id, time.UnixMilli(e.At).Sub(start).Seconds(), e.Detail))
			}
		}
	}
	t.Logf("%.0f min of 1%% loss on the TCP links: routes moved %d times; the TCP links' measured cost peaked at %.1f, jitter at %.1f, loss at %.3f; %s",
		minutes, moved, peakCost, peakJitter, peakLoss, strings.Join(changes, "; "))
	if most > 2 {
		for _, id := range l.ids {
			t.Logf("%s:\n%s", id, strings.Join(snap[id], "\n"))
		}
		t.Errorf("a route moved %d times on noise alone", most)
	}

	// An outage still detours at once and returns.
	k := l.link("T", "B")
	cut := time.Now()
	l.netem(k, "loss 100%")
	l.paths(10*time.Second, "T>H>B")
	t.Logf("T detoured %.1fs after T-B was cut", time.Since(cut).Seconds())
	if d := time.Since(cut); d > 5*time.Second {
		t.Errorf("detour took %v", d)
	}
	time.Sleep(time.Until(cut.Add(30 * time.Second)))
	l.netem(k, "loss 1%")
	restored := time.Now()
	l.paths(60*time.Second, "T>B", "B>T")
	t.Logf("T back on T-B %.1fs after it was restored", time.Since(restored).Seconds())
}
