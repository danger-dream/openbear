package mesh

import (
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"net/netip"
	"slices"
	"testing"
	"time"
)

func pinNodes(self string, ids ...string) *e2eTable {
	t := &e2eTable{byID: map[string]*e2eNode{}, byAddr: map[netip.Addr]*e2eNode{}}
	for i, id := range ids {
		a := netip.AddrFrom4([4]byte{100, 96, 0, byte(i + 1)})
		if id == self {
			t.self = a
			continue
		}
		n := &e2eNode{id: id, addr: a}
		t.byID[id], t.byAddr[a] = n, n
	}
	return t
}

func pinKeys(ids ...string) map[string]string {
	out := map[string]string{}
	for i, id := range ids {
		k := make([]byte, 32)
		k[0] = byte(i + 1)
		out[id] = hex.EncodeToString(k)
	}
	return out
}

func addrOf(nodes *e2eTable, id string) netip.Addr {
	if n := nodes.byID[id]; n != nil {
		return n.addr
	}
	return nodes.self
}

var pinIDs = []string{"S", "X", "Y", "D", "S2", "Z"}

func allUp([]string) []string { return nil }

func firstDown(nodes []string) []string { return nodes[:2] }

func TestFixedPathTableEachNodeKnowsItsHop(t *testing.T) {
	path := FixedPath{ID: "p1", From: []string{"S", "S2"}, To: []string{"D"}, Via: []string{"X", "Y"}}
	for _, tc := range []struct {
		self, src, prev, next string
	}{
		{"S", "S", "", "X"}, {"X", "S", "S", "Y"}, {"Y", "S", "X", "D"}, {"X", "S2", "S2", "Y"},
	} {
		nodes := pinNodes(tc.self, pinIDs...)
		tab := newPinRuns().tableFor(&Config{Self: Self{ID: tc.self}, Paths: []FixedPath{path}}, pinKeys(pinIDs...), nodes)
		hops := tab[[2]netip.Addr{addrOf(nodes, tc.src), addrOf(nodes, "D")}]
		if len(hops) != 1 || hops[0].prev != tc.prev || hops[0].nextID != tc.next {
			t.Fatalf("%s for %s: %+v, want prev %q next %q", tc.self, tc.src, hops, tc.prev, tc.next)
		}
	}
	// The destination and nodes off the path have nothing to do.
	for _, self := range []string{"D", "Z"} {
		nodes := pinNodes(self, pinIDs...)
		if tab := newPinRuns().tableFor(&Config{Self: Self{ID: self}, Paths: []FixedPath{path}}, pinKeys(pinIDs...), nodes); len(tab) != 0 {
			t.Fatalf("%s has %d pairs", self, len(tab))
		}
	}
}

func TestFixedPathOnlyFromTheNodeBeforeAndOnlyWhileInUse(t *testing.T) {
	nodes := pinNodes("X", pinIDs...)
	runs := newPinRuns()
	tab := runs.tableFor(&Config{Self: Self{ID: "X"}, Paths: []FixedPath{{ID: "p1", From: []string{"S"}, To: []string{"D"}, Via: []string{"X", "Y"}}}}, pinKeys(pinIDs...), nodes)
	s, d := addrOf(nodes, "S"), addrOf(nodes, "D")
	if _, ok := tab.forward(s, d, "S"); ok {
		t.Fatal("pinned before the path was found up")
	}
	runs.evaluate(allUp)
	if h, ok := tab.forward(s, d, "S"); !ok || h.nextID != "Y" {
		t.Fatalf("from S: %+v %v", h, ok)
	}
	// From anywhere else, routed as usual: a packet off the path never comes back onto it.
	for _, from := range []string{"Y", "Z", "D", ""} {
		if _, ok := tab.forward(s, d, from); ok {
			t.Fatalf("pinned a packet from %q", from)
		}
	}
	// Another destination, the reverse direction, and this node's own packets are not this pair.
	if _, ok := tab.forward(d, s, "D"); ok {
		t.Fatal("pinned the reverse direction")
	}
	if _, ok := tab.local(nodes.self, d); ok {
		t.Fatal("a relay's own packets taken as the source's")
	}
}

