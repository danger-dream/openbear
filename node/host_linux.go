//go:build linux

package node

import (
	"bytes"
	"cmp"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"slices"
	"strconv"
	"strings"
	"syscall"
	"time"
)

var topSorts = []string{"cpu", "mem", "io", "conn", "net"}

// clockTicks is USER_HZ, which Linux fixes at 100 for /proc/<pid>/stat.
const clockTicks = 100.0

var containerID = regexp.MustCompile(`(?:docker-|/docker/)([0-9a-f]{64})`)

// The CPU features worth naming, as /proc/cpuinfo spells them (x86 flags, ARM Features).
var cpuFlags = [][2]string{{"aes", "AES"}, {"avx", "AVX"}, {"avx2", "AVX2"}, {"avx512f", "AVX-512"}, {"sse4_2", "SSE4.2"},
	{"sha_ni", "SHA"}, {"sha2", "SHA2"}, {"vmx", "VT-x"}, {"svm", "AMD-V"}, {"asimd", "NEON"}, {"sve", "SVE"}}

// BIOS and DMI fields vendors leave as placeholders.
var dmiPlaceholders = []string{"", "not specified", "none", "to be filled by o.e.m.", "default string", "system product name",
	"system manufacturer", "o.e.m.", "not applicable", "0123456789", "123456789", "x.x"}

type procSnap struct {
	name        string
	uid         string
	ticks       uint64
	rss         int64
	read, write uint64
}

type linuxHost struct{ root string }

func (h linuxHost) path(p string) string { return filepath.Join(h.root, p) }

func (h linuxHost) read(p string) []byte {
	b, _ := os.ReadFile(h.path(p))
	return b
}

func (h linuxHost) pids() []int {
	entries, _ := os.ReadDir(h.path("proc"))
	out := []int{}
	for _, e := range entries {
		if pid, err := strconv.Atoi(e.Name()); err == nil && e.IsDir() {
			out = append(out, pid)
		}
	}
	return out
}

func (h linuxHost) snapshot() map[int]procSnap {
	page := int64(os.Getpagesize())
	out := map[int]procSnap{}
	for _, pid := range h.pids() {
		dir := "proc/" + strconv.Itoa(pid)
		stat := h.read(dir + "/stat")
		// comm may hold spaces and parentheses: it runs to the last ')'.
		open, end := bytes.IndexByte(stat, '('), bytes.LastIndexByte(stat, ')')
		if open < 0 || end < open || end+2 > len(stat) {
			continue
		}
		f := strings.Fields(string(stat[end+2:]))
		if len(f) < 22 {
			continue
		}
		utime, _ := strconv.ParseUint(f[11], 10, 64)
		stime, _ := strconv.ParseUint(f[12], 10, 64)
		rss, _ := strconv.ParseInt(f[21], 10, 64)
		s := procSnap{name: string(stat[open+1 : end]), ticks: utime + stime, rss: rss * page}
		for _, line := range strings.Split(string(h.read(dir+"/status")), "\n") {
			if v, ok := strings.CutPrefix(line, "Uid:"); ok {
				if fields := strings.Fields(v); len(fields) > 0 {
					s.uid = fields[0]
				}
				break
			}
		}
		for _, line := range strings.Split(string(h.read(dir+"/io")), "\n") {
			k, v, _ := strings.Cut(line, ":")
			n, _ := strconv.ParseUint(strings.TrimSpace(v), 10, 64)
			switch k {
			case "read_bytes":
				s.read = n
			case "write_bytes":
				s.write = n
			}
		}
		out[pid] = s
	}
	return out
}

func (h linuxHost) users() map[string]string {
	out := map[string]string{}
	for _, line := range strings.Split(string(h.read("etc/passwd")), "\n") {
		if f := strings.Split(line, ":"); len(f) > 2 {
			out[f[2]] = f[0]
		}
	}
	return out
}

func (h linuxHost) command(pid int) string {
	b := bytes.ReplaceAll(h.read("proc/"+strconv.Itoa(pid)+"/cmdline"), []byte{0}, []byte{' '})
	return cut(strings.TrimSpace(string(b)), 400)
}

