package mesh

import (
	"encoding/json"
	"net/netip"
	"slices"
	"strings"
	"testing"
	"time"
)

// TestRelayRoutingFollowsAdvertisements runs three relays with health,
// every one linked to the others: once their advertisements meet, each
// routes to the others directly; a bias on A-C, set on both ends, moves
// A↔C through B. AllowedIPs follow without new sessions, traffic flows
// through B, and status.json and the events show it.
func TestRelayRoutingFollowsAdvertisements(t *testing.T) {
	l := newHealthLab(t, spec{relays: []string{"A", "B", "C"}, rules: map[string][]Rule{"A": allowICMP, "B": allowICMP, "C": allowICMP}})
	keys := l.edit
	// Loopback round trips differ by tenths of a millisecond, which would
	// make a two-hop path win now and then, and it would stay: the direct
	// one is not 10 ms cheaper. 20 ms a link makes paths direct.
	bias := map[string]map[string]float64{"A": {"B": 20, "C": 20}, "B": {"A": 20, "C": 20}, "C": {"A": 20, "B": 20}}
	l.edit = func(name string, c *Config) {
		keys(name, c)
		c.Routing = &Routing{Mode: "dynamic", Bias: bias[name]}
	}
	nodes := map[string]*running{}
	for _, id := range []string{"A", "B", "C"} {
		nodes[id] = l.startHealthNode(id)
	}
	t.Cleanup(func() {
		if t.Failed() {
			for id, n := range nodes {
				raw, _ := json.Marshal(n.status().Routing)
				t.Logf("%s routing %s events %+v", id, raw, n.events.snapshot())
				t.Logf("%s counters %+v allowed %v", id, n.status().Counters, n.peers)
			}
		}
	})
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
	// A probe lost while the paths start costs a link 100 ms until ten
	// more answers and the 5 s hold, so allow for one.
	healthWait(t, 40*time.Second, paths(map[string][]string{
		"AB": {"A", "B"}, "AC": {"A", "C"}, "BA": {"B", "A"}, "BC": {"B", "C"}, "CA": {"C", "A"}, "CB": {"C", "B"},
	}))
	s := nodes["A"].status()
	raw, _ := json.Marshal(s.Routing)
	t.Logf("A routing: %s", raw)
	if s.Routing.Mode != "dynamic" || len(s.Routing.Links) != 2 {
		t.Fatalf("A routing %s", raw)
	}
	for _, link := range s.Routing.Links {
		if link.State != "up" || link.Cost == nil || link.RTTMs <= 0 || link.Loss != 0 {
			t.Fatalf("A link %+v", link)
		}
	}
	for _, key := range []string{`"peer":`, `"state":`, `"rttMs":`, `"jitterMs":`, `"loss":`, `"cost":`, `"penalty":`, `"dest":`, `"nextHop":`, `"path":`, `"since":`, `"switches":`} {
		if !strings.Contains(string(raw), key) {
			t.Fatalf("routing status lacks %s: %s", key, raw)
		}
	}
	handshake := handshakeOf(nodes["A"], "C")

	bias["A"]["C"] = 100
	bias["C"]["A"] = 100
	for _, id := range []string{"A", "C"} {
		c := l.config(id)
		c.Revision = 2
		if err := nodes[id].applyConfig(c); err != nil {
			t.Fatal(err)
		}
	}
	healthWait(t, 30*time.Second, paths(map[string][]string{
		"AB": {"A", "B"}, "AC": {"A", "B", "C"}, "BA": {"B", "A"}, "BC": {"B", "C"}, "CA": {"C", "B", "A"}, "CB": {"C", "B"},
	}))
	// Routes are applied on the health manager's next tick, 50 ms at most.
	healthWait(t, 2*time.Second, func() bool {
		return slices.Contains(allowedOf(nodes["A"], "B"), netip.PrefixFrom(l.addr("C"), 32)) && len(allowedOf(nodes["A"], "C")) == 0
	})
	found := false
	for _, e := range nodes["A"].events.snapshot() {
		if e.Kind == "route.changed" && e.Detail == "C: C -> B (better)" {
			found = true
		}
	}
	if !found {
		t.Fatalf("A events: %+v", nodes["A"].events.snapshot())
	}
	// Activity follows the link, not a data-plane session, which B-C has
	// had no packet to start yet.
	for _, p := range nodes["B"].status().Peers {
		if p.ID == "C" && !p.Active {
			t.Fatalf("B's peer C inactive over an up link: %+v", p)
		}
	}
	forwarded := nodes["B"].tun.stats.Forwarded.Load()
	// B-C has carried no data yet; its first packets may wait for a session.
	arrived := false
	for i := 0; i < 5 && !arrived; i++ {
		send(nodes["A"], packet(l.addr("A"), l.addr("C"), 1, 64, uint16(7+i), 0, false, icmp(8, 7, nil)))
		arrived = recv(nodes["C"], 2*time.Second) != nil
	}
	if !arrived {
		t.Fatal("A's pings through B did not arrive")
	}
	if nodes["B"].tun.stats.Forwarded.Load() == forwarded {
		t.Fatal("B did not forward A's ping")
	}
	if got := handshakeOf(nodes["A"], "C"); got != handshake {
		t.Fatalf("A's session with C was renewed: %d -> %d", handshake, got)
	}
	// C is no next hop of A's any more, but its session works: active,
	// not routed. B is both.
	for _, p := range nodes["A"].status().Peers {
		if (p.ID == "C" && (!p.Active || p.Routed)) || (p.ID == "B" && (!p.Active || !p.Routed)) {
			t.Fatalf("A's peer %s: active %v, routed %v (handshake %d)", p.ID, p.Active, p.Routed, handshake)
		}
	}

	// A's session with C is still fresh when C goes away; the link is not.
	nodes["C"].close()
	healthWait(t, 15*time.Second, func() bool {
		for _, p := range nodes["A"].status().Peers {
			if p.ID == "C" {
				return !p.Active
			}
		}
		return false
	})
}
