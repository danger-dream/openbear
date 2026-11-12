package mesh

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"testing"
	"time"
)

var rtAvg = regexp.MustCompile(`(\d+)% packet loss.*\n.*= [0-9.]+/([0-9.]+)/`)

// rttNow is the average round trip of a short ping burst from src to dst.
func (l *rtLab) rttNow(src, dst string) (avg float64, lossPct int) {
	out, _ := exec.Command("ip", "netns", "exec", l.ns[src], "ping", "-n", "-q", "-c", "5", "-i", "0.2", "-W", "1", l.addr[dst].String()).CombinedOutput()
	m := rtAvg.FindSubmatch(out)
	if m == nil {
		return -1, 100
	}
	lossPct, _ = strconv.Atoi(string(m[1]))
	avg, _ = strconv.ParseFloat(string(m[2]), 64)
	return avg, lossPct
}

func (l *rtLab) pathState(id, path string) string {
	for _, p := range l.status(id).Paths {
		if p.ID == path {
			return p.State
		}
	}
	return ""
}

// TestRoutingNetnsFixedPath covers design case 3. The routes go A>Z>B (20 ms)
// and M>Z>B (14 ms); fixed paths take A and member M to B through X and Y,
// and back the same way (60 and 50 ms). Cutting X-Y puts both back on the
// routes as soon as the link is seen down; once it is back they stay there
// for pinHold and then return to the fixed paths. No packet loops.
func TestRoutingNetnsFixedPath(t *testing.T) {
	rtSkip(t)
	l := newRTLab(t, []string{"A", "X", "Y", "Z", "B", "M"},
		[]string{"A-X:20", "X-Y:20", "Y-B:20", "A-Z:10", "Z-B:10", "M-X:10", "M-Z:4"}, nil)
	paths := []FixedPath{
		{ID: "p1", From: []string{"A"}, To: []string{"B"}, Via: []string{"X", "Y"}},
		{ID: "p1~back", From: []string{"B"}, To: []string{"A"}, Via: []string{"Y", "X"}},
		{ID: "p2", From: []string{"M"}, To: []string{"B"}, Via: []string{"X", "Y"}},
		{ID: "p2~back", From: []string{"B"}, To: []string{"M"}, Via: []string{"Y", "X"}},
	}
	withPaths := true
	// OBLAB_ACTIVE_MS: the probe interval, the settings' activeIntervalMs; a
	// link is down after three probes lost, so it sets how soon paths fall back.
	activeMs, _ := strconv.Atoi(os.Getenv("OBLAB_ACTIVE_MS"))
	l.configure = func(id string, c *Config) {
		if activeMs > 0 {
			c.Health = &Health{ActiveIntervalMs: activeMs}
		}
		for i := range c.Nodes {
			if c.Nodes[i].ID == "M" {
				c.Nodes[i].Role, c.Nodes[i].Leaf, c.Nodes[i].Relays = roleMember, true, []string{"X", "Z"}
			}
		}
		if id == "M" {
			c.Self.Role, c.Self.Leaf = roleMember, true
		} else {
			for i := range c.Links {
				if c.Links[i].Peer == "M" {
					c.Links[i].Kind = kindMember
				}
			}
		}
		c.RelayLinks = slices.DeleteFunc(c.RelayLinks, func(p [2]string) bool { return p[0] == "M" || p[1] == "M" })
		c.MemberLinks = [][2]string{{"M", "X"}, {"M", "Z"}}
		if withPaths {
			c.Paths = paths
		}
	}
	l.start()
	l.paths(60*time.Second, "A>Z>B", "B>Z>A", "M>Z>B", "B>Z>M", "X>Y", "Y>X")

	near := func(got, want float64) bool { return got > want-6 && got < want+12 }
	waitRTT := func(what, src, dst string, want float64, within time.Duration) time.Duration {
		t.Helper()
		start := time.Now()
		var got float64
		l.waitFor(what, within, func() bool {
			got, _ = l.rttNow(src, dst)
			return near(got, want)
		})
		t.Logf("%s: %s>%s %.1f ms after %.1fs", what, src, dst, got, time.Since(start).Seconds())
		return time.Since(start)
	}
	waitRTT("on the fixed path", "A", "B", 60, 30*time.Second)
	waitRTT("member on the fixed path", "M", "B", 50, 30*time.Second)
	if s := l.pathState("A", "p1"); s != "pinned" {
		t.Fatalf("A p1 %q", s)
	}
	if s := l.pathState("M", "p2"); s != "pinned" {
		t.Fatalf("M p2 %q", s)
	}
	// The routes themselves still say Z: only the fixed pairs leave them.
	if r, _ := l.route("A", "B"); !slices.Equal(r.Path, []string{"A", "Z", "B"}) {
		t.Fatalf("A's route to B %v", r.Path)
	}
	if got, _ := l.rttNow("Z", "B"); !near(got, 10) {
		t.Fatalf("other sources stay on their routes: Z>B %.1f", got)
	}

	// Cut X-Y: back on the routes once the link is down. A steady ping
	// tells when: replies over 40 ms came by the fixed path, under 35 by the route.
	flow := l.ping("A", "B", 50*time.Millisecond, 64)
	time.Sleep(3 * time.Second)
	cut := time.Now()
	l.netem(l.link("X", "Y"), "loss 100%")
	l.waitFor("A falls back", 20*time.Second, func() bool { return l.pathState("A", "p1") == "fallback" })
	stateAt := time.Since(cut)
	waitRTT("member fallen back", "M", "B", 14, 20*time.Second)
	time.Sleep(2 * time.Second)
	firstRoute, lost, last := time.Duration(-1), 0, time.Duration(0)
	for _, s := range flow.samples(func(s pingSample) string {
		if s.rtt > 40 {
			return "fixed"
		}
		return "route"
	}) {
		at := s.sent.Sub(cut)
		if at < 0 {
			continue
		}
		switch s.class {
		case "lost":
			lost++
		case "route":
			if firstRoute < 0 {
				firstRoute = at
			}
		case "fixed":
			last = at
		}
	}
	t.Logf("after the cut: A reports fallback at %.2fs, first reply by the route sent at %.2fs, %d of the 50 ms probes lost (%.2fs), last by the fixed path sent at %.2fs",
		stateAt.Seconds(), firstRoute.Seconds(), lost, float64(lost)*0.05, last.Seconds())
	if firstRoute < 0 || firstRoute > 5*time.Second {
		t.Fatalf("not on the route within 5 s of the cut: %v", firstRoute)
	}

	// Restore it: on the routes for pinHold, then the fixed paths again.
	time.Sleep(5 * time.Second)
	restored := time.Now()
	l.netem(l.link("X", "Y"), "")
	l.waitFor("X-Y up", 30*time.Second, func() bool { return l.pathState("A", "p1") == "waiting" })
	waitingAt := time.Since(restored)
	time.Sleep(pinHold/2 - waitingAt)
	if got, _ := l.rttNow("A", "B"); !near(got, 20) {
		t.Fatalf("back on the fixed path %.0fs after the link: %.1f ms", time.Since(restored).Seconds(), got)
	}
	if s := l.pathState("A", "p1"); s != "waiting" {
		t.Fatalf("A p1 within the hold %q", s)
	}
	back := waitRTT("back on the fixed path", "A", "B", 60, pinHold)
	waitRTT("member back on the fixed path", "M", "B", 50, 20*time.Second)
	t.Logf("cut at 0, restored at %.1fs, fixed again %.1fs after the restore", restored.Sub(cut).Seconds(), time.Since(restored).Seconds()-back.Seconds()+back.Seconds())
	if since := time.Since(restored); since < pinHold {
		t.Fatalf("fixed again %.1fs after the restore, before pinHold", since.Seconds())
	}

	// Without the paths in the config, the pairs go back to the routes.
	withPaths = false
	for _, id := range l.ids {
		c := l.config(id)
		c.Revision = 2
		raw, _ := json.Marshal(c)
		tmp := filepath.Join(l.dirs[id], "config.json.tmp")
		os.WriteFile(tmp, raw, 0o600)
		os.Rename(tmp, filepath.Join(l.dirs[id], "config.json"))
	}
	waitRTT("paths removed", "A", "B", 20, 20*time.Second)
	if s := l.status("A"); len(s.Paths) != 0 {
		t.Fatalf("A still reports %v", s.Paths)
	}

	for _, id := range l.ids {
		s := l.status(id)
		if s.Counters.DroppedTTL != 0 {
			t.Errorf("%s dropped %d packets for their TTL: a loop", id, s.Counters.DroppedTTL)
		}
		if id == "M" && s.Counters.Forwarded != 0 {
			t.Errorf("member M forwarded %d packets", s.Counters.Forwarded)
		}
	}
}

