package mesh

import (
	"errors"
	"fmt"
	"net/netip"
	"os/exec"
	"slices"
	"strings"
	"unsafe"

	"golang.org/x/sys/windows"
	"golang.zx2c4.com/wireguard/tun"
	"golang.zx2c4.com/wireguard/windows/tunnel/winipcfg"
)

const firewallGroup = "OpenBear Mesh"

func createTUN(dir string, mtu int) (tun.Device, error) {
	if _, err := ExtractWintun(dir); err != nil {
		return nil, err
	}
	return tun.CreateTUN("obmesh0", mtu)
}

type windowsHost struct {
	name  string
	luid  winipcfg.LUID
	dev   tun.Device
	cur   hostConfig
	up    bool
	dirty bool
	ports [3]int // the firewall rules' udp, tcp, tls
	rules bool
}

func newHost(name, _ string, dev tun.Device) hostNet {
	h := &windowsHost{name: name, dev: dev}
	if native, ok := dev.(*tun.NativeTun); ok {
		h.luid = winipcfg.LUID(native.LUID())
	}
	return h
}

func (h *windowsHost) apply(next hostConfig) (err error) {
	defer func() { h.dirty = err != nil }()
	if h.luid == 0 {
		return errors.New("not a wintun interface")
	}
	first := !h.up || h.dirty
	if first || next.Address != h.cur.Address {
		if err := h.luid.SetIPAddressesForFamily(windows.AF_INET, []netip.Prefix{host(next.Address)}); err != nil {
			return fmt.Errorf("address: %w", err)
		}
		// A new address takes every route through this interface with it.
		h.cur.Routes = nil
		h.cur.Address = next.Address
	}
	if first || next.MTU != h.cur.MTU {
		ipif, err := h.luid.IPInterface(windows.AF_INET)
		if err != nil {
			return fmt.Errorf("interface: %w", err)
		}
		// The size given to the TUN alone leaves the interface at 65535.
		ipif.NLMTU = uint32(next.MTU)
		if err = ipif.Set(); err != nil {
			return fmt.Errorf("mtu: %w", err)
		}
		if native, ok := h.dev.(*tun.NativeTun); ok {
			native.ForceMTU(next.MTU)
		}
	}
	h.up = true
	h.cur.Address, h.cur.MTU = next.Address, next.MTU
	// Route by route: SetRoutes flushes first and would take the address's
	// own /32 with it, after which Windows drops everything arriving here.
	for _, r := range slices.Clone(h.cur.Routes) {
		if !slices.Contains(next.Routes, r) {
			if err := h.luid.DeleteRoute(r, netip.IPv4Unspecified()); err != nil && !errors.Is(err, windows.ERROR_NOT_FOUND) {
				return fmt.Errorf("delete route %v: %w", r, err)
			}
			h.cur.Routes = slices.DeleteFunc(slices.Clone(h.cur.Routes), func(p netip.Prefix) bool { return p == r })
		}
	}
	for _, r := range next.Routes {
		if slices.Contains(h.cur.Routes, r) {
			continue
		}
		if err := h.luid.AddRoute(r, netip.IPv4Unspecified(), 0); err != nil && !errors.Is(err, windows.ERROR_OBJECT_ALREADY_EXISTS) {
			return fmt.Errorf("route %v: %w", r, err)
		}
		h.cur.Routes = append(slices.Clone(h.cur.Routes), r)
	}
	h.cur.Routes = next.Routes
	ports := [3]int{next.UDP, next.TCP, next.TLS}
	if !h.rules || ports != h.ports {
		if err := h.firewall(ports); err != nil {
			return err
		}
	}
	h.cur = next
	return nil
}

func powershell(script string) error {
	out, err := exec.Command("powershell", "-NoProfile", "-NonInteractive", "-Command", script).CombinedOutput()
	if err != nil {
		return fmt.Errorf("%v: %s", err, strings.TrimSpace(string(out)))
	}
	return nil
}

func (h *windowsHost) firewall(ports [3]int) error {
	h.rules = false
	script := fmt.Sprintf("Remove-NetFirewallRule -Group '%[1]s' -ErrorAction SilentlyContinue; "+
		"New-NetFirewallRule -Group '%[1]s' -DisplayName '%[1]s interface' -Direction Inbound -InterfaceAlias '%[2]s' -Action Allow | Out-Null", firewallGroup, h.name)
	for i, p := range ports {
		if p == 0 {
			continue
		}
		proto := "TCP"
		if i == 0 {
			proto = "UDP"
		}
		script += fmt.Sprintf("; New-NetFirewallRule -Group '%[1]s' -DisplayName '%[1]s %[2]s %[3]d' -Direction Inbound -Protocol %[2]s -LocalPort %[3]d -Action Allow | Out-Null", firewallGroup, proto, p)
	}
	if err := powershell(script); err != nil {
		return fmt.Errorf("firewall: %w", err)
	}
	h.ports, h.rules = ports, true
	return nil
}

func (h *windowsHost) heal() []string { return nil }

// ready also needs the address's own /32 route, without which Windows
// drops packets arriving for this node as not locally destined.
func (h *windowsHost) ready() bool {
	if !h.up {
		return false
	}
	rows, err := winipcfg.GetIPForwardTable2(windows.AF_INET)
	if err != nil {
		return false
	}
	want := host(h.cur.Address)
	for i := range rows {
		if rows[i].InterfaceLUID == h.luid && rows[i].DestinationPrefix.Prefix() == want {
			return true
		}
	}
	return false
}

func (h *windowsHost) close() {
	if h.rules {
		if err := powershell(fmt.Sprintf("Remove-NetFirewallRule -Group '%s' -ErrorAction SilentlyContinue", firewallGroup)); err == nil {
			h.rules = false
		}
	}
}

// RemoveFirewallRules deletes the rules a data plane that did not stop
// cleanly left behind.
func RemoveFirewallRules() error {
	return powershell(fmt.Sprintf("Remove-NetFirewallRule -Group '%s' -ErrorAction SilentlyContinue", firewallGroup))
}

type processMemoryCounters struct {
	cb                         uint32
	pageFaultCount             uint32
	peakWorkingSetSize         uintptr
	workingSetSize             uintptr
	quotaPeakPagedPoolUsage    uintptr
	quotaPagedPoolUsage        uintptr
	quotaPeakNonPagedPoolUsage uintptr
	quotaNonPagedPoolUsage     uintptr
	pagefileUsage              uintptr
	peakPagefileUsage          uintptr
}

var procGetProcessMemoryInfo = windows.NewLazySystemDLL("psapi.dll").NewProc("GetProcessMemoryInfo")

// residentBytes is the working set.
func residentBytes() uint64 {
	var c processMemoryCounters
	c.cb = uint32(unsafe.Sizeof(c))
	r, _, _ := procGetProcessMemoryInfo.Call(uintptr(windows.CurrentProcess()), uintptr(unsafe.Pointer(&c)), uintptr(c.cb))
	if r == 0 {
		return 0
	}
	return uint64(c.workingSetSize)
}