func TestFixedPathSkipsPairsAndTakesAPairsPathsInOrder(t *testing.T) {
	nodes := pinNodes("S", pinIDs...)
	runs := newPinRuns()
	c := &Config{Self: Self{ID: "S"}, Paths: []FixedPath{
		{ID: "p1", From: []string{"S"}, To: []string{"D", "Z"}, Via: []string{"X"}, Skip: [][2]string{{"S", "Z"}}},
		{ID: "p2", From: []string{"S"}, To: []string{"D", "Z"}, Via: []string{"Y"}},
	}}
	tab := runs.tableFor(c, pinKeys(pinIDs...), nodes)
	runs.evaluate(allUp)
	if h, ok := tab.local(nodes.self, addrOf(nodes, "D")); !ok || h.nextID != "X" || h.run.path != "p1" {
		t.Fatalf("to D: %+v %v", h, ok)
	}
	if h, ok := tab.local(nodes.self, addrOf(nodes, "Z")); !ok || h.nextID != "Y" || h.run.path != "p2" {
		t.Fatalf("to Z, skipped by p1: %+v %v", h, ok)
	}
	// p1 down: S to D takes p2, the next of the pair.
	runs.evaluate(func(n []string) []string {
		if n[1] == "X" {
			return n[:2]
		}
		return nil
	})
	if h, ok := tab.local(nodes.self, addrOf(nodes, "D")); !ok || h.run.path != "p2" {
		t.Fatalf("to D with p1 down: %+v %v", h, ok)
	}
}

// A pair's paths through the same relay from the same node: the relay takes
// the first in use, as the source does.
func TestFixedPathRelayTakesThePairsFirstPathInUse(t *testing.T) {
	nodes := pinNodes("X", pinIDs...)
	runs := newPinRuns()
	c := &Config{Self: Self{ID: "X"}, Paths: []FixedPath{
		{ID: "p1", From: []string{"S"}, To: []string{"D"}, Via: []string{"X", "Y"}},
		{ID: "p2", From: []string{"S"}, To: []string{"D"}, Via: []string{"X", "Z"}},
		{ID: "p3", From: []string{"S"}, To: []string{"D"}, Via: []string{"Y", "X"}},
	}}
	tab := runs.tableFor(c, pinKeys(pinIDs...), nodes)
	s, d := addrOf(nodes, "S"), addrOf(nodes, "D")
	if hops := tab[[2]netip.Addr{s, d}]; len(hops) != 3 {
		t.Fatalf("hops %+v", hops)
	}
	runs.evaluate(allUp)
	if h, ok := tab.forward(s, d, "S"); !ok || h.nextID != "Y" {
		t.Fatalf("all up: %+v %v", h, ok)
	}
	// From Y it is p3's packet whatever p1 and p2 do.
	if h, ok := tab.forward(s, d, "Y"); !ok || h.nextID != "D" || h.run.path != "p3" {
		t.Fatalf("from Y: %+v %v", h, ok)
	}
	runs.evaluate(func(n []string) []string {
		if slices.Contains(n, "Y") && n[2] == "Y" {
			return []string{"X", "Y"}
		}
		return nil
	})
	if h, ok := tab.forward(s, d, "S"); !ok || h.nextID != "Z" {
		t.Fatalf("p1 down: %+v %v", h, ok)
	}
	runs.evaluate(firstDown)
	if _, ok := tab.forward(s, d, "S"); ok {
		t.Fatal("pinned with every path down")
	}
}

