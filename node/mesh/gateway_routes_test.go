package mesh

import (
	"encoding/json"
	"net/netip"
	"slices"
	"strings"
	"testing"
)

func prefixes(s ...string) []netip.Prefix {
	out := []netip.Prefix{}
	for _, x := range s {
		out = append(out, netip.MustParsePrefix(x))
	}
	return sortPrefixes(out)
}

// TestGatewayRoutesOnMembers: with self.gatewayRoutes a member's entry
// carries the mesh and those routes, less any a direct peer carries in
// full; a direct peer its address and its subnets' overlap with them, the
// narrower prefix. Without the field everything is as before.
func TestGatewayRoutesOnMembers(t *testing.T) {
	s := spec{relays: []string{"R1", "R2"}, members: map[string][]string{"A": {"R1", "R2"}, "B": {"R1"}, "C": {"R2"}},
		direct: [][2]string{{"A", "B"}}, subnets: map[string][]netip.Prefix{"B": prefixes("10.20.20.0/24"), "C": prefixes("10.30.0.0/16")}}
	l := newLab(t, s)
	mesh := meshPrefix.String()
	b := host(l.addr("B")).String()
	cases := []struct {
		name          string
		routes        []string // nil: no field
		entry, direct []string
	}{
		{"no field", nil, []string{mesh, "10.30.0.0/16"}, []string{b, "10.20.20.0/24"}},
		{"empty", []string{}, []string{mesh}, []string{b}},
		{"part of C's", []string{"10.30.5.0/24"}, []string{mesh, "10.30.5.0/24"}, []string{b}},
		{"part of B's", []string{"10.20.20.0/25", "10.30.0.0/16"}, []string{mesh, "10.30.0.0/16"}, []string{b, "10.20.20.0/25"}},
		{"wider than B's", []string{"10.20.0.0/16"}, []string{mesh, "10.20.0.0/16"}, []string{b, "10.20.20.0/24"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			cfg := l.config("A")
			if c.routes != nil {
				gw := prefixes(c.routes...)
				cfg.Self.GatewayRoutes = &gw
			}
			raw, _ := json.Marshal(cfg)
			if err := Validate(raw); err != nil {
				t.Fatal(err)
			}
			if c.routes != nil && !strings.Contains(string(raw), `"gatewayRoutes":[`) {
				t.Fatalf("not written: %s", raw)
			}
			peers := mustPlan(t, cfg)
			var other []netip.Prefix
			for _, p := range peers {
				want := c.entry
				if p.Kind == kindDirect {
					want = c.direct
					other = append(other, p.Allowed...)
				} else {
					other = append(other, p.Allowed...)
				}
				if !slices.Equal(p.Allowed, prefixes(want...)) {
					t.Errorf("%s %s carries %v, want %v", p.Kind, p.ID, p.Allowed, want)
				}
			}
			// The OS routes: the mesh and every subnet a peer carries.
			var subnets []netip.Prefix
			for _, p := range other {
				if !meshPrefix.Overlaps(p) {
					subnets = append(subnets, p)
				}
			}
			if got := hostRoutes(cfg, peers); !slices.Equal(got, sortPrefixes(append([]netip.Prefix{meshPrefix}, subnets...))) {
				t.Errorf("OS routes %v", got)
			}
		})
	}
}