// conns counts each process's TCP connections, listening sockets aside. A
// container's sockets are in its own network namespace's tables.
func (h linuxHost) conns(pids []int) map[int]int {
	inodes, seen := map[string]bool{}, map[string]bool{}
	for _, pid := range pids {
		dir := "proc/" + strconv.Itoa(pid)
		ns, err := os.Readlink(h.path(dir + "/ns/net"))
		if err != nil || seen[ns] {
			continue
		}
		seen[ns] = true
		for _, table := range []string{"/net/tcp", "/net/tcp6"} {
			lines := strings.Split(string(h.read(dir+table)), "\n")
			for _, line := range lines[min(1, len(lines)):] {
				// sl local rem st tx:rx tr:when retrnsmt uid timeout inode
				if f := strings.Fields(line); len(f) >= 10 && f[3] != "0A" && f[9] != "0" {
					inodes[f[9]] = true
				}
			}
		}
	}
	out := map[int]int{}
	if len(inodes) == 0 {
		return out
	}
	for _, pid := range pids {
		dir := h.path("proc/" + strconv.Itoa(pid) + "/fd")
		fds, _ := os.ReadDir(dir)
		for _, fd := range fds {
			link, err := os.Readlink(filepath.Join(dir, fd.Name()))
			if inode, ok := strings.CutPrefix(link, "socket:["); err == nil && ok && inodes[strings.TrimSuffix(inode, "]")] {
				out[pid]++
			}
		}
	}
	return out
}

// devs reads one /proc/net/dev: each interface's received and sent bytes.
func devs(b []byte) map[string][2]uint64 {
	out := map[string][2]uint64{}
	for _, line := range strings.Split(string(b), "\n") {
		name, rest, ok := strings.Cut(line, ":")
		name = strings.TrimSpace(name)
		f := strings.Fields(rest)
		if !ok || name == "lo" || len(f) < 9 {
			continue
		}
		rx, _ := strconv.ParseUint(f[0], 10, 64)
		tx, _ := strconv.ParseUint(f[8], 10, 64)
		out[name] = [2]uint64{rx, tx}
	}
	return out
}

// namespaces groups the processes outside the host's network namespace by
// theirs; each group is a container, its first process stands for it.
func (h linuxHost) namespaces(pids []int) map[string][]int {
	host, _ := os.Readlink(h.path("proc/1/ns/net"))
	out := map[string][]int{}
	for _, pid := range pids {
		if ns, err := os.Readlink(h.path("proc/" + strconv.Itoa(pid) + "/ns/net")); err == nil && ns != host {
			out[ns] = append(out[ns], pid)
		}
	}
	return out
}

// containerName is Docker's name for a process's container, else its own name.
func (h linuxHost) containerName(pid int, fallback string) string {
	if m := containerID.FindSubmatch(h.read("proc/" + strconv.Itoa(pid) + "/cgroup")); m != nil {
		var config struct{ Name string }
		if json.Unmarshal(h.read("var/lib/docker/containers/"+string(m[1])+"/config.v2.json"), &config) == nil && config.Name != "" {
			return strings.TrimPrefix(config.Name, "/")
		}
		return fallback + " · " + string(m[1][:12])
	}
	return fallback
}

func (h linuxHost) nsTotals(groups map[string][]int) map[string][2]uint64 {
	out := map[string][2]uint64{}
	for ns, pids := range groups {
		var sum [2]uint64
		for _, v := range devs(h.read("proc/" + strconv.Itoa(slices.Min(pids)) + "/net/dev")) {
			sum[0], sum[1] = sum[0]+v[0], sum[1]+v[1]
		}
		out[ns] = sum
	}
	return out
}

func rates[K comparable](before, after map[K][2]uint64, elapsed float64) map[K][2]uint64 {
	out := map[K][2]uint64{}
	for k, v := range after {
		if b, ok := before[k]; ok {
			out[k] = [2]uint64{perSecond(v[0], b[0], elapsed), perSecond(v[1], b[1], elapsed)}
		}
	}
	return out
}

