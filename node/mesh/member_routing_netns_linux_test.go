package mesh

import (
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"
)

// TestRoutingNetnsMemberUpstreams covers design case 1 (data plane):
// Relays A and B, members KA (behind A), KB (behind B), and M with upstreams A and B.
// A-B is 80ms, A-M is 100ms, B-M is 10ms, KA-A is 2ms, KB-B is 2ms.
// Traffic between KB (B side) and M takes B-M (~12ms), while traffic between KA (A side)
// and M goes via B (A-B-M = 90ms < A-M = 100ms) or when A-M is 40ms, KA->M stays via A
// while KB->M goes via B. And when B-M is cut, traffic automatically falls back to A.
func TestRoutingNetnsMemberUpstreams(t *testing.T) {
	rtSkip(t)
	// A-B: 80ms, A-M: 40ms, B-M: 10ms, KA-A: 4ms, KB-B: 4ms.
	// From B side (B and KB): B->M is 10ms (via B), while B->A->M is 120ms -> chooses B.
	// From A side (A and KA): A->M is 40ms (direct A-M), while A->B->M is 90ms -> stays via A!
	l := newRTLab(t, []string{"A", "B", "M", "KA", "KB"},
		[]string{"A-B:80", "M-A:40", "M-B:10", "KA-A:4", "KB-B:4"},
		map[string]string{"A>B": "B", "B>A": "A"})
	l.configure = func(id string, c *Config) {
		isMember := func(x string) bool { return x == "M" || x == "KA" || x == "KB" }
		relaysOf := func(x string) []string {
			switch x {
			case "M":
				return []string{"A", "B"}
			case "KA":
				return []string{"A"}
			case "KB":
				return []string{"B"}
			}
			return nil
		}
		if isMember(id) {
			c.Self.Role = roleMember
			c.Self.Leaf = true
		}
		for i := range c.Nodes {
			if isMember(c.Nodes[i].ID) {
				c.Nodes[i].Role = roleMember
				c.Nodes[i].Leaf = true
				c.Nodes[i].Relays = relaysOf(c.Nodes[i].ID)
			}
		}
		for i := range c.Links {
			if isMember(c.Links[i].Peer) {
				c.Links[i].Kind = kindMember
			} else {
				c.Links[i].Kind = kindRelay
			}
		}
		c.RelayLinks = [][2]string{{"A", "B"}}
		c.MemberLinks = [][2]string{{"A", "KA"}, {"A", "M"}, {"B", "KB"}, {"B", "M"}}
	}
	l.start()

	// Wait for shortest paths to converge:
	// - B side (B, KB) reaches M directly via B; M reaches B and KB via B.
	// - A side (A, KA) reaches M directly via A; M reaches A and KA via A.
	// - M never appears as an intermediate hop between A and B (even though M-A + M-B = 50ms < A-B = 80ms!).
	l.paths(60*time.Second,
		"B>M", "M>B", "KB>B>M", "M>B>KB",
		"A>M", "M>A", "KA>A>M", "M>A>KA",
		"A>B", "B>A", "KA>A>B>KB", "KB>B>A>KA",
	)

	pBM := l.ping("KB", "M", 100*time.Millisecond, 64)
	pAM := l.ping("KA", "M", 100*time.Millisecond, 64)
	time.Sleep(5 * time.Second)

	// Cut B-M: traffic between KB and M must automatically switch to A (KB>B>A>M).
	cut := time.Now()
	l.netem(l.link("M", "B"), "loss 100%")
	l.paths(15*time.Second, "B>A>M", "M>A>B", "KB>B>A>M", "M>A>B>KB", "KA>A>M", "M>A>KA")
	detourAt := time.Since(cut)
	t.Logf("after cutting B-M, routes converged to A in %.1fs", detourAt.Seconds())
	time.Sleep(5 * time.Second)

	// Restore B-M: traffic returns to B-M after hold-down/damping.
	l.netem(l.link("M", "B"), "")
	l.paths(60*time.Second, "B>M", "M>B", "KB>B>M", "M>B>KB")
	time.Sleep(3 * time.Second)

	samplesBM := pBM.samples(func(s pingSample) string {
		switch {
		case s.rtt < 40:
			return "via-B"
		case s.rtt >= 90:
			return "via-A"
		}
		return "other"
	})
	samplesAM := pAM.samples(func(s pingSample) string {
		if s.rtt < 70 {
			return "via-A"
		}
		return "other"
	})

	viaB, viaA, lostBM := 0, 0, 0
	for _, s := range samplesBM {
		switch s.class {
		case "via-B":
			viaB++
		case "via-A":
			viaA++
		case "lost":
			lostBM++
		}
	}
	viaAFromKA, lostAM := 0, 0
	for _, s := range samplesAM {
		switch s.class {
		case "via-A":
			viaAFromKA++
		case "lost":
			lostAM++
		}
	}
	t.Logf("KB->M samples: via-B=%d via-A=%d lost=%d; KA->M samples: via-A=%d lost=%d", viaB, viaA, lostBM, viaAFromKA, lostAM)
	if viaB == 0 || viaA == 0 {
		t.Fatalf("expected KB->M traffic on both via-B (%d) and via-A (%d)", viaB, viaA)
	}
	if viaAFromKA == 0 || lostAM > 5 {
		t.Fatalf("expected KA->M traffic to stay on A without interruption: via-A=%d lost=%d", viaAFromKA, lostAM)
	}
	if s := l.status("M"); s.Counters.Forwarded != 0 {
		t.Fatalf("leaf member M forwarded %d packets, want 0", s.Counters.Forwarded)
	}
}