// The source goes down its pair's paths as they fail, to the routes when none
// is up, and back to an earlier one once it has been up for pinHold; status
// and events say where it is.
func TestFixedPathTakesTheNextAndComesBack(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	nodes := pinNodes("S", pinIDs...)
	runs := newPinRuns()
	runs.now = func() time.Time { return now }
	tab := runs.tableFor(&Config{Self: Self{ID: "S"}, Paths: []FixedPath{
		{ID: "p1", From: []string{"S"}, To: []string{"D"}, Via: []string{"X", "Y"}},
		{ID: "p2", From: []string{"S"}, To: []string{"D"}, Via: []string{"Z"}},
	}}, pinKeys(pinIDs...), nodes)
	d := addrOf(nodes, "D")
	cut := map[string]bool{}
	links := func(n []string) []string {
		for i := 0; i+1 < len(n); i++ {
			if cut[n[i]+n[i+1]] {
				return []string{n[i], n[i+1]}
			}
		}
		return nil
	}
	step := func(dt time.Duration) ([]pinSwitch, string) {
		now = now.Add(dt)
		sw := runs.evaluate(links)
		via := ""
		if h, ok := tab.local(nodes.self, d); ok {
			via = h.run.path
		}
		return sw, via
	}
	states := func() string {
		out := ""
		for _, s := range runs.status() {
			out += fmt.Sprintf("%d:%s%v ", s.Rank, s.State, s.Down)
		}
		return out
	}
	if sw, via := step(0); via != "p1" || !slices.EqualFunc(sw, []pinSwitch{{dst: "D", from: 0, to: 1}}, pinSwitchEqual) {
		t.Fatalf("all up: %v %+v", via, sw)
	}
	if st := states(); st != "1:pinned[] 2:standby[] " {
		t.Fatalf("all up: %s", st)
	}
	cut["XY"] = true
	if sw, via := step(time.Second); via != "p2" || !slices.EqualFunc(sw, []pinSwitch{{dst: "D", from: 1, to: 2, down: []string{"X", "Y"}}}, pinSwitchEqual) {
		t.Fatalf("X-Y cut: %v %+v", via, sw)
	}
	if st := states(); st != "1:fallback[X Y] 2:pinned[] " {
		t.Fatalf("X-Y cut: %s", st)
	}
	cut["SZ"] = true
	if sw, via := step(time.Second); via != "" || !slices.EqualFunc(sw, []pinSwitch{{dst: "D", from: 2, to: 0, down: []string{"S", "Z"}}}, pinSwitchEqual) {
		t.Fatalf("both cut: %v %+v", via, sw)
	}
	// X-Y back: p1 waits out the hold while the pair is on the routes.
	delete(cut, "XY")
	if sw, via := step(time.Second); via != "" || len(sw) != 0 {
		t.Fatalf("X-Y back: %v %+v", via, sw)
	}
	if st := states(); st != "1:waiting[] 2:fallback[S Z] " {
		t.Fatalf("X-Y back: %s", st)
	}
	if sw, via := step(pinHold); via != "p1" || !slices.EqualFunc(sw, []pinSwitch{{dst: "D", from: 0, to: 1}}, pinSwitchEqual) {
		t.Fatalf("after the hold: %v %+v", via, sw)
	}
	// S-Z back too: p2 waits, then stands by behind p1 with no switch.
	delete(cut, "SZ")
	step(time.Second)
	if sw, _ := step(pinHold); len(sw) != 0 || states() != "1:pinned[] 2:standby[] " {
		t.Fatalf("p2 back: %+v %s", sw, states())
	}
}

func pinSwitchEqual(a, b pinSwitch) bool {
	return a.dst == b.dst && a.from == b.from && a.to == b.to && slices.Equal(a.down, b.down)
}

