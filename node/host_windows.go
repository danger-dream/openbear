//go:build windows

package node

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os/exec"
	"runtime"
	"strconv"
	"strings"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

var topSorts = []string{"cpu", "mem"}

// stillActive is GetExitCodeProcess's answer for a running process.
const stillActive = 259

// 0 is the idle process and 4 the kernel.
func protectedPID(pid int) bool { return pid <= 4 }

// signalProcess asks the process to close as taskkill does, which console and
// service processes refuse: they report back at once, for the user to force.
func signalProcess(pid int, force bool) (bool, error) {
	if !processAlive(pid) {
		return false, errors.New("process_not_found")
	}
	if !force {
		if err := exec.Command("taskkill", "/PID", strconv.Itoa(pid)).Run(); err != nil {
			return false, nil
		}
		return true, nil
	}
	h, err := windows.OpenProcess(windows.PROCESS_TERMINATE, false, uint32(pid))
	if err != nil {
		return false, err
	}
	defer windows.CloseHandle(h)
	return true, windows.TerminateProcess(h, 1)
}

func processAlive(pid int) bool {
	h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(pid))
	if err != nil {
		return errors.Is(err, windows.ERROR_ACCESS_DENIED)
	}
	defer windows.CloseHandle(h)
	var code uint32
	return windows.GetExitCodeProcess(h, &code) == nil && code == stillActive
}

type winSnap struct {
	name  string
	times uint64 // kernel and user time, 100 ns units
	rss   int64
}

func winSnapshot() map[int]winSnap {
	out := map[int]winSnap{}
	snap, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPPROCESS, 0)
	if err != nil {
		return out
	}
	defer windows.CloseHandle(snap)
	e := windows.ProcessEntry32{Size: uint32(unsafe.Sizeof(windows.ProcessEntry32{}))}
	for err = windows.Process32First(snap, &e); err == nil; err = windows.Process32Next(snap, &e) {
		s := winSnap{name: windows.UTF16ToString(e.ExeFile[:])}
		if h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, e.ProcessID); err == nil {
			var created, exited, kernel, user windows.Filetime
			if windows.GetProcessTimes(h, &created, &exited, &kernel, &user) == nil {
				s.times = filetime(kernel) + filetime(user)
			}
			s.rss = workingSet(h)
			windows.CloseHandle(h)
		}
		out[int(e.ProcessID)] = s
	}
	return out
}

// owner is the account a process runs as, and its image path for the command.
func owner(pid int, users map[string]string) (user, image string) {
	h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(pid))
	if err != nil {
		return "", ""
	}
	defer windows.CloseHandle(h)
	buf := make([]uint16, windows.MAX_LONG_PATH)
	size := uint32(len(buf))
	if windows.QueryFullProcessImageName(h, 0, &buf[0], &size) == nil {
		image = windows.UTF16ToString(buf[:size])
	}
	var token windows.Token
	if windows.OpenProcessToken(h, windows.TOKEN_QUERY, &token) != nil {
		return "", image
	}
	defer token.Close()
	tu, err := token.GetTokenUser()
	if err != nil {
		return "", image
	}
	sid := tu.User.Sid.String()
	if name, ok := users[sid]; ok {
		return name, image
	}
	account, _, _, err := tu.User.Sid.LookupAccount("")
	if err != nil {
		account = sid
	}
	users[sid] = account
	return account, image
}

func hostTop(ctx context.Context, sort string, limit int, withCommand bool) (HostTop, error) {
	before, start := winSnapshot(), time.Now()
	select {
	case <-ctx.Done():
		return HostTop{}, ctx.Err()
	case <-time.After(topSample):
	}
	after := winSnapshot()
	elapsed := time.Since(start).Seconds() * 1e7 // in 100 ns units, as the process times
	cpus := float64(max(runtime.NumCPU(), 1))
	list := make([]HostProcess, 0, len(after))
	for pid, s := range after {
		p := HostProcess{PID: pid, Name: s.name, RSS: s.rss}
		if b, ok := before[pid]; ok && s.times >= b.times && elapsed > 0 {
			p.CPU = round(float64(s.times-b.times)/elapsed/cpus*100, 2)
		}
		list = append(list, p)
	}
	top := HostTop{Sort: sort, Sorts: topSorts, Count: len(list), Processes: rank(list, sort, limit)}
	users := map[string]string{}
	for i := range top.Processes {
		user, image := owner(top.Processes[i].PID, users)
		top.Processes[i].User = user
		if withCommand {
			top.Processes[i].Command = image
		}
	}
	return top, nil
}

// winHardware is what one CIM query in PowerShell gives: processors, board and
// BIOS, physical disks, the time zone and the DNS servers.
const winHardware = `$ErrorActionPreference='SilentlyContinue'
$p=@(Get-CimInstance Win32_Processor); $s=Get-CimInstance Win32_ComputerSystem; $b=Get-CimInstance Win32_BaseBoard | Select-Object -First 1
$bios=Get-CimInstance Win32_BIOS
[pscustomobject]@{
 cpu=@($p | ForEach-Object { @{name=$_.Name; vendor=$_.Manufacturer; cores=[int]$_.NumberOfCores; threads=[int]$_.NumberOfLogicalProcessors; mhz=[int]$_.CurrentClockSpeed; maxMhz=[int]$_.MaxClockSpeed; l2=[int]$_.L2CacheSize; l3=[int]$_.L3CacheSize} })
 vendor=[string]$s.Manufacturer; product=[string]$s.Model; boardVendor=[string]$b.Manufacturer; board=[string]$b.Product
 bios=(([string]$bios.Manufacturer + ' ' + [string]$bios.SMBIOSBIOSVersion).Trim()); biosDate=$(if ($bios.ReleaseDate) { $bios.ReleaseDate.ToString('yyyy-MM-dd') } else { '' })
 disks=@(Get-PhysicalDisk | ForEach-Object { @{name=[string]$_.DeviceId; model=[string]$_.FriendlyName; size=[int64]$_.Size; media=[string]$_.MediaType; bus=[string]$_.BusType} })
 tz=[string](Get-TimeZone).Id
 dns=@(Get-DnsClientServerAddress | ForEach-Object { $_.ServerAddresses } | Select-Object -Unique)
} | ConvertTo-Json -Depth 4 -Compress`

