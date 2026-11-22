package health

import (
	"encoding/json"
	"strings"
	"testing"

	"openbear/node/mesh/routing"
	"openbear/node/mesh/wire"
)

// TestAdvertBudget: with 36-character ids everywhere, a control message
// between any two relays holds a signed compact advertisement of at least
// the budget routing's TestCompactAdvertHolds32Relays takes (compactBudget
// there), which holds 32 relays with every route, and one of the budget's
// size stays within the message limit whichever relays forward it and
// however far the revision grows.
func TestAdvertBudget(t *testing.T) {
	id := func(c byte) string {
		b := []byte("00000000-0000-4000-8000-000000000000")
		b[0] = c
		return string(b)
	}
	m := &Manager{cfg: Config{Network: id('n'), Self: "a", Revision: 7, Links: []Link{{Peer: "b"}},
		Nodes: []Node{{ID: "b", Role: "relay"}, {ID: id('c'), Role: "relay"}, {ID: id('d'), Role: "relay"}, {ID: id('m') + "-member-ids-do-not-count", Role: "member"}}}}
	b := budgetOf(m.cfg)
	t.Logf("budget: JSON %d, compact %d", b.JSON, b.Compact)
	if b.Compact < 726 { // routing compactBudget
		t.Fatalf("compact budget %d", b.Compact)
	}
	v := message{Version: wire.Protocol, Network: m.cfg.Network, Revision: 1<<63 - 1, From: id('c'), To: id('d'), Type: msgAdvert,
		AdvertCompact: strings.Repeat("A", b.Compact), AdvertSig: make([]byte, 64)}
	raw, _ := json.Marshal(v)
	if len(raw) > maxMessage {
		t.Fatalf("%d bytes", len(raw))
	}
}

// TestAdvertBudgetFollowsConfig: the budget is the config's, worked out again
// when a new config names a longer relay id.
func TestAdvertBudgetFollowsConfig(t *testing.T) {
	c := Config{Network: "n", Self: "R", Role: "relay", Revision: 1, Parameters: Defaults(), Nodes: []Node{{ID: "A", Role: "relay"}}}
	m, err := New(t.TempDir(), withKeys(c), &offerBackend{}, Hooks{})
	if err != nil {
		t.Fatal(err)
	}
	defer m.Close()
	budget := func() (routing.Budget, routing.Budget) {
		m.mu.Lock()
		defer m.mu.Unlock()
		return m.advertBudget(), budgetOf(m.cfg)
	}
	first, want := budget()
	if first != want {
		t.Fatalf("budget %+v, config's %+v", first, want)
	}
	c.Revision = 2
	c.Nodes = append(c.Nodes, Node{ID: strings.Repeat("b", 36), Role: "relay"})
	if err := m.Configure(withKeys(c)); err != nil {
		t.Fatal(err)
	}
	if got, want := budget(); got != want || got == first {
		t.Fatalf("budget %+v after the config changed, config's %+v, before %+v", got, want, first)
	}
}
