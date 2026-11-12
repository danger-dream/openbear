//go:build darwin

package node

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"slices"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

var topSorts = []string{"cpu", "mem"}

// The CPU features worth naming: Intel's sysctl words, then Apple silicon's hw.optional flags.
var (
	intelFlags = [][2]string{{"AES", "AES"}, {"AVX1.0", "AVX"}, {"AVX2", "AVX2"}, {"AVX512F", "AVX-512"}, {"SSE4.2", "SSE4.2"},
		{"SHA", "SHA"}, {"VMX", "VT-x"}}
	armFlags = [][2]string{{"hw.optional.arm.FEAT_AES", "AES"}, {"hw.optional.arm.FEAT_SHA256", "SHA2"}, {"hw.optional.neon", "NEON"}}
)

var hardware struct {
	once sync.Once
	// system_profiler SPHardwareDataType: the model's name and its firmware.
	name, model, chip, firmware string
}

// szomb is SZOMB in <sys/proc.h>, which x/sys does not name.
const szomb = 5

// processAlive treats a zombie as gone: it has ended, only its parent has not collected it.
func processAlive(pid int) bool {
	if err := syscall.Kill(pid, 0); err != nil && !errors.Is(err, syscall.EPERM) {
		return false
	}
	k, err := unix.SysctlKinfoProc("kern.proc.pid", pid)
	return err != nil || k.Proc.P_stat != szomb
}

// hostTop reads ps once: its %cpu is the kernel's recent average for each
// process, of one core, so it is divided by the cores for the machine's share.
func hostTop(ctx context.Context, sort string, limit int, withCommand bool) (HostTop, error) {
	out, err := exec.CommandContext(ctx, "/bin/ps", "-axo", "pid=,user=,%cpu=,rss=,comm=").Output()
	if err != nil {
		return HostTop{}, err
	}
	cpus := float64(max(runtime.NumCPU(), 1))
	list := []HostProcess{}
	for _, line := range strings.Split(string(out), "\n") {
		f := strings.Fields(line)
		if len(f) < 5 {
			continue
		}
		pid, err := strconv.Atoi(f[0])
		if err != nil {
			continue
		}
		pcpu, _ := strconv.ParseFloat(f[2], 64)
		kb, _ := strconv.ParseInt(f[3], 10, 64)
		list = append(list, HostProcess{PID: pid, User: f[1], CPU: round(pcpu/cpus, 2), RSS: kb * 1024,
			Name: filepath.Base(strings.Join(f[4:], " "))})
	}
	top := HostTop{Sort: sort, Sorts: topSorts, Count: len(list), Processes: rank(list, sort, limit)}
	if withCommand && len(top.Processes) > 0 {
		pids := make([]string, len(top.Processes))
		for i, p := range top.Processes {
			pids[i] = strconv.Itoa(p.PID)
		}
		args, _ := exec.CommandContext(ctx, "/bin/ps", "-ww", "-o", "pid=,args=", "-p", strings.Join(pids, ",")).Output()
		for _, line := range strings.Split(string(args), "\n") {
			pid, command, _ := strings.Cut(strings.TrimSpace(line), " ")
			for i := range top.Processes {
				if strconv.Itoa(top.Processes[i].PID) == pid {
					top.Processes[i].Command = cut(strings.TrimSpace(command), 400)
				}
			}
		}
	}
	return top, nil
}

func sysctlUint64(name string) int64 {
	v, _ := unix.SysctlUint64(name)
	return int64(v)
}

func cacheSize(n int64) string {
	switch {
	case n >= 1<<20 && n%(1<<20) == 0:
		return strconv.FormatInt(n>>20, 10) + "M"
	case n > 0:
		return strconv.FormatInt(n>>10, 10) + "K"
	}
	return ""
}

func readHardware(ctx context.Context) {
	hardware.once.Do(func() {
		ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
		defer cancel()
		out, err := exec.CommandContext(ctx, "/usr/sbin/system_profiler", "-json", "SPHardwareDataType").Output()
		var data struct {
			Items []struct {
				Name     string `json:"machine_name"`
				Model    string `json:"machine_model"`
				Chip     string `json:"chip_type"`
				Firmware string `json:"boot_rom_version"`
			} `json:"SPHardwareDataType"`
		}
		if err == nil && json.Unmarshal(out, &data) == nil && len(data.Items) > 0 {
			i := data.Items[0]
			hardware.name, hardware.model, hardware.chip, hardware.firmware = i.Name, i.Model, i.Chip, i.Firmware
		}
	})
}

