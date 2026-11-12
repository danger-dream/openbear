package mesh

import (
	"net/netip"
	"slices"
	"strings"
	"testing"
)

// With large packets the interface takes largeInner while the mesh's routes
// keep the small MTU; host routes of the nodes with large paths come and go
// with them, and the interface is raised only after the routes that hold
// the rest down, lowered before they go.
func TestLargeHostRoutes(t *testing.T) {
	f := newFakeHost(t)
	h := newLinuxHost("obmesh0", f.exec, f.has)
	small := fwConfig
	small.MTU = 1368
	small.Routes = []netip.Prefix{small.Prefix}
	if err := h.apply(small); err != nil {
		t.Fatal(err)
	}
	b, c := netip.MustParseAddr("100.96.0.12"), netip.MustParseAddr("100.96.0.14")
	large := small
	large.MTU, large.RouteMTU, large.Hosts = largeInner, 1368, []hostRoute{{b, largeInner}}
	order := func(want ...string) {
		t.Helper()
		var got []string
		for _, call := range f.calls {
			if call[0] == "ip" {
				got = append(got, strings.Join(call, " "))
			}
		}
		at := 0
		for _, w := range want {
			i := slices.Index(got[at:], w)
			if i < 0 {
				t.Fatalf("%q not run after %d in %q", w, at, got)
			}
			at += i + 1
		}
		f.calls = nil
	}
	f.calls = nil
	if err := h.apply(large); err != nil {
		t.Fatal(err)
	}
	order("ip -4 route replace 100.96.0.0/16 dev obmesh0 src 100.96.0.13 mtu 1368",
		"ip -4 route replace 100.96.0.12/32 dev obmesh0 src 100.96.0.13 mtu 8948",
		"ip link set dev obmesh0 mtu 8948 up")
	next := large
	next.Hosts = []hostRoute{{c, largeFar}}
	if err := h.apply(next); err != nil {
		t.Fatal(err)
	}
	order("ip -4 route replace 100.96.0.14/32 dev obmesh0 src 100.96.0.13 mtu 8932", "ip -4 route del 100.96.0.12/32 dev obmesh0")
	if err := h.apply(small); err != nil {
		t.Fatal(err)
	}
	order("ip link set dev obmesh0 mtu 1368 up", "ip -4 route replace 100.96.0.0/16 dev obmesh0 src 100.96.0.13",
		"ip -4 route del 100.96.0.14/32 dev obmesh0")
	if !h.cur.equal(small) || len(h.cur.Hosts) != 0 {
		t.Fatal("host state not back")
	}
}
