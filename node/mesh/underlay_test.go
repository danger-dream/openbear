package mesh

import (
	"net/netip"
	"testing"
)

func TestUDPLimitLeavesTheFramingAndPadding(t *testing.T) {
	for _, tc := range []struct {
		mtu  int
		v6   bool
		want int
	}{
		{1500, false, 1412}, // more than the network's MTU: nothing held
		{1400, false, 1316}, // 1264 bytes end to end
		{1400, true, 1300},
		{1280, false, 1188},
		{300, false, minOwnMTU},
	} {
		if got := udpLimit(tc.mtu, tc.v6); got != tc.want {
			t.Errorf("udpLimit(%d, %v) = %d, want %d", tc.mtu, tc.v6, got, tc.want)
		}
		l := udpLimit(tc.mtu, tc.v6)
		if l == minOwnMTU {
			continue
		}
		// Its end-to-end payload, padded by either end, stays as it is, and
		// the packet, sealed and padded for the link, fits the interface.
		ip := 20
		if tc.v6 {
			ip = 40
		}
		if inner := l - e2eOverhead; inner%16 != 0 || ip+udpFraming+(l+15)&^15 > tc.mtu {
			t.Errorf("mtu %d: %d does not fit", tc.mtu, l)
		}
	}
}

func TestOwnMTUIsTheNetworksUnlessTheInterfaceTakesLess(t *testing.T) {
	c := &Config{Network: Network{MTU: 1400}}
	if c.ownMTU() != 1400 {
		t.Fatalf("no underlay: %d", c.ownMTU())
	}
	c.underlay = underlay{iface: "eth0", mtu: 1400, limit: 1316}
	if c.ownMTU() != 1316 {
		t.Fatalf("eth0 at 1400: %d", c.ownMTU())
	}
	c.Network.MTU = 1280
	if c.ownMTU() != 1280 {
		t.Fatalf("a network below the limit: %d", c.ownMTU())
	}
}

// This node's own packets are held to what its interface takes; what it
// forwards for others, to the network's MTU, and large paths keep theirs.
func TestOwnMTUHoldsOwnPacketsNotForwarded(t *testing.T) {
	p := newPaths(1400)
	p.configure(false, 1400, 1316)
	v := p.view.Load()
	z := &e2eNode{id: "B"}
	if got := v.innerMTU(z, "X"); got != 1264 {
		t.Fatalf("own end to end: %d", got)
	}
	if got := v.innerMTU(&e2eNode{id: "X"}, "X"); got != 1264 {
		t.Fatalf("own to a neighbour: %d", got)
	}
	if got := v.linkMTU("X"); got != 1400 {
		t.Fatalf("forwarded: %d", got)
	}
	p.configure(true, 1400, 1316)
	p.update(map[string]bool{"X": true}, nil)
	if got := p.view.Load().innerMTU(&e2eNode{id: "X"}, "X"); got != largeInner {
		t.Fatalf("a large link: %d", got)
	}
}

func TestUnderlayOnlyCountsUDPPeersAndShortInterfaces(t *testing.T) {
	c := &Config{Network: Network{MTU: 1400}, Links: []Link{{Endpoint: "udp://127.0.0.1:9"}, {Endpoint: "tls://127.0.0.1:9"}, {}, {Endpoint: "udp://example.com:9"}},
		Entry: &Entry{Candidates: []Candidate{{Endpoint: "udp://127.0.0.2:9"}}}}
	// The loopback interface takes far more than the network's MTU.
	if u := underlayFor(c, "obmesh0"); u != (underlay{}) {
		t.Fatalf("loopback: %+v", u)
	}
	if a, ok := udpPeerAddr("udp://[::ffff:10.0.0.1]:51999"); !ok || a != netip.MustParseAddrPort("10.0.0.1:51999") {
		t.Fatalf("mapped address: %v %v", a, ok)
	}
	if _, ok := udpPeerAddr("tcp://10.0.0.1:52000"); ok {
		t.Fatal("a TCP endpoint")
	}
}

func TestUnderlayDescribesItForEvents(t *testing.T) {
	if got := (underlay{iface: "eth0", mtu: 1400, limit: 1316}).describe(1400); got != "eth0 1400: this node's own packets up to 1264 bytes, the network's 1348" {
		t.Fatal(got)
	}
	if got := (underlay{}).describe(1400); got != "none: this node's own packets up to the network's 1348 bytes" {
		t.Fatal(got)
	}
}
