//go:build darwin

package bypass

import (
	"bufio"
	"bytes"
	"errors"
	"net"
	"os/exec"
	"strings"

	"golang.org/x/sys/unix"
)

const tunSupported = true

// virtualPrefixes name interfaces that are not the machine's own way out: tunnels (a TUN proxy's
// utun, the mesh's own), VPNs and the like.
var virtualPrefixes = []string{"utun", "tun", "tap", "ipsec", "ppp", "gif", "stf", "awdl", "llw", "lo", "bridge"}

func virtual(name string) bool {
	for _, p := range virtualPrefixes {
		if strings.HasPrefix(name, p) {
			return true
		}
	}
	return false
}

// physicalInterface: when the way to the internet is through a tunnel (a TUN proxy's utun), the
// interface of the first default route that is not one (macOS keeps a scoped default route for
// it, which a bound socket takes). 0 when the way out is not a tunnel, or no other has a default route.
func physicalInterface() int {
	if !virtual(routeInterface("1.1.1.1")) {
		return 0
	}
	out, err := exec.Command("/usr/sbin/netstat", "-rn", "-f", "inet").Output()
	if err != nil {
		return 0
	}
	// Destination Gateway Flags Netif [Expire]
	s := bufio.NewScanner(bytes.NewReader(out))
	for s.Scan() {
		f := strings.Fields(s.Text())
		if len(f) < 4 || f[0] != "default" || virtual(f[3]) {
			continue
		}
		if ifc, err := net.InterfaceByName(f[3]); err == nil && ifc.Flags&net.FlagUp != 0 {
			return ifc.Index
		}
	}
	return 0
}

// routeInterface is the interface "route get" names for the way to ip; "" if it says none.
func routeInterface(ip string) string {
	out, err := exec.Command("/sbin/route", "-n", "get", ip).Output()
	if err != nil {
		return ""
	}
	for _, line := range strings.Split(string(out), "\n") {
		if name, ok := strings.CutPrefix(strings.TrimSpace(line), "interface:"); ok {
			return strings.TrimSpace(name)
		}
	}
	return ""
}

// bindInterface sets IP_BOUND_IF or IPV6_BOUND_IF; all: both, either taking.
func bindInterface(fd uintptr, network string, all bool, index int) error {
	v4 := func() error { return unix.SetsockoptInt(int(fd), unix.IPPROTO_IP, unix.IP_BOUND_IF, index) }
	v6 := func() error { return unix.SetsockoptInt(int(fd), unix.IPPROTO_IPV6, unix.IPV6_BOUND_IF, index) }
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
