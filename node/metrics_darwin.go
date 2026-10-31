//go:build darwin

package node

import (
	"bufio"
	"encoding/binary"
	"net"
	"os"
	"os/exec"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/unix"
)

var (
	platformUUID = regexp.MustCompile(`"IOPlatformUUID" = "([0-9A-Fa-f-]{16,64})"`)
	apfsDevice   = regexp.MustCompile(`^/dev/(disk\d+)`)
	diskKinds    = map[string]bool{"apfs": true, "hfs": true, "exfat": true, "msdos": true, "ntfs": true}
)

// macOS has no sysctl for CPU ticks and the Mach call needs cgo, so one
// iostat process reports the busy share every three seconds.
type Collector struct {
	mu       sync.Mutex
	busy     float64
	sampling bool
	retryAt  time.Time

	rx, tx  uint64
	netAt   time.Time
	names   map[int]string
	namesAt time.Time
	procs   int
	procsAt time.Time
}

func NewCollector() *Collector { return &Collector{} }

func sysctl(name string) string {
	v, _ := unix.Sysctl(name)
	return strings.TrimSpace(v)
}

func sysctlUint32(name string) int64 {
	v, _ := unix.SysctlUint32(name)
	return int64(v)
}

func (c *Collector) Info() Info {
	info := Info{Platform: runtime.GOOS, Arch: runtime.GOARCH, Cores: runtime.NumCPU()}
	info.Hostname, _ = os.Hostname()
	info.OS = strings.TrimSpace("macOS " + sysctl("kern.osproductversion"))
	info.Kernel = sysctl("kern.osrelease")
	info.CPU = sysctl("machdep.cpu.brand_string")
	if sysctlUint32("kern.hv_vmm_present") == 1 {
		info.Virtualization = "apple"
	}
	mem, _ := unix.SysctlUint64("hw.memsize")
	info.MemTotal = int64(mem)
	info.SwapTotal = swapUsage().Total
	info.DiskTotal, _ = disks()
	if out, err := exec.Command("/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice").Output(); err == nil {
		if m := platformUUID.FindSubmatch(out); m != nil {
			info.MachineID = strings.ToLower(string(m[1]))
		}
	}
	return info
}

func (c *Collector) cpu() float64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.sampling && time.Now().After(c.retryAt) {
		c.sampling = c.startIostat()
		if !c.sampling {
			c.retryAt = time.Now().Add(time.Minute)
		}
	}
	return c.busy
}

func (c *Collector) startIostat() bool {
	cmd := exec.Command("/usr/sbin/iostat", "-C", "-n", "0", "-w", "3")
	out, err := cmd.StdoutPipe()
	if err != nil || cmd.Start() != nil {
		return false
	}
	go func() {
		s := bufio.NewScanner(out)
		first := true
		for s.Scan() {
			f := strings.Fields(s.Text())
			if len(f) < 3 {
				continue
			}
			idle, err := strconv.ParseFloat(f[2], 64)
			if _, e := strconv.ParseFloat(f[0], 64); e != nil || err != nil {
				continue // a header line
			}
			if first { // the first line averages since boot
				first = false
				continue
			}
			c.mu.Lock()
			c.busy = max(0, min(100, 100-idle))
			c.mu.Unlock()
		}
		cmd.Wait()
		c.mu.Lock()
		c.busy, c.sampling, c.retryAt = 0, false, time.Now().Add(30*time.Second)
		c.mu.Unlock()
	}()
	return true
}

func swapUsage() Usage {
	b, err := unix.SysctlRaw("vm.swapusage")
	if err != nil || len(b) < 24 {
		return Usage{}
	}
	total, used := binary.LittleEndian.Uint64(b[0:]), binary.LittleEndian.Uint64(b[16:])
	return Usage{Total: int64(total), Used: int64(used)}
}

func loadAverage() (load [3]float64) {
	b, err := unix.SysctlRaw("vm.loadavg")
	if err != nil || len(b) < 24 {
		return
	}
	scale := float64(binary.LittleEndian.Uint64(b[16:]))
	if scale <= 0 {
		return
	}
	for i := range 3 {
		load[i] = round(float64(binary.LittleEndian.Uint32(b[i*4:]))/scale, 2)
	}
	return
}

// memory counts free, speculative, file-backed and purgeable pages as
// available, the macOS equivalent of Linux's MemAvailable.
func memory() Usage {
	total, _ := unix.SysctlUint64("hw.memsize")
	spare := sysctlUint32("vm.page_free_count") + sysctlUint32("vm.page_speculative_count") +
		sysctlUint32("vm.page_pageable_external_count") + sysctlUint32("vm.page_purgeable_count")
	used := int64(total) - spare*int64(os.Getpagesize())
	return Usage{Total: int64(total), Used: max(0, used)}
}

