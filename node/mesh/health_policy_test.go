package mesh

import (
	"testing"
	"time"
)

func TestHealthFixedRelayRestartWithoutEndpointCache(t *testing.T) {
	l := newHealthLab(t, spec{relays: []string{"R"}, members: map[string][]string{"M": {"R"}, "N": {"R"}}, rules: map[string][]Rule{"M": allowICMP, "N": allowICMP}})
	edit := l.edit
	l.edit = func(name string, c *Config) {
		edit(name, c)
		if c.Entry != nil {
			c.Entry.Mode = "fixed"
		}
	}
	r := l.startHealthNode("R")
	m := l.startHealthNode("M")
	n := l.startHealthNode("N")
	healthWait(t, 10*time.Second, func() bool { return r.status().Owners["M"].Seq > 0 && r.status().Owners["N"].Seq > 0 })
	previous := r.status().Owners["M"].Seq
	r.close()
	r = l.startHealthNode("R")
	start := time.Now()
	t.Cleanup(func() {
		if t.Failed() {
			for id, p := range map[string]*running{"R": r, "M": m, "N": n} {
				t.Logf("fixed failure %s health=%+v status=%+v events=%+v", id, p.health.Snapshot(), p.status(), p.events.snapshot())
			}
		}
	})
	healthWait(t, 20*time.Second, func() bool {
		return r.status().Owners["M"].Seq > previous && r.status().Owners["N"].Seq > 0 && m.health.Snapshot().Since.After(start) && n.health.Snapshot().Since.After(start)
	})
	send(m, packet(l.addr("M"), l.addr("N"), 1, 64, 0, 0, false, icmp(8, 4, nil)))
	expect(t, n, "after relay cold address recovery")
	send(n, packet(l.addr("N"), l.addr("M"), 1, 64, 0, 0, false, icmp(8, 5, nil)))
	expect(t, m, "return after relay recovery")
	if m.status().Entry.Switches != 0 {
		t.Fatal("fixed mode switched")
	}
	t.Log("fixed relay without cached addresses restored", time.Since(start))
}

func TestHealthIdleAndOneWayWithoutICMP(t *testing.T) {
	allow := []Rule{{Proto: "udp", Ports: [][2]uint16{{9000, 9000}}}}
	l := newHealthLab(t, spec{relays: []string{"A", "B"}, members: map[string][]string{"M": {"A", "B"}, "N": {"A", "B"}}, rules: map[string][]Rule{"M": allow, "N": allow}})
	nodes := map[string]*running{}
	for _, id := range []string{"A", "B", "M", "N"} {
		nodes[id] = l.startHealthNode(id)
	}
	healthWait(t, 20*time.Second, func() bool { return nodes["A"].status().Owners["M"].Seq > 0 && nodes["A"].status().Owners["N"].Seq > 0 })
	time.Sleep(12 * time.Second)
	for i := 0; i < 100; i++ {
		send(nodes["M"], packet(l.addr("M"), l.addr("N"), protoUDP, 64, 0, 0, false, ports(40000, 9000, 8)))
		expect(t, nodes["N"], "one-way UDP without business replies")
	}
	send(nodes["M"], packet(l.addr("M"), l.addr("N"), protoICMP, 64, 0, 0, false, icmp(8, 6, nil)))
	expectNone(t, nodes["N"], "ICMP denied by business ACL")
	for _, id := range []string{"M", "N"} {
		if st := nodes[id].status(); st.Entry == nil || st.Entry.Switches != 0 || st.Peers[0].TimeoutRate != 0 {
			t.Fatal("idle or one-way traffic triggered false failure", id, st)
		}
	}
}

func TestHealthRevocationAndFailedConfig(t *testing.T) {
	l := newHealthLab(t, spec{relays: []string{"A", "B"}, members: map[string][]string{"M": {"A", "B"}, "N": {"B", "A"}}, rules: map[string][]Rule{"M": allowICMP, "N": allowICMP}})
	nodes := map[string]*running{}
	for _, id := range []string{"A", "B", "M", "N"} {
		nodes[id] = l.startHealthNode(id)
	}
	healthWait(t, 10*time.Second, func() bool { return nodes["A"].status().Owners["M"].Seq > 0 && nodes["B"].status().Owners["M"].Seq > 0 })
	for _, id := range []string{"A", "B", "N"} {
		cfg := l.config(id)
		cfg.Revision = 2
		keep := cfg.Nodes[:0]
		for _, p := range cfg.Nodes {
			if p.ID != "M" {
				keep = append(keep, p)
			}
		}
		cfg.Nodes = keep
		if err := nodes[id].applyConfig(cfg); err != nil {
			t.Fatal(id, err)
		}
	}
	send(nodes["M"], packet(l.addr("M"), l.addr("N"), 1, 64, 0, 0, false, icmp(8, 3, nil)))
	expectNone(t, nodes["N"], "revoked member")
	for _, id := range []string{"A", "B"} {
		if _, ok := nodes[id].health.Owner("M"); ok {
			t.Fatal("revoked ownership revived", id)
		}
		key, _ := keyHex(l.nodes["M"].pub)
		_, state := deviceState(nodes[id].dev)
		if state[key] != nil {
			t.Fatal("revoked WG peer kept", id)
		}
		if err := nodes[id].applyConfig(l.config(id)); err == nil {
			t.Fatal("old revision revived revoked member", id)
		}
	}
}