func hostTop(ctx context.Context, sort string, limit int, withCommand bool) (HostTop, error) {
	return linuxHost{root: "/"}.top(ctx, sort, limit, withCommand, runtime.NumCPU(), func(ctx context.Context) (float64, error) {
		start := time.Now()
		select {
		case <-ctx.Done():
			return 0, ctx.Err()
		case <-time.After(topSample):
		}
		return time.Since(start).Seconds(), nil
	})
}

// top looks twice, wait apart (it reports the seconds between), for the CPU
// and rate figures.
func (h linuxHost) top(ctx context.Context, sort string, limit int, withCommand bool, cpus int, wait func(context.Context) (float64, error)) (HostTop, error) {
	before := h.snapshot()
	var nics, nsBefore map[string][2]uint64
	var groups map[string][]int
	if sort == "net" {
		nics = devs(h.read("proc/net/dev"))
		groups = h.namespaces(h.pids())
		nsBefore = h.nsTotals(groups)
	}
	elapsed, err := wait(ctx)
	if err != nil {
		return HostTop{}, err
	}
	after := h.snapshot()
	users := h.users()
	list := make([]HostProcess, 0, len(after))
	for pid, s := range after {
		p := HostProcess{PID: pid, Name: s.name, User: cmp.Or(users[s.uid], s.uid), RSS: s.rss}
		if b, ok := before[pid]; ok && elapsed > 0 {
			if s.ticks >= b.ticks {
				p.CPU = round(float64(s.ticks-b.ticks)/clockTicks/elapsed/float64(max(cpus, 1))*100, 2)
			}
			p.ReadRate, p.WriteRate = perSecond(s.read, b.read, elapsed), perSecond(s.write, b.write, elapsed)
		}
		list = append(list, p)
	}
	if sort == "conn" || sort == "net" {
		pids := make([]int, 0, len(after))
		for pid := range after {
			pids = append(pids, pid)
		}
		// The lowest process reads its namespace's tables, as for the container traffic.
		slices.Sort(pids)
		counts := h.conns(pids)
		for i := range list {
			list[i].Conns = counts[list[i].PID]
		}
	}
	top := HostTop{Sort: sort, Sorts: topSorts, Count: len(list), Processes: rank(list, sort, limit)}
	for i := range top.Processes {
		if withCommand {
			top.Processes[i].Command = h.command(top.Processes[i].PID)
		}
	}
	if sort == "net" {
		top.Interfaces = []HostRate{}
		for name, r := range rates(nics, devs(h.read("proc/net/dev")), elapsed) {
			if !strings.HasPrefix(name, "veth") {
				top.Interfaces = append(top.Interfaces, HostRate{Name: name, RXRate: r[0], TXRate: r[1]})
			}
		}
		top.Interfaces = rankRates(top.Interfaces, 20)
		top.Netns = []HostRate{}
		for ns, r := range rates(nsBefore, h.nsTotals(groups), elapsed) {
			first := slices.Min(groups[ns])
			top.Netns = append(top.Netns, HostRate{Name: h.containerName(first, after[first].name), PIDs: len(groups[ns]), RXRate: r[0], TXRate: r[1]})
		}
		top.Netns = rankRates(top.Netns, limit)
	}
	return top, nil
}

// processAlive treats a zombie as gone: it has ended, only its parent has not collected it.
func processAlive(pid int) bool {
	stat, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/stat")
	if err != nil {
		return false
	}
	end := bytes.LastIndexByte(stat, ')')
	return end < 0 || end+2 >= len(stat) || stat[end+2] != 'Z'
}

func hostDetails(_ context.Context, info Info) (HostDetails, error) {
	d := linuxHost{root: "/"}.details(info, interfaces(linuxIfaceExtra))
	d.Proxy = proxyStatus()
	return d, nil
}

func linuxIfaceExtra(name string, _ int) (speed int, rx, tx uint64) {
	h := linuxHost{root: "/"}
	speed, _ = strconv.Atoi(strings.TrimSpace(string(h.read("sys/class/net/" + name + "/speed"))))
	rx, _ = strconv.ParseUint(strings.TrimSpace(string(h.read("sys/class/net/"+name+"/statistics/rx_bytes"))), 10, 64)
	tx, _ = strconv.ParseUint(strings.TrimSpace(string(h.read("sys/class/net/"+name+"/statistics/tx_bytes"))), 10, 64)
	return max(speed, 0), rx, tx
}

