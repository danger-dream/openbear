package mesh

import (
	"fmt"
	"os"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"time"

	"openbear/node/mesh/health"
)

// fastRedial is redialing on test time (OBLAB_REDIAL=fast): ten-second
// periods, the baseline after three of them, three bad ones redial, six
// healthy ones end the backoff, which is 40, 80, 160 and 240 seconds.
func fastRedial() health.RedialParams {
	return health.RedialParams{
		Period: 10 * time.Second, MinOutcomes: 7, MinAnswers: 5,
		Bucket: 5 * time.Minute, Window: 30 * time.Minute, BaselinePeriods: 3,
		BadPeriods: 3, GoodPeriods: 6,
		Backoff:   []time.Duration{40 * time.Second, 80 * time.Second, 160 * time.Second, 240 * time.Second},
		SaveEvery: time.Minute,
	}
}

// impairFlow sends what the accepting end of k sends that match selects
// (tc u32, e.g. "match ip dport 40000 0xffff") through a netem adding
// 60 ms, 40 ms either way, and losing 15%: one TCP connection on a bad
// line. An empty match ends it.
func (l *rtLab) impairFlow(k *rtLink, match string) {
	ns, dev := l.ns[k.b], k.devB
	tc := func(args ...string) { rtSh(l.t, append([]string{"ip", "netns", "exec", ns, "tc"}, args...)...) }
	if !strings.HasPrefix(k.current, "prio") {
		tc("qdisc", "del", "dev", dev, "root")
		tc(append([]string{"qdisc", "add", "dev", dev, "root", "handle", "1:", "prio", "bands", "3", "priomap"}, strings.Fields(strings.Repeat("1 ", 16))...)...)
		tc("qdisc", "add", "dev", dev, "parent", "1:2", "handle", "20:", "netem", "limit", "10000", "delay", fmt.Sprintf("%.1fms", k.rttMs/2))
		tc("qdisc", "add", "dev", dev, "parent", "1:3", "handle", "30:", "netem", "limit", "10000", "delay", fmt.Sprintf("%.1fms", k.rttMs/2+60), "40ms", "loss", "15%")
		k.current = "prio"
	} else {
		tc("filter", "del", "dev", dev, "parent", "1:", "prio", "1")
	}
	if match != "" {
		tc(append(append([]string{"filter", "add", "dev", dev, "parent", "1:", "protocol", "ip", "prio", "1", "u32", "match", "ip", "protocol", "6", "0xff"}, strings.Fields(match)...), "flowid", "1:3")...)
	}
}

var redialEvent = regexp.MustCompile(`port=(\d+)->(\d+)$`)