func TestFixedPathStatesOutliveRebuildsAndGoWithTheirPath(t *testing.T) {
	nodes := pinNodes("S", pinIDs...)
	runs := newPinRuns()
	c := &Config{Self: Self{ID: "S"}, Paths: []FixedPath{{ID: "p1", From: []string{"S"}, To: []string{"D"}, Via: []string{"X"}}}}
	runs.tableFor(c, pinKeys(pinIDs...), nodes)
	runs.evaluate(allUp)
	// Routes changed, the same config built again: still in use.
	tab := runs.tableFor(c, pinKeys(pinIDs...), nodes)
	if _, ok := tab.local(nodes.self, addrOf(nodes, "D")); !ok {
		t.Fatal("a rebuild lost the state")
	}
	// The next node without a peer for a moment keeps the state, not the pair.
	keys := pinKeys(pinIDs...)
	delete(keys, "X")
	if tab := runs.tableFor(c, keys, nodes); len(tab) != 0 || len(runs.runs) != 1 {
		t.Fatalf("without X's peer: %d pairs, %d runs", len(tab), len(runs.runs))
	}
	// Through other relays it is another path, from the start.
	c.Paths[0].Via = []string{"Y"}
	tab = runs.tableFor(c, pinKeys(pinIDs...), nodes)
	if _, ok := tab.local(nodes.self, addrOf(nodes, "D")); ok || len(runs.runs) != 1 {
		t.Fatalf("new via in use before evaluation; %d runs", len(runs.runs))
	}
	c.Paths = nil
	if runs.tableFor(c, pinKeys(pinIDs...), nodes); len(runs.runs) != 0 {
		t.Fatal("a removed path kept its state")
	}
}

func TestFixedPathFallsBackAtOnceAndReturnsAfterTheHold(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	nodes := pinNodes("S", pinIDs...)
	runs := newPinRuns()
	runs.now = func() time.Time { return now }
	tab := runs.tableFor(&Config{Self: Self{ID: "S"}, Paths: []FixedPath{{ID: "p1", From: []string{"S"}, To: []string{"D"}, Via: []string{"X", "Y"}}}}, pinKeys(pinIDs...), nodes)
	d := addrOf(nodes, "D")
	up := true
	links := func(n []string) []string {
		if !slices.Equal(n, []string{"S", "X", "Y", "D"}) {
			t.Fatalf("judged by %v", n)
		}
		if up {
			return nil
		}
		return n[1:3]
	}
	step := func(dt time.Duration) (string, bool) {
		now = now.Add(dt)
		runs.evaluate(links)
		_, ok := tab.local(nodes.self, d)
		return runs.status()[0].State, ok
	}
	if st, ok := step(0); st != "pinned" || !ok {
		t.Fatalf("first up: %s %v", st, ok)
	}
	up = false
	if st, ok := step(500 * time.Millisecond); st != "fallback" || ok {
		t.Fatalf("a link down: %s %v", st, ok)
	}
	up = true
	if st, ok := step(500 * time.Millisecond); st != "waiting" || ok {
		t.Fatalf("up again: %s %v", st, ok)
	}
	if s := runs.status()[0]; s.Until != now.Add(pinHold).UnixMilli() {
		t.Fatalf("until %d", s.Until)
	}
	if st, ok := step(pinHold - time.Second); st != "waiting" || ok {
		t.Fatalf("before the hold: %s %v", st, ok)
	}
	// Down again within the hold: the wait starts over.
	up = false
	step(500 * time.Millisecond)
	up = true
	if st, _ := step(500 * time.Millisecond); st != "waiting" {
		t.Fatalf("after a second outage: %s", st)
	}
	if st, ok := step(pinHold - time.Second); st != "waiting" || ok {
		t.Fatalf("the wait did not start over: %s %v", st, ok)
	}
	if st, ok := step(time.Second); st != "pinned" || !ok {
		t.Fatalf("after the hold: %s %v", st, ok)
	}
}

func TestFixedPathJustConfiguredWaitsOnlyForItsLinks(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	nodes := pinNodes("S", pinIDs...)
	runs := newPinRuns()
	runs.now = func() time.Time { return now }
	c := &Config{Self: Self{ID: "S"}, Paths: []FixedPath{{ID: "p1", From: []string{"S"}, To: []string{"D"}, Via: []string{"X"}}}}
	tab := runs.tableFor(c, pinKeys(pinIDs...), nodes)
	d := addrOf(nodes, "D")
	// Links still coming up after a start: not an outage.
	now = now.Add(5 * time.Second)
	runs.evaluate(firstDown)
	if s := runs.status()[0]; s.State != "starting" {
		t.Fatalf("while links come up: %s", s.State)
	}
	now = now.Add(5 * time.Second)
	runs.evaluate(allUp)
	if _, ok := tab.local(nodes.self, d); !ok {
		t.Fatal("up within the start not taken at once")
	}
	// A path that does not come up within pinStart is down: it waits the hold once up.
	c.Paths[0].ID = "p2"
	tab = runs.tableFor(c, pinKeys(pinIDs...), nodes)
	now = now.Add(pinStart)
	runs.evaluate(firstDown)
	runs.evaluate(allUp)
	if _, ok := tab.local(nodes.self, d); ok || runs.status()[0].State != "waiting" {
		t.Fatalf("late path: %s", runs.status()[0].State)
	}
}

