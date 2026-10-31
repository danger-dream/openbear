package mesh

import (
	"encoding/json"
	"fmt"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"testing"
	"time"
)

// readStatus decodes status.json as the controller does, field names and all.
func readStatus(t *testing.T, dir string) map[string]any {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(dir, "status.json"))
	if err != nil {
		t.Fatal(err)
	}
	var v map[string]any
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	return v
}

func TestStatusCPUPercent(t *testing.T) {
	var c cpuSampler
	if got := c.percent(time.Unix(100, 0), time.Second, true); got != 0 {
		t.Fatalf("first sample %v, want 0", got)
	}
	if got := c.percent(time.Unix(102, 0), 4*time.Second, true); got != 150 {
		t.Fatalf("1.5 cores for 2 s gave %v", got)
	}
	if got := c.percent(time.Unix(103, 0), 0, false); got != 0 {
		t.Fatalf("unknown CPU time gave %v", got)
	}

	// This process, burning about one core between two status writes.
	r := &runner{dir: t.TempDir(), started: time.Now(), events: &eventLog{}}
	r.write(Status{})
	for end := time.Now().Add(300 * time.Millisecond); time.Now().Before(end); {
	}
	r.write(Status{})
	res := readStatus(t, r.dir)["resources"].(map[string]any)
	got, ok := res["cpuPercent"].(float64)
	if !ok {
		t.Fatalf("no resources.cpuPercent number in %v", res)
	}
	if _, known := processCPU(); known && (got < 30 || got > 100*float64(runtime.NumCPU())+50) {
		t.Fatalf("cpuPercent %v while one goroutine spun", got)
	}
	t.Logf("cpuPercent %v", got)
}

func TestStatusSkippedRoutes(t *testing.T) {
	// A gateway subnet over one of this machine's networks.
	var lan netip.Prefix
	ifaces, _ := net.Interfaces()
	for _, ifc := range ifaces {
		addrs, _ := ifc.Addrs()
		for _, a := range addrs {
			if n, ok := a.(*net.IPNet); ok && n.IP.To4() != nil && !n.IP.IsLoopback() && !lan.IsValid() {
				addr, _ := netip.AddrFromSlice(n.IP.To4())
				if p := netip.PrefixFrom(addr, 22).Masked(); !p.Overlaps(meshPrefix) {
					lan = p
				}
			}
		}
	}
	if !lan.IsValid() {
		t.Skip("no IPv4 network on this machine")
	}
	s := spec{relays: []string{"R"}, members: map[string][]string{"B": {"R"}, "C": {"R"}},
		subnets: map[string][]netip.Prefix{"B": {lan}}, rules: map[string][]Rule{"B": allowICMP, "C": allowICMP}}
	n := newLab(t, s).up()
	if got := n["C"].status().SkippedRoutes; !slices.Equal(got, []string{lan.String()}) {
		t.Fatalf("C skipped %q, want %s", got, lan)
	}
	if got := n["B"].status().SkippedRoutes; got == nil || len(got) != 0 {
		t.Fatalf("B skipped %#v, want []", got)
	}
	r := &runner{dir: t.TempDir(), started: time.Now(), events: &eventLog{}}
	r.write(n["C"].status())
	if got := fmt.Sprint(readStatus(t, r.dir)["skippedRoutes"]); got != "["+lan.String()+"]" {
		t.Fatalf("status.json skippedRoutes %s", got)
	}
	r.write(Status{})
	if got, ok := readStatus(t, r.dir)["skippedRoutes"].([]any); !ok || len(got) != 0 {
		t.Fatalf("status.json without an engine: skippedRoutes %#v, want []", readStatus(t, r.dir)["skippedRoutes"])
	}
}

func TestStatusHostFirewall(t *testing.T) {
	for out, want := range map[string]string{
		"Firewall is enabled. (State = 1)\n":  "on",
		"Firewall is enabled. (State = 2)\n":  "on",
		"Firewall is disabled. (State = 0)\n": "",
		"Firewall is enabled.\n":              "on",
		"":                                    "",
	} {
		if got := firewallState(out); got != want {
			t.Errorf("%q: %q, want %q", out, got, want)
		}
	}
	calls := make(chan struct{}, 10)
	c := &cachedCheck{every: time.Hour, check: func() string { calls <- struct{}{}; return "on" }}
	c.get()
	deadline := time.Now().Add(2 * time.Second)
	for c.get() != "on" {
		if time.Now().After(deadline) {
			t.Fatal("the check result never showed")
		}
		time.Sleep(10 * time.Millisecond)
	}
	for i := 0; i < 100; i++ {
		c.get()
	}
	if len(calls) != 1 {
		t.Fatalf("checked %d times within the interval", len(calls))
	}
	r := &runner{dir: t.TempDir(), started: time.Now(), events: &eventLog{}}
	r.write(Status{})
	got, ok := readStatus(t, r.dir)["hostFirewall"].(string)
	if !ok {
		t.Fatal("no hostFirewall string")
	}
	if runtime.GOOS != "darwin" && got != "" {
		t.Fatalf("hostFirewall %q on %s", got, runtime.GOOS)
	}
}