func (l *rtLab) pathStates(id string) map[string]PathStatus {
	out := map[string]PathStatus{}
	for _, p := range l.status(id).Paths {
		out[p.ID] = p
	}
	return out
}

// TestRoutingNetnsFixedPathBackup: A and B have two paths, p1 through X and Y
// (60 ms) and p2 through W (30 ms); the routes go through Z (10 ms). Both ways
// take p1, p2 standing by; cutting X-Y moves them to p2, cutting W-B as well
// to the routes; X-Y back, they wait out the hold (OBLAB_PIN_HOLD, 15 s here)
// on the routes and return to p1, and p2 back again stands by. No packet loops.
func TestRoutingNetnsFixedPathBackup(t *testing.T) {
	rtSkip(t)
	const hold = 15 * time.Second
	ids := []string{"A", "X", "Y", "W", "Z", "B"}
	l := newRTLab(t, ids, []string{"A-X:20", "X-Y:20", "Y-B:20", "A-W:15", "W-B:15", "A-Z:5", "Z-B:5"}, nil)
	l.env = map[string][]string{}
	for _, id := range ids {
		l.env[id] = []string{"OBLAB_PIN_HOLD=" + hold.String()}
	}
	l.configure = func(id string, c *Config) {
		c.Paths = []FixedPath{
			{ID: "p1", From: []string{"A"}, To: []string{"B"}, Via: []string{"X", "Y"}},
			{ID: "p1~back", From: []string{"B"}, To: []string{"A"}, Via: []string{"Y", "X"}},
			{ID: "p2", From: []string{"A"}, To: []string{"B"}, Via: []string{"W"}},
			{ID: "p2~back", From: []string{"B"}, To: []string{"A"}, Via: []string{"W"}},
		}
	}
	l.start()
	l.paths(60*time.Second, "A>Z>B", "B>Z>A", "X>Y", "W>B")

	near := func(got, want float64) bool { return got > want-5 && got < want+8 }
	both := func(what string, want float64, within time.Duration) {
		t.Helper()
		start := time.Now()
		var ab, ba float64
		l.waitFor(what, within, func() bool {
			ab, _ = l.rttNow("A", "B")
			ba, _ = l.rttNow("B", "A")
			return near(ab, want) && near(ba, want)
		})
		t.Logf("%s: A>B %.1f ms, B>A %.1f ms after %.1fs", what, ab, ba, time.Since(start).Seconds())
	}
	states := func(id string) string {
		s := l.pathStates(id)
		return s["p1"+suffix(id)].State + " " + s["p2"+suffix(id)].State
	}
	switches := func(id string) []string {
		var out []string
		for _, e := range l.status(id).Events {
			if e.Kind == "pin.switch" {
				out = append(out, e.Detail)
			}
		}
		return out
	}

	both("on p1", 60, 30*time.Second)
	if a, b := states("A"), states("B"); a != "pinned standby" || b != "pinned standby" {
		t.Fatalf("all up: A %s, B %s", a, b)
	}
	l.netem(l.link("X", "Y"), "loss 100%")
	both("on p2 after X-Y cut", 30, 20*time.Second)
	if s := l.pathStates("A")["p1"]; s.State != "fallback" || !slices.Equal(s.Down, []string{"X", "Y"}) {
		t.Fatalf("A p1 after the cut: %+v", s)
	}
	l.netem(l.link("W", "B"), "loss 100%")
	both("on the routes after W-B cut", 10, 20*time.Second)
	l.netem(l.link("X", "Y"), "")
	l.waitFor("p1 waiting", 30*time.Second, func() bool { return l.pathStates("A")["p1"].State == "waiting" })
	restored := time.Now()
	if got, _ := l.rttNow("A", "B"); !near(got, 10) {
		t.Fatalf("within the hold: A>B %.1f ms", got)
	}
	both("back on p1", 60, hold+10*time.Second)
	if since := time.Since(restored); since < hold-2*time.Second {
		t.Fatalf("back on p1 %.1fs after it was seen up, before the hold", since.Seconds())
	}
	l.netem(l.link("W", "B"), "")
	l.waitFor("p2 standing by", hold+30*time.Second, func() bool { return states("A") == "pinned standby" && states("B") == "pinned standby" })
	if got, _ := l.rttNow("A", "B"); !near(got, 60) {
		t.Fatalf("p2 back took the pair: A>B %.1f ms", got)
	}
	want := []string{"B: 0 -> 1", "B: 1 -> 2 down X Y", "B: 2 -> 0 down W B", "B: 0 -> 1"}
	if got := switches("A"); !slices.Equal(got, want) {
		t.Errorf("A's switches %q, want %q", got, want)
	}
	for _, id := range ids {
		if s := l.status(id); s.Counters.DroppedTTL != 0 {
			t.Errorf("%s dropped %d packets for their TTL: a loop", id, s.Counters.DroppedTTL)
		}
	}
}