func cstring(b []int8) string {
	s := make([]byte, 0, len(b))
	for _, c := range b {
		if c == 0 {
			break
		}
		s = append(s, byte(c))
	}
	return string(s)
}

// disks counts each APFS container once: its volumes share one pool of
// space, and the sealed system volume is mounted read-only at /.
func disks() (total, used int64) {
	n, err := syscall.Getfsstat(nil, unix.MNT_NOWAIT)
	if err != nil || n <= 0 {
		return
	}
	buf := make([]syscall.Statfs_t, n+4)
	if n, err = syscall.Getfsstat(buf, unix.MNT_NOWAIT); err != nil {
		return
	}
	seen := map[string]bool{}
	for _, st := range buf[:n] {
		kind, on, from := cstring(st.Fstypename[:]), cstring(st.Mntonname[:]), cstring(st.Mntfromname[:])
		if !diskKinds[kind] || st.Flags&unix.MNT_LOCAL == 0 || st.Blocks == 0 ||
			on != "/" && st.Flags&(unix.MNT_RDONLY|unix.MNT_DONTBROWSE) != 0 {
			continue
		}
		key := from
		if m := apfsDevice.FindStringSubmatch(from); kind == "apfs" && m != nil {
			key = "apfs:" + m[1]
		}
		if seen[key] {
			continue
		}
		seen[key] = true
		total += int64(st.Blocks) * int64(st.Bsize)
		used += int64(st.Blocks-st.Bavail) * int64(st.Bsize)
	}
	return
}

// network sums the en* interfaces (Ethernet and Wi-Fi); utun, bridge and
// awdl carry traffic that is already counted there or is local.
func (c *Collector) network() (rx, tx uint64) {
	if c.names == nil || time.Since(c.namesAt) > 5*time.Minute {
		c.names, c.namesAt = map[int]string{}, time.Now()
		if list, err := net.Interfaces(); err == nil {
			for _, i := range list {
				c.names[i.Index] = i.Name
			}
		}
	}
	rib, err := syscall.RouteRIB(syscall.NET_RT_IFLIST2, 0)
	if err != nil {
		return
	}
	for len(rib) >= 4 {
		size := int(binary.LittleEndian.Uint16(rib))
		if size == 0 || size > len(rib) {
			break
		}
		if rib[3] == unix.RTM_IFINFO2 && size >= unix.SizeofIfMsghdr2 {
			m := (*unix.IfMsghdr2)(unsafe.Pointer(&rib[0]))
			if strings.HasPrefix(c.names[int(m.Index)], "en") {
				rx, tx = rx+m.Data.Ibytes, tx+m.Data.Obytes
			}
		}
		rib = rib[size:]
	}
	return
}

func (c *Collector) processes() int {
	if time.Since(c.procsAt) > 15*time.Second {
		if list, err := unix.SysctlKinfoProcSlice("kern.proc.all"); err == nil {
			c.procs, c.procsAt = len(list), time.Now()
		}
	}
	return c.procs
}

// selfRSS asks proc_pidinfo(PROC_PIDTASKINFO) for this process.
func selfRSS() int64 {
	var task [96]byte
	n, _, errno := syscall.Syscall6(syscall.SYS_PROC_INFO, 2, uintptr(os.Getpid()), 4, 0, uintptr(unsafe.Pointer(&task[0])), uintptr(len(task)))
	if errno != 0 || int(n) < 16 {
		return 0
	}
	return int64(binary.LittleEndian.Uint64(task[8:]))
}

func (c *Collector) Sample() Sample {
	var s Sample
	s.CPU = round(c.cpu(), 2)
	s.Mem = memory()
	s.Swap = swapUsage()
	s.Load = loadAverage()
	s.Disk.Total, s.Disk.Used = disks()
	rx, tx := c.network()
	now := time.Now()
	s.Net = Traffic{RX: rx, TX: tx}
	if !c.netAt.IsZero() {
		elapsed := now.Sub(c.netAt).Seconds()
		s.Net.RXRate, s.Net.TXRate = perSecond(rx, c.rx, elapsed), perSecond(tx, c.tx, elapsed)
	}
	c.rx, c.tx, c.netAt = rx, tx, now
	s.TCP, s.UDP = int(sysctlUint32("net.inet.tcp.pcbcount")), int(sysctlUint32("net.inet.udp.pcbcount"))
	s.Processes = c.processes()
	if boot, err := unix.SysctlTimeval("kern.boottime"); err == nil {
		s.Boot = boot.Sec
		s.Uptime = max(0, now.Unix()-boot.Sec)
	}
	s.NodeRSS = selfRSS()
	return s
}
