package mesh

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
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

// loadConfigs builds the load test's configs with the controller's own code
// (app/servers/mesh_config.py). Input on stdin: keys, underlay addresses,
// upload limit and revision; output: {"configs": ..., "errors": ...}.
const loadConfigs = `
import json, sys
sys.path.insert(0, '.')
from app.servers import mesh_model as model
from app.servers.mesh_config import build

given = json.load(sys.stdin)
def member(sid, role='member', **changes):
    m = model.new_member(sid, model.default_settings())
    m['role'] = role
    m.update(changes)
    return m
doc = model.empty_doc()
doc['members'] = [
    member('r1', 'relay', listen={'udp': 51999, 'tcp': 52000, 'tls': 0}),
    member('m1', uploadLimitMbps=given['uploadLimitMbps'],
           entry={'mode': 'fixed', 'candidates': [{'id': 'c1', 'relay': 'r1', 'transport': 'tcp'}], 'autoReturn': True}),
    *(member(sid, entry={'mode': 'fixed', 'candidates': [{'id': 'c1', 'relay': 'r1', 'transport': 'udp'}], 'autoReturn': True})
      for sid in ('m2', 'm3')),
]
doc['rules'] = [{'id': 'load', 'src': {'members': ['m1'], 'tags': [], 'prefixes': []},
                 'dst': {'members': ['r1'], 'tags': [], 'subnets': []}, 'proto': 'any', 'ports': [], 'note': ''},
                {'id': 'bulk', 'src': {'members': ['m2'], 'tags': [], 'prefixes': []},
                 'dst': {'members': ['r1', 'm3'], 'tags': [], 'subnets': []}, 'proto': 'any', 'ports': [], 'note': ''}]
doc = model.allocate(doc)
facts = {sid: {'name': sid.upper(), 'platform': 'linux', 'ipv4': given['ipv4'][sid], 'publicKey': keys[0], 'controlKey': keys[1],
               'signKey': keys[2], 'e2eKey': keys[3], 'localPrefixes': [], 'tls': None,
               'configSchemas': given['configSchemas']} for sid, keys in given['keys'].items()}
json.dump({'configs': build(doc, facts, 'oblab-load', given['revision']), 'errors': model.validate(doc, facts, applying=True)}, sys.stdout)
`

// Line model and targets (plan section 13).
const (
	loadLineRate    = "50mbit"
	loadLineDelay   = "30ms"          // one way, each direction
	loadLineBurst   = "64kb"          // a few ms of the line at 50 Mbit/s
	loadLineLimit   = "625000"        // bytes: 100 ms of the line's buffer
	loadUploadMbps  = 45.0            // what the user sets on a 50 Mbit/s line
	loadSeconds     = 20              // bulk transfer
	loadMaxP95Rise  = 200.0           // ms, p95 under load minus idle p95
	loadMinMbps     = 35.0            // the line has to be full
	loadMemberRSS   = 160 << 20       // bytes
	loadRelayRSS    = 256 << 20       // bytes
	loadPingGap     = "0.2"           // seconds between pings
	loadIdlePings   = 50              // baseline
	loadBusyPings   = 80              // 16 s inside the 20 s transfer
	loadBusyDelay   = 2 * time.Second // into the transfer before the pings start
	loadFragRate    = "20M"           // fragmented UDP, well within the line
	loadMaxFragLoss = 1.0             // percent of its datagrams
)

type loadResult struct {
	name          string
	p50, p95      float64 // ms; a lost ping counts as infinitely late
	sent, lost    int
	mbps          float64 // received by iperf3; 0 for the baseline
	member, relay uint64  // peak VmRSS
	droppedQueue  uint64  // member's scheduler drops during the run
	retransmits   int     // iperf3's inner TCP
}

