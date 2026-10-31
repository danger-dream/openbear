package mesh

import (
	"encoding/json"
	"net/netip"
	"slices"
	"strings"
	"testing"
)

func TestValidate(t *testing.T) {
	s := spec{relays: []string{"R1", "R2"}, members: map[string][]string{"A": {"R1", "R2"}, "B": {"R2"}},
		direct: [][2]string{{"A", "B"}}, subnets: map[string][]netip.Prefix{"B": {netip.MustParsePrefix("192.168.1.0/24")}}}
	l := newLab(t, s)
	for _, name := range []string{"R1", "A", "B"} {
		raw, _ := json.Marshal(l.config(name))
		if err := Validate(raw); err != nil {
			t.Fatalf("%s: %v", name, err)
		}
	}
	raw, _ := json.Marshal(l.config("A"))
	var loose map[string]any
	json.Unmarshal(raw, &loose)
	loose["addedLater"] = true
	raw, _ = json.Marshal(loose)
	if err := Validate(raw); err != nil {
		t.Fatalf("an unknown field was refused: %v", err)
	}
	for _, raw := range []string{`{"schema": 2, "revision": 3}`, `{"revision": 3}`, `[]`} {
		if Validate([]byte(raw)) == nil {
			t.Fatalf("accepted %s", raw)
		}
	}

	pin := strings.Repeat("ab", 32)
	cases := map[string]struct {
		node string
		edit func(c *Config)
		want string
	}{
		"address outside the mesh":   {"A", func(c *Config) { c.Self.Address = netip.MustParseAddr("10.0.0.1") }, "self.address"},
		"address in the control net": {"A", func(c *Config) { c.Self.Address = netip.MustParseAddr("100.96.255.40") }, "self.address"},
		"address taken":              {"A", func(c *Config) { c.Self.Address = c.Nodes[0].Address }, "reuses"},
		"subnet with host bits": {"B", func(c *Config) {
			c.Self.Gateway.Subnets = []netip.Prefix{netip.MustParsePrefix("192.168.1.1/24")}
		}, "host bits"},
		"overlapping gateways": {"A", func(c *Config) {
			c.Nodes[len(c.Nodes)-1].Subnets = []netip.Prefix{netip.MustParsePrefix("192.168.0.0/16")}
		}, "overlaps"},
		"subnet inside the mesh": {"B", func(c *Config) {
			c.Self.Gateway.Subnets = []netip.Prefix{netip.MustParsePrefix("100.96.8.0/24")}
		}, "network.prefix"},
		"control prefix outside": {"A", func(c *Config) { c.Network.ControlPrefix = netip.MustParsePrefix("10.1.0.0/24") }, "controlPrefix"},
		"mtu":                    {"A", func(c *Config) { c.Network.MTU = 9000 }, "mtu"},
		"role":                   {"A", func(c *Config) { c.Self.Role = "gateway" }, "self.role"},
		"member with nextHops":   {"A", func(c *Config) { c.NextHops = map[string]string{"R1": "R1"} }, "nextHops"},
		"relay with an entry":    {"R1", func(c *Config) { c.Entry = &Entry{Mode: "fixed"} }, "entry"},
		"relay with relays": {"A", func(c *Config) {
			for i := range c.Nodes {
				if c.Nodes[i].ID == "R2" {
					c.Nodes[i].Relays = []string{"R1"}
				}
			}
		}, "lists relays"},
		"member attached to a member": {"A", func(c *Config) { c.Nodes[0].Relays = []string{"A"} }, "not a relay"},
		"entry on a member":           {"A", func(c *Config) { c.Entry.Candidates[0].Relay = "B" }, "not a relay"},
		"direct link on a relay":      {"R1", func(c *Config) { c.Links[0].Kind = kindDirect }, "two members"},
		"relay link on a member":      {"A", func(c *Config) { c.Links[0].Kind = kindRelay }, "two relays"},
		"dial without endpoint":       {"A", func(c *Config) { c.Links[0].Endpoint = "" }, "endpoint"},
		"endpoint without dial":       {"B", func(c *Config) { c.Links[0].Endpoint = "udp://127.0.0.1:1" }, "does not dial"},
		"bad scheme":                  {"A", func(c *Config) { c.Entry.Candidates[0].Endpoint = "http://1.2.3.4:80" }, "udp://"},
		"tls without pins":            {"A", func(c *Config) { c.Entry.Candidates[0].Endpoint = "tls://1.2.3.4:52001" }, "tlsPins"},
		"pins on udp":                 {"A", func(c *Config) { c.Entry.Candidates[0].TLSPins = []string{pin} }, "tlsPins"},
		"three pins": {"A", func(c *Config) {
			c.Entry.Candidates[0].Endpoint = "tls://1.2.3.4:52001"
			c.Entry.Candidates[0].TLSPins = []string{pin, pin, pin}
		}, "two tlsPins"},
		"next hop not linked": {"R1", func(c *Config) { c.Links = nil }, "not a linked relay"},
		"rule proto":          {"A", func(c *Config) { c.Rules = []Rule{{Proto: "sctp"}} }, "proto"},
		"icmp with ports":     {"A", func(c *Config) { c.Rules = []Rule{{Proto: "icmp", Ports: [][2]uint16{{1, 2}}}} }, "icmp"},
		"port range":          {"A", func(c *Config) { c.Rules = []Rule{{Proto: "tcp", Ports: [][2]uint16{{9, 1}}}} }, "port range"},
		"same stream ports":   {"R1", func(c *Config) { c.Self.Listen.TCP, c.Self.Listen.TLS = 52000, 52000 }, "same port"},
		"bad key":             {"A", func(c *Config) { c.Nodes[0].PublicKey = "x" }, "publicKey"},
		"upload limit":        {"A", func(c *Config) { v := 0.0; c.Self.UploadLimitMbps = &v }, "uploadLimitMbps"},
		"punched entry over tcp": {"A", func(c *Config) {
			c.Entry.Candidates[0].Punch, c.Entry.Candidates[0].Endpoint = true, "tcp://192.0.2.1:52000"
		}, "not udp"},
		"punched entry with pins": {"A", func(c *Config) {
			c.Entry.Candidates[0].Punch, c.Entry.Candidates[0].Endpoint, c.Entry.Candidates[0].TLSPins = true, "", []string{pin}
		}, "tlsPins"},
		"stun without port": {"A", func(c *Config) { c.Reachability = &Reachability{STUN: []string{"stun.example.net"}} }, "reachability.stun[0]"},
		"punched over tcp": {"A", func(c *Config) {
			c.Links[0].Punch, c.Self.Listen.UDP, c.Links[0].Endpoint = true, 51820, "tcp://192.0.2.1:52000"
		}, "not udp"},
		"reflector without port": {"A", func(c *Config) { c.Reflectors = []string{"192.0.2.1"} }, "reflectors[0]"},
	}
	for name, tc := range cases {
		c := l.config(tc.node)
		tc.edit(c)
		raw, _ := json.Marshal(c)
		err := Validate(raw)
		if err == nil || !strings.Contains(err.Error(), tc.want) {
			t.Errorf("%s: got %v, want an error about %q", name, err, tc.want)
		}
	}

	// The controller's punched link: dial set on one side, no endpoint, and
	// reflectors that may be names or bracketed IPv6.
	c := l.config("A")
	c.Links[0].Punch, c.Links[0].Endpoint, c.Self.Listen.UDP = true, "", 51820
	c.Reflectors = []string{"203.0.113.3:51990", "berlin.example.net:51999", "[2001:db8::3]:51999"}
	if raw, _ := json.Marshal(c); Validate(raw) != nil {
		t.Errorf("punched link refused: %v", Validate(raw))
	}
	// Without a UDP listener a punched link uses an ephemeral port; a
	// punched entry has no endpoint or a UDP one; STUN servers by name.
	c.Self.Listen.UDP = 0
	c.Entry.Candidates[0].Punch, c.Entry.Candidates[0].Endpoint = true, ""
	c.Reachability = &Reachability{STUN: []string{"stun.example.net:3478"}}
	if raw, _ := json.Marshal(c); Validate(raw) != nil {
		t.Errorf("punched entry refused: %v", Validate(raw))
	}
	c.Entry.Candidates[0].Endpoint = "udp://192.0.2.7:51999"
	if raw, _ := json.Marshal(c); Validate(raw) != nil {
		t.Errorf("punched entry with an endpoint refused: %v", Validate(raw))
	}
	// Two relays punching their link.
	r := l.config("R1")
	r.Links[0].Punch, r.Links[0].Endpoint = true, ""
	if raw, _ := json.Marshal(r); Validate(raw) != nil {
		t.Errorf("punched relay link refused: %v", Validate(raw))
	}
}