// TestRoutingNetnsFixedPathSilent: a path whose links stay up but which
// carries nothing, as X drops what it forwards over it. A, B and the relays
// on the way: p1 A>X>Y>B (45 ms), p2 A>X>W>B (25 ms), the routes A>X>Z>B
// (9 ms), all through X. The ends leave a silent path within seconds, X
// follows them off it although its links are up, and they come back after
// the hold once it carries again.
func TestRoutingNetnsFixedPathSilent(t *testing.T) {
	rtSkip(t)
	const hold = 15 * time.Second
	ids := []string{"A", "X", "Y", "W", "Z", "B"}
	l := newRTLab(t, ids, []string{"A-X:5", "X-Y:20", "Y-B:20", "X-W:10", "W-B:10", "X-Z:2", "Z-B:2"}, nil)
	drop := filepath.Join(t.TempDir(), "drop")
	l.env = map[string][]string{}
	for _, id := range ids {
		l.env[id] = []string{"OBLAB_PIN_HOLD=" + hold.String()}
	}
	l.env["X"] = append(l.env["X"], "OBLAB_DROP_RANKS="+drop)
	l.configure = func(id string, c *Config) {
		c.Paths = []FixedPath{
			{ID: "p1", From: []string{"A"}, To: []string{"B"}, Via: []string{"X", "Y"}, Rank: 1},
			{ID: "p1~back", From: []string{"B"}, To: []string{"A"}, Via: []string{"Y", "X"}, Rank: 1},
			{ID: "p2", From: []string{"A"}, To: []string{"B"}, Via: []string{"X", "W"}, Rank: 2},
			{ID: "p2~back", From: []string{"B"}, To: []string{"A"}, Via: []string{"W", "X"}, Rank: 2},
		}
	}
	l.start()
	l.paths(60*time.Second, "A>X>Z>B", "B>Z>X>A")

	near := func(got, want float64) bool { return got > want-5 && got < want+8 }
	both := func(what string, want float64, within time.Duration) {
		t.Helper()
		start := time.Now()
		var ab, ba float64
		l.waitFor(what, within, func() bool {
			ab, _ = l.rttNow("A", "B")
			ba, _ = l.rttNow("B", "A")
			return near(ab, want) && near(ba, want)
		})
		t.Logf("%s: A>B %.1f ms, B>A %.1f ms after %.1fs", what, ab, ba, time.Since(start).Seconds())
	}
	switches := func(id string) []string {
		var out []string
		for _, e := range l.status(id).Events {
			if e.Kind == "pin.switch" {
				out = append(out, e.Detail)
			}
		}
		return out
	}

	both("on p1", 45, 30*time.Second)
	// Which path answers first while all come up varies; what follows does not.
	before := len(switches("A"))
	os.WriteFile(drop, []byte("1"), 0o600)
	cut := time.Now()
	l.waitFor("A off p1", 15*time.Second, func() bool { return l.pathStates("A")["p1"].State == "fallback" })
	if took := time.Since(cut); took > pathSilent+3*time.Second {
		t.Errorf("left the silent path after %.1fs", took.Seconds())
	} else {
		t.Logf("A left p1 %.1fs after X began dropping", took.Seconds())
	}
	if s := l.pathStates("A")["p1"]; !s.Silent || s.Down != nil {
		t.Fatalf("A p1 silent: %+v", s)
	}
	both("on p2 with p1 silent", 25, 20*time.Second)
	os.WriteFile(drop, []byte("1 2"), 0o600)
	both("on the routes with p1 and p2 silent", 9, 20*time.Second)
	os.WriteFile(drop, nil, 0o600)
	l.waitFor("p1 waiting", 20*time.Second, func() bool { return l.pathStates("A")["p1"].State == "waiting" })
	restored := time.Now()
	both("back on p1", 45, hold+15*time.Second)
	if since := time.Since(restored); since < hold-2*time.Second {
		t.Fatalf("back on p1 %.1fs after it answered, before the hold", since.Seconds())
	}
	want := []string{"B: 1 -> 2 silent", "B: 2 -> 0 silent", "B: 0 -> 1"}
	if got := switches("A")[before:]; !slices.Equal(got, want) {
		t.Errorf("A's switches %q, want %q", got, want)
	}
	for _, id := range ids {
		if s := l.status(id); s.Counters.DroppedTTL != 0 {
			t.Errorf("%s dropped %d packets for their TTL: a loop", id, s.Counters.DroppedTTL)
		}
	}
}

