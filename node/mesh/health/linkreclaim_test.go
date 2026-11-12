package health

import (
	"testing"
	"time"
)

// A member that becomes a relay linked to its relay, its new config first:
// it prepares the link while the relay, on the old config, still has it for
// a member, so the relay answers ready and commits nothing. Once the relay has the
// new config too, it asks for the prepare again until the path is committed,
// and its routing takes the link up only then; the probes it answers prove
// nothing, going back over the path they came on. So too when the committed
// path goes away later.
func TestLinkPreparedBeforeItWasALinkIsReclaimed(t *testing.T) {
	member := []Node{{ID: "S", Role: "member", Relays: []string{"R"}}}
	m, b := offerManager(t, Config{Network: "n", Self: "R", Role: "relay", Revision: 1, Nodes: member},
		map[string][]string{"S": {"R"}}, map[string]bool{"R": true})
	path := Path{ID: "p1", Generation: 1}
	token := nonce()
	id := uint64(0)
	probe := func() message {
		t.Helper()
		id++
		at := time.Now()
		ping, pong := echoProbe(t, m, b, path, token, id, at)
		echoSeen(t, m, ping, pong.Echo, path, at)
		return pong
	}
	prepare := func() {
		t.Helper()
		r := signal(t, "S", "R", "prepare", message{Token: token, Generation: path.Generation})
		r.Path = path
		m.receive(r)
	}
	link := Link{Peer: "S"}

	probe()
	prepare()
	if !m.inbound[path].prepared || b.commits != 0 {
		t.Fatalf("on the old config: prepared %v, commits %d", m.inbound[path].prepared, b.commits)
	}
	sentOf(t, b, "ready")

	cfg := m.cfg
	cfg.Revision = 2
	cfg.Nodes = withKeys(Config{Nodes: []Node{{ID: "S", Role: "relay"}}}).Nodes
	cfg.Links = []Link{link}
	if err := m.configure(cfg); err != nil {
		t.Fatal(err)
	}
	if pong := probe(); !pong.Reclaim || m.routeLive(link) {
		t.Fatalf("prepared, not committed: reclaim %v, live %v", pong.Reclaim, m.routeLive(link))
	}
	prepare()
	if !b.Committed("S", path) {
		t.Fatal("the prepare asked for again did not commit the path")
	}
	if pong := probe(); pong.Reclaim || !m.routeLive(link) {
		t.Fatalf("committed: reclaim %v, live %v", pong.Reclaim, m.routeLive(link))
	}

	b.Deactivate("S", path)
	if pong := probe(); !pong.Reclaim || m.routeLive(link) {
		t.Fatalf("no longer committed: reclaim %v, live %v", pong.Reclaim, m.routeLive(link))
	}
}
