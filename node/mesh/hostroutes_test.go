package mesh

import (
	"net/netip"
	"slices"
	"testing"
)

func TestParseRouteTable(t *testing.T) {
	table := "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n" +
		"eth0\t00000000\t0100000A\t0003\t0\t0\t0\t00000000\t0\t0\t0\n" +
		"wg0\t000AA8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n" +
		"docker0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n" +
		"obmesh0\t00006064\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n" +
		"lo\t0000007F\t00000000\t0001\t0\t0\t0\t000000FF\t0\t0\t0\n" +
		"wg0\t000AA8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n"
	got := parseRouteTable([]byte(table), "obmesh0")
	want := []heldRoute{{netip.MustParsePrefix("192.168.10.0/24"), "wg0"}, {netip.MustParsePrefix("172.17.0.0/16"), "docker0"}}
	if !slices.Equal(got, want) {
		t.Fatalf("%v, want %v", got, want)
	}
	if got := parseRouteTable(nil, "obmesh0"); len(got) != 0 {
		t.Fatalf("no table: %v", got)
	}
}

func TestVirtualBridge(t *testing.T) {
	for dev, want := range map[string]bool{"docker0": true, "br-0a1b2c3d4e5f": true, "virbr0": true, "cni0": true, "flannel.1": true,
		"br-lan": false, "br-wan": false, "br-0a1b2c3d4e5": false, "eth0": false, "wg0": false, "bridge0": false} {
		if virtualBridge(dev) != want {
			t.Errorf("%s: %v, want %v", dev, !want, want)
		}
	}
}

// Test networks no machine running this is on.
func TestWithoutLocalHeldRoutesAndBridges(t *testing.T) {
	p := netip.MustParsePrefix
	mesh := p("100.96.0.0/16")
	held := []heldRoute{{p("198.51.100.0/24"), "wg0"}, {p("198.18.0.0/15"), "docker0"}, {p("203.0.113.0/25"), "br-lan"}}
	routes := []netip.Prefix{mesh, p("198.51.100.0/24"), p("198.18.1.0/24"), p("198.18.0.0/15"), p("198.16.0.0/12"), p("203.0.113.0/24"), p("192.0.2.0/24")}

	kept, skipped := withoutLocal(routes, mesh, "obmesh0", held, nil)
	if want := []netip.Prefix{mesh, p("198.18.1.0/24"), p("192.0.2.0/24")}; !slices.Equal(kept, want) {
		t.Fatalf("kept %v, want %v", kept, want)
	}
	if want := []netip.Prefix{p("198.51.100.0/24"), p("198.18.0.0/15"), p("198.16.0.0/12"), p("203.0.113.0/24")}; !slices.Equal(skipped, want) {
		t.Fatalf("skipped %v, want %v", skipped, want)
	}
	// Taken over, a route clashing with one of this machine's goes in.
	kept, skipped = withoutLocal(routes, mesh, "obmesh0", held, []netip.Prefix{p("198.51.100.0/24")})
	if !slices.Contains(kept, p("198.51.100.0/24")) || slices.Contains(skipped, p("198.51.100.0/24")) {
		t.Fatalf("taken over: kept %v, skipped %v", kept, skipped)
	}
}
