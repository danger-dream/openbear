//go:build linux

package node

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

func link(t *testing.T, root, name, target string) {
	t.Helper()
	p := filepath.Join(root, name)
	os.MkdirAll(filepath.Dir(p), 0o755)
	if err := os.Symlink(target, p); err != nil {
		t.Fatal(err)
	}
}

// proc writes one process: its stat (ticks, rss pages), owner and I/O counters.
func proc(t *testing.T, root string, pid int, name string, ticks, rssPages, readBytes, writeBytes uint64, uid string) {
	t.Helper()
	dir := fmt.Sprintf("proc/%d/", pid)
	// pid (comm) state ppid ... field 14 utime, 15 stime, 24 rss
	write(t, root, dir+"stat", fmt.Sprintf("%d (%s) S 1 1 1 0 -1 0 0 0 0 0 %d 0 0 0 20 0 1 0 100 1000 %d 0 0", pid, name, ticks, rssPages))
	write(t, root, dir+"status", "Name:\t"+name+"\nUid:\t"+uid+"\t"+uid+"\t"+uid+"\t"+uid+"\n")
	write(t, root, dir+"io", fmt.Sprintf("rchar: 1\nread_bytes: %d\nwrite_bytes: %d\n", readBytes, writeBytes))
}

func TestHostTopRanksByCPUMemoryAndIO(t *testing.T) {
	root := t.TempDir()
	write(t, root, "etc/passwd", "root:x:0:0::/root:/bin/sh\nwww:x:33:33::/var/www:/bin/sh\n")
	proc(t, root, 10, "python (worker)", 100, 10, 0, 0, "0")
	proc(t, root, 20, "nginx", 100, 1000, 0, 0, "33")
	proc(t, root, 30, "fio", 100, 5, 1000, 0, "1001")
	write(t, root, "proc/20/cmdline", "nginx: master\x00-g\x00daemon off;\x00")
	h := linuxHost{root: root}
	top, err := h.top(context.Background(), "cpu", 2, true, 2, func(context.Context) (float64, error) {
		proc(t, root, 10, "python (worker)", 200, 10, 0, 0, "0") // 100 ticks in 1 s of 2 CPUs: half the machine
		proc(t, root, 20, "nginx", 120, 1000, 0, 0, "33")
		proc(t, root, 30, "fio", 100, 5, 51000, 2000, "1001")
		return 1, nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if top.Count != 3 || len(top.Processes) != 2 {
		t.Fatalf("count %d, rows %d", top.Count, len(top.Processes))
	}
	if p := top.Processes[0]; p.PID != 10 || p.Name != "python (worker)" || p.CPU != 50 || p.User != "root" {
		t.Fatalf("busiest %+v", p)
	}
	if p := top.Processes[1]; p.PID != 20 || p.CPU != 10 || p.User != "www" || p.Command != "nginx: master -g daemon off;" || p.RSS != 1000*int64(os.Getpagesize()) {
		t.Fatalf("second %+v", p)
	}
	mem, _ := h.top(context.Background(), "mem", 1, false, 2, func(context.Context) (float64, error) { return 1, nil })
	if mem.Processes[0].PID != 20 || mem.Processes[0].Command != "" {
		t.Fatalf("by memory %+v", mem.Processes)
	}
	proc(t, root, 30, "fio", 100, 5, 1000, 0, "1001")
	io, _ := h.top(context.Background(), "io", 3, false, 2, func(context.Context) (float64, error) {
		proc(t, root, 30, "fio", 100, 5, 51000, 2000, "1001")
		return 2, nil
	})
	if p := io.Processes[0]; p.PID != 30 || p.ReadRate != 25000 || p.WriteRate != 1000 || p.User != "1001" {
		t.Fatalf("by I/O %+v", io.Processes)
	}
}

func TestHostTopCountsConnectionsAndTrafficPerContainer(t *testing.T) {
	root := t.TempDir()
	const header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
	sock := func(inode, state string) string {
		return "   0: 0100007F:1F90 0100007F:D431 " + state + " 00000000:00000000 00:00000000 00000000     0        0 " + inode + " 1\n"
	}
	proc(t, root, 1, "systemd", 0, 1, 0, 0, "0")
	proc(t, root, 100, "xray", 0, 1, 0, 0, "0")
	proc(t, root, 200, "node", 0, 1, 0, 0, "0")
	proc(t, root, 201, "node", 0, 1, 0, 0, "0")
	for _, pid := range []string{"1", "100"} {
		link(t, root, "proc/"+pid+"/ns/net", "net:[4026531840]")
	}
	for _, pid := range []string{"200", "201"} {
		link(t, root, "proc/"+pid+"/ns/net", "net:[4026532999]")
	}
	// The host's table: two connections and a listener of xray; the container's own table: one of node.
	write(t, root, "proc/1/net/tcp", header+sock("5001", "01")+sock("5002", "01")+sock("5003", "0A"))
	write(t, root, "proc/1/net/tcp6", header)
	write(t, root, "proc/200/net/tcp", header+sock("6001", "01"))
	for fd, target := range map[string]string{"3": "socket:[5001]", "4": "socket:[5002]", "5": "socket:[5003]", "6": "/var/log/x"} {
		link(t, root, "proc/100/fd/"+fd, target)
	}
	link(t, root, "proc/200/fd/3", "socket:[6001]")
	write(t, root, "proc/200/cgroup", "0::/system.slice/docker-"+strings.Repeat("ab", 32)+".scope\n")
	write(t, root, "var/lib/docker/containers/"+strings.Repeat("ab", 32)+"/config.v2.json", `{"Name":"/web-app"}`)
	dev := func(eth0rx, eth0tx, vethrx, docker string) string {
		return "Inter-|\n face |\n    lo: 9 0 0 0 0 0 0 0 9 0\n  eth0: " + eth0rx + " 0 0 0 0 0 0 0 " + eth0tx + " 0\nveth1: " + vethrx + " 0 0 0 0 0 0 0 0 0\ndocker0: " + docker + " 0 0 0 0 0 0 0 0 0\n"
	}
	write(t, root, "proc/net/dev", dev("1000", "2000", "50", "0"))
	write(t, root, "proc/200/net/dev", "Inter-|\n face |\n    lo: 9 0 0 0 0 0 0 0 9 0\n  eth0: 100 0 0 0 0 0 0 0 100 0\n")
	top, err := linuxHost{root: root}.top(context.Background(), "net", 10, false, 1, func(context.Context) (float64, error) {
		write(t, root, "proc/net/dev", dev("3000", "2500", "999", "400"))
		write(t, root, "proc/200/net/dev", "Inter-|\n face |\n    lo: 99 0 0 0 0 0 0 0 99 0\n  eth0: 900 0 0 0 0 0 0 0 300 0\n")
		return 2, nil
	})
	if err != nil {
		t.Fatal(err)
	}
	got := []string{}
	for _, p := range top.Processes {
		got = append(got, fmt.Sprint(p.PID, ":", p.Conns))
	}
	if !slices.Equal(got, []string{"100:2", "200:1"}) {
		t.Fatalf("by connections %v", got)
	}
	if len(top.Interfaces) != 2 || top.Interfaces[0] != (HostRate{Name: "eth0", RXRate: 1000, TXRate: 250}) || top.Interfaces[1] != (HostRate{Name: "docker0", RXRate: 200}) {
		t.Fatalf("interfaces %+v", top.Interfaces)
	}
	if len(top.Netns) != 1 || top.Netns[0] != (HostRate{Name: "web-app", PIDs: 2, RXRate: 400, TXRate: 100}) {
		t.Fatalf("containers %+v", top.Netns)
	}
}

func TestHostDetailsReadsTheMachine(t *testing.T) {
	root := t.TempDir()
	cpu := "processor\t: 0\nvendor_id\t: AuthenticAMD\nmodel name\t: AMD EPYC 7B13 64-Core Processor\nphysical id\t: 0\ncpu cores\t: 2\ncpu MHz\t\t: 2449.998\ncache size\t: 512 KB\nflags\t\t: fpu sse4_2 avx avx2 aes svm hypervisor\n\n"
	write(t, root, "proc/cpuinfo", cpu+strings.Replace(cpu, "processor\t: 0", "processor\t: 1", 1))
	write(t, root, "sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq", "3500000\n")
	for i, c := range [][3]string{{"1", "Data", "32K"}, {"1", "Instruction", "32K"}, {"2", "Unified", "512K"}, {"3", "Unified", "32768K"}} {
		dir := fmt.Sprintf("sys/devices/system/cpu/cpu0/cache/index%d/", i)
		write(t, root, dir+"level", c[0])
		write(t, root, dir+"type", c[1])
		write(t, root, dir+"size", c[2])
	}
	write(t, root, "sys/class/dmi/id/sys_vendor", "QEMU\n")
	write(t, root, "sys/class/dmi/id/product_name", "Standard PC (Q35 + ICH9, 2009)\n")
	write(t, root, "sys/class/dmi/id/product_version", "To Be Filled By O.E.M.\n")
	write(t, root, "sys/class/dmi/id/bios_vendor", "SeaBIOS\n")
	write(t, root, "sys/class/dmi/id/bios_version", "1.16.0\n")
	write(t, root, "sys/class/dmi/id/bios_date", "04/01/2014\n")
	write(t, root, "sys/block/vda/size", "41943040\n")
	write(t, root, "sys/block/vda/queue/rotational", "1\n")
	write(t, root, "sys/block/vda/device/vendor", "0x1af4\n")
	write(t, root, "sys/block/loop0/device/x", "")
	write(t, root, "sys/block/loop0/size", "8\n")
	write(t, root, "proc/stat", "cpu 1 2 3\nbtime 1700000000\n")
	link(t, root, "etc/localtime", "/usr/share/zoneinfo/Asia/Shanghai")
	write(t, root, "etc/resolv.conf", "# stub\nnameserver 127.0.0.53\nsearch lan\n")
	write(t, root, "run/systemd/resolve/resolv.conf", "nameserver 1.1.1.1\nnameserver 8.8.8.8\n")
	write(t, root, "proc/self/mountinfo", "22 1 254:1 / / rw - ext4 /dev/vda1 rw\n23 22 254:1 /x /bind rw - ext4 /dev/vda1 rw\n24 22 0:5 / /proc rw - proc proc rw\n")
	h := linuxHost{root: root}
	d := h.details(Info{Hostname: "box", OS: "Debian GNU/Linux 12 (bookworm)", Kernel: "6.12.95", Arch: "amd64", CPU: "AMD EPYC 7B13 64-Core Processor",
		Virtualization: "kvm", Cores: 2, MemTotal: 1 << 30}, nil)
	c := d.CPU
	if c.Vendor != "AuthenticAMD" || c.Sockets != 1 || c.Cores != 2 || c.Threads != 2 || c.MHz != 2450 || c.MaxMHz != 3500 ||
		c.Cache != "L1d 32K · L1i 32K · L2 512K · L3 32M" || !slices.Equal(c.Flags, []string{"AES", "AVX", "AVX2", "SSE4.2", "AMD-V"}) {
		t.Fatalf("cpu %+v", c)
	}
	if d.Board != (HostBoard{Vendor: "QEMU", Product: "Standard PC (Q35 + ICH9, 2009)", BIOS: "SeaBIOS 1.16.0（04/01/2014）"}) {
		t.Fatalf("board %+v", d.Board)
	}
	if len(d.Disks) != 1 || d.Disks[0] != (HostDisk{Name: "vda", Model: "0x1af4", Size: 20 << 30, Kind: "virtual"}) {
		t.Fatalf("disks %+v", d.Disks)
	}
	if len(d.Mounts) != 1 || d.Mounts[0].Path != "/" || d.Mounts[0].FS != "ext4" || d.Mounts[0].Device != "/dev/vda1" {
		t.Fatalf("mounts %+v", d.Mounts)
	}
	if d.Boot != 1700000000 || d.Timezone != "Asia/Shanghai" || !slices.Equal(d.DNS, []string{"1.1.1.1", "8.8.8.8"}) || d.OS == "" || d.MemTotal != 1<<30 {
		t.Fatalf("system %+v", d)
	}
	write(t, root, "sys/block/vda/queue/rotational", "0\n")
	if disks := h.blockDisks(false); disks[0].Kind != "ssd" {
		t.Fatalf("a physical SSD %+v", disks)
	}
}

func TestARMBoardFromDeviceTree(t *testing.T) {
	root := t.TempDir()
	var cpuinfo strings.Builder
	for i, part := range []string{"0xd05", "0xd05", "0xd0b", "0xd0b"} {
		fmt.Fprintf(&cpuinfo, "processor\t: %d\nBogoMIPS\t: 48.00\nFeatures\t: fp asimd aes sha2\nCPU implementer\t: 0x41\nCPU part\t: %s\n\n", i, part)
	}
	write(t, root, "proc/cpuinfo", cpuinfo.String())
	write(t, root, "proc/device-tree/model", "FriendlyElec NanoPC-T6\x00")
	write(t, root, "proc/device-tree/compatible", "friendlyelec,nanopc-t6\x00rockchip,rk3588\x00")
	if got := (&Collector{root: root}).Info().CPU; got != "Rockchip RK3588 (2× Cortex-A55 + 2× Cortex-A76)" {
		t.Fatalf("cpu %q", got)
	}
	if b := (linuxHost{root: root}).board(); b != (HostBoard{Product: "FriendlyElec NanoPC-T6"}) {
		t.Fatalf("board %+v", b)
	}
	if got := armCPU([]byte("processor\t: 0\nCPU part\t: 0xd03\n"), nil); got != "1× Cortex-A53" {
		t.Fatalf("without a device tree %q", got)
	}
}

func TestHostTopOnThisMachine(t *testing.T) {
	top, err := hostTop(context.Background(), "conn", 5, false)
	if err != nil || top.Count == 0 || top.Sort != "conn" {
		t.Fatalf("%+v %v", top, err)
	}
	for _, p := range top.Processes {
		if p.Conns == 0 || p.Command != "" {
			t.Fatalf("row %+v", p)
		}
	}
	if !processAlive(os.Getpid()) || processAlive(1<<22) {
		t.Fatal("alive")
	}
}

func TestHostMethodsKeepConsentForCommandsAndKilling(t *testing.T) {
	call := func(a *Agent, method, params string) (any, error) {
		result, _, err := a.dispatch(context.Background(), envelope{Method: method, Params: json.RawMessage(params)})
		return result, err
	}
	off, on := NewAgent(Config{}, nil), NewAgent(Config{Capabilities: Capabilities{Exec: true}}, nil)
	// Listing needs no consent, but the command lines (which may carry secrets) only come with it.
	for a, wantCommand := range map[*Agent]bool{off: false, on: true} {
		result, err := call(a, "host.top", `{"sort":"mem","limit":3}`)
		top, _ := result.(HostTop)
		if err != nil || len(top.Processes) != 3 || (top.Processes[0].Command != "") != wantCommand {
			t.Fatalf("exec %v: %+v %v", wantCommand, top.Processes, err)
		}
	}
	if _, err := call(off, "host.top", `{"sort":"gpu"}`); err == nil || err.Error() != "unsupported_sort" {
		t.Fatalf("unknown order: %v", err)
	}
	if result, err := call(off, "host.details", ``); err != nil || result.(HostDetails).Hostname == "" {
		t.Fatalf("details: %v", err)
	}
	child := exec.Command("sleep", "30")
	if err := child.Start(); err != nil {
		t.Fatal(err)
	}
	go child.Wait()
	params := fmt.Sprintf(`{"pid":%d}`, child.Process.Pid)
	if _, err := call(off, "host.kill", params); err == nil || err.Error() != "capability_disabled" {
		t.Fatalf("without consent: %v", err)
	}
	for _, pid := range []string{`{"pid":1}`, `{"pid":0}`, fmt.Sprintf(`{"pid":%d}`, os.Getpid())} {
		if _, err := call(on, "host.kill", pid); err == nil || err.Error() != "invalid_pid" {
			t.Fatalf("%s: %v", pid, err)
		}
	}
	result, err := call(on, "host.kill", params)
	if err != nil || result.(map[string]any)["exited"] != true {
		t.Fatalf("kill: %v %v", result, err)
	}
	if _, err := call(on, "host.kill", params); err == nil || err.Error() != "process_not_found" {
		t.Fatalf("again: %v", err)
	}
}
