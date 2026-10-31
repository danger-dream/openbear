package node

import (
	"context"
	"encoding/binary"
	"errors"
	"path/filepath"
	"slices"
	"unsafe"

	"golang.org/x/sys/windows"
)

var (
	procGetExtendedTcpTable = iphlpapi.NewProc("GetExtendedTcpTable")
	procGetExtendedUdpTable = iphlpapi.NewProc("GetExtendedUdpTable")
)

func bindRefused(err error) bool {
	return errors.Is(err, windows.WSAEADDRINUSE) || errors.Is(err, windows.WSAEACCES)
}

// Row layouts of the OWNER_PID tables: size, local port and pid offsets.
// Ports sit in network byte order in the low half of their DWORD.
var socketRows = map[string][2][3]int{
	"tcp": {{24, 8, 20}, {56, 20, 52}}, // MIB_TCPROW_OWNER_PID, MIB_TCP6ROW_OWNER_PID
	"udp": {{12, 4, 8}, {28, 20, 24}},  // MIB_UDPROW_OWNER_PID, MIB_UDP6ROW_OWNER_PID
}

const (
	tcpTableOwnerPidListener = 3
	udpTableOwnerPid         = 1
)

// portOwners reads the system's socket tables, at once.
// bindsBesideListeners: a wildcard bind is let through beside a listener on one address
// (SO_REUSEADDR on the BSDs, the default on Windows).
const bindsBesideListeners = true

func portOwners(_ context.Context, busy map[string][]int) map[string]map[int][]portOwner {
	return ownersEach(busy, networkOwners)
}

func networkOwners(network string, ports []int) map[int][]portOwner {
	proc, class := procGetExtendedTcpTable, uintptr(tcpTableOwnerPidListener)
	if network == "udp" {
		proc, class = procGetExtendedUdpTable, udpTableOwnerPid
	}
	out := map[int][]portOwner{}
	if proc.Find() != nil {
		return out
	}
	names := map[int]portOwner{}
	for i, af := range []uintptr{windows.AF_INET, windows.AF_INET6} {
		row := socketRows[network][i]
		table := socketTable(proc, af, class)
		if len(table) < 4 {
			continue
		}
		n := int(binary.LittleEndian.Uint32(table))
		for r := 0; r < n && 4+(r+1)*row[0] <= len(table); r++ {
			b := table[4+r*row[0]:]
			port := int(b[row[1]])<<8 | int(b[row[1]+1])
			pid := int(binary.LittleEndian.Uint32(b[row[2]:]))
			if pid == 0 || !slices.Contains(ports, port) || slices.ContainsFunc(out[port], func(o portOwner) bool { return o.pid == pid }) {
				continue
			}
			o, ok := names[pid]
			if !ok {
				o = processOwner(pid)
				names[pid] = o
			}
			out[port] = append(out[port], o)
		}
	}
	return out
}

func socketTable(proc *windows.LazyProc, af, class uintptr) []byte {
	size := uint32(16 << 10)
	for range 5 {
		buf := make([]byte, size)
		r, _, _ := proc.Call(uintptr(unsafe.Pointer(&buf[0])), uintptr(unsafe.Pointer(&size)), 0, af, class, 0)
		switch windows.Errno(r) {
		case windows.ERROR_SUCCESS:
			return buf[:min(int(size), len(buf))]
		case windows.ERROR_INSUFFICIENT_BUFFER:
			size += 4 << 10 // the table may grow before the next call
			continue
		}
		return nil
	}
	return nil
}

func processOwner(pid int) portOwner {
	o := portOwner{pid: pid}
	if pid == 4 {
		o.name = "System" // http.sys and other kernel listeners
		return o
	}
	h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(pid))
	if err != nil {
		return o
	}
	defer windows.CloseHandle(h)
	buf := make([]uint16, windows.MAX_LONG_PATH)
	n := uint32(len(buf))
	if windows.QueryFullProcessImageName(h, 0, &buf[0], &n) == nil {
		o.exe = windows.UTF16ToString(buf[:n])
		o.name = filepath.Base(o.exe)
	}
	return o
}
