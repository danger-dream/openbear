package mesh

import (
	"encoding/json"
	"fmt"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestOnlineUpdateLeavesOtherSessionsAlone(t *testing.T) {
	open := []Rule{{Proto: "icmp"}, {Proto: "tcp", Ports: [][2]uint16{{7777, 7778}}}}
	l := newLab(t, twoMembers(allowICMP, open))
	n := l.up()
	a, b, r := n["A"], n["B"], n["R"]
	A, B := l.addr("A"), l.addr("B")
	// The relay, which dials no member, has a handshake with one only from
	// the first packet after it: up waits for the members' side alone.
	healthWait(t, 5*time.Second, func() bool { return handshakeOf(r, "A") > 0 && handshakeOf(r, "B") > 0 })
	before := map[string]int64{"A": handshakeOf(r, "A"), "B": handshakeOf(r, "B")}

	// The relay learns of a new member; nothing else about it changes.
	_, newPub := newKey(t)
	next := l.config("R")
	next.Revision = 2
	next.Nodes = append(next.Nodes, Node{ID: "N", Role: roleMember, Address: netip.MustParseAddr("100.96.0.99"),
		ControlAddress: netip.MustParseAddr("100.96.255.99"), PublicKey: newPub, SignKey: testSignPub("N"), E2EKey: testE2EPub("N"), Relays: []string{"R"}})
	old := slices.Clone(r.peers)
	diff, _ := peerDiff(old, mustPlan(t, next), r.wire)
	if strings.Count(diff, "public_key=") != 1 {
		t.Fatalf("diff touches untouched peers:\n%s", diff)
	}
	if err := r.applyConfig(next); err != nil {
		t.Fatal(err)
	}
	if got := allowedOf(r, "N"); !slices.Equal(got, []netip.Prefix{netip.MustParsePrefix("100.96.0.99/32")}) {
		t.Fatalf("N carries %v", got)
	}
	send(a, packet(A, B, protoICMP, 64, 1, 0, false, icmp(8, 7, nil)))
	expect(t, b, "A to B after a member was added at the relay")
	for name, h := range before {
		if handshakeOf(r, name) != h {
			t.Fatalf("session with %s was renewed by the update", name)
		}
	}

	// B opens 7777 and 7778 to A, then revokes 7778: the open connection on
	// it stops, the other one and B's own connections keep going.
	for _, p := range []uint16{7777, 7778} {
		send(a, packet(A, B, protoTCP, 64, p, 0, false, ports(40000+p, p, 20)))
		expect(t, b, fmt.Sprintf("A to B:%d", p))
		send(b, packet(B, A, protoTCP, 64, p, 0, false, ports(p, 40000+p, 20)))
		expect(t, a, fmt.Sprintf("B:%d reply", p))
	}
	send(b, packet(B, A, protoICMP, 64, 9, 0, false, icmp(8, 9, nil)))
	expect(t, a, "B's ping to A")
	nb := l.config("B")
	nb.Revision = 2
	nb.Rules = []Rule{{Proto: "icmp"}, {Proto: "tcp", Ports: [][2]uint16{{7777, 7777}}}}
	if err := b.applyConfig(nb); err != nil {
		t.Fatal(err)
	}
	send(a, packet(A, B, protoTCP, 64, 1, 0, false, ports(47778, 7778, 20)))
	expectNone(t, b, "revoked 7778 connection")
	send(a, packet(A, B, protoTCP, 64, 2, 0, false, ports(47777, 7777, 20)))
	expect(t, b, "7777 connection after the revocation")
	send(a, packet(A, B, protoICMP, 64, 3, 0, false, icmp(0, 9, nil)))
	expect(t, b, "reply to B's own ping")
}

func mustPlan(t *testing.T, c *Config) []peerSpec {
	t.Helper()
	if err := c.check(); err != nil {
		t.Fatal(err)
	}
	p, err := planPeers(c, newStaticAttachment(c))
	if err != nil {
		t.Fatal(err)
	}
	return p
}

func TestFailedUpdateKeepsTheOldConfig(t *testing.T) {
	l := newLab(t, twoMembers(allowICMP, allowICMP))
	n := l.up()
	a, b := n["A"], n["B"]
	dir := l.dirs["A"]
	raw, _ := json.Marshal(a.cfg)
	os.WriteFile(filepath.Join(dir, "config.json"), raw, 0o600)
	run := &runner{dir: dir, started: time.Now(), events: a.events}
	run.reload(a.engine)
	run.applied = 1

	step := 0
	write := func(c any) {
		step++
		raw, _ := json.Marshal(c)
		path := filepath.Join(dir, "config.json")
		os.WriteFile(path+".tmp", raw, 0o600)
		os.Rename(path+".tmp", path)
		later := time.Now().Add(time.Duration(step) * time.Second)
		os.Chtimes(path, later, later)
		run.reload(a.engine)
	}
	// A TCP listener on a port that is taken, then an unknown schema.
	busy, err := net.Listen("tcp", ":0")
	if err != nil {
		t.Fatal(err)
	}
	defer busy.Close()
	bad := l.config("A")
	bad.Revision = 2
	bad.Self.Listen.TCP = busy.Addr().(*net.TCPAddr).Port
	bad.Rules = nil
	write(bad)
	if run.revision != 2 || run.applied != 1 || !strings.Contains(run.err, "in use") {
		t.Fatalf("revision %d applied %d error %q", run.revision, run.applied, run.err)
	}
	send(a, packet(l.addr("A"), l.addr("B"), protoICMP, 64, 1, 0, false, icmp(8, 1, nil)))
	expect(t, b, "A to B on the old config")
	send(b, packet(l.addr("B"), l.addr("A"), protoICMP, 64, 2, 0, false, icmp(8, 2, nil)))
	expect(t, a, "old rules still apply at A")

	future := map[string]any{"schema": ConfigSchema + 1, "revision": 3}
	write(future)
	if run.revision != 3 || run.applied != 1 || !strings.Contains(run.err, fmt.Sprintf("schema %d", ConfigSchema+1)) {
		t.Fatalf("revision %d applied %d error %q", run.revision, run.applied, run.err)
	}
	good := l.config("A")
	good.Revision = 4
	write(good)
	if run.revision != 4 || run.applied != 4 || run.err != "" {
		t.Fatalf("revision %d applied %d error %q", run.revision, run.applied, run.err)
	}
	run.write(Status{ConfigRevision: run.revision, AppliedRevision: run.applied})
	var st Status
	raw, _ = os.ReadFile(filepath.Join(dir, "status.json"))
	if err := json.Unmarshal(raw, &st); err != nil || st.PID != os.Getpid() || st.AppliedRevision != 4 || len(st.Events) == 0 {
		t.Fatalf("status.json %s", raw)
	}
}

// fakeAttach answers what a test sets and signals each change.
type fakeAttach struct {
	mu      sync.Mutex
	owners  map[string]string
	entry   string
	changes chan struct{}
}

func (f *fakeAttach) Owner(m string) (string, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	r, ok := f.owners[m]
	return r, ok
}

func (f *fakeAttach) ActiveEntry() (string, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.entry, f.entry != ""
}

func (f *fakeAttach) Changes() <-chan struct{} { return f.changes }

func (f *fakeAttach) set(fn func()) {
	f.mu.Lock()
	fn()
	f.mu.Unlock()
	f.changes <- struct{}{}
}

func TestAttachmentChangesMoveRoutesOnline(t *testing.T) {
	s := spec{relays: []string{"R1", "R2"}, members: map[string][]string{"A": {"R1", "R2"}, "B": {"R2"}},
		rules: map[string][]Rule{"A": allowICMP, "B": allowICMP}}
	l := newLab(t, s)
	n := map[string]*running{}
	att := map[string]*fakeAttach{}
	for _, name := range []string{"R1", "R2", "A", "B"} {
		f := &fakeAttach{owners: map[string]string{"A": "R1", "B": "R2"}, changes: make(chan struct{}, 1)}
		if name == "A" || name == "B" {
			f.entry = "c1"
		}
		att[name] = f
		n[name] = l.start(name, f)
	}
	for _, x := range n {
		for _, p := range x.peers {
			if p.Endpoint != "" {
				kick(x.dev, p.Key)
			}
		}
	}
	waitHandshakes(t, n)
	A, B, R1, R2 := l.addr("A"), l.addr("B"), l.addr("R1"), l.addr("R2")
	ping := func(id uint16, from, to string) {
		t.Helper()
		send(n[from], packet(l.addr(from), l.addr(to), protoICMP, 64, id, 0, false, icmp(8, id, nil)))
		expect(t, n[to], from+" to "+to)
	}
	ping(1, "A", "B")
	if got := allowedOf(n["R2"], "R1"); !slices.Equal(got, sortPrefixes([]netip.Prefix{host(R1), host(A)})) {
		t.Fatalf("R2 reaches A through R1 with %v", got)
	}
	if got := allowedOf(n["R2"], "A"); len(got) != 0 {
		t.Fatalf("R2 routes to A directly while R1 owns it: %v", got)
	}

	// A moves to R2: its own entry and both relays' ownership change, each
	// applied online when the attachment signals.
	for _, name := range []string{"R1", "R2", "A"} {
		x, f := n[name], att[name]
		f.set(func() {
			f.owners["A"] = "R2"
			if name == "A" {
				f.entry = "c2"
			}
		})
		<-f.Changes()
		if err := x.reroute(); err != nil {
			t.Fatal(err)
		}
	}
	waitHandshakes(t, map[string]*running{"A": n["A"]})
	if got := allowedOf(n["R2"], "A"); !slices.Equal(got, []netip.Prefix{host(A)}) {
		t.Fatalf("R2 does not route to A after the move: %v", got)
	}
	if got := allowedOf(n["R1"], "R2"); !slices.Equal(got, sortPrefixes([]netip.Prefix{host(R2), host(A), host(B)})) {
		t.Fatalf("R1 reaches A through R2 with %v", got)
	}
	if got := allowedOf(n["A"], "R2"); !slices.Equal(got, []netip.Prefix{meshPrefix}) || allowedOf(n["A"], "R1") != nil {
		t.Fatalf("A's entry R2 carries %v, R1 %v", got, allowedOf(n["A"], "R1"))
	}
	ping(2, "A", "B")
	ping(3, "B", "A")
	if st := n["A"].status(); st.Entry.Candidate != "c2" || st.Entry.Switches != 1 {
		t.Fatalf("entry status %+v", st.Entry)
	}
}

func TestRestoredAddressStillLearns(t *testing.T) {
	l := newLab(t, twoMembers(nil, nil))
	l.dirs["R"] = t.TempDir()
	if err := saveEndpoints(l.dirs["R"], map[string]string{"A": "127.0.0.1:9"}); err != nil {
		t.Fatal(err)
	}
	r := l.start("R", nil)
	_, st := deviceState(r.dev)
	if got := st[hexOf(t, l.nodes["A"].pub)].endpoint; stripScheme(got) != "127.0.0.1:9" {
		t.Fatalf("restored endpoint %q", got)
	}
	a := l.start("A", nil)
	waitHandshakes(t, map[string]*running{"A": a})
	want := fmt.Sprintf("127.0.0.1:%d", l.nodes["A"].port)
	deadline := time.Now().Add(5 * time.Second)
	for r.learnedEndpoints()["A"] != want {
		if time.Now().After(deadline) {
			t.Fatalf("relay kept %q instead of learning %s", r.learnedEndpoints()["A"], want)
		}
		time.Sleep(20 * time.Millisecond)
	}
	if err := saveEndpoints(l.dirs["R"], r.learnedEndpoints()); err != nil {
		t.Fatal(err)
	}
	if loadState(l.dirs["R"]).Endpoints["A"] != want {
		t.Fatal("state.json lost the learned address")
	}
}

func hexOf(t *testing.T, b64 string) string {
	k, err := keyHex(b64)
	if err != nil {
		t.Fatal(err)
	}
	return k
}

// A dialing member keeps its configured endpoint even when the relay's
// packets arrive from elsewhere; the relay keeps learning the member's.
func TestDialedEndpointStaysFixed(t *testing.T) {
	l := newLab(t, twoMembers(allowICMP, allowICMP))
	n := l.up()
	a, r := n["A"], n["R"]
	// up waits for the members' handshakes only. R holds a session it
	// answered once the member's first packet after it arrives; that packet
	// lost to the move, R may not send to B for RekeyTimeout after its
	// response, all of expect's wait.
	for _, m := range []string{"A", "B"} {
		for deadline := time.Now().Add(5 * time.Second); ; time.Sleep(20 * time.Millisecond) {
			if _, st := deviceState(r.dev); st[hexOf(t, l.nodes[m].pub)] != nil && st[hexOf(t, l.nodes[m].pub)].handshakeNano != 0 {
				break
			}
			if time.Now().After(deadline) {
				t.Fatalf("R's session with %s was not confirmed", m)
			}
		}
	}
	configured := strings.TrimPrefix(l.endpoint("R"), "udp://")
	moved := l.config("R")
	moved.Revision = 2
	moved.Self.Listen.UDP = freePort(t)
	if err := r.applyConfig(moved); err != nil {
		t.Fatal(err)
	}
	send(r, packet(l.addr("R"), l.addr("A"), protoICMP, 64, 1, 0, false, icmp(8, 1, nil)))
	expect(t, a, "R's ping from its new port")
	_, st := deviceState(a.dev)
	if got := stripScheme(st[hexOf(t, l.nodes["R"].pub)].endpoint); got != configured {
		t.Fatalf("A's endpoint for R moved to %s", got)
	}

	ma := l.config("A")
	ma.Revision = 2
	ma.Self.Listen.UDP = freePort(t)
	if err := r.applyConfig(l.config("R")); err != nil {
		t.Fatal(err)
	}
	if err := a.applyConfig(ma); err != nil {
		t.Fatal(err)
	}
	send(a, packet(l.addr("A"), l.addr("B"), protoICMP, 64, 2, 0, false, icmp(8, 2, nil)))
	expect(t, n["B"], "A from its new port")
	if got := r.learnedEndpoints()["A"]; got != fmt.Sprintf("127.0.0.1:%d", ma.Self.Listen.UDP) {
		t.Fatalf("relay did not learn A's new port: %s", got)
	}
}
