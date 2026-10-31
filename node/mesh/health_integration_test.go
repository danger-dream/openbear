package mesh

import (
	"openbear/node/mesh/health"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func newHealthLab(t *testing.T, s spec) *lab {
	l := newLab(t, s)
	pubs := map[string]string{}
	for name := range l.nodes {
		priv, pub := newKey(t)
		pubs[name] = pub
		l.dirs[name] = t.TempDir()
		if err := os.MkdirAll(filepath.Join(l.dirs[name], "keys"), 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(l.dirs[name], "keys", "control.key"), []byte(priv), 0600); err != nil {
			t.Fatal(err)
		}
	}
	l.edit = func(name string, c *Config) {
		for i := range c.Nodes {
			c.Nodes[i].ControlKey = pubs[c.Nodes[i].ID]
		}
		if c.Entry != nil {
			c.Entry.Mode = "failover"
		}
	}
	return l
}
func healthWait(t *testing.T, within time.Duration, f func() bool) {
	t.Helper()
	end := time.Now().Add(within)
	for time.Now().Before(end) {
		if f() {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("health integration condition timed out")
}
func (l *lab) startHealthNode(name string) *running {
	return l.start(name, NewAttachment(l.config(name)))
}
func TestHealthRealDataAndRelayRecovery(t *testing.T) {
	l := newHealthLab(t, spec{relays: []string{"A", "B", "Z"}, members: map[string][]string{"M": {"A", "B"}, "N": {"B", "A"}}, rules: map[string][]Rule{"A": allowICMP, "B": allowICMP, "Z": allowICMP, "M": allowICMP, "N": allowICMP}})
	nodes := map[string]*running{}
	for _, name := range []string{"A", "B", "Z", "M", "N"} {
		nodes[name] = l.startHealthNode(name)
	}
	t.Cleanup(func() {
		if t.Failed() {
			for id, n := range nodes {
				t.Logf("failure %s health=%+v status=%+v events=%+v", id, n.health.Snapshot(), n.status(), n.events.snapshot())
			}
		}
	})
	healthWait(t, 15*time.Second, func() bool {
		for _, id := range []string{"A", "B", "Z"} {
			n := nodes[id]
			for _, link := range l.config(id).Links {
				ready := false
				for _, p := range n.bind.(*streamBind).Paths() {
					if p.PeerID == link.Peer && p.Active && n.control.DataReady(p.PeerID, health.Path(p.Ref)) {
						ready = true
					}
				}
				if !ready {
					return false
				}
			}
		}
		// Every relay a packet below crosses must know both owners, not Z alone.
		for _, id := range []string{"A", "B", "Z"} {
			owners := nodes[id].status().Owners
			if owners["M"].Relay != "A" || owners["N"].Relay != "B" {
				return false
			}
		}
		return nodes["M"].status().Entry != nil && nodes["N"].status().Entry != nil
	})
	for _, pair := range [][2]string{{"M", "N"}, {"N", "M"}, {"B", "M"}, {"M", "B"}, {"Z", "M"}} {
		sent := nodes[pair[0]].e2e.status().Sent
		send(nodes[pair[0]], packet(l.addr(pair[0]), l.addr(pair[1]), 1, 64, 0, 0, false, icmp(8, 50, nil)))
		got := recv(nodes[pair[1]], 5*time.Second)
		if got == nil {
			for name, n := range nodes {
				_, state := deviceState(n.dev)
				t.Log(name, n.status(), state, n.bind.(*streamBind).Paths())
			}
			t.Fatalf("%s -> %s not delivered", pair[0], pair[1])
		}
		// None of these is the other's next hop: each went end to end.
		if nodes[pair[0]].e2e.status().Sent != sent+1 {
			t.Fatal("not sent end to end", pair)
		}
	}
	t.Log("initial actual data/backup business source validation passed")
	healthWait(t, 40*time.Second, func() bool {
		ready := 0
		for _, p := range nodes["M"].health.Snapshot().Candidates {
			if p.Ready && p.RouteReady && p.Probes.Failures == 0 {
				ready++
			}
		}
		return ready == 2
	})
	stopped := time.Now()
	nodes["A"].bind.Close()
	healthWait(t, 8*time.Second, func() bool {
		return nodes["M"].status().Entry != nil && nodes["M"].status().Entry.Relay == "B" && nodes["Z"].status().Owners["M"].Relay == "B"
	})
	for _, pair := range [][2]string{{"M", "N"}, {"N", "M"}, {"Z", "M"}} {
		send(nodes[pair[0]], packet(l.addr(pair[0]), l.addr(pair[1]), 1, 64, 0, 0, false, icmp(8, 51, nil)))
		expect(t, nodes[pair[1]], pair[0]+" -> "+pair[1]+" after entry stop")
	}
	t.Log("bidirectional data restoration", time.Since(stopped))
}