// TestGatewayRoutesOnRelays: a relay forwards every subnet whatever its
// gatewayRoutes; only its OS routes follow them.
func TestGatewayRoutesOnRelays(t *testing.T) {
	lan := netip.MustParsePrefix("10.20.20.0/24")
	s := spec{relays: []string{"R1", "R2", "R3"}, members: map[string][]string{"M1": {"R3", "R2"}, "M2": {"R1"}},
		subnets: map[string][]netip.Prefix{"M1": {lan}}}
	l := newLab(t, s)
	c := l.config("R1")
	c.Links = slices.DeleteFunc(c.Links, func(x Link) bool { return x.Peer == "R3" })
	c.NextHops["R3"] = "R2"
	before := mustPlan(t, c)
	for _, tc := range []struct {
		routes []string
		os     []netip.Prefix
	}{
		{nil, []netip.Prefix{meshPrefix, lan}},
		{[]string{}, []netip.Prefix{meshPrefix}},
		{[]string{"10.20.20.0/24"}, []netip.Prefix{meshPrefix, lan}},
		{[]string{"10.20.20.128/25", "192.168.0.0/16"}, prefixes(meshPrefix.String(), "10.20.20.128/25")},
	} {
		if tc.routes != nil {
			gw := prefixes(tc.routes...)
			c.Self.GatewayRoutes = &gw
		}
		peers := mustPlan(t, c)
		if len(peers) != len(before) {
			t.Fatalf("%v: peers %+v", tc.routes, peers)
		}
		for i := range peers {
			if !peers[i].equal(before[i]) {
				t.Errorf("%v: %s carries %v, before %v", tc.routes, peers[i].ID, peers[i].Allowed, before[i].Allowed)
			}
		}
		if got := hostRoutes(c, peers); !slices.Equal(got, sortPrefixes(tc.os)) {
			t.Errorf("%v: OS routes %v, want %v", tc.routes, got, tc.os)
		}
	}
}

// TestGatewaySubnetsOfARelay: a relay's subnets go where the relay itself
// goes, as a member's go to its owner.
func TestGatewaySubnetsOfARelay(t *testing.T) {
	home := netip.MustParsePrefix("10.10.10.0/24")
	l := newLab(t, spec{relays: []string{"R1", "R2", "R3"}, members: map[string][]string{}, subnets: map[string][]netip.Prefix{"R3": {home}}})
	c := l.config("R1")
	c.Links = slices.DeleteFunc(c.Links, func(x Link) bool { return x.Peer == "R3" })
	c.NextHops["R3"] = "R2"
	for _, tc := range []struct {
		routes []string
		os     []netip.Prefix
	}{
		{[]string{"10.10.10.0/24"}, []netip.Prefix{meshPrefix, home}},
		{[]string{}, []netip.Prefix{meshPrefix}},
	} {
		gw := prefixes(tc.routes...)
		c.Self.GatewayRoutes = &gw
		peers := mustPlan(t, c)
		i := slices.IndexFunc(peers, func(p peerSpec) bool { return p.ID == "R2" })
		if want := prefixes(host(l.addr("R2")).String(), host(l.addr("R3")).String(), home.String()); i < 0 || !slices.Equal(peers[i].Allowed, want) {
			t.Fatalf("%v: R2 carries %+v, want %v", tc.routes, peers, want)
		}
		if got := hostRoutes(c, peers); !slices.Equal(got, sortPrefixes(tc.os)) {
			t.Errorf("%v: OS routes %v, want %v", tc.routes, got, tc.os)
		}
	}
}

func TestValidateGatewayRoutes(t *testing.T) {
	l := newLab(t, spec{relays: []string{"R1"}, members: map[string][]string{"A": {"R1"}}})
	for routes, want := range map[string]string{
		`[]`:                 "",
		`["10.20.0.0/16"]`:   "",
		`["10.20.0.1/16"]`:   "host bits",
		`["100.96.8.0/24"]`:  "network.prefix",
		`["fd00::/64"]`:      "IPv4",
		`["not a prefix"]`:   "invalid",
		`null`:               "",
		`["0.0.0.0/0"]`:      "network.prefix",
		`["192.168.1.0/24"]`: "",
	} {
		raw, _ := json.Marshal(l.config("A"))
		raw = []byte(strings.Replace(string(raw), `"self":{`, `"self":{"gatewayRoutes":`+routes+`,`, 1))
		err := Validate(raw)
		if (want == "") != (err == nil) || (err != nil && !strings.Contains(err.Error(), want)) {
			t.Errorf("%s: %v, want %q", routes, err, want)
		}
	}
}
