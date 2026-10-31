//go:build linux || darwin || windows

package node

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net"
	"net/netip"
	"slices"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

func TestNetAddrsFiltersAndClassifies(t *testing.T) {
	cidr := func(s string) net.Addr {
		ip, n, err := net.ParseCIDR(s)
		if err != nil {
			t.Fatal(err)
		}
		n.IP = ip
		return n
	}
	mac := net.HardwareAddr{0x52, 0x54, 0, 0x12, 0x34, 0x56}
	up := net.FlagUp | net.FlagBroadcast
	ifaces := []netIface{
		{name: "lo", flags: net.FlagUp | net.FlagLoopback, addrs: []net.Addr{cidr("127.0.0.1/8"), cidr("::1/128")}},
		{name: "eth0", flags: up, mac: mac, addrs: []net.Addr{cidr("2001:db8::10/64"), cidr("fe80::5054:ff:fe12:3456/64"),
			cidr("10.10.10.251/24"), cidr("10.10.10.9/24"), &net.IPNet{IP: net.ParseIP("192.168.1.5"), Mask: net.CIDRMask(120, 128)}}},
		{name: "eth1", flags: net.FlagBroadcast, mac: mac, addrs: []net.Addr{cidr("169.254.7.7/16")}},
		{name: "docker0", flags: up, mac: mac, addrs: []net.Addr{cidr("172.17.0.1/16")}},
		{name: "veth1234", flags: up, mac: mac, addrs: []net.Addr{cidr("fd00::1/64")}},
		{name: "vEthernet (WSL (Hyper-V firewall))", flags: up, mac: mac, addrs: []net.Addr{cidr("172.29.0.1/20")}},
		{name: "vEthernet (Default Switch)", flags: up, mac: mac, addrs: []net.Addr{cidr("172.30.0.1/20")}},
		{name: "vEthernet (External Switch)", flags: up, mac: mac, addrs: []net.Addr{cidr("10.20.20.31/24")}},
		{name: "obmesh0", flags: up | net.FlagPointToPoint, addrs: []net.Addr{cidr("100.96.0.2/16"), cidr("100.96.255.2/24")}},
		{name: "utun3", flags: up | net.FlagPointToPoint, addrs: []net.Addr{cidr("fe80::1/64"), cidr("100.96.0.3/32")}},
		{name: "utun4", flags: up | net.FlagPointToPoint, addrs: []net.Addr{cidr("10.8.0.2/24")}},
		{name: "wg0", flags: up | net.FlagPointToPoint, addrs: []net.Addr{cidr("10.0.9.2/24")}},
		{name: "en5", flags: up, addrs: []net.Addr{cidr("fe80::2/64")}},
		{name: "ham0", flags: up, addrs: []net.Addr{cidr("25.1.2.3/8")}},
	}
	mesh := []netip.Prefix{netip.MustParsePrefix("100.96.0.0/16"), netip.MustParsePrefix("100.96.255.0/24")}
	got := netInterfaces(ifaces, mesh)
	data, _ := json.Marshal(got)
	want := `[` +
		`{"name":"eth0","up":true,"mac":"52:54:00:12:34:56","kind":"ethernet","addrs":[` +
		`{"ip":"10.10.10.9","prefixLen":24,"family":4},{"ip":"10.10.10.251","prefixLen":24,"family":4},` +
		`{"ip":"192.168.1.5","prefixLen":24,"family":4},{"ip":"2001:db8::10","prefixLen":64,"family":6}]},` +
		`{"name":"eth1","up":false,"mac":"52:54:00:12:34:56","kind":"ethernet","addrs":[{"ip":"169.254.7.7","prefixLen":16,"family":4}]},` +
		`{"name":"vEthernet (External Switch)","up":true,"mac":"52:54:00:12:34:56","kind":"ethernet","addrs":[{"ip":"10.20.20.31","prefixLen":24,"family":4}]},` +
		`{"name":"obmesh0","up":true,"mac":"","kind":"mesh","addrs":[{"ip":"100.96.0.2","prefixLen":16,"family":4},{"ip":"100.96.255.2","prefixLen":24,"family":4}]},` +
		`{"name":"utun3","up":true,"mac":"","kind":"mesh","addrs":[{"ip":"100.96.0.3","prefixLen":32,"family":4}]},` +
		`{"name":"utun4","up":true,"mac":"","kind":"tunnel","addrs":[{"ip":"10.8.0.2","prefixLen":24,"family":4}]},` +
		`{"name":"wg0","up":true,"mac":"","kind":"tunnel","addrs":[{"ip":"10.0.9.2","prefixLen":24,"family":4}]},` +
		`{"name":"ham0","up":true,"mac":"","kind":"other","addrs":[{"ip":"25.1.2.3","prefixLen":8,"family":4}]}]`
	if string(data) != want {
		t.Fatalf("got\n%s\nwant\n%s", data, want)
	}
	// Without the mesh config a utun is just a tunnel.
	if got = netInterfaces(ifaces[9:10], nil); got[0].Kind != "tunnel" {
		t.Fatalf("utun without config: %+v", got)
	}
	if data, _ = json.Marshal(netInterfaces(nil, nil)); string(data) != "[]" {
		t.Fatalf("no interfaces: %s", data)
	}
}

