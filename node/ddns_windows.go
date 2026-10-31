package node

import (
	"errors"
	"net"
	"net/netip"
	"unsafe"

	"golang.org/x/sys/windows"
)

// ddnsV6Flags reads the IPv6 unicast addresses of every adapter. A random
// suffix marks a temporary address; no preferred lifetime left, or the
// deprecated DAD state, marks a deprecated one.
func ddnsV6Flags() (map[ddnsAddrKey]ddnsAddrFlags, error) {
	size := uint32(16 << 10)
	for {
		buf := make([]byte, size)
		first := (*windows.IpAdapterAddresses)(unsafe.Pointer(&buf[0]))
		err := windows.GetAdaptersAddresses(windows.AF_INET6,
			windows.GAA_FLAG_SKIP_ANYCAST|windows.GAA_FLAG_SKIP_MULTICAST|windows.GAA_FLAG_SKIP_DNS_SERVER, 0, first, &size)
		if errors.Is(err, windows.ERROR_BUFFER_OVERFLOW) && int(size) > len(buf) {
			continue
		}
		if err != nil {
			return nil, err
		}
		out := map[ddnsAddrKey]ddnsAddrFlags{}
		for aa := first; aa != nil; aa = aa.Next {
			// The index package net gives the interface.
			index := int(aa.IfIndex)
			if index == 0 {
				index = int(aa.Ipv6IfIndex)
			}
			for ua := aa.FirstUnicastAddress; ua != nil; ua = ua.Next {
				addr, ok := netip.AddrFromSlice(ua.Address.IP())
				if !ok || !addr.Is6() {
					continue
				}
				out[ddnsAddrKey{index, addr}] = ddnsAddrFlags{
					temporary:  ua.SuffixOrigin == windows.IpSuffixOriginRandom,
					deprecated: ua.PreferredLifetime == 0 || ua.DadState == windows.IpDadStateDeprecated,
				}
			}
		}
		return out, nil
	}
}

// ddnsBindDevice is not used here: binding binds the interface's address.
func ddnsBindDevice(*net.Dialer, string) bool { return false }