func TestFixedPathConfigChecks(t *testing.T) {
	for _, tc := range []struct {
		name string
		p    FixedPath
	}{
		{"no via", FixedPath{ID: "p", From: []string{"m1"}, To: []string{"m2"}}},
		{"member in via", FixedPath{ID: "p", From: []string{"r1"}, To: []string{"r2"}, Via: []string{"m1"}}},
		{"repeated via", FixedPath{ID: "p", From: []string{"m1"}, To: []string{"m2"}, Via: []string{"r1", "r1"}}},
		{"end in via", FixedPath{ID: "p", From: []string{"r1"}, To: []string{"m2"}, Via: []string{"r1"}}},
		{"unknown end", FixedPath{ID: "p", From: []string{"nobody"}, To: []string{"m2"}, Via: []string{"r1"}}},
		{"no id", FixedPath{From: []string{"m1"}, To: []string{"m2"}, Via: []string{"r1"}}},
		{"rank past the mark", FixedPath{ID: "p", From: []string{"m1"}, To: []string{"m2"}, Via: []string{"r1"}, Rank: maxPathRank + 1}},
	} {
		c := &Config{Self: Self{ID: "m1", Role: roleMember}, Nodes: []Node{{ID: "r1", Role: roleRelay}, {ID: "r2", Role: roleRelay}, {ID: "m2", Role: roleMember}}, Paths: []FixedPath{tc.p}}
		if c.checkPaths() == nil {
			t.Errorf("%s accepted", tc.name)
		}
	}
	ok := &Config{Self: Self{ID: "m1", Role: roleMember}, Nodes: []Node{{ID: "r1", Role: roleRelay}, {ID: "m2", Role: roleMember}},
		Paths: []FixedPath{{ID: "p", From: []string{"m1"}, To: []string{"m2"}, Via: []string{"r1"}}}}
	if err := ok.checkPaths(); err != nil {
		t.Fatal(err)
	}
}

// A marked packet goes over the path of its rank while that path's links are
// up, whatever the relay judges in use; rank 0 is the routes. The ranks are
// the config's, also for a relay that is on only some of the pair's paths.
func TestFixedPathMarkedTakesItsRankWhileItsLinksAreUp(t *testing.T) {
	nodes := pinNodes("X", pinIDs...)
	runs := newPinRuns()
	tab := runs.tableFor(&Config{Self: Self{ID: "X"}, Paths: []FixedPath{
		{ID: "p1", From: []string{"S"}, To: []string{"D"}, Via: []string{"X", "Y"}, Rank: 1},
		{ID: "p3", From: []string{"S"}, To: []string{"D"}, Via: []string{"X", "Z"}, Rank: 3},
	}}, pinKeys(pinIDs...), nodes)
	s, d := addrOf(nodes, "S"), addrOf(nodes, "D")
	if _, ok := tab.marked(s, d, "S", 1); ok {
		t.Fatal("marked before the links were found up")
	}
	runs.evaluate(allUp)
	for rank, next := range map[int]string{1: "Y", 3: "Z", 0: "", 2: ""} {
		h, ok := tab.marked(s, d, "S", rank)
		if ok != (next != "") || ok && h.nextID != next {
			t.Fatalf("rank %d: %+v %v, want %q", rank, h, ok, next)
		}
	}
	if _, ok := tab.marked(s, d, "Y", 1); ok {
		t.Fatal("marked from a node off the path")
	}
	// X-Y down: rank 1 is routed as usual; rank 3 has its links up.
	runs.evaluate(func(n []string) []string {
		if n[2] == "Y" {
			return n[1:3]
		}
		return nil
	})
	if _, ok := tab.marked(s, d, "S", 1); ok {
		t.Fatal("rank 1 with X-Y down")
	}
	if h, ok := tab.marked(s, d, "S", 3); !ok || h.nextID != "Z" {
		t.Fatalf("rank 3: %+v %v", h, ok)
	}
}