type winHardwareReport struct {
	CPU []struct {
		Name, Vendor                       string
		Cores, Threads, MHz, MaxMHz, L2, L3 int
	} `json:"cpu"`
	Vendor, Product, BoardVendor, Board, BIOS, BIOSDate, TZ string
	Disks                                                   []struct {
		Name, Model, Media, Bus string
		Size                    int64
	} `json:"disks"`
	DNS []string `json:"dns"`
}

func hostDetails(ctx context.Context, info Info) (HostDetails, error) {
	d := HostDetails{Hostname: info.Hostname, OS: info.OS, Kernel: info.Kernel, Arch: info.Arch, Virtualization: info.Virtualization,
		MemTotal: info.MemTotal, SwapTotal: info.SwapTotal, CPU: HostCPU{Model: info.CPU, Threads: info.Cores},
		Interfaces: interfaces(winIfaceExtra), Mounts: winMounts(), Disks: []HostDisk{}}
	if r, _, _ := procGetTickCount64.Call(); r > 0 {
		d.Boot = time.Now().Unix() - int64(r/1000)
	}
	cmd, cleanup, err := powershellCommand(psPrologue + winHardware)
	if err != nil {
		return d, nil
	}
	defer cleanup()
	var out bytes.Buffer
	cmd.Stdout = &out
	if cmd.Start() != nil {
		return d, nil
	}
	// CIM can hang on a broken WMI repository; the details come back without it.
	timer := time.AfterFunc(30*time.Second, func() { cmd.Process.Kill() })
	stop := context.AfterFunc(ctx, func() { cmd.Process.Kill() })
	err = cmd.Wait()
	timer.Stop()
	stop()
	var hw winHardwareReport
	if err != nil || json.Unmarshal(out.Bytes(), &hw) != nil {
		return d, nil
	}
	if len(hw.CPU) > 0 {
		c := hw.CPU[0]
		d.CPU = HostCPU{Model: cmpOr(strings.TrimSpace(c.Name), info.CPU), Vendor: c.Vendor, Sockets: len(hw.CPU), MHz: c.MHz, MaxMHz: c.MaxMHz, Threads: info.Cores}
		for _, p := range hw.CPU {
			d.CPU.Cores += p.Cores
		}
		caches := []string{}
		for _, c := range [][2]any{{"L2", c.L2}, {"L3", c.L3}} {
			if kb := c[1].(int); kb > 0 {
				caches = append(caches, c[0].(string)+" "+cacheKB(kb))
			}
		}
		d.CPU.Cache = strings.Join(caches, " · ")
	}
	d.Board = HostBoard{Vendor: cmpOr(hw.Vendor, hw.BoardVendor), Product: cmpOr(hw.Product, hw.Board), BIOS: hw.BIOS}
	if hw.BIOSDate != "" && d.Board.BIOS != "" {
		d.Board.BIOS += "（" + hw.BIOSDate + "）"
	}
	for _, disk := range hw.Disks {
		kind := map[string]string{"SSD": "ssd", "4": "ssd", "HDD": "hdd", "3": "hdd"}[disk.Media]
		if disk.Bus == "NVMe" || disk.Bus == "17" {
			kind = "nvme"
		}
		if info.Virtualization != "" {
			kind = "virtual"
		}
		d.Disks = append(d.Disks, HostDisk{Name: "PhysicalDrive" + disk.Name, Model: disk.Model, Size: disk.Size, Kind: kind})
	}
	d.Timezone, d.DNS = hw.TZ, hw.DNS
	return d, nil
}

func cacheKB(kb int) string {
	if kb >= 1024 && kb%1024 == 0 {
		return strconv.Itoa(kb/1024) + "M"
	}
	return strconv.Itoa(kb) + "K"
}

func cmpOr(a, b string) string {
	if a != "" {
		return a
	}
	return b
}

func winIfaceExtra(_ string, index int) (speed int, rx, tx uint64) {
	row := windows.MibIfRow2{InterfaceIndex: uint32(index)}
	if windows.GetIfEntry2Ex(windows.MibIfEntryNormal, &row) != nil {
		return 0, 0, 0
	}
	return int(row.TransmitLinkSpeed / 1e6), row.InOctets, row.OutOctets
}

func winMounts() []HostMount {
	out := []HostMount{}
	roots, _ := driveRoots()
	for _, root := range roots {
		name, _ := windows.UTF16PtrFromString(root)
		if windows.GetDriveType(name) != windows.DRIVE_FIXED {
			continue
		}
		var free, size, totalFree uint64
		if windows.GetDiskFreeSpaceEx(name, &free, &size, &totalFree) != nil || size == 0 {
			continue
		}
		fs := make([]uint16, windows.MAX_PATH+1)
		windows.GetVolumeInformation(name, nil, 0, nil, nil, nil, &fs[0], uint32(len(fs)))
		out = append(out, HostMount{Path: root, FS: windows.UTF16ToString(fs), Total: int64(size), Used: int64(size - totalFree)})
	}
	return out
}