func TestAllowedIPsOnRelays(t *testing.T) {
	// R1 - R2 - R3 in a line; M1 attaches to R3 and has a LAN, M2 to R1.
	lan := netip.MustParsePrefix("10.20.20.0/24")
	s := spec{relays: []string{"R1", "R2", "R3"}, members: map[string][]string{"M1": {"R3", "R2"}, "M2": {"R1"}},
		subnets: map[string][]netip.Prefix{"M1": {lan}}}
	l := newLab(t, s)
	c := l.config("R1")
	c.Links = slices.DeleteFunc(c.Links, func(x Link) bool { return x.Peer == "R3" })
	c.NextHops["R3"] = "R2"
	peers := mustPlan(t, c)
	byID := map[string]peerSpec{}
	for _, p := range peers {
		byID[p.ID] = p
	}
	h := func(n string) netip.Prefix { return host(l.addr(n)) }
	want := map[string][]netip.Prefix{
		"R2": sortPrefixes([]netip.Prefix{h("R2"), h("R3"), h("M1"), lan}),
		"M2": {h("M2")},
	}
	if len(peers) != 2 {
		t.Fatalf("R1 peers %+v", peers)
	}
	for id, w := range want {
		if !slices.Equal(byID[id].Allowed, w) {
			t.Errorf("R1's %s carries %v, want %v", id, byID[id].Allowed, w)
		}
	}
	if byID["M2"].Endpoint != "" || byID["M2"].Kind != kindMember || byID["R2"].Endpoint == "" {
		t.Errorf("R1 peers %+v", peers)
	}

	// R2 may take M1 but does not own it: its peer stays, empty.
	c2 := l.config("R2")
	p2 := mustPlan(t, c2)
	for _, p := range p2 {
		switch p.ID {
		case "M1":
			if len(p.Allowed) != 0 {
				t.Errorf("R2 routes to M1 it does not own: %v", p.Allowed)
			}
		case "R3":
			if !slices.Equal(p.Allowed, sortPrefixes([]netip.Prefix{h("R3"), h("M1"), lan})) {
				t.Errorf("R2's R3 carries %v", p.Allowed)
			}
		case "M2":
			t.Error("R2 has a peer for M2, which may not attach to it")
		}
	}
	if got := hostRoutes(c, peers); !slices.Equal(got, sortPrefixes([]netip.Prefix{meshPrefix, lan})) {
		t.Errorf("R1's OS routes %v", got)
	}
}