func (h linuxHost) details(info Info, ifaces []HostIface) HostDetails {
	d := HostDetails{Hostname: info.Hostname, OS: info.OS, Kernel: info.Kernel, Arch: info.Arch, Virtualization: info.Virtualization,
		MemTotal: info.MemTotal, SwapTotal: info.SwapTotal, CPU: h.cpuDetails(info), Board: h.board(), Disks: h.blockDisks(info.Virtualization != ""),
		Mounts: h.mounts(), Interfaces: ifaces, Timezone: h.timezone(), DNS: h.dns()}
	for _, line := range strings.Split(string(h.read("proc/stat")), "\n") {
		if v, ok := strings.CutPrefix(line, "btime "); ok {
			d.Boot, _ = strconv.ParseInt(strings.TrimSpace(v), 10, 64)
		}
	}
	return d
}

func (h linuxHost) cpuDetails(info Info) HostCPU {
	c := HostCPU{Model: info.CPU}
	sockets, coresPer, features := map[string]bool{}, 0, ""
	for _, line := range strings.Split(string(h.read("proc/cpuinfo")), "\n") {
		k, v, ok := strings.Cut(line, ":")
		k, v = strings.TrimSpace(k), strings.TrimSpace(v)
		if !ok {
			continue
		}
		switch k {
		case "processor":
			c.Threads++
		case "physical id":
			sockets[v] = true
		case "cpu cores":
			coresPer, _ = strconv.Atoi(v)
		case "vendor_id":
			c.Vendor = v
		case "cpu MHz":
			if c.MHz == 0 {
				mhz, _ := strconv.ParseFloat(v, 64)
				c.MHz = int(mhz + .5)
			}
		case "cache size":
			c.Cache = cmp.Or(c.Cache, v)
		case "flags", "Features":
			features = cmp.Or(features, v)
		}
	}
	c.Threads = cmp.Or(c.Threads, info.Cores)
	if len(sockets) > 0 {
		c.Sockets = len(sockets)
		if coresPer > 0 {
			c.Cores = coresPer * len(sockets)
		}
	}
	if khz, err := strconv.Atoi(strings.TrimSpace(string(h.read("sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")))); err == nil {
		c.MaxMHz = khz / 1000
	}
	if caches := h.caches(); caches != "" {
		c.Cache = caches
	}
	have := strings.Fields(features)
	for _, f := range cpuFlags {
		if slices.Contains(have, f[0]) {
			c.Flags = append(c.Flags, f[1])
		}
	}
	return c
}

// caches lists cpu0's caches by level, as lscpu does: "L1d 32K · L1i 32K · L2 512K · L3 32M".
func (h linuxHost) caches() string {
	dirs, _ := filepath.Glob(h.path("sys/devices/system/cpu/cpu0/cache/index*"))
	slices.Sort(dirs)
	parts := []string{}
	for _, dir := range dirs {
		read := func(name string) string { b, _ := os.ReadFile(filepath.Join(dir, name)); return strings.TrimSpace(string(b)) }
		level, kind, size := read("level"), read("type"), read("size")
		if level == "" || size == "" || kind == "Unified" && level == "1" {
			continue
		}
		name := "L" + level
		if level == "1" {
			name += map[string]string{"Data": "d", "Instruction": "i"}[kind]
		}
		if k, err := strconv.Atoi(strings.TrimSuffix(size, "K")); err == nil && strings.HasSuffix(size, "K") && k >= 1024 && k%1024 == 0 {
			size = strconv.Itoa(k/1024) + "M"
		}
		parts = append(parts, name+" "+size)
	}
	return strings.Join(parts, " · ")
}

func (h linuxHost) dmi(name string) string {
	v := strings.TrimSpace(string(h.read("sys/class/dmi/id/" + name)))
	if slices.Contains(dmiPlaceholders, strings.ToLower(v)) {
		return ""
	}
	return v
}

