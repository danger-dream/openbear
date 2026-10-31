package mesh

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"testing"
	"time"
)

// TestSoak needs root; OBLAB_SOAK=1 runs it, OBLAB_SOAK_CYCLES cycles (20
// by default). Relays SG and LAX are public; HOME is a member behind the
// NAT N1 entering SG over UDP with LAX over TCP as the other candidate and
// automatic return; OFFICE is a member behind N2 entering LAX. Each cycle
// cuts HOME off SG until it moves to LAX and lets it return, restarts LAX,
// and gives N1 a new public address, as a PPPoE redial does, a different
// one every time. Goroutines,
// heap and open files of the processes that run throughout have to stay
// where they were after the first cycles.
func TestSoak(t *testing.T) {
	if os.Getenv("OBLAB_SOAK") != "1" || os.Getuid() != 0 {
		t.Skip("set OBLAB_SOAK=1 and run as root")
	}
	cycles := 20
	if n, err := strconv.Atoi(os.Getenv("OBLAB_SOAK_CYCLES")); err == nil && n > 0 {
		cycles = n
	}
	l := newNSLab(t, "sk", "W", "SG", "LAX", "N1", "N2", "HOME", "OFFICE")
	l.router("W")
	l.veth("W", "ws", "10.210.0.1/24", "SG", "s", "10.210.0.2/24")
	l.route("SG", "10.210.0.1")
	l.veth("W", "wl", "10.210.3.1/24", "LAX", "l", "10.210.3.2/24")
	l.route("LAX", "10.210.3.1")
	for i, n := range []string{"N1", "N2"} {
		k := strconv.Itoa(i + 1)
		l.veth("W", "w"+k, "10.210."+k+"0.1/24", n, "n"+k, "10.210."+k+"0.2/24")
		l.route(n, "10.210."+k+"0.1")
		l.router(n)
		l.in(n, "iptables", "-t", "nat", "-A", "POSTROUTING", "-o", l.ifname("n"+k), "-j", "MASQUERADE")
		l.bridge(n, "b"+k, "192.168."+k+"0.1/24")
	}
	l.onLAN("N1", "b1", "HOME", "192.168.10.2/24", "192.168.10.1")
	l.onLAN("N2", "b2", "OFFICE", "192.168.20.2/24", "192.168.20.1")
	l.node("SG", roleRelay, 1)
	l.node("LAX", roleRelay, 2)
	l.node("HOME", roleMember, 11, "SG", "LAX")
	l.node("OFFICE", roleMember, 12, "LAX")
	fast := &Health{ActiveIntervalMs: 250, StandbyIntervalMs: 250, ColdRetryMs: 3000, ReturnHealthyMs: 5000, MinResidenceMs: 5000}
	sg := l.config("SG", Listen{UDP: 51820, TCP: 52000})
	sg.Links = []Link{{Peer: "LAX", Kind: kindRelay}}
	sg.NextHops = map[string]string{"LAX": "LAX"}
	lax := l.config("LAX", Listen{UDP: 51820, TCP: 52000})
	lax.Links = []Link{{Peer: "SG", Kind: kindRelay, Dial: true, Endpoint: "udp://10.210.0.2:51820"}}
	lax.NextHops = map[string]string{"SG": "SG"}
	home := l.config("HOME", Listen{UDP: 51820})
	home.Entry = &Entry{Mode: "failover", AutoReturn: true, Candidates: []Candidate{{ID: "c1", Relay: "SG", Endpoint: "udp://10.210.0.2:51820"}, {ID: "c2", Relay: "LAX", Endpoint: "tcp://10.210.3.2:52000"}}}
	office := l.config("OFFICE", Listen{UDP: 51820})
	office.Entry = &Entry{Mode: "fixed", Candidates: []Candidate{{ID: "c1", Relay: "LAX", Endpoint: "udp://10.210.3.2:51820"}}}
	configs := map[string]*Config{"SG": sg, "LAX": lax, "HOME": home, "OFFICE": office}
	for _, c := range configs {
		c.Health = fast
		if c.Self.Role == roleRelay {
			c.Routing = &Routing{Mode: "static"}
		}
	}
	l.run(configs)
	self, _ := os.Executable()

	reach := func(from, to string) func() bool {
		return func() bool { _, rtt, _ := l.pings(from, to, 1); return rtt > 0 }
	}
	on := func(cand string) func() bool {
		return func() bool { e := l.status("HOME").Entry; return e != nil && e.Candidate == cand }
	}
	l.wait("HOME on SG", 60*time.Second, on("c1"))
	l.wait("HOME reaching OFFICE", 60*time.Second, reach("HOME", "OFFICE"))

	type sample struct {
		goroutines, files int
		heap, rss         uint64
	}
	measure := func(id string) sample {
		s := l.status(id)
		files, _ := os.ReadDir(fmt.Sprintf("/proc/%d/fd", s.PID))
		return sample{s.Resources.Goroutines, len(files), s.Resources.HeapBytes, s.Resources.RSSBytes}
	}
	watched := []string{"SG", "HOME", "OFFICE"}
	var first map[string]sample
	block := []string{"FORWARD", "-i", l.ifname("w1"), "-d", "10.210.0.2", "-j", "DROP"}
	for i := 1; i <= cycles; i++ {
		start := time.Now()
		l.in("W", append([]string{"iptables", "-I"}, block...)...)
		l.wait("HOME moving to LAX", 60*time.Second, on("c2"))
		l.wait("HOME reaching OFFICE through LAX", 30*time.Second, reach("HOME", "OFFICE"))
		l.in("W", append([]string{"iptables", "-D"}, block...)...)
		l.wait("HOME back on SG", 90*time.Second, on("c1"))

		l.stopNode("LAX")
		l.startNode("LAX", self)
		l.wait("OFFICE reaching HOME after LAX restarted", 60*time.Second, reach("OFFICE", "HOME"))

		next, old := fmt.Sprintf("10.210.10.%d/24", 10+i), fmt.Sprintf("10.210.10.%d/24", 9+i)
		if i == 1 {
			old = "10.210.10.2/24"
		}
		l.in("N1", "ip", "addr", "add", next, "dev", l.ifname("n1"))
		l.in("N1", "ip", "addr", "del", old, "dev", l.ifname("n1"))
		l.in("N1", "ip", "route", "replace", "default", "via", "10.210.10.1")
		l.in("N1", "conntrack", "-F")
		l.wait("HOME reaching OFFICE from a new address", 90*time.Second, reach("HOME", "OFFICE"))

		now := map[string]sample{}
		for _, id := range watched {
			now[id] = measure(id)
		}
		t.Logf("cycle %d (%v): %+v; HOME switches %d", i, time.Since(start).Round(time.Second), now, l.status("HOME").Entry.Switches)
		if i == 2 {
			first = now
		}
	}
	if first == nil {
		return
	}
	for _, id := range watched {
		a, b := first[id], measure(id)
		if b.goroutines > a.goroutines+a.goroutines/5+5 || b.files > a.files+3 || b.heap > a.heap+8<<20 {
			t.Errorf("%s grew from %+v after cycle 2 to %+v after cycle %d", id, a, b, cycles)
		}
	}
	for _, id := range watched {
		raw, _ := os.ReadFile(filepath.Join(l.dirs[id], "mesh.log"))
		t.Logf("%s log, last part:\n%s", id, tail(raw, 1500))
	}
}
