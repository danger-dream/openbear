package mesh

import (
	"crypto/rand"
	"encoding/base64"
	"encoding/binary"
	"fmt"
	"net"
	"os"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"golang.zx2c4.com/wireguard/device"
	"openbear/node/mesh/wire"
)

// TestRelayRestartReconnect needs root; OBLAB_RECONNECT=1 runs it. Members
// behind one NAT, so one source address for the relay's per-IP handshake
// limit, all lose their relay when it restarts and have to come back:
// first by themselves, then while a host elsewhere floods the relay with
// initiations that pass MAC1 (anyone who knows its public key can make
// them), so that the relay is under load and answers with cookies.
// OBLAB_RECONNECT_MEMBERS sets how many members (10 by default).
func TestRelayRestartReconnect(t *testing.T) {
	if os.Getenv("OBLAB_RECONNECT") != "1" || os.Getuid() != 0 {
		t.Skip("set OBLAB_RECONNECT=1 and run as root")
	}
	count := 10
	if n, err := strconv.Atoi(os.Getenv("OBLAB_RECONNECT_MEMBERS")); err == nil && n > 0 && n < 50 {
		count = n
	}
	var members []string
	for i := 1; i <= count; i++ {
		members = append(members, fmt.Sprintf("M%d", i))
	}
	l := newNSLab(t, "hq", append([]string{"W", "R", "N", "X"}, members...)...)
	l.router("W")
	l.veth("W", "wr", "10.219.0.1/24", "R", "r", "10.219.0.2/24")
	l.route("R", "10.219.0.1")
	l.veth("W", "wn", "10.219.1.1/24", "N", "n", "10.219.1.10/24")
	l.route("N", "10.219.1.1")
	l.veth("W", "wx", "10.219.2.1/24", "X", "x", "10.219.2.2/24")
	l.route("X", "10.219.2.1")
	l.router("N")
	l.in("N", "iptables", "-t", "nat", "-A", "POSTROUTING", "-o", l.ifname("n"), "-j", "MASQUERADE")
	l.bridge("N", "b", "192.168.40.1/24")
	l.node("R", roleRelay, 1)
	configs := map[string]*Config{}
	for i, m := range members {
		l.onLAN("N", "b", m, fmt.Sprintf("192.168.40.%d/24", 10+i), "192.168.40.1")
		l.node(m, roleMember, byte(11+i), "R")
	}
	// Every node's config names every other one: written once all exist.
	configs["R"] = l.config("R", Listen{UDP: 51820})
	configs["R"].Routing = &Routing{Mode: "static"}
	for _, m := range members {
		c := l.config(m, Listen{UDP: 51820})
		c.Entry = &Entry{Mode: "fixed", Candidates: []Candidate{{ID: "c1", Relay: "R", Endpoint: udpEndpoint("10.219.0.2", 51820)}}}
		configs[m] = c
	}
	// Cookie replies leaving the relay: WireGuard type 3 after the
	// transport's preamble and plane byte.
	l.in("R", "iptables", "-A", "OUTPUT", "-p", "udp", "--sport", "51820", "-m", "u32", "--u32", "0>>22&0x3C@13=0x03000000")
	cookies := func() int {
		out := l.in("R", "iptables", "-L", "OUTPUT", "-v", "-x", "-n")
		for _, line := range strings.Split(out, "\n") {
			if f := strings.Fields(line); len(f) > 2 && strings.Contains(line, "u32") {
				n, _ := strconv.Atoi(f[0])
				return n
			}
		}
		return -1
	}
	l.run(configs)
	self, _ := os.Executable()

	all := func() (missing []string) {
		var mu sync.Mutex
		var wg sync.WaitGroup
		for _, m := range members {
			wg.Add(1)
			go func() {
				defer wg.Done()
				if out, _ := execIn(l.ns[m], "ping", "-n", "-c", "1", "-W", "1", l.nodes["R"].Address.String()); !strings.Contains(out, " 0% packet loss") {
					mu.Lock()
					missing = append(missing, m)
					mu.Unlock()
				}
			}()
		}
		wg.Wait()
		return missing
	}
	waitAll := func(what string, within time.Duration) time.Duration {
		t.Helper()
		var missing []string
		took := l.wait(what, within, func() bool { missing = all(); return len(missing) == 0 })
		return took
	}
	took := waitAll("every member reaching R", 90*time.Second)
	t.Logf("%d members behind one NAT reach R %v after start", count, took.Round(100*time.Millisecond))

	// cpu is the relay's processor time so far, in clock ticks.
	cpu := func() int {
		raw, _ := os.ReadFile(fmt.Sprintf("/proc/%d/stat", l.procs["R"].Process.Pid))
		f := strings.Fields(string(raw[strings.LastIndexByte(string(raw), ')')+1:]))
		u, _ := strconv.Atoi(f[11])
		s, _ := strconv.Atoi(f[12])
		return u + s
	}
	restart := func(what string) time.Duration {
		t.Helper()
		l.stopNode("R")
		time.Sleep(2 * time.Second)
		before := cookies()
		l.startNode("R", self)
		took := waitAll(what, 60*time.Second)
		t.Logf("%s: every member reached R %v after it started again; R sent %d cookie replies, used %d ticks of CPU", what, took.Round(100*time.Millisecond), cookies()-before, cpu())
		return took
	}
	quiet := restart("restart")

	// The flood: from X, one source address, as fast as rate allows.
	flood := exec.Command("ip", "netns", "exec", l.ns["X"], self, "-test.run", "^TestNetnsFlood$")
	flood.Env = append(os.Environ(), "OBLAB_FLOOD_TARGET=10.219.0.2:51820", "OBLAB_FLOOD_KEY="+l.nodes["R"].PublicKey, "OBLAB_FLOOD_RATE=4000")
	if err := flood.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { flood.Process.Kill(); flood.Wait() })
	time.Sleep(time.Second)
	loaded := restart("restart under a handshake flood")
	if quiet > 30*time.Second || loaded > 45*time.Second {
		t.Errorf("reconnecting took %v, under the flood %v", quiet, loaded)
	}
	if c := cookies(); c <= 0 {
		t.Errorf("the relay sent no cookie reply under the flood (%d)", c)
	}
}

