package mesh

import (
	"fmt"
	"net/netip"
	"os/exec"
	"slices"
	"strconv"
	"strings"

	"golang.zx2c4.com/wireguard/tun"
)

// createTUN takes the next free utunN; status reports the actual name.
func createTUN(dir string, mtu int) (tun.Device, error) {
	return tun.CreateTUN("utun", mtu)
}

func run(args ...string) error {
	out, err := exec.Command(args[0], args[1:]...).CombinedOutput()
	if err != nil {
		return fmt.Errorf("%s: %v: %s", strings.Join(args, " "), err, strings.TrimSpace(string(out)))
	}
	return nil
}

type darwinHost struct {
	name  string
	cur   hostConfig
	up    bool
	dirty bool
}

func newHost(name, _ string, _ tun.Device) hostNet { return &darwinHost{name: name} }

// apply leaves the firewall alone: the macOS application firewall is
// usually off, and when on it asks the user.
func (h *darwinHost) apply(next hostConfig) (err error) {
	defer func() { h.dirty = err != nil }()
	first := !h.up || h.dirty
	a := next.Address.String()
	if first || next.Address != h.cur.Address || next.MTU != h.cur.MTU {
		if h.cur.Address.IsValid() && next.Address != h.cur.Address {
			run("/sbin/ifconfig", h.name, "inet", h.cur.Address.String(), "delete")
		}
		if err := run("/sbin/ifconfig", h.name, "inet", a, a, "netmask", "255.255.255.255", "mtu", strconv.Itoa(next.MTU), "up"); err != nil {
			return err
		}
	}
	h.up = true
	h.cur.Address, h.cur.MTU = next.Address, next.MTU
	for _, r := range slices.Clone(h.cur.Routes) {
		if !slices.Contains(next.Routes, r) {
			if err := run("/sbin/route", "-q", "-n", "delete", "-inet", r.String(), "-interface", h.name); err != nil && !strings.Contains(err.Error(), "not in table") {
				return err
			}
			h.cur.Routes = slices.DeleteFunc(slices.Clone(h.cur.Routes), func(p netip.Prefix) bool { return p == r })
		}
	}
	for _, r := range next.Routes {
		if !first && slices.Contains(h.cur.Routes, r) {
			continue
		}
		run("/sbin/route", "-q", "-n", "delete", "-inet", r.String(), "-interface", h.name)
		if err := run("/sbin/route", "-q", "-n", "add", "-inet", r.String(), "-interface", h.name); err != nil {
			return err
		}
		if !slices.Contains(h.cur.Routes, r) {
			h.cur.Routes = append(slices.Clone(h.cur.Routes), r)
		}
	}
	h.cur = next
	return nil
}

func (h *darwinHost) heal() []string { return nil }

func (h *darwinHost) ready() bool { return h.up }

// close leaves routes to the interface, which goes away with them.
func (h *darwinHost) close() {}

// residentBytes approximates the RSS with the memory the Go runtime holds:
// the current RSS needs task_info, which a cgo-free build cannot call, and
// getrusage only has the peak. Code and shared libraries are not counted.
func residentBytes() uint64 { return goMappedBytes() }
