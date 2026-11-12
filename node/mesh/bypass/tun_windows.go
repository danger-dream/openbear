//go:build windows

package bypass

import (
	"encoding/binary"
	"errors"
	"math"
	"strings"

	"golang.org/x/sys/windows"
	"golang.zx2c4.com/wireguard/windows/tunnel/winipcfg"
)

const tunSupported = true

// IP_UNICAST_IF and IPV6_UNICAST_IF, as Windows numbers them.
const (
	ipUnicastIF   = 31
	ipv6UnicastIF = 31
	// hardwareInterface is MIB_IF_ROW2's HardwareInterface flag.
	hardwareInterface = 1
)

// physicalInterface: when the way to the internet is through an adapter that is not hardware (a
// TUN proxy's Wintun, as Clash's "Meta Tunnel"), the hardware adapter with the best default route.
// 0 when the way out is a hardware adapter already, or none has a default route (a Hyper-V host
// whose way out is its virtual switch).
func physicalInterface() int {
	var best uint32
	if windows.GetBestInterfaceEx(&windows.SockaddrInet4{Addr: [4]byte{1, 1, 1, 1}}, &best) != nil || hardware(best) {
		return 0
	}
	rows, err := winipcfg.GetIPForwardTable2(windows.AF_INET)
	if err != nil {
		return 0
	}
	index, metric := 0, uint64(math.MaxUint64)
	for i := range rows {
		r := &rows[i]
		if r.DestinationPrefix.PrefixLength != 0 || !hardware(r.InterfaceIndex) {
			continue
		}
		m := uint64(r.Metric)
		if ifc, err := r.InterfaceLUID.IPInterface(windows.AF_INET); err == nil {
			m += uint64(ifc.Metric)
		}
		if m < metric {
			index, metric = int(r.InterfaceIndex), m
		}
	}
	return index
}

func hardware(index uint32) bool {
	row := windows.MibIfRow2{InterfaceIndex: index}
	return windows.GetIfEntry2Ex(windows.MibIfEntryNormal, &row) == nil && row.InterfaceAndOperStatusFlags&hardwareInterface != 0 &&
		row.OperStatus == windows.IfOperStatusUp
}

// bindInterface sets IP_UNICAST_IF (the index in network order) or IPV6_UNICAST_IF; all: both,
// either taking (a dual-stack socket, or one whose other family is off).
func bindInterface(fd uintptr, network string, all bool, index int) error {
	h := windows.Handle(fd)
	v4 := func() error {
		var b [4]byte
		binary.BigEndian.PutUint32(b[:], uint32(index))
		return windows.SetsockoptInt(h, windows.IPPROTO_IP, ipUnicastIF, int(int32(binary.NativeEndian.Uint32(b[:]))))
	}
	v6 := func() error { return windows.SetsockoptInt(h, windows.IPPROTO_IPV6, ipv6UnicastIF, index) }
	if all {
		e4, e6 := v4(), v6()
		if e4 != nil && e6 != nil {
			return errors.Join(e4, e6)
		}
		return nil
	}
	if strings.HasSuffix(network, "6") {
		return v6()
	}
	return v4()
}
