package mesh

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

// Data plane memory after a long load: two members in network namespaces,
// each on one CPU like a 1-vCPU machine, linked over TCP with jumbo frames,
// iperf3 both ways for OBLAB_MEMORY_MINUTES (8), then idle. It reports heap,
// RSS and idle CPU, and leaves heap profiles in OBLAB_MEMORY_OUT.
// OBLAB_NETNS=1 OBLAB_MEMORY=1 enables it; it needs root and iperf3.
func TestMemoryNetnsAfterLoad(t *testing.T) {
	if os.Getenv("OBLAB_NETNS") != "1" || os.Getenv("OBLAB_MEMORY") != "1" || os.Getuid() != 0 {
		t.Skip("set OBLAB_NETNS=1 OBLAB_MEMORY=1 and run as root")
	}
	if _, err := exec.LookPath("iperf3"); err != nil {
		t.Skip("needs iperf3")
	}
	minutes := 8
	if v, err := strconv.Atoi(os.Getenv("OBLAB_MEMORY_MINUTES")); err == nil && v > 0 {
		minutes = v
	}
	out := os.Getenv("OBLAB_MEMORY_OUT")
	cpu := map[string]string{"A": "2", "B": "3"}
	l := newRTLab(t, []string{"A", "B"}, []string{"A-B:0:tcp"}, nil)
	k := l.links[0]
	for _, end := range [][2]string{{k.a, k.devA}, {k.b, k.devB}} {
		rtSh(t, "ip", "netns", "exec", l.ns[end[0]], "tc", "qdisc", "del", "dev", end[1], "root")
	}
	l.configure = func(id string, c *Config) {
		c.Self.Resources = roleMember
		c.Network.TCPJumbo = true
		c.Rules = []Rule{{Proto: "any"}}
	}
	l.wrap = map[string][]string{"A": {"taskset", "-c", cpu["A"]}, "B": {"taskset", "-c", cpu["B"]}}
	l.start()
	l.paths(60*time.Second, "A>B", "B>A")
	server := exec.Command("ip", "netns", "exec", l.ns["B"], "taskset", "-c", cpu["B"], "iperf3", "-s", "-B", l.addr["B"].String(), "-p", "5299")
	if err := server.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { server.Process.Kill(); server.Wait() })
	time.Sleep(time.Second)
	report := func(when string) {
		for _, id := range l.ids {
			s := l.status(id)
			pid := l.procs[id].Process.Pid
			ticks := func() int {
				raw, _ := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
				f := strings.Fields(string(raw))
				u, _ := strconv.Atoi(f[13])
				sy, _ := strconv.Atoi(f[14])
				return u + sy
			}
			a := ticks()
			time.Sleep(5 * time.Second)
			t.Logf("%s %s: heap %.1f MiB, rss %.1f MiB, goroutines %d, cpu %d%%, dropped queue %d", when, id,
				float64(s.Resources.HeapBytes)/(1<<20), float64(s.Resources.RSSBytes)/(1<<20), s.Resources.Goroutines, (ticks()-a)*100/5/100, s.Counters.DroppedQueue)
		}
	}
	report("before")
	for end := time.Now().Add(time.Duration(minutes) * time.Minute); time.Now().Before(end); {
		for _, args := range [][]string{{"-P", "4"}, {"-R", "-P", "4"}} {
			cmd := append([]string{"ip", "netns", "exec", l.ns["A"], "taskset", "-c", cpu["A"], "iperf3", "-c", l.addr["B"].String(), "-p", "5299", "-t", "20"}, args...)
			if b, err := exec.Command(cmd[0], cmd[1:]...).CombinedOutput(); err != nil {
				t.Logf("iperf3 %v: %v: %s", args, err, b)
			}
		}
	}
	report("loaded")
	time.Sleep(time.Minute)
	report("idle")
	for _, id := range l.ids {
		l.procs[id].Process.Signal(syscall.SIGUSR1)
	}
	time.Sleep(2 * time.Second)
	if out != "" {
		for _, id := range l.ids {
			if b, err := os.ReadFile(filepath.Join(l.dirs[id], "heap-1.pprof")); err == nil {
				os.WriteFile(filepath.Join(out, "heap-"+id+".pprof"), b, 0o600)
			}
		}
	}
}
