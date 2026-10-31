package transport

import "golang.org/x/sys/unix"

func setNotSent(fd int) error {
	return unix.SetsockoptInt(fd, unix.IPPROTO_TCP, unix.TCP_NOTSENT_LOWAT, 16<<10)
}

// allowFragments sends IPv4 datagrams without DF, as kernel WireGuard does
// and as macOS and Windows do by default. Linux sets DF and relies on
// "fragmentation needed" coming back; where a link on the way is smaller
// than the hosts' and that ICMP is dropped (a cloud security group, some
// DS-Lite and MAP-E lines), full-size packets vanish while small ones pass.
// Without DF the router before the small link fragments them. It applies
// to IPv4 sent from the dual-stack socket too; IPv6 keeps path MTU
// discovery, as its routers never fragment. Best effort: a socket that
// refuses keeps the default.
func allowFragments(fd int) {
	unix.SetsockoptInt(fd, unix.IPPROTO_IP, unix.IP_MTU_DISCOVER, unix.IP_PMTUDISC_DONT)
}
