package mesh

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"openbear/node/mesh/health"
	"openbear/node/mesh/transport"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

type proxyPort struct {
	Port    int
	Network string
}

func TestHealthProxy(t *testing.T) {
	raw := os.Getenv("OBLAB_HEALTH_PROXY")
	if raw == "" {
		t.Skip("test helper")
	}
	var ports []proxyPort
	if err := json.Unmarshal([]byte(raw), &ports); err != nil {
		t.Fatal(err)
	}
	dir := os.Getenv("OBLAB_HEALTH_WORK")
	for _, p := range ports {
		target := fmt.Sprintf("10.111.0.2:%d", p.Port)
		drop := filepath.Join(dir, fmt.Sprintf("drop-%s-%d", p.Network, p.Port))
		if p.Network == "tcp" {
			ln, err := net.Listen("tcp", fmt.Sprintf(":%d", p.Port))
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { ln.Close() })
			go func() {
				for {
					a, err := ln.Accept()
					if err != nil {
						return
					}
					go func() {
						defer a.Close()
						b, err := net.DialTimeout("tcp", target, time.Second)
						if err != nil {
							return
						}
						defer b.Close()
						go func() { io.Copy(a, b); a.Close() }()
						buf := make([]byte, 32768)
						for {
							n, err := a.Read(buf)
							if n > 0 {
								if _, blocked := os.Stat(drop); blocked != nil {
									if _, err := b.Write(buf[:n]); err != nil {
										return
									}
								}
							}
							if err != nil {
								return
							}
						}
					}()
				}
			}()
		} else {
			addr, _ := net.ResolveUDPAddr("udp4", fmt.Sprintf(":%d", p.Port))
			sock, err := net.ListenUDP("udp4", addr)
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { sock.Close() })
			go func() {
				peers := map[string]*net.UDPConn{}
				buf := make([]byte, 4096)
				for {
					n, src, err := sock.ReadFromUDP(buf)
					if err != nil {
						return
					}
					dst := peers[src.String()]
					if dst == nil {
						targetAddr, _ := net.ResolveUDPAddr("udp4", target)
						dst, err = net.DialUDP("udp4", nil, targetAddr)
						if err != nil {
							continue
						}
						peers[src.String()] = dst
						go func(c *net.UDPConn, client *net.UDPAddr) {
							defer c.Close()
							b := make([]byte, 4096)
							for {
								n, e := c.Read(b)
								if e != nil {
									if errors.Is(e, net.ErrClosed) {
										return
									}
									continue
								}
								sock.WriteToUDP(b[:n], client)
							}
						}(dst, src)
					}
					if _, blocked := os.Stat(drop); blocked != nil {
						dst.Write(buf[:n])
					} else if n >= 9 && buf[4] == 0 && binary.LittleEndian.Uint32(buf[5:9]) == 1 {
						os.WriteFile(drop+".data-handshake", []byte("unexpected data handshake on failed trial"), 0600)
					}
				}
			}()
		}
	}
	os.WriteFile(filepath.Join(dir, "proxy.ready"), []byte("ready"), 0600)
	select {}
}

func TestHealthAcceptance(t *testing.T) {
	mode := os.Getenv("OBLAB_HEALTH_ACCEPT")
	if mode == "" {
		t.Skip("set OBLAB_HEALTH_ACCEPT=stop|udp|tcp|cold|trials|return")
	}
	if os.Getenv("OBLAB_HEALTH_CHILD") == "1" {
		healthAcceptanceChild(t, mode)
		return
	}
	if os.Getuid() != 0 {
		t.Skip("requires root for isolated namespaces")
	}
	a, b := "oblab-health-node", "oblab-health-proxy"
	run := func(args ...string) {
		t.Helper()
		if out, err := exec.Command(args[0], args[1:]...).CombinedOutput(); err != nil {
			t.Fatalf("%v: %v %s", args, err, out)
		}
	}
	for _, n := range []string{a, b} {
		if out, _ := exec.Command("ip", "netns", "list").Output(); bytes.Contains(out, []byte(n)) {
			t.Fatal("existing namespace", n)
		}
		run("ip", "netns", "add", n)
		t.Cleanup(func() { exec.Command("ip", "netns", "del", n).Run() })
		run("ip", "-n", n, "link", "set", "lo", "up")
	}
	run("ip", "link", "add", "oblh0", "netns", a, "type", "veth", "peer", "name", "oblh1", "netns", b)
	for i, n := range []string{a, b} {
		dev := fmt.Sprintf("oblh%d", i)
		addr := "10.111.0.2/24"
		if i == 1 {
			addr = "10.111.0.1/24"
		}
		run("ip", "-n", n, "addr", "add", addr, "dev", dev)
		run("ip", "-n", n, "link", "set", dev, "up")
		run("ip", "netns", "exec", n, "tc", "qdisc", "add", "dev", dev, "root", "netem", "delay", "37.5ms")
	}
	run("ip", "netns", "exec", a, "sysctl", "-qw", "net.ipv4.icmp_echo_ignore_all=1")
	executable, _ := os.Executable()
	cmd := exec.Command("ip", "netns", "exec", a, executable, "-test.run", "^TestHealthAcceptance$", "-test.v", "-test.timeout", "35m")
	cmd.Env = append(os.Environ(), "OBLAB_HEALTH_CHILD=1")
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		t.Fatal(err)
	}
}

