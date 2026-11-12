package mesh

import (
	"fmt"
	"net"
	"net/netip"
	"strings"
)

// Over UDP a link takes the network's MTU and the mesh's framing on each
// machine's own network: an IP and UDP header, the datagram's preamble and
// plane, WireGuard's header and tag, and its padding to 16 bytes. A machine
// whose interface toward its UDP peers takes less, a VPS at 1400 say, cannot
// send what the network lets through: its own packets, end to end and to its
// neighbours, are held to what that interface takes, which the OS learns
// from the mesh's routes and from "fragmentation needed" (tooBigLocal). What
// it receives, and what it forwards for others, is not.

// udpFraming is what a link over UDP adds past the IP header: the UDP
// header, the preamble and plane, WireGuard's header and tag.
const udpFraming = 8 + 5 + 32

// minOwnMTU is the least this node holds its own packets to.
const minOwnMTU = 576

// underlay is this machine's interface toward its UDP peers that takes the
// shortest packets, when it takes less than the network's MTU.
type underlay struct {
	iface string
	mtu   int // the interface's
	limit int // the longest packet a link over UDP carries out of it
}

// udpLimit is the longest packet of this node's own a link over UDP carries
// out of an interface of mtu, over IPv6 when v6: one whose end-to-end
// payload is a multiple of 16, so that the other end, answering as long and
// padding to 16 too, sends nothing longer back.
func udpLimit(mtu int, v6 bool) int {
	ip := 20
	if v6 {
		ip = 40
	}
	return max(minOwnMTU, ((mtu-ip-udpFraming)&^15-e2eOverhead)&^15+e2eOverhead)
}

// ownMTU is the longest packet of this node's own that its links carry.
func (c *Config) ownMTU() int {
	if l := c.underlay.limit; l > 0 && l < c.Network.MTU {
		return l
	}
	return c.Network.MTU
}

// defaultTargets stand for the peers that dial in over UDP: what goes back
// to them leaves by the default route.
var defaultTargets = []netip.AddrPort{netip.MustParseAddrPort("1.1.1.1:53")}

// underlayFor is the interface of the least limit among those the UDP
// endpoints c dials, links and entry candidates, are reached through, and
// the default route's when c listens on UDP; mesh, the mesh's own
// interface, never counts.
func underlayFor(c *Config, mesh string) underlay {
	var targets []netip.AddrPort
	add := func(endpoint string) {
		if a, ok := udpPeerAddr(endpoint); ok {
			targets = append(targets, a)
		}
	}
	for _, l := range c.Links {
		add(l.Endpoint)
	}
	if c.Entry != nil {
		for _, cand := range c.Entry.Candidates {
			add(cand.Endpoint)
		}
	}
	if c.Self.Listen.UDP != 0 {
		targets = append(targets, defaultTargets...)
	}
	var out underlay
	seen := map[netip.Addr]bool{}
	for _, t := range targets {
		local, ok := localFor(t)
		if !ok || seen[local] {
			continue
		}
		seen[local] = true
		name, mtu, ok := interfaceOf(local)
		if !ok || name == mesh || mtu <= 0 {
			continue
		}
		if l := udpLimit(mtu, !local.Is4()); l < c.Network.MTU && (out.limit == 0 || l < out.limit) {
			out = underlay{iface: name, mtu: mtu, limit: l}
		}
	}
	return out
}

// OwnMTUStatus is the interface, its MTU and the longest packet the OS may
// send into the mesh.
type OwnMTUStatus struct {
	Iface    string `json:"iface"`
	IfaceMTU int    `json:"ifaceMtu"`
	MTU      int    `json:"mtu"`
}

func udpPeerAddr(e string) (netip.AddrPort, bool) {
	rest, ok := strings.CutPrefix(e, "udp://")
	if !ok {
		return netip.AddrPort{}, false
	}
	a, err := netip.ParseAddrPort(rest)
	return netip.AddrPortFrom(a.Addr().Unmap(), a.Port()), err == nil
}

// localFor is the address this machine sends to t from; connecting a UDP
// socket sends nothing.
func localFor(t netip.AddrPort) (netip.Addr, bool) {
	c, err := net.DialUDP("udp", nil, net.UDPAddrFromAddrPort(t))
	if err != nil {
		return netip.Addr{}, false
	}
	defer c.Close()
	a, ok := c.LocalAddr().(*net.UDPAddr)
	if !ok {
		return netip.Addr{}, false
	}
	return a.AddrPort().Addr().Unmap(), true
}

func interfaceOf(local netip.Addr) (string, int, bool) {
	ifaces, err := net.Interfaces()
	if err != nil {
		return "", 0, false
	}
	for _, ifc := range ifaces {
		addrs, _ := ifc.Addrs()
		for _, a := range addrs {
			if n, ok := a.(*net.IPNet); ok {
				if ip, ok := netip.AddrFromSlice(n.IP); ok && ip.Unmap() == local {
					return ifc.Name, ifc.MTU, true
				}
			}
		}
	}
	return "", 0, false
}

// describe is u for events, for a network of mtu: the longest packet the OS
// may send into the mesh, this node's and the network's.
func (u underlay) describe(mtu int) string {
	if u.limit == 0 {
		return fmt.Sprintf("none: this node's own packets up to the network's %d bytes", mtu-e2eOverhead)
	}
	return fmt.Sprintf("%s %d: this node's own packets up to %d bytes, the network's %d", u.iface, u.mtu, u.limit-e2eOverhead, mtu-e2eOverhead)
}
