package transport

import (
	"context"
	"errors"
	"net"
	"net/netip"
	"slices"
	"strconv"
	"time"
)

// Endpoints may name their host: a relay on a dynamic address behind DDNS.
// The name is looked up when the path is created and again every
// nameRefresh, and the path follows a new address once the one it uses is
// no longer among the name's.
var (
	nameRefresh = time.Minute
	lookupNetIP = net.DefaultResolver.LookupNetIP
	// routable says whether this machine has a route and a usable source
	// address toward ip.
	routable = hasRoute
)

// resolveRemote turns host:port into the address to send to. A name keeps
// current while it still resolves to it; otherwise IPv4 comes first unless
// this machine cannot reach it, as on an IPv6-only host.
func resolveRemote(ctx context.Context, address string, current netip.AddrPort) (netip.AddrPort, error) {
	host, portText, err := net.SplitHostPort(address)
	if err != nil {
		return netip.AddrPort{}, err
	}
	port, err := strconv.ParseUint(portText, 10, 16)
	if err != nil {
		return netip.AddrPort{}, err
	}
	if ip, err := netip.ParseAddr(host); err == nil {
		return netip.AddrPortFrom(ip.Unmap(), uint16(port)), nil
	}
	addrs, err := lookupNetIP(ctx, "ip", host)
	if err != nil {
		return netip.AddrPort{}, err
	}
	for i := range addrs {
		addrs[i] = addrs[i].Unmap()
	}
	if current.IsValid() && current.Port() == uint16(port) && slices.Contains(addrs, current.Addr()) {
		return current, nil
	}
	slices.SortStableFunc(addrs, func(a, b netip.Addr) int {
		if a.Is4() == b.Is4() {
			return 0
		}
		if a.Is4() {
			return -1
		}
		return 1
	})
	if len(addrs) == 0 {
		return netip.AddrPort{}, errors.New("no address for " + host)
	}
	for _, ip := range addrs {
		if routable(ip) {
			return netip.AddrPortFrom(ip, uint16(port)), nil
		}
	}
	return netip.AddrPortFrom(addrs[0], uint16(port)), nil
}

// hasRoute asks the system for a source address toward ip, as a UDP
// connect does without sending anything.
func hasRoute(ip netip.Addr) bool {
	network := "udp4"
	if ip.Is6() {
		network = "udp6"
	}
	c, err := net.DialUDP(network, nil, net.UDPAddrFromAddrPort(netip.AddrPortFrom(ip, 9)))
	if err != nil {
		return false
	}
	defer c.Close()
	src := c.LocalAddr().(*net.UDPAddr).AddrPort().Addr()
	return ip.IsLinkLocalUnicast() || ip.IsLoopback() || !src.Unmap().IsLinkLocalUnicast()
}

func namedHost(address string) bool {
	host, _, err := net.SplitHostPort(address)
	if err != nil {
		return false
	}
	_, err = netip.ParseAddr(host)
	return err != nil
}

// refreshNames looks up the named outbound endpoints of run r again.
func (b *Bind) refreshNames(r *bindRun) {
	defer r.wg.Done()
	tick := time.NewTicker(nameRefresh)
	defer tick.Stop()
	for {
		select {
		case <-r.ctx.Done():
			return
		case <-tick.C:
		}
		type named struct {
			ep      *endpoint
			address string
			remote  netip.AddrPort
		}
		var list []named
		b.mu.Lock()
		for _, ep := range b.paths {
			if ep.outbound && !ep.retired && namedHost(ep.address) {
				list = append(list, named{ep, ep.address, ep.remote})
			}
		}
		b.mu.Unlock()
		for _, n := range list {
			ctx, cancel := context.WithTimeout(r.ctx, 5*time.Second)
			next, err := resolveRemote(ctx, n.address, n.remote)
			cancel()
			if err != nil || next == n.remote {
				continue
			}
			b.mu.Lock()
			moved := b.run == r && !n.ep.retired && n.ep.remote == n.remote
			if moved {
				n.ep.remote = next
				if n.ep.transport == "udp" {
					n.ep.stats.RemoteAddr = next.String()
				}
			}
			logf := b.opt.Logf
			b.mu.Unlock()
			if moved && logf != nil {
				logf("mesh transport %s: now at %s (was %s)", n.ep.key, next, n.remote)
			}
		}
	}
}