type healthObservation struct {
	node           string
	sent, received time.Time
}
type healthProbeRun struct {
	stop         chan struct{}
	done         sync.WaitGroup
	mu           sync.Mutex
	observations []healthObservation
}

func startHealthProbes(t *testing.T, l *lab, nodes map[string]*running) *healthProbeRun {
	run := &healthProbeRun{stop: make(chan struct{})}
	for _, id := range []string{"M", "N"} {
		id := id
		n := nodes[id]
		other := "N"
		if id == "N" {
			other = "M"
		}
		run.done.Add(2)
		go func() {
			defer run.done.Done()
			tick := time.NewTicker(200 * time.Millisecond)
			defer tick.Stop()
			for {
				select {
				case <-run.stop:
					return
				case now := <-tick.C:
					payload := make([]byte, 8)
					binary.BigEndian.PutUint64(payload, uint64(now.UnixNano()))
					p := packet(l.addr(id), l.addr(other), 1, 64, 0, 0, false, icmp(8, 55, payload))
					select {
					case n.ct.Outbound <- p:
					case <-run.stop:
						return
					}
				}
			}
		}()
		go func() {
			defer run.done.Done()
			for {
				select {
				case <-run.stop:
					return
				case p := <-n.ct.Inbound:
					if len(p) < 36 {
						continue
					}
					if p[20] == 8 {
						reply := packet(l.addr(id), l.addr(other), 1, 64, 0, 0, false, icmp(0, 55, p[28:36]))
						select {
						case n.ct.Outbound <- reply:
						case <-run.stop:
							return
						}
					} else if p[20] == 0 {
						sent := time.Unix(0, int64(binary.BigEndian.Uint64(p[28:36])))
						run.mu.Lock()
						run.observations = append(run.observations, healthObservation{id, sent, time.Now()})
						run.mu.Unlock()
					}
				}
			}
		}()
	}
	t.Cleanup(func() { close(run.stop); run.done.Wait() })
	return run
}
func (r *healthProbeRun) recovered(after time.Time) (time.Duration, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	first := map[string]time.Time{}
	for _, o := range r.observations {
		if !o.sent.Before(after) && first[o.node].IsZero() {
			first[o.node] = o.received
		}
	}
	if len(first) < 2 {
		return 0, false
	}
	end := first["M"]
	if first["N"].After(end) {
		end = first["N"]
	}
	return end.Sub(after), true
}
func healthFailedTrials(t *testing.T, l *lab, nodes map[string]*running, probes *healthProbeRun, dir string) {
	m := nodes["M"]
	bind := m.bind.(*streamBind)
	active := func() transport.PathInfo {
		for _, p := range bind.Paths() {
			if p.Active && p.PeerID == "A" {
				return p
			}
		}
		return transport.PathInfo{}
	}
	original := active()
	if original.Ref.ID == "" || original.Transport != "tcp" {
		t.Fatal("no active TCP")
	}
	time.Sleep(10 * time.Second)
	start := time.Now()
	drop := filepath.Join(dir, fmt.Sprintf("drop-udp-%d", l.nodes["A"].port))
	if err := os.WriteFile(drop, []byte("blackhole trial"), 0600); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 100; i++ {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		path, err := m.control.Prepare(ctx, health.Candidate{ID: "trial", Peer: "A", Endpoint: fmt.Sprintf("udp://10.111.0.1:%d", l.nodes["A"].port)})
		cancel()
		if err != nil {
			t.Fatal(err)
		}
		if err = m.control.Handshake("A", path); err != nil {
			t.Fatal(err)
		}
		n := fmt.Sprintf("%032x", i+1)
		raw, _ := json.Marshal(map[string]any{"v": 1, "mesh": l.config("M").Network.ID, "rev": 1, "from": "M", "to": "A", "type": "ping", "nonce": n, "token": n, "gen": path.Generation, "probe": i + 1})
		if err = m.control.Send("A", path, 0, raw); err != nil {
			t.Fatal(err)
		}
		time.Sleep(3 * time.Second)
		for _, p := range bind.Paths() {
			if p.Ref == transport.PathRef(path) && p.ControlAuthenticated {
				t.Fatal("blackholed trial authenticated")
			}
		}
		if err = m.control.Release(path); err != nil {
			t.Fatal(err)
		}
		current := active()
		if current.Ref != original.Ref || current.LocalAddr != original.LocalAddr || !current.Connected || m.status().Entry.Switches != 0 {
			t.Fatal("trial changed active connection", i, current, original)
		}
	}
	end := time.Now()
	if _, err := os.Stat(drop + ".data-handshake"); err == nil {
		t.Fatal("trial reset business handshake")
	}
	probes.mu.Lock()
	observations := append([]healthObservation(nil), probes.observations...)
	probes.mu.Unlock()
	var baseline, during []float64
	last := map[string]time.Time{}
	maxGap := time.Duration(0)
	for _, o := range observations {
		rtt := float64(o.received.Sub(o.sent)) / float64(time.Millisecond)
		if o.sent.Before(start) && o.sent.After(start.Add(-8*time.Second)) {
			baseline = append(baseline, rtt)
		}
		if !o.sent.Before(start) && o.received.Before(end) {
			during = append(during, rtt)
			if prev := last[o.node]; !prev.IsZero() {
				maxGap = max(maxGap, o.received.Sub(prev))
			}
			last[o.node] = o.received
		}
	}
	base, trial := percentile(baseline, .95), percentile(during, .95)
	t.Logf("FAILED TRIALS=100 baseline_n=%d trial_n=%d baseline_p95=%.3fms trial_p95=%.3fms extra=%.3fms max_reply_gap=%v active=%+v", len(baseline), len(during), base, trial, trial-base, maxGap, original.Ref)
	if len(baseline) < 30 || len(during) < 2000 || trial-base > 50 || maxGap >= time.Second {
		t.Fatal("failed trial disturbed ongoing traffic")
	}
}