// TestRoutingNetnsRollingUpgrade covers design case 4:
// Old member on new relay, new member on old relay (legacy failover+autoReturn config),
// and upgrading the remaining node so M transitions live from legacy entry mode to
// multi-upstream leaf routing while continuous ping keeps running.
func TestRoutingNetnsRollingUpgrade(t *testing.T) {
	rtSkip(t)
	l := newRTLab(t, []string{"A", "B", "M", "OLD"},
		[]string{"A-B:60", "M-A:40", "M-B:10", "OLD-A:10"},
		map[string]string{"A>B": "B", "B>A": "A"})

	// Stage 1:
	// - Relays A and B are upgraded, OLD is an old member on A (fixed entry),
	// - M is a new member whose upstream B is not yet reporting the feature in controller config,
	//   so controller issues legacy failover+autoReturn config to M (and A, B do not list M in Links).
	stage := 1
	l.configure = func(id string, c *Config) {
		for i := range c.Nodes {
			if c.Nodes[i].ID == "OLD" {
				c.Nodes[i].Role = roleMember
				c.Nodes[i].Relays = []string{"A"}
			}
			if c.Nodes[i].ID == "M" {
				c.Nodes[i].Role = roleMember
				c.Nodes[i].Relays = []string{"A", "B"}
				if stage == 2 {
					c.Nodes[i].Leaf = true
				}
			}
		}
		c.RelayLinks = [][2]string{{"A", "B"}}
		if stage == 2 {
			c.MemberLinks = [][2]string{{"A", "M"}, {"B", "M"}}
		}
		switch id {
		case "A", "B":
			// Remove links to legacy members; keep links to relays (and to M in stage 2).
			var kept []Link
			for _, k := range c.Links {
				if k.Peer == "OLD" || (k.Peer == "M" && stage == 1) {
					continue
				}
				if k.Peer == "M" {
					k.Kind = kindMember
				}
				kept = append(kept, k)
			}
			c.Links = kept
		case "OLD":
			c.Self.Role = roleMember
			var epA string
			for _, k := range c.Links {
				if k.Peer == "A" {
					epA = k.Endpoint
				}
			}
			c.Links = nil
			c.Routing = nil
			c.RelayLinks = nil
			c.Entry = &Entry{
				Mode:       "fixed",
				Candidates: []Candidate{{ID: "c1", Relay: "A", Endpoint: epA}},
			}
		case "M":
			c.Self.Role = roleMember
			if stage == 1 {
				var epA, epB string
				for _, k := range c.Links {
					if k.Peer == "A" {
						epA = k.Endpoint
					} else if k.Peer == "B" {
						epB = k.Endpoint
					}
				}
				c.Links = nil
				c.Routing = nil
				c.RelayLinks = nil
				c.Entry = &Entry{
					Mode:       "failover",
					AutoReturn: true,
					Candidates: []Candidate{
						{ID: "c1", Relay: "A", Endpoint: epA},
						{ID: "c2", Relay: "B", Endpoint: epB},
					},
				}
			} else {
				c.Self.Leaf = true
			}
		}
	}
	l.start()

	// Wait for stage 1 connectivity: OLD (on A) <-> M (legacy failover on A) works.
	l.paths(30*time.Second, "A>B", "B>A")
	l.waitFor("stage 1 OLD and M committed", 30*time.Second, func() bool {
		hasCommitted := func(id string) bool {
			for _, e := range l.status(id).Events {
				if e.Kind == "entry.committed" {
					return true
				}
			}
			return false
		}
		return hasCommitted("OLD") && hasCommitted("M")
	})
	p := l.ping("OLD", "M", 100*time.Millisecond, 64)
	l.waitFor("stage 1 ping replies", 30*time.Second, func() bool {
		p.mu.Lock()
		defer p.mu.Unlock()
		return len(p.replies) >= 15
	})

	// Stage 2: Rolling upgrade completes — controller pushes revision 2 with M as routed leaf member
	// and A, B having M in their links. Hot-reload via ApplyConfig (writing config.json).
	stage = 2
	for _, id := range l.ids {
		cfg := l.config(id)
		cfg.Revision = 2
		raw, _ := json.Marshal(cfg)
		if err := Validate(raw); err != nil {
			t.Fatalf("stage 2 validate %s: %v", id, err)
		}
		tmp := filepath.Join(l.dirs[id], "config.json.tmp")
		os.WriteFile(tmp, raw, 0o600)
		os.Rename(tmp, filepath.Join(l.dirs[id], "config.json"))
	}

	// Wait for M to join dynamic routing and pick shortest paths.
	l.paths(60*time.Second, "A>M", "B>M", "M>A", "M>B")
	time.Sleep(5 * time.Second)

	all := p.samples(func(s pingSample) string { return "ok" })
	lost := 0
	for _, s := range all {
		if !s.ok {
			lost++
		}
	}
	t.Logf("rolling upgrade OLD->M: %d total pings, %d lost", len(all), lost)
	// Continuous ping should not break; during live transition from legacy entry
	// to routed links, M's link to A re-prepares over 2 probe intervals (~2s = 20 pings at 100ms).
	if len(all) < 40 || lost > 25 {
		t.Fatalf("rolling upgrade lost too many packets: %d/%d lost (%s)", lost, len(all), strings.Join(func() []string {
			var out []string
			for _, s := range all {
				if !s.ok {
					out = append(out, "loss")
				}
			}
			return out
		}(), ","))
	}
	if !slices.Contains([]string{"A", "B"}, l.status("M").Routing.Routes[0].NextHop) {
		t.Fatalf("M has no valid routing next hop: %+v", l.status("M").Routing)
	}
}