func TestProbeParamsDefaultsAndRanges(t *testing.T) {
	p, err := parseProbeParams(json.RawMessage(`{"mode":"ping","target":"example.com"}`))
	if err != nil || p.Count != 10 || p.Interval != time.Second || p.Timeout != 2*time.Second || p.Family != 0 || p.Port != 0 || p.MaxHops != 0 {
		t.Fatalf("ping defaults: %+v %v", p, err)
	}
	p, err = parseProbeParams(json.RawMessage(`{"mode":"trace","target":"2001:db8::1","port":80,"family":6}`))
	if err != nil || p.Count != 3 || p.MaxHops != 30 || p.Port != 0 {
		t.Fatalf("trace defaults: %+v %v", p, err)
	}
	p, err = parseProbeParams(json.RawMessage(`{"mode":"tcping","target":"10.0.0.1","port":65535,"family":4,"count":100,"intervalMs":200,"timeoutMs":10000}`))
	if err != nil || p.Port != 65535 || p.Count != 100 || p.Interval != 200*time.Millisecond || p.Timeout != 10*time.Second {
		t.Fatalf("tcping: %+v %v", p, err)
	}
	long := strings.Repeat("a", 251) + ".io"
	for _, raw := range []string{
		`[]`, `{"target":"a.com"}`, `{"mode":"PING","target":"a.com"}`, `{"mode":"ping"}`,
		`{"mode":"ping","target":"https://a.com"}`, `{"mode":"ping","target":"a.com:443"}`, `{"mode":"ping","target":"a .com"}`,
		`{"mode":"ping","target":"[::1]"}`, `{"mode":"ping","target":"` + long + `"}`,
		`{"mode":"tcping","target":"a.com"}`, `{"mode":"tcping","target":"a.com","port":0}`, `{"mode":"tcping","target":"a.com","port":65536}`,
		`{"mode":"ping","target":"a.com","family":5}`, `{"mode":"ping","target":"10.0.0.1","family":6}`, `{"mode":"ping","target":"::1","family":4}`,
		`{"mode":"ping","target":"a.com","count":0}`, `{"mode":"ping","target":"a.com","count":101}`, `{"mode":"trace","target":"a.com","count":11}`,
		`{"mode":"ping","target":"a.com","intervalMs":199}`, `{"mode":"ping","target":"a.com","timeoutMs":10001}`,
		`{"mode":"trace","target":"a.com","maxHops":65}`, `{"mode":"trace","target":"a.com","maxHops":0}`, `{"mode":"ping","target":"a.com","count":1.5}`,
	} {
		if _, err := parseProbeParams(json.RawMessage(raw)); !errors.Is(err, errInvalidProbe) {
			t.Errorf("%s: want invalid_params, got %v", raw, err)
		}
	}
	if _, err := parseProbeParams(json.RawMessage(`{"mode":"ping","target":"` + long[1:] + `"}`)); err != nil {
		t.Errorf("253 characters are allowed: %v", err)
	}
}

