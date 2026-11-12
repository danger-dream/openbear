package mesh

import (
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"net/netip"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
)

// heldRoute is a route of this machine's own, outside the mesh.
type heldRoute struct {
	Prefix netip.Prefix
	Dev    string
}

// routeHolder is a host that knows this machine's own routes: held lists
// them, those Takeover replaced included; restored says what it put back or
// took over since it was last asked.
type routeHolder interface {
	held() []heldRoute
	restored() []string
}

// HostRoute is one of this machine's IPv4 routes, as the controller is told.
type HostRoute struct {
	Net string `json:"net"`
	Dev string `json:"dev"`
}

// TakenFile keeps, by gateway subnet, the routes Takeover replaced, as
// "ip route show" printed them, so they come back after a crash too.
const TakenFile = "routes-taken.json"

// parseRouteTable reads /proc/net/route: the main table's IPv4 routes
// without the default route, loopback and the interface skip.
func parseRouteTable(data []byte, skip string) []heldRoute {
	lines := strings.Split(string(data), "\n")
	var out []heldRoute
	for _, line := range lines[min(1, len(lines)):] {
		f := strings.Fields(line)
		if len(f) < 8 || f[0] == "lo" || f[0] == skip {
			continue
		}
		dest, err1 := hex.DecodeString(f[1])
		mask, err2 := hex.DecodeString(f[7])
		if err1 != nil || err2 != nil || len(dest) != 4 || len(mask) != 4 {
			continue
		}
		bits := 0
		for m := binary.LittleEndian.Uint32(mask); m != 0; m >>= 1 {
			bits += int(m & 1)
		}
		// Each address is printed as a little-endian number: 0001A8C0 is 192.168.1.0.
		addr := netip.AddrFrom4([4]byte{dest[3], dest[2], dest[1], dest[0]})
		if bits == 0 {
			continue
		}
		r := heldRoute{netip.PrefixFrom(addr, bits).Masked(), f[0]}
		if !slices.Contains(out, r) {
			out = append(out, r)
		}
	}
	return out
}

// loadTaken reads TakenFile in dir: nothing when there is none.
func loadTaken(dir string) map[netip.Prefix][]string {
	out := map[netip.Prefix][]string{}
	if dir == "" {
		return out
	}
	raw, err := os.ReadFile(filepath.Join(dir, TakenFile))
	var saved map[string][]string
	if err != nil || json.Unmarshal(raw, &saved) != nil {
		return out
	}
	for k, lines := range saved {
		if p, err := netip.ParsePrefix(k); err == nil && p.Addr().Is4() && len(lines) > 0 {
			out[p.Masked()] = lines
		}
	}
	return out
}

// takenDev is the interface the first of the replaced routes went through.
func takenDev(lines []string) string {
	for _, line := range lines {
		f := strings.Fields(line)
		if i := slices.Index(f, "dev"); i >= 0 && i+1 < len(f) {
			return f[i+1]
		}
	}
	return ""
}

// virtualBridge is a container or VM bridge: docker0, Docker's own
// br-<id>, libvirt's virbr0, macOS's bridge100 and the like. Its network is
// this host's alone and often wide (172.17.0.0/16), so a gateway subnet
// strictly inside it is somewhere else. OpenWrt's br-lan is a real LAN.
func virtualBridge(dev string) bool {
	for _, p := range []string{"docker", "virbr", "cni", "flannel", "podman", "lxdbr", "lxcbr", "vmnet", "vboxnet"} {
		if strings.HasPrefix(dev, p) {
			return true
		}
	}
	if id, ok := strings.CutPrefix(dev, "br-"); ok && len(id) == 12 {
		_, err := hex.DecodeString(id)
		return err == nil
	}
	if n, ok := strings.CutPrefix(dev, "bridge"); ok && runtime.GOOS == "darwin" && len(n) == 3 && n[0] == '1' {
		return strings.Trim(n, "0123456789") == ""
	}
	return false
}

// clashes: r overlaps one of nets, other than a virtual bridge's network
// that holds it.
func clashes(r netip.Prefix, nets []heldRoute) bool {
	return slices.ContainsFunc(nets, func(n heldRoute) bool {
		return r.Overlaps(n.Prefix) && !(virtualBridge(n.Dev) && r.Bits() > n.Prefix.Bits())
	})
}