func percentile(v []float64, p float64) float64 {
	s := append([]float64(nil), v...)
	sort.Float64s(s)
	if len(s) == 0 {
		return 0
	}
	return s[min(len(s)-1, int(float64(len(s))*p))]
}

func healthAcceptanceChild(t *testing.T, mode string) {
	l := newHealthLab(t, spec{relays: []string{"A", "B", "Z"}, members: map[string][]string{"M": {"A", "B"}, "N": {"Z"}}, rules: map[string][]Rule{"M": allowICMP, "N": allowICMP}})
	tcpPorts := map[string]int{}
	var ports []proxyPort
	for id, n := range l.nodes {
		ports = append(ports, proxyPort{n.port, "udp"})
		tcpPorts[id] = freePort(t)
		ports = append(ports, proxyPort{tcpPorts[id], "tcp"})
	}
	dir := t.TempDir()
	raw, _ := json.Marshal(ports)
	executable, _ := os.Executable()
	proxy := exec.Command("ip", "netns", "exec", "oblab-health-proxy", executable, "-test.run", "^TestHealthProxy$", "-test.v")
	proxy.Env = append(os.Environ(), "OBLAB_HEALTH_PROXY="+string(raw), "OBLAB_HEALTH_WORK="+dir)
	proxy.Stdout = os.Stdout
	proxy.Stderr = os.Stderr
	if err := proxy.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { proxy.Process.Kill(); proxy.Wait() })
	healthWait(t, 3*time.Second, func() bool { _, err := os.Stat(filepath.Join(dir, "proxy.ready")); return err == nil })
	baseEdit := l.edit
	l.edit = func(id string, c *Config) {
		baseEdit(id, c)
		c.Self.Listen.TCP = tcpPorts[id]
		for i := range c.Links {
			if c.Links[i].Dial {
				c.Links[i].Endpoint = fmt.Sprintf("udp://10.111.0.1:%d", l.nodes[c.Links[i].Peer].port)
			}
		}
		if c.Entry != nil {
			for i := range c.Entry.Candidates {
				p := &c.Entry.Candidates[i]
				p.Endpoint = fmt.Sprintf("udp://10.111.0.1:%d", l.nodes[p.Relay].port)
				if mode == "tcp" || mode == "trials" {
					p.Endpoint = fmt.Sprintf("tcp://10.111.0.1:%d", tcpPorts[p.Relay])
				}
			}
			c.Entry.AutoReturn = mode == "return"
			if mode == "trials" {
				c.Entry.Candidates = c.Entry.Candidates[:1]
			}
		}
	}
	nodes := map[string]*running{}
	for _, id := range []string{"A", "B", "Z", "M", "N"} {
		nodes[id] = l.startHealthNode(id)
	}
	t.Cleanup(func() {
		if t.Failed() {
			for id, n := range nodes {
				_, devState := deviceState(n.dev)
				t.Logf("FAILED %s health=%+v paths=%+v events=%+v data=%+v configPeers=%+v", id, n.health.Snapshot(), n.bind.(*streamBind).Paths(), n.events.snapshot(), devState, n.peers)
			}
		}
	})
	probes := startHealthProbes(t, l, nodes)
	healthWait(t, 15*time.Second, func() bool { _, ok := probes.recovered(time.Time{}); return ok })
	rounds := 30
	if mode == "return" {
		rounds = 1
	}
	if s := os.Getenv("OBLAB_HEALTH_ROUNDS"); s != "" {
		n, err := strconv.Atoi(s)
		if err != nil || n < 1 {
			t.Fatal(s)
		}
		rounds = n
	}
	if mode == "trials" {
		healthFailedTrials(t, l, nodes, probes, dir)
		return
	}
	var samples []float64
	for round := 0; round < rounds; round++ {
		healthWait(t, 50*time.Second, func() bool {
			h := nodes["M"].health.Snapshot()
			ready := 0
			for _, p := range h.Candidates {
				if p.Ready && p.RouteReady && p.Probes.Failures == 0 && time.Since(p.Probes.LastSuccess) < 3*time.Second {
					ready++
				}
			}
			return ready == 2
		})
		active := nodes["M"].status().Entry.Relay
		inactive := "A"
		if active == "A" {
			inactive = "B"
		}
		if mode == "cold" {
			nodes[inactive].close()
			healthWait(t, 15*time.Second, func() bool {
				for _, p := range nodes["M"].health.Snapshot().Candidates {
					if p.Peer == inactive && p.Probes.Failures >= 3 {
						return true
					}
				}
				return false
			})
		}
		fault := time.Now()
		var drop string
		if mode == "stop" || mode == "cold" || mode == "return" {
			nodes[active].close()
		} else {
			network := "udp"
			port := l.nodes[active].port
			if mode == "tcp" || mode == "trials" {
				network = "tcp"
				port = tcpPorts[active]
			}
			drop = filepath.Join(dir, fmt.Sprintf("drop-%s-%d", network, port))
			if err := os.WriteFile(drop, []byte("blackhole client to relay"), 0600); err != nil {
				t.Fatal(err)
			}
		}
		if mode == "cold" {
			nodes[inactive] = l.startHealthNode(inactive)
		}
		healthWait(t, 45*time.Second, func() bool {
			if nodes["M"].status().Entry.Relay == active {
				return false
			}
			_, ok := probes.recovered(fault)
			return ok
		})
		elapsed, _ := probes.recovered(fault)
		samples = append(samples, elapsed.Seconds())
		phases := map[string]float64{}
		for _, e := range nodes["M"].events.snapshot() {
			if e.At >= fault.UnixMilli() && (e.Kind == "entry.prepare" || e.Kind == "entry.committed" || e.Kind == "path.ready") {
				phases[e.Kind] = float64(e.At-fault.UnixMilli()) / 1000
			}
		}
		for _, e := range nodes["Z"].events.snapshot() {
			if e.At >= fault.UnixMilli() && e.Kind == "owner.changed" && strings.HasPrefix(e.Detail, "M -> ") {
				phases["third-relay-converged"] = float64(e.At-fault.UnixMilli()) / 1000
			}
		}
		t.Logf("round=%d mode=%s both-directions=%.3fs phases=%v", round+1, mode, elapsed.Seconds(), phases)
		if mode == "stop" || mode == "cold" || mode == "return" {
			nodes[active] = l.startHealthNode(active)
		} else {
			os.Remove(drop)
		}
	}
	if mode == "return" {
		switched := nodes["M"].health.Snapshot().Since
		healthWait(t, 130*time.Second, func() bool { return nodes["M"].status().Entry.Relay == "A" })
		returned := nodes["M"].health.Snapshot().Since
		if returned.Sub(switched) < 60*time.Second {
			t.Fatal("real preferred-path return ignored residence", returned.Sub(switched))
		}
		healthWait(t, 3*time.Second, func() bool { _, ok := probes.recovered(returned); return ok })
		t.Logf("REAL RETURN residence=%v switches=%d", returned.Sub(switched), nodes["M"].status().Entry.Switches)
	}
	p95 := percentile(samples, .95)
	label := "HOT"
	limit := 8.0
	if mode == "cold" {
		label = "COLD"
		limit = 15
	}
	t.Logf("%s mode=%s n=%d p95=%.3fs all=%v", label, mode, len(samples), p95, samples)
	if p95 > limit {
		if mode == "cold" {
			t.Logf("cold recovery did not meet suggested %.0fs target", limit)
		} else {
			t.Fatalf("%s bidirectional recovery exceeds %.0fs", label, limit)
		}
	}
}