func TestFixedPathMarkInTheOuterHeader(t *testing.T) {
	b := outerPacket(netip.MustParseAddr("100.96.0.1"), netip.MustParseAddr("100.96.0.4"), make([]byte, 32))
	p, _ := parseIPv4(b)
	if _, marked := p.pathRank(); marked {
		t.Fatal("a fresh packet marked")
	}
	for _, rank := range []int{0, 3} {
		markPath(b, rank)
		p, ok := parseIPv4(b)
		if got, marked := p.pathRank(); !ok || !marked || got != rank {
			t.Fatalf("rank %d: %d %v %v", rank, got, marked, ok)
		}
		if headerChecksum(b[:20]) != 0 {
			t.Fatalf("rank %d: checksum % x", rank, b[10:12])
		}
	}
}

// A path whose links are up but whose probes go unanswered is down at the
// source: the pair takes the next path at once and comes back once the probes
// have been answered again for pinHold. Events and status say it was silent.
func TestFixedPathSilentFallsBackAndComesBack(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	nodes := pinNodes("S", pinIDs...)
	runs := newPinRuns()
	runs.now = func() time.Time { return now }
	tab := runs.tableFor(&Config{Self: Self{ID: "S"}, Paths: []FixedPath{
		{ID: "p1", From: []string{"S"}, To: []string{"D"}, Via: []string{"X", "Y"}, Rank: 1},
		{ID: "p2", From: []string{"S"}, To: []string{"D"}, Via: []string{"Z"}, Rank: 2},
	}}, pinKeys(pinIDs...), nodes)
	d := addrOf(nodes, "D")
	answering := map[int]bool{1: true, 2: true}
	step := func(dt time.Duration) ([]pinSwitch, string) {
		now = now.Add(dt)
		for _, p := range runs.probes() {
			if p.dst != "D" || int(p.id[0]) != p.rank {
				t.Fatalf("probe %+v", p)
			}
			if answering[p.rank] {
				tab.answer(nodes.self, d, p.rank, binary.BigEndian.Uint32(p.id[1:5]))
			}
		}
		sw := runs.evaluate(allUp)
		via := ""
		if h, ok := tab.local(nodes.self, d); ok {
			via = h.run.path
		}
		return sw, via
	}
	states := func() string {
		out := ""
		for _, s := range runs.status() {
			out += fmt.Sprintf("%d:%s%v ", s.Rank, s.State, s.Silent)
		}
		return out
	}
	step(0)
	if _, via := step(time.Second); via != "p1" {
		t.Fatalf("all answering: %s", via)
	}
	// p1 stops answering: still in use within pathSilent of the first probe
	// left unanswered, then down.
	answering[1] = false
	step(time.Second)
	if _, via := step(pathSilent - time.Second); via != "p1" {
		t.Fatalf("within pathSilent: %s", via)
	}
	sw, via := step(time.Second)
	if via != "p2" || !slices.EqualFunc(sw, []pinSwitch{{dst: "D", from: 1, to: 2, silent: true}}, func(a, b pinSwitch) bool { return pinSwitchEqual(a, b) && a.silent == b.silent }) {
		t.Fatalf("silent: %s %+v", via, sw)
	}
	if st := states(); st != "1:fallbacktrue 2:pinnedfalse " {
		t.Fatalf("silent: %s", st)
	}
	// Answering again: p1 waits out the hold, then the pair is back on it.
	answering[1] = true
	if _, via := step(time.Second); via != "p2" || states() != "1:waitingfalse 2:pinnedfalse " {
		t.Fatalf("answering again: %s %s", via, states())
	}
	if sw, via := step(pinHold); via != "p1" || len(sw) != 1 || sw[0].to != 1 {
		t.Fatalf("after the hold: %s %+v", via, sw)
	}
	// A probe outstanding when a link goes down does not count once it is up.
	answering[1] = false
	runs.probes()
	runs.evaluate(firstDown)
	answering[1] = true
	if _, via := step(pathSilent); via != "" || states() != "1:waitingfalse 2:waitingfalse " {
		t.Fatalf("after a link outage: %s %s", via, states())
	}
}

