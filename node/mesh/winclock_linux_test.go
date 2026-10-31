package mesh

import (
	"encoding/json"
	"os"
	"testing"
	"time"
)

// TestWindowsClockNetns needs root; OBLAB_WINCLOCK=1 runs it. The office
// LAN 10.20.20.0/24 behind the router R: the site relay T and the members
// W and X, each 2.9 ms from the LAN (a round trip of 5.8 ms between two);
// the remote relay S, 19 ms from R. W runs on a clock as Windows' (time.Now
// in 15.625 ms steps), the others on their own. W enters through T over
// UDP, S over TCP its standby; W and X punch a direct link.
//
// Each of W's round trips is what the network takes, not 0 or a step.
func TestWindowsClockNetns(t *testing.T) {
	if os.Getenv("OBLAB_WINCLOCK") != "1" || os.Getuid() != 0 {
		t.Skip("set OBLAB_WINCLOCK=1 and run as root")
	}
	l := newNSLab(t, "wc", "R", "T", "W", "X", "S")
	l.router("R")
	l.bridge("R", "lan", "10.20.20.1/24")
	l.onLAN("R", "lan", "T", "10.20.20.7/24", "10.20.20.1")
	l.onLAN("R", "lan", "W", "10.20.20.3/24", "10.20.20.1")
	l.onLAN("R", "lan", "X", "10.20.20.253/24", "10.20.20.1")
	l.veth("R", "rs", "10.212.9.1/24", "S", "s", "10.212.9.2/24")
	l.route("S", "10.212.9.1")
	for _, x := range [][3]string{{"T", "eth", "2900us"}, {"W", "eth", "2900us"}, {"X", "eth", "2900us"}, {"R", "rs", "19ms"}, {"S", "s", "19ms"}} {
		l.in(x[0], "tc", "qdisc", "add", "dev", l.ifname(x[1]), "root", "netem", "delay", x[2])
	}
	l.node("T", roleRelay, 1)
	l.node("S", roleRelay, 2)
	l.node("W", roleMember, 11, "T", "S")
	l.node("X", roleMember, 12, "T", "S")

	tUDP, sUDP, sTCP := udpEndpoint("10.20.20.7", 51998), udpEndpoint("10.212.9.2", 51820), "tcp://10.212.9.2:52000"
	reflect := []string{"10.20.20.7:51998"}
	tc := l.config("T", Listen{UDP: 51998})
	tc.Links = []Link{{Peer: "S", Kind: kindRelay, Dial: true, Endpoint: sUDP}}
	sc := l.config("S", Listen{UDP: 51820, TCP: 52000})
	sc.Links = []Link{{Peer: "T", Kind: kindRelay}}
	member := func(id, peer string) *Config {
		c := l.config(id, Listen{UDP: 51999})
		c.Entry = &Entry{Mode: "failover", Candidates: []Candidate{{ID: "c1", Relay: "T", Endpoint: tUDP}, {ID: "c2", Relay: "S", Endpoint: sTCP}}}
		c.Links = []Link{{Peer: peer, Kind: kindDirect, Punch: true}}
		c.Reflectors = reflect
		return c
	}
	l.run(map[string]*Config{"T": tc, "S": sc, "X": member("X", "W")})
	l.env = []string{"OBLAB_CLOCK=windows"}
	l.run(map[string]*Config{"W": member("W", "X")})

	l.wait("W on T with S ready, its link with X direct", 90*time.Second, func() bool {
		e := l.status("W").Entry
		d := l.peer("W", "X").Direct
		return e != nil && e.Candidate == "c1" && len(e.Candidates) == 2 && e.Candidates[1].State == "ready" && d != nil && d.State == "direct"
	})
	time.Sleep(20 * time.Second) // a window of probes on each path

	within := func(v, lo, hi float64) bool { return v >= lo && v <= hi }
	st := l.status("W")
	raw, _ := json.Marshal(st.Entry.Candidates)
	t.Logf("W entry.candidates %s", raw)
	want := map[string][2]float64{"c1": {5, 8}, "c2": {40, 46}}
	for _, c := range st.Entry.Candidates {
		w := want[c.ID]
		if c.RTTMs == nil || !within(*c.RTTMs, w[0], w[1]) || c.Timeouts != 0 {
			t.Errorf("W %s: rttMs %v, want %v to %v ms; %d of %d probes timed out", c.ID, deref(c.RTTMs), w[0], w[1], c.Timeouts, c.Probes)
		}
	}
	for id, w := range map[string][2]float64{"T": {5, 8}, "X": {5, 8}} {
		p := l.peer("W", id)
		t.Logf("W -> %s %s rttMs=%v %s -> %s", id, p.Kind, p.RTTMs, p.LocalAddr, p.RemoteAddr)
		if !within(p.RTTMs, w[0], w[1]) {
			t.Errorf("W -> %s: rttMs %v, want %v to %v ms", id, p.RTTMs, w[0], w[1])
		}
	}
	// The same paths from X, on its own clock.
	for id := range map[string]bool{"T": true, "W": true} {
		t.Logf("X -> %s rttMs=%v", id, l.peer("X", id).RTTMs)
	}
}

func deref(p *float64) any {
	if p == nil {
		return nil
	}
	return *p
}
