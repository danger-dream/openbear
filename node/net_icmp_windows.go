//go:build windows

package node

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"strconv"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

// Windows pings through iphlpapi: raw ICMP sockets are restricted there.
var (
	procIcmpCreateFile  = iphlpapi.NewProc("IcmpCreateFile")
	procIcmp6CreateFile = iphlpapi.NewProc("Icmp6CreateFile")
	procIcmpCloseHandle = iphlpapi.NewProc("IcmpCloseHandle")
	procIcmpSendEcho2   = iphlpapi.NewProc("IcmpSendEcho2")
	procIcmp6SendEcho2  = iphlpapi.NewProc("Icmp6SendEcho2")
)

type ipOptionInformation struct {
	TTL, TOS, Flags, OptionsSize uint8
	OptionsData                  uintptr
}

type icmpEchoReply struct {
	Address       [4]byte
	Status        uint32
	RoundTripTime uint32
	DataSize      uint16
	Reserved      uint16
	Data          uintptr
	Options       ipOptionInformation
}

// ICMPV6_ECHO_REPLY: a packed IPV6_ADDRESS_EX (port, flowinfo, address,
// scope id; 26 bytes), then Status and RoundTripTime at 28 and 32.
const (
	icmp6ReplyAddr   = 6
	icmp6ReplyStatus = 28
)

// IP_STATUS values from ipexport.h.
const (
	ipDestNetUnreachable  = 11002
	ipDestHostUnreachable = 11003
	ipDestProtUnreachable = 11004
	ipDestPortUnreachable = 11005
	ipReqTimedOut         = 11010
	ipTTLExpiredTransit   = 11013
	ipTTLExpiredReassem   = 11014
	ipDestUnreachable     = 11040
	ipTimeExceeded        = 11041
	ipBadRoute            = 11012
	ipBadDestination      = 11018
)

// What the other statuses mean, for the ones a user is likely to see;
// the Win32 errors are what IcmpSendEcho2 fails with before any reply.
var icmpStatusText = map[uint32]string{
	11001: "应答缓冲区太小",
	11006: "系统资源不足",
	11007: "IP 选项无效",
	11008: "网卡硬件错误",
	11009: "数据包太大",
	11011: "请求无效",
	11015: "参数错误",
	11016: "对方要求降低发送速率",
	11050: "系统网络故障（常见于本机没有对应的地址或被防火墙拦截）",
	5:     "没有权限",
	8:     "系统资源不足",
	87:    "请求无效",
	1214:  "目标地址无效",
	1784:  "应答缓冲区太小",
}

type windowsEcho struct{ dst netip.Addr }

func openEcho(dst netip.Addr, _ int) (echoConn, error) {
	for _, p := range []*windows.LazyProc{procIcmpCreateFile, procIcmp6CreateFile, procIcmpCloseHandle, procIcmpSendEcho2, procIcmp6SendEcho2} {
		if err := p.Find(); err != nil {
			return nil, err
		}
	}
	return windowsEcho{dst}, nil
}

func (windowsEcho) Close() error { return nil }

