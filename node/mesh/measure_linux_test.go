package mesh

import (
	"encoding/json"
	"net/netip"
	"os"
	"strings"
	"testing"
	"time"
)

// TestCandidateMeasurements needs root; OBLAB_MEASURE=1 runs it. Two
// public relays, P (20 ms away) and Q (40 ms), Q dialing P; a home behind
// the NAT N1 with a site relay S1 dialing P and the member M1; the member
// M2 behind N2. M1's entry is S1 punched, then P over TCP (the hot
// standby), then Q over TCP (cold); M2's P over UDP, then Q over TCP. M1
// and M2 punch a direct link.
//
// Each entry candidate shows what its probes measured, the cold one what
// its checks did, without probes of its own between them; every path in
// use shows both of its ends as addresses.
func TestCandidateMeasurements(t *testing.T) {
	if os.Getenv("OBLAB_MEASURE") != "1" || os.Getuid() != 0 {
		t.Skip("set OBLAB_MEASURE=1 and run as root")
	}
	l := newNSLab(t, "cm", "W", "P", "Q", "N1", "N2", "S1", "M1", "M2")
	l.router("W")
	l.veth("W", "wp", "10.212.0.1/24", "P", "p", "10.212.0.2/24")
	l.route("P", "10.212.0.1")
	l.veth("W", "wq", "10.212.5.1/24", "Q", "q", "10.212.5.2/24")
	l.route("Q", "10.212.5.1")
	for _, x := range [][3]string{{"W", "wp", "10ms"}, {"P", "p", "10ms"}, {"W", "wq", "20ms"}, {"Q", "q", "20ms"}} {
		l.in(x[0], "tc", "qdisc", "add", "dev", l.ifname(x[1]), "root", "netem", "delay", x[2])
	}
	for _, k := range []string{"1", "2"} {
		n := "N" + k
		l.veth("W", "w"+k, "10.212."+k+".1/24", n, "n"+k, "10.212."+k+".2/24")
		l.route(n, "10.212."+k+".1")
		l.router(n)
		l.in(n, "iptables", "-t", "nat", "-A", "POSTROUTING", "-o", l.ifname("n"+k), "-j", "MASQUERADE")
		l.in("W", "iptables", "-t", "mangle", "-A", "PREROUTING", "-i", l.ifname("w"+k), "-j", "TTL", "--ttl-dec", "5")
		l.bridge(n, "l"+k, "192.168."+k+".1/24")
	}
	l.onLAN("N1", "l1", "S1", "192.168.1.2/24", "192.168.1.1")
	l.onLAN("N1", "l1", "M1", "192.168.1.3/24", "192.168.1.1")
	l.onLAN("N2", "l2", "M2", "192.168.2.3/24", "192.168.2.1")
	l.node("P", roleRelay, 1)
	l.node("S1", roleRelay, 2)
	l.node("Q", roleRelay, 4)
	l.node("M1", roleMember, 11, "S1", "P", "Q")
	l.node("M2", roleMember, 12, "P", "Q")

	pUDP, pTCP, qTCP := udpEndpoint("10.212.0.2", 51820), "tcp://10.212.0.2:52000", "tcp://10.212.5.2:52000"
	static := &Routing{Mode: "static"}
	reflect := []string{"10.212.0.2:51820"}
	p := l.config("P", Listen{UDP: 51820, TCP: 52000})
	p.Links = []Link{{Peer: "S1", Kind: kindRelay}, {Peer: "Q", Kind: kindRelay}}
	p.NextHops = map[string]string{"S1": "S1", "Q": "Q"}
	p.Routing = static
	q := l.config("Q", Listen{UDP: 51820, TCP: 52000})
	q.Links = []Link{{Peer: "P", Kind: kindRelay, Dial: true, Endpoint: pUDP}}
	q.NextHops = map[string]string{"P": "P", "S1": "P"}
	q.Routing = static
	s1 := l.config("S1", Listen{UDP: 51820})
	s1.Links = []Link{{Peer: "P", Kind: kindRelay, Dial: true, Endpoint: pUDP}}
	s1.NextHops = map[string]string{"P": "P", "Q": "P"}
	s1.Reflectors = reflect
	s1.Routing = static
	m1 := l.config("M1", Listen{UDP: 51820})
	// The punched c1 needs another candidate's relay to signal through:
	// M1 enters through P first and returns to c1.
	m1.Health = &Health{ColdRetryMs: 10000, ReturnHealthyMs: 10000, MinResidenceMs: 10000}
	m1.Entry = &Entry{Mode: "failover", AutoReturn: true, Candidates: []Candidate{{ID: "c1", Relay: "S1", Punch: true}, {ID: "c2", Relay: "P", Endpoint: pTCP}, {ID: "c3", Relay: "Q", Endpoint: qTCP}}}
	m1.Links = []Link{{Peer: "M2", Kind: kindDirect, Punch: true}}
	m1.Reflectors = reflect
	m2 := l.config("M2", Listen{UDP: 51820})
	m2.Entry = &Entry{Mode: "failover", Candidates: []Candidate{{ID: "c1", Relay: "P", Endpoint: pUDP}, {ID: "c2", Relay: "Q", Endpoint: qTCP}}}
	m2.Links = []Link{{Peer: "M1", Kind: kindDirect, Punch: true}}
	m2.Reflectors = reflect
	l.run(map[string]*Config{"P": p, "Q": q, "S1": s1, "M1": m1, "M2": m2})

	settled := func() bool {
		a, b := l.status("M1").Entry, l.status("M2").Entry
		d1, d2 := l.peer("M1", "M2").Direct, l.peer("M2", "M1").Direct
		return a != nil && a.Candidate == "c1" && b != nil && b.Candidate == "c1" && d1 != nil && d1.State == "direct" && d2 != nil && d2.State == "direct"
	}
	if took := waitFor(120*time.Second, settled); took < 0 {
		for _, id := range []string{"M1", "M2"} {
			s := l.status(id)
			raw, _ := json.Marshal(map[string]any{"entry": s.Entry, "peers": s.Peers, "reach": s.Reachability, "events": s.Events})
			t.Logf("%s: %s", id, raw)
		}
		t.Fatal("M1 on S1, M2 on P, their link direct: not within 120 s")
	}

	// The cold candidate over two minutes: what each 10 s check (±20 %)
	// measured; flat in between.
	type sample struct {
		at time.Time
		c  CandidateHealth
	}
	var seen []sample
	for end := time.Now().Add(2 * time.Minute); time.Now().Before(end); time.Sleep(250 * time.Millisecond) {
		seen = append(seen, sample{time.Now(), l.candidate("M1", "c3")})
	}
	checks, flat := 0, time.Duration(0)
	var last sample
	for i, s := range seen {
		if i == 0 {
			last = s
			continue
		}
		if s.c.Probes < last.c.Probes || s.c.Timeouts < last.c.Timeouts {
			t.Fatalf("c3's counts went down: %+v, then %+v", last.c, s.c)
		}
		if s.c.Probes > last.c.Probes {
			if s.c.CheckedAt <= last.c.CheckedAt {
				t.Fatalf("c3 probed, checkedAt did not move: %+v, then %+v", last.c, s.c)
			}
			if gap := s.at.Sub(last.at); gap > 5*time.Second {
				checks++
				flat = max(flat, gap)
			}
			last = s
		} else if s.c.CheckedAt != last.c.CheckedAt {
			t.Fatalf("c3's checkedAt moved without a probe: %+v, then %+v", last.c, s.c)
		}
	}
	first, end := seen[0].c, seen[len(seen)-1].c
	perCheck := float64(end.Probes-first.Probes) / float64(max(1, checks))
	t.Logf("c3 over %v: %d checks, %d probes (%.1f a check), %d timeouts, longest without a probe %v; now %+v",
		seen[len(seen)-1].at.Sub(seen[0].at).Round(time.Second), checks, end.Probes-first.Probes, perCheck, end.Timeouts-first.Timeouts, flat.Round(time.Second), end)
	if checks < 4 || perCheck > 15 {
		t.Errorf("c3: %d checks of %.1f probes in two minutes, want one of at most 15 each 10 s", checks, perCheck)
	}

	// Round trips: S1 on the LAN, P 20 ms and Q 40 ms away.
	want := map[string][2]float64{"M1/c1": {0, 5}, "M1/c2": {18, 35}, "M1/c3": {38, 60}, "M2/c1": {18, 35}, "M2/c2": {38, 60}}
	for _, id := range []string{"M1", "M2"} {
		raw, _ := json.Marshal(l.status(id).Entry.Candidates)
		t.Logf("%s entry.candidates %s", id, raw)
		for _, c := range l.status(id).Entry.Candidates {
			w := want[id+"/"+c.ID]
			if c.RTTMs == nil || *c.RTTMs < w[0] || *c.RTTMs > w[1] {
				t.Errorf("%s %s: rttMs %v, want %v to %v ms", id, c.ID, c.RTTMs, w[0], w[1])
			}
			if c.LossRate == nil || *c.LossRate > .1 || c.CheckedAt == 0 || c.Probes == 0 {
				t.Errorf("%s %s: %+v", id, c.ID, c)
			}
		}
	}

	// Both ends of every path in use, as addresses.
	ends := map[string][2]string{
		"M1/S1": {"192.168.1.3:51820", "192.168.1.2:51820"}, // the punched entry, on the LAN
		"M1/M2": {"192.168.1.3:51820", "10.212.2.2:"},       // the punched direct link, to M2's NAT
		"M2/M1": {"192.168.2.3:51820", "10.212.1.2:"},
		"M2/P":  {"192.168.2.3:51820", "10.212.0.2:51820"}, // a UDP entry
		"S1/P":  {"192.168.1.2:51820", "10.212.0.2:51820"}, // a dialed relay link
		"P/S1":  {"10.212.0.2:51820", "10.212.1.2:"},       // and its other end
		"Q/P":   {"10.212.5.2:51820", "10.212.0.2:51820"},
		"P/M2":  {"10.212.0.2:51820", "10.212.2.2:"}, // a member's entry, at the relay
	}
	for _, id := range []string{"P", "Q", "S1", "M1", "M2"} {
		for _, ps := range l.status(id).Peers {
			t.Logf("%s -> %s %s active=%v transport=%s endpoint=%q localAddr=%q remoteAddr=%q", id, ps.ID, ps.Kind, ps.Active, ps.Transport, ps.Endpoint, ps.LocalAddr, ps.RemoteAddr)
			if ps.Transport == "" {
				if ps.Endpoint != "" || ps.LocalAddr != "" || ps.RemoteAddr != "" {
					t.Errorf("%s -> %s: addresses without a path in use", id, ps.ID)
				}
				continue
			}
			for _, a := range []string{ps.LocalAddr, ps.RemoteAddr} {
				if ap, err := netip.ParseAddrPort(a); err != nil || ap.Addr().IsUnspecified() {
					t.Errorf("%s -> %s: %q is not an address", id, ps.ID, a)
				}
			}
			if e, ok := ends[id+"/"+ps.ID]; ok && (ps.LocalAddr != e[0] || !strings.HasPrefix(ps.RemoteAddr, e[1])) {
				t.Errorf("%s -> %s: %s -> %s, want %s -> %s…", id, ps.ID, ps.LocalAddr, ps.RemoteAddr, e[0], e[1])
			}
			delete(ends, id+"/"+ps.ID)
		}
	}
	for k := range ends {
		t.Errorf("%s: no path in use", k)
	}
}

func waitFor(within time.Duration, ok func() bool) time.Duration {
	start := time.Now()
	for !ok() {
		if time.Since(start) > within {
			return -1
		}
		time.Sleep(100 * time.Millisecond)
	}
	return time.Since(start)
}