// TestRedialNetns: relays A, S and V, A dialing S over TCP (40 ms), V one
// UDP hop from each (36 ms). OBLAB_NETNS=1 OBLAB_REDIAL_NETNS=1 enables it;
// it needs root. Redialing runs on test time (fastRedial).
//
//  1. S's segments to A's connection, by its port, get 60±40 ms more and
//     15% loss. A redials within the expected time, the new connection is
//     clean, routes go back to the link, and nothing saw it go down.
//  2. All of S's segments to A get the same: A redials once, then only
//     after 40 and 80 s more, not again within 160 s.
//  3. The line clean again, the backoff ends.
func TestRedialNetns(t *testing.T) {
	if os.Getenv("OBLAB_NETNS") != "1" || os.Getenv("OBLAB_REDIAL_NETNS") != "1" || os.Getuid() != 0 {
		t.Skip("set OBLAB_NETNS=1 OBLAB_REDIAL_NETNS=1 and run as root")
	}
	t.Setenv("OBLAB_REDIAL", "fast")
	rp := fastRedial()
	l := newRTLab(t, []string{"A", "S", "V"}, []string{"A-S:40:tcp", "A-V:36", "V-S:36"},
		map[string]string{"A>S": "S", "S>A": "A", "A>V": "V", "V>A": "A", "V>S": "S", "S>V": "V"})
	l.start()
	l.paths(60*time.Second, "A>S", "S>A")
	k := l.link("A", "S")
	peer := func() PeerStatus {
		for _, p := range l.status("A").Peers {
			if p.ID == "S" {
				return p
			}
		}
		return PeerStatus{}
	}
	l.waitFor("A's baseline of A-S", 90*time.Second, func() bool { r := peer().Redial; return r != nil && r.BaselineMs != nil })
	base := *peer().Redial.BaselineMs
	first := peer().LocalAddr
	oldPort, _ := strconv.Atoi(first[strings.LastIndexByte(first, ':')+1:])
	t.Logf("A-S baseline %.1f ms; A's connection from %s", base, first)
	if base < 39 || base > 50 {
		t.Errorf("baseline %.1f ms on a 40 ms link", base)
	}
	redials := func(since time.Time) []Event {
		var out []Event
		for _, e := range l.status("A").Events {
			if strings.HasPrefix(e.Kind, "link.redial") && !time.UnixMilli(e.At).Before(since) {
				out = append(out, e)
			}
		}
		return out
	}
	downs := func(since time.Time) []string {
		var out []string
		for _, id := range []string{"A", "S"} {
			for _, e := range l.status(id).Events {
				if e.Kind == "route.changed" && strings.HasSuffix(e.Detail, "(down)") && !time.UnixMilli(e.At).Before(since) {
					out = append(out, id+" "+e.Detail)
				}
			}
		}
		return out
	}

	// 1. One bad connection.
	ping := l.ping("A", "S", 200*time.Millisecond, 64)
	start := time.Now()
	l.impairFlow(k, fmt.Sprintf("match ip dport %d 0xffff", oldPort))
	detoured := false
	l.waitFor("a redial", 3*time.Minute, func() bool {
		if r, ok := l.route("A", "S"); ok && len(r.Path) > 2 {
			detoured = true
		}
		return len(redials(start)) > 0
	})
	e := redials(start)[0]
	at := time.UnixMilli(e.At)
	m := redialEvent.FindStringSubmatch(e.Detail)
	t.Logf("%s %s, %.0f s after the line went bad (detoured before: %v)", e.Kind, e.Detail, at.Sub(start).Seconds(), detoured)
	if e.Kind != "link.redial" || m == nil || m[1] != strconv.Itoa(oldPort) || m[2] == m[1] || !strings.HasPrefix(e.Detail, "link:S ") {
		t.Fatalf("event %s %q", e.Kind, e.Detail)
	}
	// Three bad periods, the first maybe partly: from two periods on, at
	// most five.
	if d := at.Sub(start); d < time.Duration(rp.BadPeriods-1)*rp.Period || d > time.Duration(rp.BadPeriods+2)*rp.Period {
		t.Errorf("redialed %v after the line went bad", d)
	}
	l.waitFor("the new connection in status", 10*time.Second, func() bool { return strings.HasSuffix(peer().LocalAddr, ":"+m[2]) })
	l.waitFor("A-S clean and both routes on it", 90*time.Second, func() bool {
		s := l.status("A")
		for _, k := range s.Routing.Links {
			if k.Peer == "S" && (k.State != "up" || k.RTTMs > 50) {
				return false
			}
		}
		a, ok1 := l.route("A", "S")
		b, ok2 := l.route("S", "A")
		return ok1 && ok2 && len(a.Path) == 2 && len(b.Path) == 2
	})
	t.Logf("A-S clean and routed directly both ways %.1f s after the redial", time.Since(at).Seconds())
	if d := downs(start); len(d) > 0 {
		t.Errorf("routes moved by down: %v", d)
	}
	lost := 0
	for _, s := range ping.samples(func(pingSample) string { return "ok" }) {
		if s.class == "lost" && s.sent.After(at.Add(-time.Second)) && s.sent.Before(at.Add(5*time.Second)) {
			lost++
		}
	}
	if lost > 0 {
		t.Errorf("%d pings lost around the redial", lost)
	}
	healthyBy := at.Add(time.Duration(rp.GoodPeriods+2) * rp.Period)
	time.Sleep(time.Until(healthyBy))
	if r := peer().Redial; len(redials(start)) != 1 || r.Level != 0 || r.Redials != 1 {
		t.Fatalf("after the redial: %+v, events %+v", r, redials(start))
	}

	// 2. Every connection bad.
	phase2 := time.Now()
	l.impairFlow(k, "match ip sport 51821 0xffff")
	time.Sleep(rp.Backoff[0] + rp.Backoff[1] + time.Duration(rp.BadPeriods+4)*rp.Period)
	got := redials(phase2)
	var times []string
	for _, e := range got {
		times = append(times, fmt.Sprintf("%+.0fs %s", time.UnixMilli(e.At).Sub(phase2).Seconds(), e.Detail))
	}
	t.Logf("all connections bad: %s", strings.Join(times, "; "))
	if len(got) < 2 || len(got) > 3 {
		t.Fatalf("%d redials while every connection was bad", len(got))
	}
	for i := 1; i < len(got); i++ {
		gap := time.UnixMilli(got[i].At).Sub(time.UnixMilli(got[i-1].At))
		if gap < rp.Backoff[i-1] {
			t.Errorf("redial %d %v after the one before, backoff %v", i, gap, rp.Backoff[i-1])
		}
	}
	if r := peer().Redial; r.Level != len(got) {
		t.Errorf("level %d after %d redials", r.Level, len(got))
	}

	// 3. Clean again.
	l.impairFlow(k, "")
	clean := time.Now()
	l.waitFor("the backoff to end", time.Duration(rp.GoodPeriods+6)*rp.Period, func() bool { return peer().Redial.Level == 0 })
	t.Logf("backoff ended %.0f s after the line was clean", time.Since(clean).Seconds())
	if n := len(redials(phase2)); n != len(got) {
		t.Errorf("%d more redials on a clean line", n-len(got))
	}
	l.paths(5*time.Minute, "A>S", "S>A")
	t.Logf("routes back on A-S %.0f s after the line was clean", time.Since(clean).Seconds())
	for _, id := range []string{"A", "S"} {
		for _, e := range l.status(id).Events {
			if e.Kind == "route.changed" && !time.UnixMilli(e.At).Before(start) {
				t.Logf("%s %+.0fs %s", id, time.UnixMilli(e.At).Sub(start).Seconds(), e.Detail)
			}
		}
	}
}
