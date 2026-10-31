//go:build linux || darwin || windows

package node

import (
	"cmp"
	"net"
	"net/netip"
	"slices"
	"strings"
)

type netIface struct {
	name  string
	flags net.Flags
	mac   net.HardwareAddr
	addrs []net.Addr
}

type netAddr struct {
	IP        string `json:"ip"`
	PrefixLen int    `json:"prefixLen"`
	Family    int    `json:"family"`
}

type netInterface struct {
	Name  string    `json:"name"`
	Up    bool      `json:"up"`
	MAC   string    `json:"mac"`
	Kind  string    `json:"kind"`
	Addrs []netAddr `json:"addrs"`
}

func (a *Agent) netAddrs() map[string]any {
	list, _ := net.Interfaces()
	ifaces := make([]netIface, 0, len(list))
	for _, i := range list {
		addrs, _ := i.Addrs()
		ifaces = append(ifaces, netIface{name: i.Name, flags: i.Flags, mac: i.HardwareAddr, addrs: addrs})
	}
	return map[string]any{"interfaces": netInterfaces(ifaces, a.meshNode().meshPrefixes())}
}

// Hyper-V's internal switches; an external switch carries the machine's real address.
var windowsVirtualSwitches = []string{"vEthernet (WSL", "vEthernet (Default Switch)", "vEthernet (nat)", "vEthernet (DockerNAT)"}

func netInterfaces(ifaces []netIface, mesh []netip.Prefix) []netInterface {
	out := []netInterface{}
	for _, i := range ifaces {
		if i.flags&net.FlagLoopback != 0 || meshVirtualBridge(i.name) ||
			slices.ContainsFunc(windowsVirtualSwitches, func(p string) bool { return strings.HasPrefix(i.name, p) }) {
			continue
		}
		var v4, v6 []netip.Prefix
		ours := i.name == "obmesh0"
		for _, a := range i.addrs {
			var ip net.IP
			var ones, bits int
			switch a := a.(type) {
			case *net.IPNet:
				ip = a.IP
				ones, bits = a.Mask.Size()
			case *net.IPAddr:
				ip = a.IP
			default:
				continue
			}
			addr, ok := netip.AddrFromSlice(ip)
			if !ok || addr.IsLoopback() || addr.Is6() && !addr.Is4In6() && addr.IsLinkLocalUnicast() {
				continue
			}
			if addr.Is4In6() {
				addr = addr.Unmap()
				if bits == 128 {
					ones -= 96
				}
			}
			if bits == 0 {
				ones = addr.BitLen()
			}
			if meshUtun(i.name, addr, mesh) {
				ours = true
			}
			if addr.Is4() {
				v4 = append(v4, netip.PrefixFrom(addr, ones))
			} else {
				v6 = append(v6, netip.PrefixFrom(addr, ones))
			}
		}
		if len(v4)+len(v6) == 0 {
			continue
		}
		byAddr := func(a, b netip.Prefix) int { return cmp.Or(a.Addr().Compare(b.Addr()), a.Bits()-b.Bits()) }
		slices.SortFunc(v4, byAddr)
		slices.SortFunc(v6, byAddr)
		addrs := []netAddr{}
		for _, p := range slices.Concat(v4, v6) {
			if len(addrs) > 0 && addrs[len(addrs)-1].IP == p.Addr().String() {
				continue
			}
			family := 4
			if p.Addr().Is6() {
				family = 6
			}
			addrs = append(addrs, netAddr{IP: p.Addr().String(), PrefixLen: p.Bits(), Family: family})
		}
		mac := ""
		if len(i.mac) > 0 {
			mac = i.mac.String()
		}
		out = append(out, netInterface{Name: i.name, Up: i.flags&net.FlagUp != 0, MAC: mac, Kind: netKind(i, ours), Addrs: addrs})
	}
	return out
}

func netKind(i netIface, mesh bool) string {
	switch {
	case mesh:
		return "mesh"
	case slices.ContainsFunc([]string{"tun", "tap", "wg", "utun", "ppp", "ipsec"}, func(p string) bool { return strings.HasPrefix(i.name, p) }),
		i.flags&net.FlagPointToPoint != 0 && !hasMAC(i.mac):
		return "tunnel"
	case hasMAC(i.mac):
		return "ethernet"
	}
	return "other"
}

func hasMAC(mac net.HardwareAddr) bool {
	return slices.ContainsFunc(mac, func(b byte) bool { return b != 0 })
}