func (h linuxHost) board() HostBoard {
	b := HostBoard{Vendor: h.dmi("sys_vendor"), Product: h.dmi("product_name"), Version: h.dmi("product_version")}
	if b.Product == "" {
		b.Vendor, b.Product = cmp.Or(b.Vendor, h.dmi("board_vendor")), h.dmi("board_name")
	}
	if b.Product == "" {
		// ARM boards have no DMI; the device tree names them, e.g. "FriendlyElec NanoPC-T6".
		b.Product = strings.TrimSpace(strings.TrimRight(string(h.read("proc/device-tree/model")), "\x00"))
	}
	if bios := strings.TrimSpace(h.dmi("bios_vendor") + " " + h.dmi("bios_version")); bios != "" {
		if date := h.dmi("bios_date"); date != "" {
			bios += "（" + date + "）"
		}
		b.BIOS = bios
	}
	return b
}

// blockDisks lists the whole disks. In a VM the kernel's rotational flag says
// what the hypervisor emulates, not what the disk is.
func (h linuxHost) blockDisks(virtual bool) []HostDisk {
	entries, _ := os.ReadDir(h.path("sys/block"))
	out := []HostDisk{}
	for _, e := range entries {
		name := e.Name()
		if _, err := os.Stat(h.path("sys/block/" + name + "/device")); err != nil ||
			strings.HasPrefix(name, "loop") || strings.HasPrefix(name, "ram") || strings.HasPrefix(name, "zram") || strings.HasPrefix(name, "sr") {
			continue
		}
		sectors, _ := strconv.ParseInt(strings.TrimSpace(string(h.read("sys/block/"+name+"/size"))), 10, 64)
		d := HostDisk{Name: name, Size: sectors * 512,
			Model: strings.Join(strings.Fields(string(h.read("sys/block/"+name+"/device/vendor"))+" "+string(h.read("sys/block/"+name+"/device/model"))), " ")}
		switch rotational := strings.TrimSpace(string(h.read("sys/block/" + name + "/queue/rotational"))); {
		case virtual:
			d.Kind = "virtual"
		case strings.HasPrefix(name, "nvme"):
			d.Kind = "nvme"
		case rotational == "0":
			d.Kind = "ssd"
		case rotational == "1":
			d.Kind = "hdd"
		}
		out = append(out, d)
	}
	return out
}

// mounts lists each physical filesystem once, at its first mount point.
func (h linuxHost) mounts() []HostMount {
	out, seen := []HostMount{}, map[string]bool{}
	for _, line := range strings.Split(string(h.read("proc/self/mountinfo")), "\n") {
		left, right, ok := strings.Cut(line, " - ")
		lf, rf := strings.Fields(left), strings.Fields(right)
		if !ok || len(lf) < 5 || len(rf) < 2 || !diskTypes[rf[0]] || seen[lf[2]] {
			continue
		}
		path := unescape(lf[4])
		var st syscall.Statfs_t
		if syscall.Statfs(h.path(path), &st) != nil || st.Blocks == 0 {
			continue
		}
		seen[lf[2]] = true
		out = append(out, HostMount{Path: path, FS: rf[0], Device: rf[1], Total: int64(st.Blocks) * int64(st.Bsize),
			Used: int64(st.Blocks-st.Bfree) * int64(st.Bsize)})
	}
	return out
}

func (h linuxHost) timezone() string {
	if link, err := os.Readlink(h.path("etc/localtime")); err == nil {
		if _, zone, ok := strings.Cut(link, "zoneinfo/"); ok {
			return zone
		}
	}
	return strings.TrimSpace(string(h.read("etc/timezone")))
}

// dns reads resolv.conf; behind systemd-resolved's stub it reads the servers it forwards to.
func (h linuxHost) dns() []string {
	read := func(name string) []string {
		out := []string{}
		for _, line := range strings.Split(string(h.read(name)), "\n") {
			if f := strings.Fields(line); len(f) >= 2 && f[0] == "nameserver" && !slices.Contains(out, f[1]) {
				out = append(out, f[1])
			}
		}
		return out
	}
	servers := read("etc/resolv.conf")
	if len(servers) == 1 && servers[0] == "127.0.0.53" {
		if upstream := read("run/systemd/resolve/resolv.conf"); len(upstream) > 0 {
			return upstream
		}
	}
	return servers
}