func TestAllowedIPsOnMembers(t *testing.T) {
	lan, lan2 := netip.MustParsePrefix("10.20.20.0/24"), netip.MustParsePrefix("10.30.0.0/16")
	s := spec{relays: []string{"R1", "R2"}, members: map[string][]string{"A": {"R1", "R2"}, "B": {"R1"}, "C": {"R2"}},
		direct: [][2]string{{"A", "B"}}, subnets: map[string][]netip.Prefix{"B": {lan}, "C": {lan2}}}
	l := newLab(t, s)
	peers := mustPlan(t, l.config("A"))
	if len(peers) != 2 {
		t.Fatalf("A peers %+v: only the active entry and the direct peer", peers)
	}
	for _, p := range peers {
		switch p.ID {
		case "R1":
			if p.Kind != kindEntry || p.Candidate != "c1" || !slices.Equal(p.Allowed, sortPrefixes([]netip.Prefix{meshPrefix, lan2})) {
				t.Errorf("entry %+v", p)
			}
		case "B":
			if p.Kind != kindDirect || p.Endpoint == "" || !slices.Equal(p.Allowed, sortPrefixes([]netip.Prefix{host(l.addr("B")), lan})) {
				t.Errorf("direct %+v", p)
			}
		default:
			t.Errorf("unexpected peer %+v", p)
		}
	}
	pb := mustPlan(t, l.config("B"))
	for _, p := range pb {
		if p.ID == "A" && p.Endpoint != "" {
			t.Error("B dials A, which dials it")
		}
	}
}

