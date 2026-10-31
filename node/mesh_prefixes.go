//go:build linux || darwin || windows

package node

import (
	"encoding/json"
	"net"
	"net/netip"
	"os"
	"slices"
	"strings"
)

type meshIface struct {
	name  string
	flags net.Flags
	addrs []net.Addr
}

// localPrefixes lists the IPv4 networks on this machine's interfaces, so the
// controller can tell whether a mesh prefix would collide with one of them.
func (h *meshHost) localPrefixes() []string {
	return meshLocalPrefixes(meshInterfaces(), h.meshPrefixes())
}

// localAddresses lists this machine's private IPv4 addresses with their
// prefix length, so the controller can tell which nodes share a LAN.
func (h *meshHost) localAddresses() []string {
	return meshLocalAddresses(meshInterfaces(), h.meshPrefixes())
}

func meshInterfaces() []meshIface {
	list, _ := net.Interfaces()
	ifaces := make([]meshIface, 0, len(list))
	for _, i := range list {
		addrs, _ := i.Addrs()
		ifaces = append(ifaces, meshIface{name: i.Name, flags: i.Flags, addrs: addrs})
	}
	return ifaces
}

// meshPrefixes reads the address ranges of the applied config; on macOS they
// are the only way to tell the mesh's utun apart from other tunnels.
func (h *meshHost) meshPrefixes() []netip.Prefix {
	var c struct {
		Network struct {
			Prefix        string `json:"prefix"`
			ControlPrefix string `json:"controlPrefix"`
		} `json:"network"`
	}
	data, err := os.ReadFile(h.path("config.json"))
	if err != nil || json.Unmarshal(data, &c) != nil {
		return nil
	}
	var out []netip.Prefix
	for _, s := range []string{c.Network.Prefix, c.Network.ControlPrefix} {
		if p, err := netip.ParsePrefix(s); err == nil {
			out = append(out, p.Masked())
		}
	}
	return out
}

func meshLocalPrefixes(ifaces []meshIface, mesh []netip.Prefix) []string {
	var all []netip.Prefix
	for _, i := range meshLocalNets(ifaces, mesh) {
		for _, a := range i.nets {
			all = append(all, a.Masked())
		}
	}
	return sortedPrefixes(all)
}

// meshLocalAddresses: the private addresses among meshLocalNets, except
// those of container and VM bridges (docker0, macOS's bridge100 and the
// like), which every such host has alike and no other machine reaches.
func meshLocalAddresses(ifaces []meshIface, mesh []netip.Prefix) []string {
	var all []netip.Prefix
	for _, i := range meshLocalNets(ifaces, mesh) {
		if meshVirtualBridge(i.name) {
			continue
		}
		for _, a := range i.nets {
			if a.Addr().IsPrivate() {
				all = append(all, a)
			}
		}
	}
	return sortedPrefixes(all)
}

func meshVirtualBridge(name string) bool {
	for _, p := range []string{"docker", "br-", "veth", "virbr", "cni", "flannel", "podman", "vmnet", "vboxnet", "lxdbr", "lxcbr", "bridge1"} {
		if strings.HasPrefix(name, p) {
			return true
		}
	}
	return false
}

type meshNets struct {
	name string
	nets []netip.Prefix // addresses with their prefix length
}

// meshLocalNets: each interface's IPv4 addresses, without loopback, the
// mesh's own interface and, on macOS, its utun.
func meshLocalNets(ifaces []meshIface, mesh []netip.Prefix) []meshNets {
	var out []meshNets
	for _, i := range ifaces {
		if i.flags&net.FlagLoopback != 0 || i.name == "obmesh0" {
			continue
		}
		var nets []netip.Prefix
		ours := false
		for _, a := range i.addrs {
			n, ok := a.(*net.IPNet)
			if !ok {
				continue
			}
			ip, ok := netip.AddrFromSlice(n.IP)
			ones, bits := n.Mask.Size()
			if !ok || !ip.Unmap().Is4() || ip.IsLoopback() || bits == 0 {
				continue
			}
			ip = ip.Unmap()
			if bits == 128 {
				ones -= 96
			}
			if meshUtun(i.name, ip, mesh) {
				ours = true
			}
			nets = append(nets, netip.PrefixFrom(ip, ones))
		}
		if !ours {
			out = append(out, meshNets{i.name, nets})
		}
	}
	return out
}

// meshUtun: on macOS the mesh's interface is a utun the system names, told
// apart from other tunnels by an address in the mesh's ranges.
func meshUtun(name string, ip netip.Addr, mesh []netip.Prefix) bool {
	return strings.HasPrefix(name, "utun") && slices.ContainsFunc(mesh, func(p netip.Prefix) bool { return p.Contains(ip) })
}

func sortedPrefixes(all []netip.Prefix) []string {
	slices.SortFunc(all, func(a, b netip.Prefix) int {
		if c := a.Addr().Compare(b.Addr()); c != 0 {
			return c
		}
		return a.Bits() - b.Bits()
	})
	out := []string{}
	for _, p := range slices.Compact(all) {
		out = append(out, p.String())
	}
	return out
}
