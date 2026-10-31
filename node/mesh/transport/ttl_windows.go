package transport

import "golang.org/x/sys/windows"

func getTTL(fd uintptr, v6 bool) (int, error) {
	if v6 {
		return windows.GetsockoptInt(windows.Handle(fd), windows.IPPROTO_IPV6, windows.IPV6_UNICAST_HOPS)
	}
	return windows.GetsockoptInt(windows.Handle(fd), windows.IPPROTO_IP, windows.IP_TTL)
}

func setTTL(fd uintptr, v6 bool, ttl int) error {
	if v6 {
		return windows.SetsockoptInt(windows.Handle(fd), windows.IPPROTO_IPV6, windows.IPV6_UNICAST_HOPS, ttl)
	}
	return windows.SetsockoptInt(windows.Handle(fd), windows.IPPROTO_IP, windows.IP_TTL, ttl)
}