// suffix is the id suffix of the paths a node is the source of here: B's are the ways back.
func suffix(id string) string {
	if id == "B" {
		return "~back"
	}
	return ""
}

// TestRoutingNetnsFixedPathMembers: both ends are members routing over their
// relays, S on X and Z, D on Y and Z; the routes go S>Z>D (8 ms) and the own
// route S>X>Y>D (30 ms). Both ways take it.
func TestRoutingNetnsFixedPathMembers(t *testing.T) {
	rtSkip(t)
	fixedPathMembers(t, []string{"S-X:10", "X-Y:10", "Y-D:10", "S-Z:4", "Z-D:4", "X-Z:20", "Y-Z:20"}, false, 30, "X>Y", "Y>X")
}

// The same with S on X and X to Y over TCP, packets as large as TCP takes
// there (tcpJumbo), and Y to D over UDP.
func TestRoutingNetnsFixedPathMembersJumbo(t *testing.T) {
	rtSkip(t)
	fixedPathMembers(t, []string{"S-X:10:tcp", "X-Y:10:tcp", "Y-D:10", "S-Z:4", "Z-D:4", "X-Z:20", "Y-Z:20"}, true, 30, "X>Y", "Y>X")
}

// TestRoutingNetnsFixedPathMembersElsewhere: as on the mesh, the relays next
// to the ends route them through Z (4 ms), not over the path's links
// (40 ms): X and Y still take the ends' packets that the path brings.
func TestRoutingNetnsFixedPathMembersElsewhere(t *testing.T) {
	rtSkip(t)
	fixedPathMembers(t, []string{"S-X:40", "X-Y:10", "Y-D:40", "S-Z:2", "Z-D:2", "X-Z:2", "Y-Z:2"}, false, 90, "X>Z>S", "Y>Z>D")
}