// A path to a destination that does not answer probes (an older node) is not
// probed: in use while its links are up, and never silent.
func TestFixedPathNotProbedGoesByItsLinks(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	nodes := pinNodes("S", pinIDs...)
	runs := newPinRuns()
	runs.now = func() time.Time { return now }
	tab := runs.tableFor(&Config{Self: Self{ID: "S"}, Paths: []FixedPath{{ID: "p1", From: []string{"S"}, To: []string{"D"}, Via: []string{"X"}, Rank: 1, NoProbe: true}}}, pinKeys(pinIDs...), nodes)
	d := addrOf(nodes, "D")
	runs.evaluate(allUp)
	if p := runs.probes(); len(p) != 0 {
		t.Fatalf("probed: %+v", p)
	}
	now = now.Add(pinStart)
	runs.evaluate(allUp)
	if s := runs.status()[0]; s.State != "pinned" || s.Silent {
		t.Fatalf("not probed: %+v", s)
	}
	if h, ok := tab.local(nodes.self, d); !ok || h.run.path != "p1" {
		t.Fatal("not in use")
	}
	runs.evaluate(firstDown)
	if _, ok := tab.local(nodes.self, d); ok {
		t.Fatal("in use with a link down")
	}
}

// A path just configured that has not answered yet is still coming up: off
// while silent, but in use at its first answer, without the hold.
func TestFixedPathSilentBeforeItsFirstAnswerIsNoOutage(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	nodes := pinNodes("S", pinIDs...)
	runs := newPinRuns()
	runs.now = func() time.Time { return now }
	tab := runs.tableFor(&Config{Self: Self{ID: "S"}, Paths: []FixedPath{{ID: "p1", From: []string{"S"}, To: []string{"D"}, Via: []string{"X"}, Rank: 1}}}, pinKeys(pinIDs...), nodes)
	d := addrOf(nodes, "D")
	runs.evaluate(allUp)
	probe := runs.probes()[0]
	now = now.Add(pathSilent)
	runs.evaluate(allUp)
	if s := runs.status()[0]; s.State != "starting" || !s.Silent {
		t.Fatalf("silent before an answer: %+v", s)
	}
	if _, ok := tab.local(nodes.self, d); ok {
		t.Fatal("in use while silent")
	}
	tab.answer(nodes.self, d, 1, binary.BigEndian.Uint32(probe.id[1:5]))
	runs.evaluate(allUp)
	if _, ok := tab.local(nodes.self, d); !ok || runs.status()[0].State != "pinned" {
		t.Fatalf("first answer: %+v", runs.status()[0])
	}
	// Never answered within pinStart: down, and the hold once it answers.
	tab = runs.tableFor(&Config{Self: Self{ID: "S"}, Paths: []FixedPath{{ID: "p2", From: []string{"S"}, To: []string{"D"}, Via: []string{"X"}, Rank: 1}}}, pinKeys(pinIDs...), nodes)
	runs.evaluate(allUp)
	probe = runs.probes()[0]
	now = now.Add(pinStart)
	runs.evaluate(allUp)
	if s := runs.status()[0]; s.State != "fallback" || !s.Silent {
		t.Fatalf("silent past pinStart: %+v", s)
	}
	tab.answer(nodes.self, d, 1, binary.BigEndian.Uint32(probe.id[1:5]))
	runs.evaluate(allUp)
	if s := runs.status()[0]; s.State != "waiting" {
		t.Fatalf("answered after pinStart: %+v", s)
	}
}