// echo blocks for at most the timeout; each call has its own handle so
// a trace's hops can wait side by side.
func (e windowsEcho) echo(_ context.Context, _, ttl int, timeout time.Duration) echoReply {
	create := procIcmpCreateFile
	if e.dst.Is6() {
		create = procIcmp6CreateFile
	}
	h, _, err := create.Call()
	if windows.Handle(h) == windows.InvalidHandle {
		return echoReply{kind: replyFailed, err: err}
	}
	defer procIcmpCloseHandle.Call(h)
	var opt *ipOptionInformation
	if ttl > 0 {
		opt = &ipOptionInformation{TTL: uint8(ttl)}
	}
	buf := make([]byte, 512+len(probePayload))
	ms := uintptr(timeout.Milliseconds())
	start := time.Now()
	var n uintptr
	var status uint32
	var from netip.Addr
	r := echoReply{}
	if e.dst.Is4() {
		a := e.dst.As4()
		n, _, err = procIcmpSendEcho2.Call(h, 0, 0, 0, uintptr(binary.LittleEndian.Uint32(a[:])),
			uintptr(unsafe.Pointer(&probePayload[0])), uintptr(len(probePayload)), uintptr(unsafe.Pointer(opt)),
			uintptr(unsafe.Pointer(&buf[0])), uintptr(len(buf)), ms)
		reply := (*icmpEchoReply)(unsafe.Pointer(&buf[0]))
		status, from, r.ttl = reply.Status, netip.AddrFrom4(reply.Address), int(reply.Options.TTL)
	} else {
		src := windows.RawSockaddrInet6{Family: windows.AF_INET6}
		dst := windows.RawSockaddrInet6{Family: windows.AF_INET6, Addr: e.dst.As16(), Scope_id: zoneIndex(e.dst.Zone())}
		n, _, err = procIcmp6SendEcho2.Call(h, 0, 0, 0, uintptr(unsafe.Pointer(&src)), uintptr(unsafe.Pointer(&dst)),
			uintptr(unsafe.Pointer(&probePayload[0])), uintptr(len(probePayload)), uintptr(unsafe.Pointer(opt)),
			uintptr(unsafe.Pointer(&buf[0])), uintptr(len(buf)), ms)
		status = binary.LittleEndian.Uint32(buf[icmp6ReplyStatus:])
		from = netip.AddrFrom16([16]byte(buf[icmp6ReplyAddr : icmp6ReplyAddr+16]))
	}
	r.rtt = time.Since(start)
	// Without a reply in the buffer the status is the call's error.
	if n == 0 && status == 0 {
		var errno syscall.Errno
		if !errors.As(err, &errno) || errno == 0 {
			return echoReply{kind: replyFailed, err: err}
		}
		status = uint32(errno)
	}
	if !from.IsUnspecified() {
		r.from = from
	}
	if status == 0 {
		if n == 0 {
			return echoReply{kind: replyFailed, err: err}
		}
		r.kind = replyEcho
		return r
	}
	kind, failure := icmpStatus(status)
	if kind == replyFailed || kind == replyTimeout {
		return echoReply{kind: kind, err: failure}
	}
	r.kind, r.ttl = kind, 0
	return r
}

// icmpStatus classes a failed echo's IP_STATUS or Win32 error; a failure
// comes with words for it rather than its number.
func icmpStatus(status uint32) (replyKind, error) {
	switch status {
	case ipTTLExpiredTransit, ipTTLExpiredReassem, ipTimeExceeded:
		return replyExceeded, nil
	case ipDestNetUnreachable, ipDestHostUnreachable, ipDestProtUnreachable, ipDestPortUnreachable, ipDestUnreachable,
		ipBadRoute, ipBadDestination, uint32(windows.ERROR_NETWORK_UNREACHABLE), uint32(windows.ERROR_HOST_UNREACHABLE),
		uint32(windows.ERROR_PROTOCOL_UNREACHABLE), uint32(windows.ERROR_PORT_UNREACHABLE):
		return replyUnreachable, nil
	case ipReqTimedOut, uint32(windows.ERROR_SEM_TIMEOUT), uint32(windows.WAIT_TIMEOUT), uint32(windows.ERROR_TIMEOUT):
		return replyTimeout, nil
	}
	if text, ok := icmpStatusText[status]; ok {
		return replyFailed, errors.New(text)
	}
	return replyFailed, fmt.Errorf("ICMP 错误（代码 %d）", status)
}

func zoneIndex(zone string) uint32 {
	if zone == "" {
		return 0
	}
	if i, err := net.InterfaceByName(zone); err == nil {
		return uint32(i.Index)
	}
	n, _ := strconv.ParseUint(zone, 10, 32)
	return uint32(n)
}

func dialErrorKind(err error) string {
	switch {
	case errors.Is(err, windows.WSAECONNREFUSED), errors.Is(err, windows.ERROR_CONNECTION_REFUSED):
		return "refused"
	case errors.Is(err, windows.WSAENETUNREACH), errors.Is(err, windows.WSAEHOSTUNREACH),
		errors.Is(err, windows.ERROR_NETWORK_UNREACHABLE), errors.Is(err, windows.ERROR_HOST_UNREACHABLE):
		return "unreachable"
	}
	return ""
}

// noRoute: the system has no route or source address for the target, or
// none of its family at all.
func noRoute(err error) bool {
	return errors.Is(err, windows.WSAEADDRNOTAVAIL) || errors.Is(err, windows.WSAENETUNREACH) || errors.Is(err, windows.WSAEHOSTUNREACH) ||
		errors.Is(err, windows.WSAEAFNOSUPPORT) || errors.Is(err, windows.ERROR_NETWORK_UNREACHABLE) || errors.Is(err, windows.ERROR_HOST_UNREACHABLE)
}