func TestProbeSummary(t *testing.T) {
	ms := func(v float64) time.Duration { return time.Duration(v * float64(time.Millisecond)) }
	results := []probeResult{{OK: true, rtt: ms(10.04)}, {Error: "timeout"}, {OK: true, rtt: ms(12.36)}, {OK: true, rtt: ms(11)}}
	data, _ := json.Marshal(probeSummary(results))
	if want := `{"sent":4,"received":3,"lossPct":25,"minMs":10,"avgMs":11.1,"maxMs":12.4,"jitterMs":1.8}`; string(data) != want {
		t.Fatalf("got %s, want %s", data, want)
	}
	data, _ = json.Marshal(probeSummary(results[:3]))
	if want := `{"sent":3,"received":2,"lossPct":33.3,"minMs":10,"avgMs":11.2,"maxMs":12.4,"jitterMs":2.3}`; string(data) != want {
		t.Fatalf("got %s, want %s", data, want)
	}
	data, _ = json.Marshal(probeSummary(results[:2]))
	if want := `{"sent":2,"received":1,"lossPct":50,"minMs":10,"avgMs":10,"maxMs":10,"jitterMs":null}`; string(data) != want {
		t.Fatalf("one success has no jitter: got %s", data)
	}
	data, _ = json.Marshal(probeSummary(results[1:2]))
	if want := `{"sent":1,"received":0,"lossPct":100,"minMs":null,"avgMs":null,"maxMs":null,"jitterMs":null}`; string(data) != want {
		t.Fatalf("all lost: got %s", data)
	}
	if probeSummary(nil) != nil {
		t.Fatal("no results must give no summary")
	}
}

// fakeEcho answers from a function; a timeout waits the probe's timeout out.
type fakeEcho struct {
	reply func(seq, ttl int) echoReply
	mu    sync.Mutex
	sent  [][2]int // seq, ttl
}

func (f *fakeEcho) echo(ctx context.Context, seq, ttl int, timeout time.Duration) echoReply {
	f.mu.Lock()
	f.sent = append(f.sent, [2]int{seq, ttl})
	f.mu.Unlock()
	r := f.reply(seq, ttl)
	if r.kind == replyTimeout {
		select {
		case <-time.After(timeout):
		case <-ctx.Done():
		}
	} else {
		time.Sleep(r.rtt)
	}
	return r
}

func (f *fakeEcho) Close() error { return nil }

func testProbes(f *fakeEcho) *probeManager {
	m := newProbeManager()
	m.openEcho = func(netip.Addr, int) (echoConn, error) { return f, nil }
	m.route = func(netip.Addr) error { return nil }
	m.lookupIP = func(context.Context, string, string) ([]netip.Addr, error) { return nil, errors.New("no DNS in tests") }
	m.lookupAddr = func(_ context.Context, addr string) ([]string, error) {
		if addr == "10.0.0.1" {
			return nil, errors.New("no name")
		}
		return []string{"r-" + addr + ".example."}, nil
	}
	return m
}

func startProbe(t *testing.T, m *probeManager, params string) string {
	t.Helper()
	r, err := m.start(json.RawMessage(params))
	if err != nil {
		t.Fatalf("start %s: %v", params, err)
	}
	return r.(map[string]any)["id"].(string)
}

func probeStatus(t *testing.T, m *probeManager, id string) map[string]any {
	t.Helper()
	r, err := m.status(id)
	if err != nil {
		t.Fatalf("status: %v", err)
	}
	data, _ := json.Marshal(r)
	var out map[string]any
	json.Unmarshal(data, &out)
	return out
}

