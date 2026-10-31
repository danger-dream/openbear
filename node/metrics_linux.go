//go:build linux

package node

import (
	"bufio"
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"slices"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// Virtual, tunnel and container interfaces would double count the traffic
// that providers bill on the physical interface.
var skipInterfaces = []string{"lo", "docker", "br-", "veth", "virbr", "vmbr", "cni", "flannel", "podman", "cali",
	"vxlan", "kube", "lxcbr", "lxdbr", "wg", "tun", "tap", "tailscale", "zt", "ifb", "dummy", "sit", "ip6tnl", "gre",
	"erspan", "ipip", "obmesh"}

// layered interfaces carry what the ones below them already count.
var layered = []string{"DEVTYPE=bridge", "DEVTYPE=bond", "DEVTYPE=vlan", "DEVTYPE=macvlan", "DEVTYPE=ipvlan"}

var diskTypes = map[string]bool{"ext2": true, "ext3": true, "ext4": true, "xfs": true, "btrfs": true, "zfs": true,
	"f2fs": true, "jfs": true, "reiserfs": true, "bcachefs": true, "simfs": true}

type Collector struct {
	root     string
	cpuTotal uint64
	cpuIdle  uint64
	cpuWait  uint64
	cpuSteal uint64
	rx, tx   uint64
	netAt    time.Time
	ioR, ioW uint64
	ioAt     time.Time
	block    map[string]bool
	blockAt  time.Time
}

func NewCollector() *Collector { return &Collector{root: "/"} }

func (c *Collector) path(p string) string { return filepath.Join(c.root, p) }

func (c *Collector) read(p string) []byte {
	b, _ := os.ReadFile(c.path(p))
	return b
}

func (c *Collector) Info() Info {
	info := Info{Platform: runtime.GOOS, Arch: runtime.GOARCH, Cores: runtime.NumCPU()}
	info.Hostname, _ = os.Hostname()
	info.Kernel = strings.TrimSpace(string(c.read("proc/sys/kernel/osrelease")))
	info.OS = osName(c.read("etc/os-release"))
	info.CPU = cpuModel(c.read("proc/cpuinfo"))
	info.Virtualization = c.virtualization()
	mem := meminfo(c.read("proc/meminfo"))
	info.MemTotal, info.SwapTotal = mem["MemTotal"], mem["SwapTotal"]
	info.DiskTotal, _ = c.disks()
	for _, p := range []string{"etc/machine-id", "var/lib/dbus/machine-id"} {
		if id := strings.TrimSpace(string(c.read(p))); len(id) >= 16 && len(id) <= 64 {
			info.MachineID = id
			break
		}
	}
	return info
}

func osName(b []byte) string {
	values := map[string]string{}
	for _, line := range strings.Split(string(b), "\n") {
		if k, v, ok := strings.Cut(line, "="); ok {
			values[k] = strings.Trim(v, `"'`)
		}
	}
	if values["PRETTY_NAME"] != "" {
		return values["PRETTY_NAME"]
	}
	if values["NAME"] != "" {
		return strings.TrimSpace(values["NAME"] + " " + values["VERSION"])
	}
	return "Linux"
}

func cpuModel(b []byte) string {
	for _, key := range []string{"model name", "Hardware", "Processor", "cpu model"} {
		for _, line := range strings.Split(string(b), "\n") {
			if k, v, ok := strings.Cut(line, ":"); ok && strings.TrimSpace(k) == key {
				if v = strings.TrimSpace(v); v != "" {
					return v
				}
			}
		}
	}
	return ""
}

func (c *Collector) virtualization() string {
	if c.root == "/" {
		if out, err := exec.Command("systemd-detect-virt").Output(); err == nil {
			if v := strings.TrimSpace(string(out)); v != "" && v != "none" {
				return v
			}
		}
	}
	if _, err := os.Stat(c.path("proc/vz")); err == nil {
		if _, err = os.Stat(c.path("proc/bc")); err != nil {
			return "openvz"
		}
	}
	if _, err := os.Stat(c.path(".dockerenv")); err == nil {
		return "docker"
	}
	if env := c.read("proc/1/environ"); bytes.Contains(env, []byte("container=lxc")) {
		return "lxc"
	}
	product := strings.ToLower(strings.TrimSpace(string(c.read("sys/class/dmi/id/product_name")) + " " + string(c.read("sys/class/dmi/id/sys_vendor"))))
	for _, pair := range vmVendors {
		if strings.Contains(product, pair[0]) {
			return pair[1]
		}
	}
	return ""
}

func meminfo(b []byte) map[string]int64 {
	out := map[string]int64{}
	for _, line := range strings.Split(string(b), "\n") {
		k, v, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		fields := strings.Fields(v)
		if len(fields) == 0 {
			continue
		}
		n, _ := strconv.ParseInt(fields[0], 10, 64)
		if len(fields) > 1 && fields[1] == "kB" {
			n *= 1024
		}
		out[k] = n
	}
	return out
}

// cpu returns busy, iowait and steal percentages since the previous call,
// plus the boot time, from one read of /proc/stat.
func (c *Collector) cpu() (busy, wait, steal float64, boot int64) {
	stat := string(c.read("proc/stat"))
	for _, line := range strings.Split(stat, "\n") {
		if v, ok := strings.CutPrefix(line, "btime "); ok {
			boot, _ = strconv.ParseInt(strings.TrimSpace(v), 10, 64)
			break
		}
	}
	line, _, _ := strings.Cut(stat, "\n")
	fields := strings.Fields(line)
	if len(fields) < 5 || fields[0] != "cpu" {
		return
	}
	var total, idle, iowait, stolen uint64
	for i, f := range fields[1:] {
		if i >= 8 {
			break // guest time is already counted in user time.
		}
		n, _ := strconv.ParseUint(f, 10, 64)
		total += n
		switch i {
		case 3:
			idle += n
		case 4:
			idle += n
			iowait = n
		case 7:
			stolen = n
		}
	}
	prevTotal, prevIdle, prevWait, prevSteal := c.cpuTotal, c.cpuIdle, c.cpuWait, c.cpuSteal
	c.cpuTotal, c.cpuIdle, c.cpuWait, c.cpuSteal = total, idle, iowait, stolen
	if prevTotal == 0 || total <= prevTotal || idle < prevIdle || idle-prevIdle > total-prevTotal || iowait < prevWait || stolen < prevSteal {
		return 0, 0, 0, boot
	}
	dt := float64(total - prevTotal)
	return float64(total-prevTotal-(idle-prevIdle)) / dt * 100, float64(iowait-prevWait) / dt * 100, float64(stolen-prevSteal) / dt * 100, boot
}

// disks sums each physical filesystem once, even when bind-mounted repeatedly.
func (c *Collector) disks() (total, used int64) {
	seen := map[string]bool{}
	for _, line := range strings.Split(string(c.read("proc/self/mountinfo")), "\n") {
		left, right, ok := strings.Cut(line, " - ")
		if !ok {
			continue
		}
		lf, rf := strings.Fields(left), strings.Fields(right)
		if len(lf) < 5 || len(rf) < 2 || !diskTypes[rf[0]] || seen[lf[2]] {
			continue
		}
		var st syscall.Statfs_t
		if syscall.Statfs(c.path(unescape(lf[4])), &st) != nil || st.Blocks == 0 {
			continue
		}
		seen[lf[2]] = true
		total += int64(st.Blocks) * int64(st.Bsize)
		used += int64(st.Blocks-st.Bfree) * int64(st.Bsize)
	}
	if total == 0 {
		var st syscall.Statfs_t
		if syscall.Statfs(c.path("/"), &st) == nil {
			total, used = int64(st.Blocks)*int64(st.Bsize), int64(st.Blocks-st.Bfree)*int64(st.Bsize)
		}
	}
	return
}

func unescape(p string) string {
	return strings.NewReplacer(`\040`, " ", `\011`, "\t", `\012`, "\n", `\134`, `\`).Replace(p)
}

func countedInterface(name string) bool {
	for _, prefix := range skipInterfaces {
		if strings.HasPrefix(name, prefix) {
			return false
		}
	}
	return true
}

func (c *Collector) layered(name string) bool {
	if strings.Contains(name, "/") {
		return false
	}
	for _, line := range strings.Split(string(c.read("sys/class/net/"+name+"/uevent")), "\n") {
		if slices.Contains(layered, line) {
			return true
		}
	}
	return false
}

func (c *Collector) network() (rx, tx uint64) {
	s := bufio.NewScanner(bytes.NewReader(c.read("proc/net/dev")))
	for s.Scan() {
		name, rest, ok := strings.Cut(s.Text(), ":")
		name = strings.TrimSpace(name)
		if !ok || !countedInterface(name) || c.layered(name) {
			continue
		}
		f := strings.Fields(rest)
		if len(f) < 9 {
			continue
		}
		r, _ := strconv.ParseUint(f[0], 10, 64)
		t, _ := strconv.ParseUint(f[8], 10, 64)
		rx, tx = rx+r, tx+t
	}
	return
}

// sockets reads the kernel's socket counters. Listing /proc/net/tcp instead
// costs time proportional to the connections, which is heavy on proxies.
func (c *Collector) sockets() (tcp, udp int) {
	for _, name := range []string{"proc/net/sockstat", "proc/net/sockstat6"} {
		for _, line := range strings.Split(string(c.read(name)), "\n") {
			proto, rest, ok := strings.Cut(line, ":")
			if !ok {
				continue
			}
			f := strings.Fields(rest)
			for i := 0; i+1 < len(f); i += 2 {
				n, _ := strconv.Atoi(f[i+1])
				switch {
				case (proto == "TCP" || proto == "TCP6") && (f[i] == "inuse" || f[i] == "tw"):
					tcp += n
				case (proto == "UDP" || proto == "UDP6") && f[i] == "inuse":
					udp += n
				}
			}
		}
	}
	return
}

// wholeDisk reports block devices backed by hardware or a hypervisor disk;
// partitions, loop, device-mapper and md devices would double count.
func (c *Collector) wholeDisk(name string) bool {
	if c.block == nil || time.Since(c.blockAt) > 5*time.Minute {
		c.block, c.blockAt = map[string]bool{}, time.Now()
	}
	if v, ok := c.block[name]; ok {
		return v
	}
	_, err := os.Stat(c.path("sys/block/" + name + "/device"))
	v := err == nil && !strings.HasPrefix(name, "loop") && !strings.HasPrefix(name, "ram") && !strings.HasPrefix(name, "zram") && !strings.HasPrefix(name, "sr")
	c.block[name] = v
	return v
}

func (c *Collector) diskIO() (read, write uint64) {
	for _, line := range strings.Split(string(c.read("proc/diskstats")), "\n") {
		f := strings.Fields(line)
		if len(f) < 10 || !c.wholeDisk(f[2]) {
			continue
		}
		r, _ := strconv.ParseUint(f[5], 10, 64)
		w, _ := strconv.ParseUint(f[9], 10, 64)
		read, write = read+r*512, write+w*512
	}
	return
}

func (c *Collector) selfRSS() int64 {
	if f := strings.Fields(string(c.read("proc/self/statm"))); len(f) > 1 {
		pages, _ := strconv.ParseInt(f[1], 10, 64)
		return pages * int64(os.Getpagesize())
	}
	return 0
}

func (c *Collector) processes() int {
	entries, _ := os.ReadDir(c.path("proc"))
	n := 0
	for _, e := range entries {
		if e.IsDir() && e.Name()[0] >= '0' && e.Name()[0] <= '9' {
			n++
		}
	}
	return n
}

func (c *Collector) Sample() Sample {
	var s Sample
	busy, wait, steal, boot := c.cpu()
	s.CPU, s.IOWait, s.Steal, s.Boot = round(busy, 2), round(wait, 2), round(steal, 2), boot
	mem := meminfo(c.read("proc/meminfo"))
	s.Mem = Usage{Total: mem["MemTotal"], Used: mem["MemTotal"] - mem["MemAvailable"]}
	if _, ok := mem["MemAvailable"]; !ok {
		s.Mem.Used = mem["MemTotal"] - mem["MemFree"] - mem["Buffers"] - mem["Cached"]
	}
	s.Swap = Usage{Total: mem["SwapTotal"], Used: mem["SwapTotal"] - mem["SwapFree"]}
	if f := strings.Fields(string(c.read("proc/loadavg"))); len(f) >= 3 {
		for i := range 3 {
			s.Load[i], _ = strconv.ParseFloat(f[i], 64)
		}
	}
	s.Disk.Total, s.Disk.Used = c.disks()
	rx, tx := c.network()
	now := time.Now()
	s.Net = Traffic{RX: rx, TX: tx}
	if !c.netAt.IsZero() {
		elapsed := now.Sub(c.netAt).Seconds()
		s.Net.RXRate, s.Net.TXRate = perSecond(rx, c.rx, elapsed), perSecond(tx, c.tx, elapsed)
	}
	c.rx, c.tx, c.netAt = rx, tx, now
	read, write := c.diskIO()
	if !c.ioAt.IsZero() {
		elapsed := now.Sub(c.ioAt).Seconds()
		s.IO = DiskIO{ReadRate: perSecond(read, c.ioR, elapsed), WriteRate: perSecond(write, c.ioW, elapsed)}
	}
	c.ioR, c.ioW, c.ioAt = read, write, now
	s.TCP, s.UDP = c.sockets()
	s.Processes = c.processes()
	if f := strings.Fields(string(c.read("proc/uptime"))); len(f) > 0 {
		up, _ := strconv.ParseFloat(f[0], 64)
		s.Uptime = int64(up)
	}
	s.NodeRSS = c.selfRSS()
	return s
}