func TestConnTrackCountsNewConnections(t *testing.T) {
	var tr connTrack
	known := map[string]bool{"R": true, "M": true}
	peer := func(c streamConn) string { return c.peer }
	out := func(id string) streamConn {
		return streamConn{peer: "R", endpoint: "tcp://r:1", transport: "tcp", id: id, outbound: true}
	}
	in := func(id string) streamConn {
		return streamConn{peer: "M", endpoint: "tcp://m:" + id, transport: "tcp", id: id}
	}
	steps := []struct {
		conns []streamConn
		r, m  int
	}{
		{nil, 0, 0},
		{[]streamConn{out("a/1"), in("x")}, 0, 0},          // first connections
		{[]streamConn{out("a/1"), in("x"), in("y")}, 0, 0}, // a second one alongside
		{[]streamConn{in("y")}, 0, 0},                      // down
		{[]streamConn{out("a/2"), in("y"), in("z")}, 1, 1}, // back
		{[]streamConn{out("a/2"), in("y"), in("z")}, 1, 1},
		{[]streamConn{out("b/1"), in("w"), in("v")}, 2, 3},
	}
	for i, s := range steps {
		tr.update(s.conns, peer, known)
		if tr.count["R"] != s.r || tr.count["M"] != s.m {
			t.Fatalf("step %d: R %d M %d, want %d %d", i, tr.count["R"], tr.count["M"], s.r, s.m)
		}
	}
	tr.update(nil, peer, map[string]bool{"R": true})
	if _, ok := tr.count["M"]; ok || len(tr.slots) != 1 {
		t.Fatal("a removed peer is still tracked")
	}
}

// A member's TCP entry is cut: both ends count the new connection, and the
// UDP member counts none.
func TestStatusReconnects(t *testing.T) {
	r := newRecoveryLab(t, "A", "B")
	proxy := newTCPProxy(t, fmt.Sprintf("127.0.0.1:%d", r.tcp["A"]))
	r.via["M/A"] = "tcp://" + proxy.Addr().String()
	r.up()
	defer r.dump()
	r.waitSettled(t, map[string]string{"M": "A", "N": "A"}, 20*time.Second)
	reconnects := func(node, peer string) int {
		for _, p := range r.nodes[node].status().Peers {
			if p.ID == peer {
				return p.Reconnects
			}
		}
		t.Fatalf("%s has no peer %s", node, peer)
		return 0
	}
	all := func() string {
		var b strings.Builder
		for _, k := range []string{"M/A", "A/M", "N/A", "N/B", "A/N"} {
			from, to, _ := strings.Cut(k, "/")
			fmt.Fprintf(&b, "%s=%d ", k, reconnects(from, to))
		}
		return b.String()
	}
	if got := all(); got != "M/A=0 A/M=0 N/A=0 N/B=0 A/N=0 " {
		t.Fatalf("before the cut: %s", got)
	}
	if proxy.cut() == 0 {
		t.Fatal("no connection to cut")
	}
	healthWait(t, 15*time.Second, func() bool { return reconnects("M", "A") >= 1 && reconnects("A", "M") >= 1 })
	t.Log("after the cut:", all())
	for _, k := range []string{"N/A", "N/B", "A/N"} {
		from, to, _ := strings.Cut(k, "/")
		if n := reconnects(from, to); n != 0 {
			t.Fatalf("UDP peer %s reconnected %d times", k, n)
		}
	}
}

// residentBytes on macOS: the platform test is host_darwin_test.go.
func TestGoMappedBytes(t *testing.T) {
	var m runtime.MemStats
	runtime.ReadMemStats(&m)
	got := goMappedBytes()
	if got < m.HeapInuse || got > m.Sys {
		t.Fatalf("goMappedBytes %d: heap in use %d, from the OS %d", got, m.HeapInuse, m.Sys)
	}
}

// status.json counts the signatures a node checked: the relay a member
// attaches to checks its claim, the other relay the notice of it.
func TestStatusSecurity(t *testing.T) {
	l := newHealthLab(t, spec{relays: []string{"A", "B"}, members: map[string][]string{"M": {"A"}}, rules: map[string][]Rule{"A": allowICMP, "B": allowICMP, "M": allowICMP}})
	nodes := map[string]*running{}
	for _, name := range []string{"A", "B", "M"} {
		nodes[name] = l.startHealthNode(name)
	}
	healthWait(t, 15*time.Second, func() bool { return nodes["B"].status().Owners["M"].Relay == "A" })
	for _, id := range []string{"A", "B"} {
		if sec := nodes[id].status().Security; sec == nil || sec.Signing.Verified == 0 || sec.Signing.Rejected != 0 || sec.Claims.Expired != 0 {
			t.Fatalf("%s security %+v", id, sec)
		}
	}
	r := &runner{dir: t.TempDir(), started: time.Now(), events: &eventLog{}}
	r.write(nodes["B"].status())
	got, _ := readStatus(t, r.dir)["security"].(map[string]any)
	signing, _ := got["signing"].(map[string]any)
	claims, _ := got["claims"].(map[string]any)
	if v, _ := signing["verified"].(float64); v < 1 || signing["rejected"] != float64(0) || claims["expired"] != float64(0) || signing["byHop"] != nil {
		t.Fatalf("status.json security %v", got)
	}
	r.write(Status{})
	if _, ok := readStatus(t, r.dir)["security"]; ok {
		t.Fatal("status.json without an engine has a security section")
	}
}
