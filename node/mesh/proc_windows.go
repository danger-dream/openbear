package mesh

import (
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

func processCPU() (time.Duration, bool) {
	var creation, exit, kernel, user windows.Filetime
	if err := windows.GetProcessTimes(windows.CurrentProcess(), &creation, &exit, &kernel, &user); err != nil {
		return 0, false
	}
	ticks := func(t windows.Filetime) int64 { return int64(t.HighDateTime)<<32 | int64(t.LowDateTime) } // 100 ns
	return time.Duration(ticks(kernel)+ticks(user)) * 100, true
}

var procGlobalMemoryStatusEx = windows.NewLazySystemDLL("kernel32.dll").NewProc("GlobalMemoryStatusEx")

// machineMemory is the physical memory GlobalMemoryStatusEx reports.
func machineMemory() uint64 {
	var s struct {
		Length, MemoryLoad                        uint32
		TotalPhys, AvailPhys                      uint64
		TotalPageFile, AvailPageFile              uint64
		TotalVirtual, AvailVirtual, AvailExtended uint64
	}
	s.Length = uint32(unsafe.Sizeof(s))
	if r, _, _ := procGlobalMemoryStatusEx.Call(uintptr(unsafe.Pointer(&s))); r == 0 {
		return 0
	}
	return s.TotalPhys
}

func hostFirewall() string { return "" }

func hostFirewallIssues() []FirewallIssue { return nil }