// rootMeshState lists the root namespace's links, qdiscs and iptables rules
// that a load test node or its line could leave there.
func rootMeshState() []string {
	var out []string
	if b, err := exec.Command("ip", "-o", "link", "show").Output(); err == nil {
		for _, l := range strings.Split(string(b), "\n") {
			if f := strings.Fields(l); len(f) > 1 {
				if name, _, _ := strings.Cut(strings.TrimSuffix(f[1], ":"), "@"); strings.HasPrefix(name, "obl") || strings.HasPrefix(name, "obmesh") {
					out = append(out, "link "+name)
				}
			}
		}
	}
	if b, err := exec.Command("tc", "qdisc", "show").Output(); err == nil {
		for _, l := range strings.Split(string(b), "\n") {
			if strings.Contains(l, "obl") || strings.Contains(l, "netem") || strings.Contains(l, "tbf") {
				out = append(out, l)
			}
		}
	}
	for _, table := range []string{"filter", "nat"} {
		if b, err := exec.Command("iptables", "-t", table, "-S").Output(); err == nil {
			for _, l := range strings.Split(string(b), "\n") {
				if strings.Contains(l, "OBMESH") {
					out = append(out, table+": "+l)
				}
			}
		}
	}
	return out
}

// TestLoad answers plan section 13 for one member entering a relay over
// TCP on a 50 Mbit/s, 60 ms line: the rise of ping p95 while a bulk TCP
// transfer fills the line with the upload limit at 45 Mbit/s, and the data
// plane processes' peak RSS. A run without the limit is the reference.
// Two more members on unshaped lines of their own measure the data plane's
// throughput between next hops (m2 to r1) and end to end through the relay
// (m2 to m3).
// OBLAB_NETNS=1 OBLAB_LOAD=1 enables it; it needs root, iperf3 and the
// repository's .venv.
func TestLoad(t *testing.T) {
	if os.Getenv("OBLAB_NETNS") != "1" || os.Getenv("OBLAB_LOAD") != "1" || os.Getuid() != 0 {
		t.Skip("set OBLAB_NETNS=1 OBLAB_LOAD=1 and run as root")
	}
	if _, err := exec.LookPath("iperf3"); err != nil {
		t.Skip("iperf3 is not installed")
	}
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	wd, _ := os.Getwd()
	repo := filepath.Clean(filepath.Join(wd, "..", ".."))
	ids := []string{"r1", "m1", "m2", "m3"}
	ns := map[string]string{"r1": "oblab-lr", "m1": "oblab-lm", "m2": "oblab-lm2", "m3": "oblab-lm3"}
	dev := map[string]string{"r1": "obllr", "m1": "obllm", "m2": "obllm2", "m3": "obllm3"}
	underlay := map[string]string{"r1": "10.99.7.1", "m1": "10.99.7.2", "m2": "10.99.7.3", "m3": "10.99.7.4"}
	sh := func(args ...string) string {
		t.Helper()
		out, err := exec.Command(args[0], args[1:]...).CombinedOutput()
		if err != nil {
			t.Fatalf("%s: %v: %s", strings.Join(args, " "), err, out)
		}
		return string(out)
	}
	in := func(id string, args ...string) *exec.Cmd {
		return exec.Command("ip", append([]string{"netns", "exec", ns[id]}, args...)...)
	}
	cleanup := func() {
		for _, n := range ns {
			exec.Command("ip", "netns", "del", n).Run()
		}
	}
	cleanup()
	// This machine may run a mesh node of its own: only what is new in the
	// root namespace is the test's.
	had := rootMeshState()
	t.Cleanup(func() {
		cleanup()
		if out, _ := exec.Command("ip", "netns", "list").Output(); bytes.Contains(out, []byte("oblab-l")) {
			t.Errorf("namespaces left behind: %s", out)
		}
		for _, s := range rootMeshState() {
			if !slices.Contains(had, s) {
				t.Errorf("left behind in the root namespace: %s", s)
			}
		}
	})
	for _, id := range ids {
		sh("ip", "netns", "add", ns[id])
		sh("ip", "-n", ns[id], "link", "set", "lo", "up")
	}
	sh("ip", "link", "add", dev["r1"], "netns", ns["r1"], "type", "veth", "peer", "name", dev["m1"], "netns", ns["m1"])
	for _, id := range []string{"r1", "m1"} {
		sh("ip", "-n", ns[id], "addr", "add", underlay[id]+"/24", "dev", dev[id])
		sh("ip", "-n", ns[id], "link", "set", dev[id], "up")
		// Whole packets through the shaper, as on a real line.
		sh("ip", "netns", "exec", ns[id], "ethtool", "-K", dev[id], "tso", "off", "gso", "off", "gro", "off")
	}
	// The member's uplink: 30 ms, then 50 Mbit/s with 100 ms of buffer.
	sh("ip", "netns", "exec", ns["m1"], "tc", "qdisc", "add", "dev", dev["m1"], "root", "handle", "1:", "netem", "delay", loadLineDelay, "limit", "100000")
	sh("ip", "netns", "exec", ns["m1"], "tc", "qdisc", "add", "dev", dev["m1"], "parent", "1:1", "handle", "10:", "tbf",
		"rate", loadLineRate, "burst", loadLineBurst, "limit", loadLineLimit)
	// The way back: the same 30 ms.
	sh("ip", "netns", "exec", ns["r1"], "tc", "qdisc", "add", "dev", dev["r1"], "root", "netem", "delay", loadLineDelay, "limit", "100000")
	// m2 and m3 reach r1's address over unshaped links of their own, in its
	// subnet: r1 answers them from that address.
	for i, id := range []string{"m2", "m3"} {
		rdev := fmt.Sprintf("obllr%d", i+2)
		sh("ip", "link", "add", rdev, "netns", ns["r1"], "type", "veth", "peer", "name", dev[id], "netns", ns[id])
		sh("ip", "-n", ns["r1"], "link", "set", rdev, "up")
		sh("ip", "-n", ns["r1"], "route", "add", underlay[id]+"/32", "dev", rdev, "src", underlay["r1"])
		sh("ip", "-n", ns[id], "addr", "add", underlay[id]+"/24", "dev", dev[id])
		sh("ip", "-n", ns[id], "link", "set", dev[id], "up")
	}

	dirs, keys := map[string]string{}, map[string][4]string{}
	for _, id := range ids {
		dirs[id] = t.TempDir()
		wgPriv, wgPub := newKey(t)
		ctlPriv, ctlPub := newKey(t)
		keys[id] = [4]string{wgPub, ctlPub, testSignPub(id), testE2EPub(id)}
		os.MkdirAll(filepath.Join(dirs[id], "keys"), 0o700)
		os.WriteFile(filepath.Join(dirs[id], "keys", "wg.key"), []byte(wgPriv), 0o600)
		os.WriteFile(filepath.Join(dirs[id], "keys", "control.key"), []byte(ctlPriv), 0o600)
		writeTestKeys(t, dirs[id], id)
	}
	addr := map[string]string{}
	generate := func(revision int, upload *float64) {
		t.Helper()
		input, _ := json.Marshal(map[string]any{"keys": keys, "ipv4": underlay, "uploadLimitMbps": upload, "revision": revision, "configSchemas": ConfigSchemas()})
		cmd := exec.Command(filepath.Join(repo, ".venv", "bin", "python"), "-c", loadConfigs)
		cmd.Dir, cmd.Stdin = repo, bytes.NewReader(input)
		out, err := cmd.Output()
		if err != nil {
			t.Fatalf("controller config build: %v: %s", err, out)
		}
		var got struct {
			Configs map[string]json.RawMessage `json:"configs"`
			Errors  []map[string]any           `json:"errors"`
		}
		if err := json.Unmarshal(out, &got); err != nil {
			t.Fatal(err)
		}
		if len(got.Errors) > 0 {
			t.Fatalf("the controller refuses its own draft: %v", got.Errors)
		}
		for _, id := range ids {
			c, err := ParseConfig(got.Configs[id])
			if err != nil {
				t.Fatalf("the data plane refuses the controller's config for %s: %v\n%s", id, err, got.Configs[id])
			}
			addr[id] = c.Self.Address.String()
			path := filepath.Join(dirs[id], "config.json")
			os.WriteFile(path+".tmp", got.Configs[id], 0o600)
			if err := os.Rename(path+".tmp", path); err != nil {
				t.Fatal(err)
			}
		}
	}
	status := func(id string) Status {
		var s Status
		raw, _ := os.ReadFile(filepath.Join(dirs[id], "status.json"))
		json.Unmarshal(raw, &s)
		return s
	}
	waitFor := func(what string, ok func() bool) {
		t.Helper()
		for deadline := time.Now().Add(30 * time.Second); !ok(); time.Sleep(200 * time.Millisecond) {
			if time.Now().After(deadline) {
				t.Fatalf("timed out waiting for %s", what)
			}
		}
	}

	limit := loadUploadMbps
	generate(1, &limit)
	procs := map[string]*exec.Cmd{}
	for _, id := range ids {
		log, _ := os.Create(filepath.Join(dirs[id], "mesh.log"))
		cmd := exec.Command("ip", "netns", "exec", ns[id], self, "-test.run", "^TestNetnsNode$", "-test.v")
		cmd.Env = append(os.Environ(), "OBLAB_MESH_DIR="+dirs[id])
		cmd.Stdout, cmd.Stderr = log, log
		if err := cmd.Start(); err != nil {
			t.Fatal(err)
		}
		procs[id] = cmd
	}
	t.Cleanup(func() {
		for id, cmd := range procs {
			cmd.Process.Signal(os.Interrupt)
			done := make(chan error, 1)
			go func() { done <- cmd.Wait() }()
			select {
			case <-done:
			case <-time.After(10 * time.Second):
				cmd.Process.Kill()
				t.Errorf("%s did not stop on SIGINT", id)
			}
		}
		if t.Failed() {
			for _, id := range ids {
				raw, _ := os.ReadFile(filepath.Join(dirs[id], "mesh.log"))
				st, _ := os.ReadFile(filepath.Join(dirs[id], "status.json"))
				t.Logf("%s log:\n%s\nstatus: %s", id, raw, st)
			}
		}
	})
	for _, id := range ids {
		waitFor(id+" ready", func() bool {
			s := status(id)
			return s.Ready && s.AppliedRevision == 1 && s.PID == procs[id].Process.Pid
		})
	}
	for _, p := range status("r1").Peers {
		if p.ID == "m1" && p.Transport != "tcp" {
			t.Fatalf("r1's peer %+v: m1 should come in over tcp", p)
		}
	}

	// iperf3's server stays up for both runs.
	server := in("r1", "iperf3", "-s", "-B", addr["r1"])
	if err := server.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { server.Process.Kill(); server.Wait() })

	rtt := regexp.MustCompile(`time=([0-9.]+) ms`)
	pings := func(count int) (times []float64, lost int) {
		out, _ := in("m1", "ping", "-n", "-i", loadPingGap, "-c", strconv.Itoa(count), "-W", "2", addr["r1"]).Output()
		for _, m := range rtt.FindAllStringSubmatch(string(out), -1) {
			v, _ := strconv.ParseFloat(m[1], 64)
			times = append(times, v)
		}
		return times, count - len(times)
	}
	percentiles := func(r *loadResult, times []float64, lost int) {
		for i := 0; i < lost; i++ {
			times = append(times, math.Inf(1))
		}
		sort.Float64s(times)
		at := func(q float64) float64 { return times[int(math.Ceil(q*float64(len(times))))-1] }
		r.sent, r.lost, r.p50, r.p95 = len(times), lost, at(0.50), at(0.95)
	}
	rss := func(id string) uint64 {
		raw, _ := os.ReadFile(fmt.Sprintf("/proc/%d/status", procs[id].Process.Pid))
		for _, line := range strings.Split(string(raw), "\n") {
			if f := strings.Fields(line); len(f) == 3 && f[0] == "VmRSS:" {
				kb, _ := strconv.ParseUint(f[1], 10, 64)
				return kb << 10
			}
		}
		return 0
	}
	// sample keeps the peak RSS of both processes until stop is closed.
	sample := func(r *loadResult, stop chan struct{}, wg *sync.WaitGroup) {
		defer wg.Done()
		tick := time.NewTicker(time.Second)
		defer tick.Stop()
		for {
			r.member, r.relay = max(r.member, rss("m1")), max(r.relay, rss("r1"))
			select {
			case <-stop:
				return
			case <-tick.C:
			}
		}
	}

	baseline := loadResult{name: "idle"}
	times, lost := pings(loadIdlePings)
	percentiles(&baseline, times, lost)
	baseline.member, baseline.relay = rss("m1"), rss("r1")

	run := func(name string) loadResult {
		t.Helper()
		r := loadResult{name: name}
		dropped := status("m1").Counters.DroppedQueue
		stop, wg := make(chan struct{}), &sync.WaitGroup{}
		wg.Add(1)
		go sample(&r, stop, wg)
		var report []byte
		var stderr bytes.Buffer
		var clientErr error
		done := make(chan struct{})
		go func() {
			defer close(done)
			client := in("m1", "iperf3", "-c", addr["r1"], "-t", strconv.Itoa(loadSeconds), "-J")
			client.Stderr = &stderr
			report, clientErr = client.Output()
		}()
		time.Sleep(loadBusyDelay)
		times, lost := pings(loadBusyPings)
		<-done
		close(stop)
		wg.Wait()
		if clientErr != nil {
			t.Fatalf("%s: iperf3: %v: %s %s", name, clientErr, report, stderr.Bytes())
		}
		var res struct {
			End struct {
				SumSent struct {
					Retransmits int `json:"retransmits"`
				} `json:"sum_sent"`
				SumReceived struct {
					BitsPerSecond float64 `json:"bits_per_second"`
				} `json:"sum_received"`
			} `json:"end"`
		}
		if err := json.Unmarshal(report, &res); err != nil {
			t.Fatalf("%s: iperf3 report: %v", name, err)
		}
		r.mbps, r.retransmits = res.End.SumReceived.BitsPerSecond/1e6, res.End.SumSent.Retransmits
		percentiles(&r, times, lost)
		r.droppedQueue = status("m1").Counters.DroppedQueue - dropped
		return r
	}
	limited := run(fmt.Sprintf("load, limit %g Mbit/s", loadUploadMbps))

	// The reference: the same transfer without the limit.
	time.Sleep(3 * time.Second) // let the queues drain
	generate(2, nil)
	for _, id := range ids {
		waitFor(id+" applying revision 2", func() bool { s := status(id); return s.AppliedRevision == 2 && s.Error == "" })
	}
	unlimited := run("load, no limit (reference)")

	// Fragmented UDP: 8000-byte datagrams, six fragments each through the
	// mesh, the later ones let in at r1 on what it kept of the first.
	var fragRes struct {
		End struct {
			Sum struct {
				BitsPerSecond float64 `json:"bits_per_second"`
				LostPercent   float64 `json:"lost_percent"`
				Packets       int     `json:"packets"`
			} `json:"sum"`
		} `json:"end"`
	}
	fragOut, err := in("m1", "iperf3", "-c", addr["r1"], "-u", "-l", "8000", "-b", loadFragRate, "-t", "10", "-J").Output()
	if err != nil {
		t.Fatalf("fragmented UDP: iperf3: %v: %s", err, fragOut)
	}
	if err := json.Unmarshal(fragOut, &fragRes); err != nil {
		t.Fatalf("fragmented UDP: iperf3 report: %v", err)
	}
	frag := fragRes.End.Sum

	// Throughput without a line in the way: one WireGuard layer between
	// next hops, two end to end with the relay forwarding.
	bulkServer := in("m3", "iperf3", "-s", "-B", addr["m3"])
	if err := bulkServer.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { bulkServer.Process.Kill(); bulkServer.Wait() })
	time.Sleep(300 * time.Millisecond)
	bulk := func(to string) float64 {
		t.Helper()
		out, err := in("m2", "iperf3", "-c", addr[to], "-t", "10", "-J").Output()
		var res struct {
			End struct {
				SumReceived struct {
					BitsPerSecond float64 `json:"bits_per_second"`
				} `json:"sum_received"`
			} `json:"end"`
		}
		if err != nil || json.Unmarshal(out, &res) != nil {
			t.Fatalf("m2 to %s: iperf3: %v: %s", to, err, out)
		}
		return res.End.SumReceived.BitsPerSecond / 1e6
	}
	sent, forwarded := status("m2").E2E.Sent, status("r1").Counters.Forwarded
	hopMbps := bulk("r1")
	if s := status("m2").E2E.Sent; s > sent+100 {
		t.Fatalf("m2 sent %d packets to its own entry end to end", s-sent)
	}
	e2eMbps := bulk("m3")
	waitFor("the end-to-end transfer in status", func() bool {
		return status("m2").E2E.Sent > sent+1000 && status("r1").Counters.Forwarded > forwarded+1000
	})

	ms := func(v float64) string {
		if math.IsInf(v, 1) {
			return "lost"
		}
		return fmt.Sprintf("%.1f", v)
	}
	mib := func(v uint64) string { return fmt.Sprintf("%.1f", float64(v)/(1<<20)) }
	var table strings.Builder
	fmt.Fprintf(&table, "line: %s with %s burst and %s bytes of buffer up, %s each way; m1 enters r1 over tcp\n",
		loadLineRate, loadLineBurst, loadLineLimit, loadLineDelay)
	fmt.Fprintf(&table, "%-28s %9s %9s %9s %10s %6s %8s %13s %12s %12s\n",
		"run", "p50 ms", "p95 ms", "p95 rise", "Mbit/s", "lost", "retrans", "queue drops", "m1 RSS MiB", "r1 RSS MiB")
	for _, r := range []loadResult{baseline, limited, unlimited} {
		rise, mbps, retrans, drops := "-", "-", "-", "-"
		if r.name != baseline.name {
			rise = ms(r.p95 - baseline.p95)
			mbps = fmt.Sprintf("%.1f", r.mbps)
			retrans, drops = strconv.Itoa(r.retransmits), strconv.FormatUint(r.droppedQueue, 10)
		}
		fmt.Fprintf(&table, "%-28s %9s %9s %9s %10s %3d/%-2d %8s %13s %12s %12s\n",
			r.name, ms(r.p50), ms(r.p95), rise, mbps, r.lost, r.sent, retrans, drops, mib(r.member), mib(r.relay))
	}
	fmt.Fprintf(&table, "fragmented UDP, 8000-byte datagrams at %s: %d sent, %.2f%% lost, %.1f Mbit/s\n", loadFragRate, frag.Packets, frag.LostPercent, frag.BitsPerSecond/1e6)
	fmt.Fprintf(&table, "unshaped TCP: m2 to r1 (next hop) %.0f Mbit/s, m2 to m3 end to end through r1 %.0f Mbit/s (%.0f%%)\n",
		hopMbps, e2eMbps, 100*e2eMbps/hopMbps)
	t.Log("\n" + table.String())
	if e2eMbps < loadMinMbps {
		t.Errorf("end to end the transfer got %.1f Mbit/s, less than %g", e2eMbps, loadMinMbps)
	}
	if frag.Packets == 0 || frag.LostPercent > loadMaxFragLoss {
		t.Errorf("fragmented UDP lost %.2f%% of %d datagrams", frag.LostPercent, frag.Packets)
	}

	if rise := limited.p95 - baseline.p95; !(rise <= loadMaxP95Rise) {
		t.Errorf("with the limit, ping p95 rose by %s ms under load, more than %g ms", ms(rise), loadMaxP95Rise)
	}
	if limited.mbps < loadMinMbps {
		t.Errorf("with the limit, the transfer got %.1f Mbit/s, less than %g: the line was not full", limited.mbps, loadMinMbps)
	}
	if limited.member > loadMemberRSS {
		t.Errorf("m1's RSS peaked at %s MiB, over %s MiB", mib(limited.member), mib(loadMemberRSS))
	}
	if limited.relay > loadRelayRSS {
		t.Errorf("r1's RSS peaked at %s MiB, over %s MiB", mib(limited.relay), mib(loadRelayRSS))
	}
}