func waitProbe(t *testing.T, m *probeManager, id string) map[string]any {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for {
		s := probeStatus(t, m, id)
		if s["state"] != "running" || time.Now().After(deadline) {
			return s
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func TestProbePingRecordsEachAttemptInOrder(t *testing.T) {
	f := &fakeEcho{reply: func(seq, _ int) echoReply {
		switch seq {
		case 1:
			return echoReply{kind: replyTimeout}
		case 2:
			return echoReply{kind: replyEcho, rtt: 5 * time.Millisecond, ttl: 57}
		}
		return echoReply{kind: replyUnreachable}
	}}
	m := testProbes(f)
	s := waitProbe(t, m, startProbe(t, m, `{"mode":"ping","target":"192.0.2.1","count":3,"intervalMs":200,"timeoutMs":500}`))
	if s["state"] != "done" || s["ip"] != "192.0.2.1" || s["family"] != 4.0 || s["endedAt"].(float64) < s["startedAt"].(float64) ||
		len(s["hops"].([]any)) != 0 || s["summary"] == nil {
		t.Fatalf("unexpected status %v", s)
	}
	var got []string
	for _, r := range s["results"].([]any) {
		r := r.(map[string]any)
		got = append(got, strings.TrimSpace(strings.Join([]string{jsonText(r["seq"]), jsonText(r["ok"]), jsonText(r["ttl"]), r["error"].(string)}, " ")))
	}
	if want := []string{"1 false 0 timeout", "2 true 57", "3 false 0 unreachable"}; !slices.Equal(got, want) {
		t.Fatalf("results %v, want %v", got, want)
	}
	if rtt := s["results"].([]any)[1].(map[string]any)["rttMs"].(float64); rtt < 5 || rtt > 50 {
		t.Fatalf("rtt %v", rtt)
	}
}

func jsonText(v any) string {
	data, _ := json.Marshal(v)
	return string(data)
}

func TestProbeTraceStopsAtTheTarget(t *testing.T) {
	target := netip.MustParseAddr("192.0.2.9")
	f := &fakeEcho{}
	f.reply = func(seq, ttl int) echoReply {
		round := seq >> 8
		switch {
		case ttl == 3:
			return echoReply{kind: replyTimeout}
		case ttl == 2 && round%2 == 1:
			return echoReply{kind: replyExceeded, from: netip.MustParseAddr("10.0.0.22"), rtt: 2 * time.Millisecond}
		case ttl < 5:
			return echoReply{kind: replyExceeded, from: netip.AddrFrom4([4]byte{10, 0, 0, byte(ttl)}), rtt: time.Duration(ttl) * time.Millisecond}
		case ttl == 5 && round == 0:
			// The first answer from the target comes from beyond its hop.
			return echoReply{kind: replyEcho, from: target, rtt: 30 * time.Millisecond}
		}
		return echoReply{kind: replyEcho, from: target, rtt: time.Millisecond}
	}
	m := testProbes(f)
	id := startProbe(t, m, `{"mode":"trace","target":"192.0.2.9","count":3,"intervalMs":200,"timeoutMs":300}`)
	waitProbe(t, m, id)
	time.Sleep(50 * time.Millisecond) // the reverse lookups
	s := probeStatus(t, m, id)
	if s["state"] != "done" || s["reached"] != true || len(s["results"].([]any)) != 0 || s["summary"] != nil {
		t.Fatalf("unexpected status %v", s)
	}
	hops := s["hops"].([]any)
	var got []string
	for _, h := range hops {
		h := h.(map[string]any)
		line := jsonText(h["ttl"]) + " " + jsonText(h["addrs"]) + " " + jsonText(h["sent"]) + "/" + jsonText(h["received"]) +
			" loss " + jsonText(h["lossPct"]) + " avg " + jsonText(h["avgMs"]) + " reached " + jsonText(h["reached"])
		got = append(got, line)
	}
	want := []string{
		`1 [{"ip":"10.0.0.1","name":""}] 3/3 loss 0 avg 1 reached false`,
		`2 [{"ip":"10.0.0.2","name":"r-10.0.0.2.example"},{"ip":"10.0.0.22","name":"r-10.0.0.22.example"}] 3/3 loss 0 avg 2 reached false`,
		`3 [] 3/0 loss 100 avg null reached false`,
		`4 [{"ip":"10.0.0.4","name":"r-10.0.0.4.example"}] 3/3 loss 0 avg 4 reached false`,
		`5 [{"ip":"192.0.2.9","name":"r-192.0.2.9.example"}] 3/3 loss 0 avg 10.7 reached true`,
	}
	if !slices.Equal(got, want) {
		t.Fatalf("hops\n%s\nwant\n%s", strings.Join(got, "\n"), strings.Join(want, "\n"))
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, s := range f.sent {
		if s[0]>>8 > 0 && s[1] > 5 {
			t.Fatalf("round %d sent ttl %d beyond the target", s[0]>>8, s[1])
		}
	}
}

func TestProbeTraceKeepsTheNearestAnswer(t *testing.T) {
	// A target that answers at ttl 6 before its answer at ttl 4 arrives.
	f := &fakeEcho{reply: func(seq, ttl int) echoReply {
		if ttl < 4 {
			return echoReply{kind: replyExceeded, from: netip.AddrFrom4([4]byte{10, 0, 0, byte(ttl)}), rtt: time.Millisecond}
		}
		rtt := time.Millisecond
		if ttl == 4 && seq>>8 == 0 {
			rtt = 40 * time.Millisecond
		}
		return echoReply{kind: replyEcho, from: netip.MustParseAddr("192.0.2.9"), rtt: rtt}
	}}
	m := testProbes(f)
	s := waitProbe(t, m, startProbe(t, m, `{"mode":"trace","target":"192.0.2.9","count":2,"intervalMs":200,"maxHops":10}`))
	hops := s["hops"].([]any)
	if len(hops) != 4 || hops[3].(map[string]any)["reached"] != true || hops[3].(map[string]any)["sent"] != 2.0 {
		t.Fatalf("hops %v", hops)
	}
}

func TestProbeLeaseStopsAnUnwatchedTest(t *testing.T) {
	m := testProbes(&fakeEcho{reply: func(int, int) echoReply { return echoReply{kind: replyTimeout} }})
	m.lease = 150 * time.Millisecond
	watched := startProbe(t, m, `{"mode":"ping","target":"192.0.2.1","count":100,"intervalMs":200,"timeoutMs":200}`)
	idle := startProbe(t, m, `{"mode":"ping","target":"192.0.2.2","count":100,"intervalMs":200,"timeoutMs":200}`)
	for range 8 {
		time.Sleep(50 * time.Millisecond)
		probeStatus(t, m, watched)
	}
	m.mu.Lock()
	states := m.probes[watched].state + " " + m.probes[idle].state
	m.mu.Unlock()
	if states != "running stopped" {
		t.Fatalf("states %s", states)
	}
	if s := probeStatus(t, m, idle); s["state"] != "stopped" || s["endedAt"] == 0.0 {
		t.Fatalf("idle test %v", s)
	}
	m.stop(watched)
}

func TestProbeLimits(t *testing.T) {
	m := testProbes(&fakeEcho{reply: func(int, int) echoReply { return echoReply{kind: replyTimeout} }})
	m.keep = 150 * time.Millisecond
	params := `{"mode":"ping","target":"192.0.2.1","count":100,"intervalMs":200}`
	var ids []string
	for range probeMaxRunning {
		ids = append(ids, startProbe(t, m, params))
	}
	if _, err := m.start(json.RawMessage(params)); err == nil || err.Error() != "probe_busy" {
		t.Fatalf("a fifth test must wait: %v", err)
	}
	if _, err := m.start(json.RawMessage(`{"mode":"ping"}`)); !errors.Is(err, errInvalidProbe) {
		t.Fatalf("invalid params: %v", err)
	}
	m.mu.Lock()
	echoIDs := map[int]bool{}
	for _, p := range m.probes {
		echoIDs[p.echoID] = true
	}
	m.mu.Unlock()
	if len(echoIDs) != probeMaxRunning || echoIDs[0] {
		t.Fatalf("each test needs its own echo ID: %v", echoIDs)
	}
	if r, err := m.stop(ids[0]); err != nil || len(r.(map[string]any)) != 0 {
		t.Fatalf("stop: %v %v", r, err)
	}
	if s := probeStatus(t, m, ids[0]); s["state"] != "stopped" || s["endedAt"] == 0.0 {
		t.Fatalf("stopped test %v", s)
	}
	if _, err := m.stop(ids[0]); err != nil {
		t.Fatalf("stopping an ended test again: %v", err)
	}
	ids = append(ids, startProbe(t, m, params))
	if _, err := m.stop("nope"); err == nil || err.Error() != "not_found" {
		t.Fatalf("unknown id: %v", err)
	}
	if _, err := m.status("nope"); err == nil || err.Error() != "not_found" {
		t.Fatalf("unknown id: %v", err)
	}
	for _, id := range ids {
		m.stop(id)
	}
	time.Sleep(250 * time.Millisecond)
	if _, err := m.status(ids[0]); err == nil || err.Error() != "not_found" {
		t.Fatalf("an ended test is kept only for a while after it was last looked at: %v", err)
	}

	m.maxRun = 300 * time.Millisecond
	if s := waitProbe(t, m, startProbe(t, m, params)); s["state"] != "done" {
		t.Fatalf("a test past its run time ends as done: %v", s)
	}
}

func TestProbeResolvesTheTarget(t *testing.T) {
	m := testProbes(&fakeEcho{reply: func(int, int) echoReply { return echoReply{kind: replyEcho} }})
	s := waitProbe(t, m, startProbe(t, m, `{"mode":"ping","target":"missing.example","count":1}`))
	if s["state"] != "error" || s["error"] != "dns_failed: no DNS in tests" || s["ip"] != "" || jsonText(s["results"]) != "[]" ||
		jsonText(s["hops"]) != "[]" || s["summary"] != nil {
		t.Fatalf("dns failure: %v", s)
	}
	m.lookupIP = func(_ context.Context, network, host string) ([]netip.Addr, error) {
		if host == "nx.example" {
			return nil, &net.DNSError{Err: "no such host", Name: host, IsNotFound: true}
		}
		all := []netip.Addr{netip.MustParseAddr("2001:db8::1"), netip.MustParseAddr("::ffff:192.0.2.7")}
		return slices.DeleteFunc(all, func(a netip.Addr) bool { return network == "ip6" && a.Unmap().Is4() }), nil
	}
	if s = waitProbe(t, m, startProbe(t, m, `{"mode":"ping","target":"nx.example","count":1}`)); s["error"] != "dns_failed: no such host" {
		t.Fatalf("dns failure: %v", s)
	}
	if s = waitProbe(t, m, startProbe(t, m, `{"mode":"ping","target":"both.example","count":1}`)); s["ip"] != "192.0.2.7" || s["family"] != 4.0 {
		t.Fatalf("auto prefers IPv4: %v", s)
	}
	if s = waitProbe(t, m, startProbe(t, m, `{"mode":"ping","target":"both.example","count":1,"family":6}`)); s["ip"] != "2001:db8::1" || s["family"] != 6.0 {
		t.Fatalf("IPv6 only: %v", s)
	}
}

// Without a route or source address for the target's family the test ends
// at once with the reason; a name with both families takes the one that
// has a way out.
func TestProbeNeedsARouteToTheTarget(t *testing.T) {
	f := &fakeEcho{reply: func(int, int) echoReply { return echoReply{kind: replyEcho} }}
	m := testProbes(f)
	m.route = func(ip netip.Addr) error {
		if ip.Is6() {
			return syscall.EADDRNOTAVAIL
		}
		return nil
	}
	for _, mode := range []string{`"ping"`, `"trace"`, `"tcping","port":443`} {
		s := waitProbe(t, m, startProbe(t, m, `{"mode":`+mode+`,"target":"2001:4860:4860::8888","count":1}`))
		if s["state"] != "error" || s["error"] != "本机没有可用的 IPv6 地址或路由" || s["ip"] != "2001:4860:4860::8888" ||
			jsonText(s["results"]) != "[]" || jsonText(s["hops"]) != "[]" {
			t.Fatalf("%s without IPv6: %v", mode, s)
		}
	}
	m.lookupIP = func(_ context.Context, network, host string) ([]netip.Addr, error) {
		return []netip.Addr{netip.MustParseAddr("2001:db8::1"), netip.MustParseAddr("192.0.2.7")}, nil
	}
	m.route = func(ip netip.Addr) error {
		if ip.Is4() {
			return syscall.ENETUNREACH
		}
		return nil
	}
	if s := waitProbe(t, m, startProbe(t, m, `{"mode":"ping","target":"both.example","count":1}`)); s["state"] != "done" || s["ip"] != "2001:db8::1" {
		t.Fatalf("an IPv6-only machine takes the IPv6 address: %v", s)
	}
	m.route = func(netip.Addr) error { return syscall.ENETUNREACH }
	if s := waitProbe(t, m, startProbe(t, m, `{"mode":"ping","target":"both.example","count":1}`)); s["error"] != "本机没有可用的 IPv4 或 IPv6 地址或路由" || s["ip"] != "192.0.2.7" {
		t.Fatalf("no network at all: %v", s)
	}
	if s := waitProbe(t, m, startProbe(t, m, `{"mode":"ping","target":"192.0.2.7","count":1}`)); s["error"] != "本机没有可用的 IPv4 地址或路由" {
		t.Fatalf("no IPv4 route: %v", s)
	}
}

func TestCheckRouteAcceptsReachableAddresses(t *testing.T) {
	if err := checkRoute(netip.MustParseAddr("127.0.0.1")); err != nil {
		t.Fatalf("loopback: %v", err)
	}
	// A test network address is routed whenever there is a default route;
	// either way the check must not take long or send anything.
	start := time.Now()
	checkRoute(netip.MustParseAddr("192.0.2.1"))
	if time.Since(start) > time.Second {
		t.Fatal("the route check waited on the network")
	}
}

func TestProbeTcping(t *testing.T) {
	ln, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			c.Close()
		}
	}()
	closed, _ := net.Listen("tcp4", "127.0.0.1:0")
	closedPort := closed.Addr().(*net.TCPAddr).Port
	closed.Close()
	m := testProbes(&fakeEcho{})
	tcping := func(port int) map[string]any {
		s := waitProbe(t, m, startProbe(t, m, `{"mode":"tcping","target":"127.0.0.1","port":`+jsonText(port)+`,"count":2,"intervalMs":200}`))
		if s["state"] != "done" || s["port"] != float64(port) || len(s["results"].([]any)) != 2 {
			t.Fatalf("tcping %d: %v", port, s)
		}
		return s["results"].([]any)[0].(map[string]any)
	}
	if r := tcping(ln.Addr().(*net.TCPAddr).Port); r["ok"] != true || r["rttMs"] == nil || r["error"] != "" {
		t.Fatalf("open port: %v", r)
	}
	if r := tcping(closedPort); r["ok"] != false || r["rttMs"] != nil || r["error"] != "refused" {
		t.Fatalf("closed port: %v", r)
	}
	m.dial = func(ctx context.Context, _, _ string) (net.Conn, error) {
		<-ctx.Done()
		return nil, &net.OpError{Op: "dial", Net: "tcp", Err: ctx.Err()}
	}
	if r := tcping(80); r["error"] != "timeout" {
		t.Fatalf("no answer: %v", r)
	}
}

func TestNetMethodsNeedNeitherConsentNorABusySlot(t *testing.T) {
	a := NewAgent(Config{}, log.New(io.Discard, "", 0))
	a.probes = testProbes(&fakeEcho{reply: func(int, int) echoReply { return echoReply{kind: replyEcho} }})
	for range cap(a.busy) {
		a.busy <- struct{}{}
	}
	call := func(method, params string) (any, error) {
		result, _, err := a.dispatch(context.Background(), envelope{Method: method, Params: json.RawMessage(params)})
		return result, err
	}
	if r, err := call("net.addrs", "{}"); err != nil || r.(map[string]any)["interfaces"] == nil {
		t.Fatalf("net.addrs: %v %v", r, err)
	}
	r, err := call("net.probe.start", `{"mode":"ping","target":"192.0.2.1","count":1}`)
	if err != nil {
		t.Fatal(err)
	}
	id := r.(map[string]any)["id"].(string)
	if _, err = call("net.probe.status", `{"id":"`+id+`"}`); err != nil {
		t.Fatal(err)
	}
	if _, err = call("net.probe.stop", `{"id":"`+id+`"}`); err != nil {
		t.Fatal(err)
	}
	for _, c := range []struct{ method, params, err string }{
		{"net.probe.status", `{"id":"x"}`, "not_found"},
		{"net.probe.status", `{}`, "invalid_params"},
		{"net.probe.stop", `{"id":5}`, "invalid_params"},
		{"net.probe.start", `{"mode":"dig","target":"a.com"}`, "invalid_params"},
		{"net.other", `{}`, "unsupported_method"},
		{"exec", `{}`, "capability_disabled"},
	} {
		if _, err := call(c.method, c.params); err == nil || err.Error() != c.err {
			t.Errorf("%s %s: want %s, got %v", c.method, c.params, c.err, err)
		}
	}
}