var floodLine = regexp.MustCompile(`^([0-9.]+):(\d+)$`)

// TestNetnsFlood sends initiations with a valid MAC1 for the key in
// OBLAB_FLOOD_KEY to OBLAB_FLOOD_TARGET, OBLAB_FLOOD_RATE a second, in the
// transport's datagram framing, until it is killed.
func TestNetnsFlood(t *testing.T) {
	target := os.Getenv("OBLAB_FLOOD_TARGET")
	if target == "" || !floodLine.MatchString(target) {
		t.Skip("run by TestRelayRestartReconnect")
	}
	key, err := base64.StdEncoding.DecodeString(os.Getenv("OBLAB_FLOOD_KEY"))
	if err != nil || len(key) != 32 {
		t.Fatal("OBLAB_FLOOD_KEY")
	}
	rate, _ := strconv.Atoi(os.Getenv("OBLAB_FLOOD_RATE"))
	c, err := net.Dial("udp", target)
	if err != nil {
		t.Fatal(err)
	}
	var gen device.CookieGenerator
	gen.Init(device.NoisePublicKey(key))
	msg := make([]byte, device.MessageInitiationSize)
	packet := make([]byte, 5+len(msg))
	copy(packet, "OBM"+string(rune('1'+wire.Protocol)))
	tick := time.NewTicker(time.Second / 100)
	defer tick.Stop()
	for range tick.C {
		for i := 0; i < max(1, rate/100); i++ {
			rand.Read(msg)
			binary.LittleEndian.PutUint32(msg, device.MessageInitiationType)
			gen.AddMacs(msg)
			copy(packet[5:], msg)
			c.Write(packet)
		}
	}
}