func hostDetails(ctx context.Context, info Info) (HostDetails, error) {
	readHardware(ctx)
	d := HostDetails{Hostname: info.Hostname, OS: info.OS, Kernel: info.Kernel, Arch: info.Arch, Virtualization: info.Virtualization,
		MemTotal: info.MemTotal, SwapTotal: info.SwapTotal, Interfaces: interfaces(darwinIfaceExtra())}
	d.CPU = HostCPU{Model: info.CPU, Vendor: sysctl("machdep.cpu.vendor"), Sockets: int(sysctlUint32("hw.packages")),
		Cores: int(sysctlUint32("hw.physicalcpu")), Threads: int(sysctlUint32("hw.logicalcpu")), MaxMHz: int(sysctlUint64("hw.cpufrequency_max") / 1e6)}
	caches := []string{}
	for _, c := range [][2]string{{"L1d", "hw.l1dcachesize"}, {"L1i", "hw.l1icachesize"}, {"L2", "hw.l2cachesize"}, {"L3", "hw.l3cachesize"}} {
		if size := cacheSize(sysctlUint64(c[1])); size != "" {
			caches = append(caches, c[0]+" "+size)
		}
	}
	d.CPU.Cache = strings.Join(caches, " · ")
	words := strings.Fields(sysctl("machdep.cpu.features") + " " + sysctl("machdep.cpu.leaf7_features"))
	for _, f := range intelFlags {
		if slices.Contains(words, f[0]) {
			d.CPU.Flags = append(d.CPU.Flags, f[1])
		}
	}
	for _, f := range armFlags {
		if sysctlUint32(f[0]) == 1 {
			d.CPU.Flags = append(d.CPU.Flags, f[1])
		}
	}
	if hardware.chip != "" && d.CPU.Model == "" {
		d.CPU.Model = hardware.chip
	}
	d.Board = HostBoard{Vendor: "Apple", Product: strings.TrimSpace(hardware.name + " " + cmpParen(hardware.model, sysctl("hw.model"))), BIOS: hardware.firmware}
	d.Disks = darwinDisks(ctx)
	d.Mounts = darwinMounts()
	if boot, err := unix.SysctlTimeval("kern.boottime"); err == nil {
		d.Boot = boot.Sec
	}
	if link, err := os.Readlink("/etc/localtime"); err == nil {
		if _, zone, ok := strings.Cut(link, "zoneinfo/"); ok {
			d.Timezone = zone
		}
	}
	if out, err := exec.CommandContext(ctx, "/usr/sbin/scutil", "--dns").Output(); err == nil {
		for _, line := range strings.Split(string(out), "\n") {
			if k, v, ok := strings.Cut(line, ":"); ok && strings.HasPrefix(strings.TrimSpace(k), "nameserver[") {
				if v = strings.TrimSpace(v); !slices.Contains(d.DNS, v) && len(d.DNS) < 6 {
					d.DNS = append(d.DNS, v)
				}
			}
		}
	}
	return d, nil
}

// darwinIfaceExtra gives each interface its byte counters from one read of the
// interface list, the one the traffic totals come from.
func darwinIfaceExtra() func(string, int) (int, uint64, uint64) {
	counts := map[int][2]uint64{}
	ifaceCounters(func(index int, rx, tx uint64) { counts[index] = [2]uint64{rx, tx} })
	return func(_ string, index int) (int, uint64, uint64) {
		return 0, counts[index][0], counts[index][1]
	}
}

// cmpParen is "(model)" from the first non-empty of the two.
func cmpParen(a, b string) string {
	for _, v := range []string{a, b} {
		if v != "" {
			return "(" + v + ")"
		}
	}
	return ""
}

// darwinDisks asks diskutil for the physical disks, then each one's name, size and medium.
func darwinDisks(ctx context.Context) []HostDisk {
	out, err := exec.CommandContext(ctx, "/usr/sbin/diskutil", "list", "physical").Output()
	disks := []HostDisk{}
	if err != nil {
		return disks
	}
	for _, line := range strings.Split(string(out), "\n") {
		dev, _, ok := strings.Cut(line, " (")
		name, isDisk := strings.CutPrefix(dev, "/dev/")
		if !ok || !isDisk {
			continue
		}
		info, err := exec.CommandContext(ctx, "/usr/sbin/diskutil", "info", name).Output()
		if err != nil {
			continue
		}
		d := HostDisk{Name: name}
		for _, field := range strings.Split(string(info), "\n") {
			k, v, ok := strings.Cut(field, ":")
			k, v = strings.TrimSpace(k), strings.TrimSpace(v)
			switch {
			case !ok:
			case k == "Device / Media Name":
				d.Model = v
			case k == "Disk Size":
				if _, bytes, ok := strings.Cut(v, "("); ok {
					if f := strings.Fields(bytes); len(f) > 0 {
						d.Size, _ = strconv.ParseInt(f[0], 10, 64)
					}
				}
			case k == "Solid State" && v == "Yes":
				d.Kind = cmpOr(d.Kind, "ssd")
			case k == "Solid State" && v == "No":
				d.Kind = "hdd"
			case k == "Protocol" && v == "PCI-Express":
				d.Kind = "nvme"
			}
		}
		disks = append(disks, d)
	}
	return disks
}

func cmpOr(a, b string) string {
	if a != "" {
		return a
	}
	return b
}

func darwinMounts() []HostMount {
	out := []HostMount{}
	n, err := syscall.Getfsstat(nil, unix.MNT_NOWAIT)
	if err != nil || n <= 0 {
		return out
	}
	buf := make([]syscall.Statfs_t, n+4)
	if n, err = syscall.Getfsstat(buf, unix.MNT_NOWAIT); err != nil {
		return out
	}
	for _, st := range buf[:n] {
		kind, on, from := cstring(st.Fstypename[:]), cstring(st.Mntonname[:]), cstring(st.Mntfromname[:])
		if !diskKinds[kind] || st.Flags&unix.MNT_LOCAL == 0 || st.Blocks == 0 || on != "/" && st.Flags&unix.MNT_DONTBROWSE != 0 {
			continue
		}
		out = append(out, HostMount{Path: on, FS: kind, Device: from, Total: int64(st.Blocks) * int64(st.Bsize),
			Used: int64(st.Blocks-st.Bavail) * int64(st.Bsize)})
	}
	return out
}
