//go:build windows

package node

import (
	"net"
	"os"
	"runtime"
	"strconv"
	"strings"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"
)

var (
	kernel32 = windows.NewLazySystemDLL("kernel32.dll")
	iphlpapi = windows.NewLazySystemDLL("iphlpapi.dll")

	procGetSystemTimes        = kernel32.NewProc("GetSystemTimes")
	procGlobalMemoryStatusEx  = kernel32.NewProc("GlobalMemoryStatusEx")
	procGetTickCount64        = kernel32.NewProc("GetTickCount64")
	procGetProcessMemoryInfo  = kernel32.NewProc("K32GetProcessMemoryInfo")
	procGetTcpStatisticsEx    = iphlpapi.NewProc("GetTcpStatisticsEx")
	procGetUdpStatisticsEx    = iphlpapi.NewProc("GetUdpStatisticsEx")
	ioctlDiskPerformance      = uint32(0x70020)
	hardwareInterface         = uint8(1)
	physicalDriveProbeTimeout = 5 * time.Minute
)

type memoryStatus struct {
	Length, MemoryLoad                        uint32
	TotalPhys, AvailPhys                      uint64
	TotalPageFile, AvailPageFile              uint64
	TotalVirtual, AvailVirtual, AvailExtended uint64
}

type Collector struct {
	idle, busy uint64
	rx, tx     uint64
	netAt      time.Time
	nics       []uint32
	nicsAt     time.Time
	ioR, ioW   uint64
	ioAt       time.Time
	disks      []int
	disksAt    time.Time
}

func NewCollector() *Collector { return &Collector{} }

func regString(k registry.Key, name string) string {
	v, _, _ := k.GetStringValue(name)
	return strings.TrimSpace(v)
}

func regInt(k registry.Key, name string) uint64 {
	v, _, _ := k.GetIntegerValue(name)
	return v
}

func (c *Collector) Info() Info {
	info := Info{Platform: runtime.GOOS, Arch: runtime.GOARCH, Cores: runtime.NumCPU()}
	info.Hostname, _ = os.Hostname()
	if k, err := registry.OpenKey(registry.LOCAL_MACHINE, `SOFTWARE\Microsoft\Windows NT\CurrentVersion`, registry.QUERY_VALUE); err == nil {
		build := regString(k, "CurrentBuildNumber")
		name := regString(k, "ProductName")
		// Windows 11 still calls itself Windows 10 here.
		if n, _ := strconv.Atoi(build); n >= 22000 {
			name = strings.Replace(name, "Windows 10", "Windows 11", 1)
		}
		if release := regString(k, "DisplayVersion"); release != "" {
			name += " (" + release + ")"
		} else if release = regString(k, "ReleaseId"); release != "" {
			name += " (" + release + ")"
		}
		info.OS = strings.TrimSpace(name)
		info.Kernel = strconv.FormatUint(regInt(k, "CurrentMajorVersionNumber"), 10) + "." + strconv.FormatUint(regInt(k, "CurrentMinorVersionNumber"), 10) +
			"." + build + "." + strconv.FormatUint(regInt(k, "UBR"), 10)
		k.Close()
	}
	if info.OS == "" {
		info.OS = "Windows"
	}
	if k, err := registry.OpenKey(registry.LOCAL_MACHINE, `HARDWARE\DESCRIPTION\System\CentralProcessor\0`, registry.QUERY_VALUE); err == nil {
		info.CPU = regString(k, "ProcessorNameString")
		k.Close()
	}
	product := strings.ToLower(info.CPU)
	if k, err := registry.OpenKey(registry.LOCAL_MACHINE, `HARDWARE\DESCRIPTION\System\BIOS`, registry.QUERY_VALUE); err == nil {
		product += " " + strings.ToLower(regString(k, "SystemManufacturer")+" "+regString(k, "SystemProductName"))
		k.Close()
	}
	for _, pair := range vmVendors {
		if strings.Contains(product, pair[0]) {
			info.Virtualization = pair[1]
			break
		}
	}
	if k, err := registry.OpenKey(registry.LOCAL_MACHINE, `SOFTWARE\Microsoft\Cryptography`, registry.QUERY_VALUE|registry.WOW64_64KEY); err == nil {
		if id := regString(k, "MachineGuid"); len(id) >= 16 && len(id) <= 64 {
			info.MachineID = strings.ToLower(id)
		}
		k.Close()
	}
	mem, swap := memory()
	info.MemTotal, info.SwapTotal = mem.Total, swap.Total
	info.DiskTotal, _ = fixedDisks()
	return info
}

func filetime(f windows.Filetime) uint64 { return uint64(f.HighDateTime)<<32 | uint64(f.LowDateTime) }

// cpu returns the busy share since the previous call; kernel time includes idle.
func (c *Collector) cpu() float64 {
	var idle, kernel, user windows.Filetime
	if r, _, _ := procGetSystemTimes.Call(uintptr(unsafe.Pointer(&idle)), uintptr(unsafe.Pointer(&kernel)), uintptr(unsafe.Pointer(&user))); r == 0 {
		return 0
	}
	i, total := filetime(idle), filetime(kernel)+filetime(user)
	prevIdle, prevTotal := c.idle, c.busy
	c.idle, c.busy = i, total
	if prevTotal == 0 || total <= prevTotal || i < prevIdle || i-prevIdle > total-prevTotal {
		return 0
	}
	return float64(total-prevTotal-(i-prevIdle)) / float64(total-prevTotal) * 100
}

