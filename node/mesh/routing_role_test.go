package mesh

import (
	"encoding/json"
	"net/netip"
	"slices"
	"testing"
	"time"
)

// TestRoutingFollowsRoleChange: H starts as a member of A and later
// becomes a relay linked to A and B through a new config, as a home server
// turned site relay did. A failed attempt leaves it a member without a
// router; the applied one gives it one that measures its links, exchanges
// advertisements, chooses next hops and shows in status.json; back to a
// member, routing is gone on H and A stops routing to it.
func TestRoutingFollowsRoleChange(t *testing.T) {
	l := newHealthLab(t, spec{relays: []string{"A", "B"}, members: map[string][]string{"H": {"A"}}, rules: map[string][]Rule{"A": allowICMP, "B": allowICMP, "H": allowICMP}})
	keys := l.edit
	bias := map[string]map[string]float64{"A": {"B": 20, "H": 20}, "B": {"A": 20, "H": 20}, "H": {"A": 20, "B": 20}}
	l.edit = func(name string, c *Config) {
		keys(name, c)
		if c.Self.Role == roleRelay {
			b := map[string]float64{}
			for _, link := range c.Links {
				if v, ok := bias[name][link.Peer]; ok {
					b[link.Peer] = v
				}
			}
			c.Routing = &Routing{Mode: "dynamic", Bias: b}
		}
	}
	nodes := map[string]*running{}
	for _, id := range []string{"A", "B", "H"} {
		nodes[id] = l.startHealthNode(id)
	}
	t.Cleanup(func() {
		if t.Failed() {
			for id, n := range nodes {
				raw, _ := json.Marshal(n.status().Routing)
				t.Logf("%s routing %s events %+v", id, raw, n.events.snapshot())
			}
		}
	})
	apply := func(revision int64, ids ...string) {
		t.Helper()
		for _, id := range ids {
			c := l.config(id)
			c.Revision = revision
			if err := c.check(); err != nil {
				t.Fatalf("%s: %v", id, err)
			}
			if err := nodes[id].applyConfig(c); err != nil {
				t.Fatalf("%s revision %d: %v", id, revision, err)
			}
		}
	}
	h := nodes["H"]
	if h.router() != nil || h.status().Routing != nil {
		t.Fatal("a member has routing")
	}

	// The relay config fails at the health manager: the new router goes.
	l.nodes["H"].member = false
	l.spec.relays = append(l.spec.relays, "H")
	delete(l.spec.members, "H")
	bad := l.config("H")
	bad.Revision = 2
	bad.Health = &Health{ConsecutiveFailures: 17}
	if err := h.applyConfig(bad); err == nil {
		t.Fatal("an invalid health config was applied")
	}
	h.engine.mu.Lock()
	relay := h.engine.cfg.isRelay()
	h.engine.mu.Unlock()
	if h.router() != nil || h.status().Routing != nil || relay {
		t.Fatalf("after a failed change: router %v, relay %v", h.router() != nil, relay)
	}

	apply(3, "A", "B", "H")
	paths := func(want map[string][]string) func() bool {
		return func() bool {
			for id, n := range nodes {
				s := n.status().Routing
				if s == nil || len(s.Routes) != 2 {
					return false
				}
				for _, r := range s.Routes {
					if r.Cost == nil || !slices.Equal(r.Path, want[id+r.Dest]) {
						return false
					}
				}
			}
			return true
		}
	}
	// Paths need both ends' advertisements: H's routes show what it
	// received, A's and B's routes to H what it sent.
	healthWait(t, 40*time.Second, paths(map[string][]string{
		"AB": {"A", "B"}, "AH": {"A", "H"}, "BA": {"B", "A"}, "BH": {"B", "H"}, "HA": {"H", "A"}, "HB": {"H", "B"},
	}))
	s := h.status().Routing
	raw, _ := json.Marshal(s)
	t.Logf("H routing: %s", raw)
	if s.Mode != "dynamic" || len(s.Links) != 2 {
		t.Fatalf("H routing %s", raw)
	}
	for _, link := range s.Links {
		if link.State != "up" || link.Cost == nil || link.RTTMs <= 0 {
			t.Fatalf("H link %+v", link)
		}
	}

	// Next hops come from the router: a bias on H's end of H-A moves H↔A
	// through B. A sees it only in H's advertisements (a link costs what
	// the dearer end says), and H's path needs B-A from A's or B's.
	bias["H"]["A"] = 100
	apply(4, "H")
	healthWait(t, 30*time.Second, paths(map[string][]string{
		"AB": {"A", "B"}, "AH": {"A", "B", "H"}, "BA": {"B", "A"}, "BH": {"B", "H"}, "HA": {"H", "B", "A"}, "HB": {"H", "B"},
	}))
	healthWait(t, 2*time.Second, func() bool {
		return slices.Contains(allowedOf(h, "B"), netip.PrefixFrom(l.addr("A"), 32)) && !slices.Contains(allowedOf(h, "A"), netip.PrefixFrom(l.addr("A"), 32))
	})

	// Back to a member of A.
	l.nodes["H"].member = true
	l.spec.relays = l.spec.relays[:2]
	l.spec.members["H"] = []string{"A"}
	apply(5, "A", "B", "H")
	if h.router() != nil || h.status().Routing != nil {
		t.Fatal("routing left on a member")
	}
	if raw, _ := json.Marshal(h.status()); jsonHas(raw, "routing") {
		t.Fatalf("status.json still has routing: %s", raw)
	}
	healthWait(t, 10*time.Second, func() bool {
		s := nodes["A"].status().Routing
		if s == nil || len(s.Links) != 1 || len(s.Routes) != 1 {
			return false
		}
		return s.Links[0].Peer == "B" && s.Routes[0].Dest == "B" && slices.Equal(s.Routes[0].Path, []string{"A", "B"})
	})
}

func jsonHas(raw []byte, key string) bool {
	var m map[string]json.RawMessage
	json.Unmarshal(raw, &m)
	_, ok := m[key]
	return ok
}
