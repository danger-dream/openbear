package mesh

import (
	"os"
	"slices"
)

const routeTable = "/proc/net/route"

// HostRoutes are this machine's IPv4 routes outside the mesh (the main
// table, without the default route and loopback) and those Takeover
// replaced, kept in dir: what the controller checks gateway subnets against.
func HostRoutes(dir, iface string) []HostRoute {
	data, _ := os.ReadFile(routeTable)
	held := parseRouteTable(data, iface)
	for p, lines := range loadTaken(dir) {
		held = append(held, heldRoute{p, takenDev(lines)})
	}
	out := []HostRoute{}
	for _, r := range held {
		h := HostRoute{r.Prefix.String(), r.Dev}
		if !slices.Contains(out, h) {
			out = append(out, h)
		}
	}
	return out
}