// The controller writes "entry": null for relays, "nextHops": {} for
// members and tlsPins on tls links; TLS listeners are for relays only.
func TestValidateControllerForms(t *testing.T) {
	s := spec{relays: []string{"R1", "R2"}, members: map[string][]string{"A": {"R1"}, "B": {"R1"}}, direct: [][2]string{{"A", "B"}}}
	l := newLab(t, s)
	pin := strings.Repeat("0f", 32)
	set := func(name string, edit func(m map[string]any)) []byte {
		raw, _ := json.Marshal(l.config(name))
		var m map[string]any
		json.Unmarshal(raw, &m)
		edit(m)
		raw, _ = json.Marshal(m)
		return raw
	}
	relay := set("R1", func(m map[string]any) {
		m["entry"] = nil
		m["links"].([]any)[0].(map[string]any)["endpoint"] = "tls://192.0.2.1:52001"
		m["links"].([]any)[0].(map[string]any)["tlsPins"] = []string{pin}
		m["self"].(map[string]any)["listen"].(map[string]any)["tls"] = 52001
	})
	member := set("A", func(m map[string]any) { m["nextHops"] = map[string]any{} })
	// A member with only direct links gets an entry without candidates.
	unattached := set("B", func(m map[string]any) {
		m["entry"] = map[string]any{"mode": "fixed", "candidates": []any{}, "autoReturn": true}
	})
	for name, raw := range map[string][]byte{"relay": relay, "member": member, "unattached member": unattached} {
		if err := Validate(raw); err != nil {
			t.Errorf("%s: %v", name, err)
		}
	}
	if c, _ := ParseConfig(unattached); len(mustPlan(t, c)) != 1 {
		t.Fatalf("a member without candidates should have its direct peer only: %+v", mustPlan(t, c))
	}
	c, err := ParseConfig(relay)
	if err != nil {
		t.Fatal(err)
	}
	peers := mustPlan(t, c)
	if peers[0].ID != "R2" || len(peers[0].Pins) != 1 || peers[0].Pins[0] != pin {
		t.Fatalf("the tls link's pins did not reach its peer: %+v", peers[0])
	}
	bad := map[string][]byte{
		"tls listener on a member": set("A", func(m map[string]any) { m["self"].(map[string]any)["listen"].(map[string]any)["tls"] = 52001 }),
		"tls link without pins": set("R1", func(m map[string]any) {
			m["links"].([]any)[0].(map[string]any)["endpoint"] = "tls://192.0.2.1:52001"
		}),
		"pins on a link that does not dial": set("B", func(m map[string]any) {
			m["links"].([]any)[0].(map[string]any)["tlsPins"] = []string{pin}
		}),
	}
	for name, raw := range bad {
		if Validate(raw) == nil {
			t.Errorf("%s was accepted", name)
		}
	}
}