// want is the path's round trip; routes the relays' routes to wait for
// before measuring.
func fixedPathMembers(t *testing.T, links []string, jumbo bool, want float64, routes ...string) {
	ids := []string{"S", "X", "Y", "Z", "D"}
	l := newRTLab(t, ids, links, nil)
	members := map[string][]string{"S": {"X", "Z"}, "D": {"Y", "Z"}}
	l.configure = func(id string, c *Config) {
		c.Network.TCPJumbo = jumbo
		for i := range c.Nodes {
			if relays, ok := members[c.Nodes[i].ID]; ok {
				c.Nodes[i].Role, c.Nodes[i].Leaf, c.Nodes[i].Relays = roleMember, true, relays
			}
		}
		if _, ok := members[id]; ok {
			c.Self.Role, c.Self.Leaf = roleMember, true
		} else {
			for i := range c.Links {
				if _, ok := members[c.Links[i].Peer]; ok {
					c.Links[i].Kind = kindMember
				}
			}
		}
		c.RelayLinks = slices.DeleteFunc(c.RelayLinks, func(p [2]string) bool { return members[p[0]] != nil || members[p[1]] != nil })
		c.MemberLinks = [][2]string{{"S", "X"}, {"S", "Z"}, {"D", "Y"}, {"D", "Z"}}
		c.Paths = []FixedPath{
			{ID: "p1", From: []string{"S"}, To: []string{"D"}, Via: []string{"X", "Y"}},
			{ID: "p1~back", From: []string{"D"}, To: []string{"S"}, Via: []string{"Y", "X"}},
		}
	}
	l.start()
	l.paths(60*time.Second, routes...)
	near := func(got, want float64) bool { return got > want-5 && got < want+8 }
	var sd, ds float64
	l.waitFor("S and D on the own route", 30*time.Second, func() bool {
		sd, _ = l.rttNow("S", "D")
		ds, _ = l.rttNow("D", "S")
		return near(sd, want) && near(ds, want)
	})
	t.Logf("S>D %.1f ms, D>S %.1f ms", sd, ds)
}