// memory reports physical use and the page file's share of committed memory.
func memory() (mem, swap Usage) {
	s := memoryStatus{Length: uint32(unsafe.Sizeof(memoryStatus{}))}
	if r, _, _ := procGlobalMemoryStatusEx.Call(uintptr(unsafe.Pointer(&s))); r == 0 {
		return
	}
	mem = Usage{Total: int64(s.TotalPhys), Used: int64(s.TotalPhys - s.AvailPhys)}
	if s.TotalPageFile > s.TotalPhys {
		swap.Total = int64(s.TotalPageFile - s.TotalPhys)
		swap.Used = min(max(0, int64(s.TotalPageFile-s.AvailPageFile)-mem.Used), swap.Total)
	}
	return
}

func fixedDisks() (total, used int64) {
	roots, _ := driveRoots()
	for _, root := range roots {
		name, _ := windows.UTF16PtrFromString(root)
		if windows.GetDriveType(name) != windows.DRIVE_FIXED {
			continue
		}
		var free, size, totalFree uint64
		if windows.GetDiskFreeSpaceEx(name, &free, &size, &totalFree) == nil {
			total, used = total+int64(size), used+int64(size-totalFree)
		}
	}
	return
}

// network sums physical Ethernet and Wi-Fi adapters; VPN, Hyper-V and other
// virtual adapters carry traffic already counted there.
func (c *Collector) network() (rx, tx uint64) {
	if c.nics == nil || time.Since(c.nicsAt) > 5*time.Minute {
		c.nics, c.nicsAt = []uint32{}, time.Now()
		if list, err := net.Interfaces(); err == nil {
			for _, i := range list {
				row := windows.MibIfRow2{InterfaceIndex: uint32(i.Index)}
				if windows.GetIfEntry2Ex(windows.MibIfEntryNormal, &row) == nil && row.InterfaceAndOperStatusFlags&hardwareInterface != 0 &&
					(row.Type == windows.IF_TYPE_ETHERNET_CSMACD || row.Type == windows.IF_TYPE_IEEE80211) {
					c.nics = append(c.nics, uint32(i.Index))
				}
			}
		}
	}
	for _, index := range c.nics {
		row := windows.MibIfRow2{InterfaceIndex: index}
		if windows.GetIfEntry2Ex(windows.MibIfEntryNormal, &row) == nil {
			rx, tx = rx+row.InOctets, tx+row.OutOctets
		}
	}
	return
}

func diskPerformance(n int) (read, write uint64, ok bool) {
	name, _ := windows.UTF16PtrFromString(`\\.\PhysicalDrive` + strconv.Itoa(n))
	h, err := windows.CreateFile(name, 0, windows.FILE_SHARE_READ|windows.FILE_SHARE_WRITE, nil, windows.OPEN_EXISTING, 0, 0)
	if err != nil {
		return 0, 0, false
	}
	defer windows.CloseHandle(h)
	var perf [88]byte
	var size uint32
	if windows.DeviceIoControl(h, ioctlDiskPerformance, nil, 0, &perf[0], uint32(len(perf)), &size, nil) != nil || size < 16 {
		return 0, 0, false
	}
	return *(*uint64)(unsafe.Pointer(&perf[0])), *(*uint64)(unsafe.Pointer(&perf[8])), true
}

func (c *Collector) diskIO() (read, write uint64) {
	if c.disks == nil || time.Since(c.disksAt) > physicalDriveProbeTimeout {
		c.disks, c.disksAt = []int{}, time.Now()
		for n := range 16 {
			if _, _, ok := diskPerformance(n); ok {
				c.disks = append(c.disks, n)
			}
		}
	}
	for _, n := range c.disks {
		if r, w, ok := diskPerformance(n); ok {
			read, write = read+r, write+w
		}
	}
	return
}

// sockets counts the TCP and UDP tables of both address families.
func sockets() (tcp, udp int) {
	for _, family := range []uintptr{windows.AF_INET, windows.AF_INET6} {
		var t [15]uint32
		if r, _, _ := procGetTcpStatisticsEx.Call(uintptr(unsafe.Pointer(&t[0])), family); r == 0 {
			tcp += int(t[14])
		}
		var u [5]uint32
		if r, _, _ := procGetUdpStatisticsEx.Call(uintptr(unsafe.Pointer(&u[0])), family); r == 0 {
			udp += int(u[4])
		}
	}
	return
}

func processes() int {
	pids := make([]uint32, 1024)
	for {
		var size uint32
		if windows.EnumProcesses(pids, &size) != nil {
			return 0
		}
		if n := int(size / 4); n < len(pids) {
			return n
		}
		pids = make([]uint32, len(pids)*2)
	}
}

func selfRSS() int64 {
	var counters struct {
		Cb, PageFaultCount                                 uint32
		PeakWorkingSetSize, WorkingSetSize                 uintptr
		QuotaPeakPagedPoolUsage, QuotaPagedPoolUsage       uintptr
		QuotaPeakNonPagedPoolUsage, QuotaNonPagedPoolUsage uintptr
		PagefileUsage, PeakPagefileUsage                   uintptr
	}
	counters.Cb = uint32(unsafe.Sizeof(counters))
	if r, _, _ := procGetProcessMemoryInfo.Call(uintptr(windows.CurrentProcess()), uintptr(unsafe.Pointer(&counters)), uintptr(counters.Cb)); r == 0 {
		return 0
	}
	return int64(counters.WorkingSetSize)
}

func (c *Collector) Sample() Sample {
	var s Sample
	s.CPU = round(c.cpu(), 2)
	s.Mem, s.Swap = memory()
	s.Disk.Total, s.Disk.Used = fixedDisks()
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
	s.TCP, s.UDP = sockets()
	s.Processes = processes()
	if r, _, _ := procGetTickCount64.Call(); r > 0 {
		s.Uptime = int64(r / 1000)
		s.Boot = now.Unix() - s.Uptime
	}
	s.NodeRSS = selfRSS()
	return s
}
