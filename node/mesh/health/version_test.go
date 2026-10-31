package health

import (
	"encoding/json"
	"testing"
	"time"

	"openbear/node/mesh/wire"
)

// protoBackend records what the manager tells the transport about peers'
// ranges.
type protoBackend struct {
	*offerBackend
	told map[string]wire.Range
}

func (b *protoBackend) SetPeerProtocol(peer string, r wire.Range) { b.told[peer] = r }

// versionManager is a member speaking r, linked to every peer the tests
// use, so pings are answered.
func versionManager(t *testing.T, r wire.Range) (*Manager, *protoBackend) {
	t.Helper()
	b := &protoBackend{offerBackend: &offerBackend{}, told: map[string]wire.Range{}}
	cfg := Config{Network: "n", Self: "m", Role: "member", Revision: 1, Parameters: Defaults(), Protocol: r,
		Links: []Link{{Peer: "old"}, {Peer: "new"}, {Peer: "newer"}}}
	return &Manager{cfg: cfg, backend: b, inbound: map[Path]*inbound{}, directs: map[string]*directState{}, links: map[string]*candidateState{},
		candidates: map[string]*candidateState{}}, b
}

// ping is a ping from peer in version v declaring low..high (0, 0: a node
// from before the range was declared), with extra fields merged in.
func ping(t *testing.T, peer string, v, low, high int, extra map[string]any) Received {
	t.Helper()
	m := map[string]any{"v": v, "mesh": "n", "rev": 1, "from": peer, "to": "m", "type": "ping",
		"nonce": nonce(), "token": nonce(), "gen": 1, "probe": 1}
	if high > 0 {
		m["vmin"], m["vmax"] = low, high
	}
	for k, x := range extra {
		m[k] = x
	}
	raw, _ := json.Marshal(m)
	return Received{Peer: peer, Path: Path{ID: "p-" + peer, Generation: 1}, At: time.Now(), Payload: raw}
}

// lastSent is the newest message sent, or false.
func lastSent(b *protoBackend) (message, bool) {
	if len(b.sent) == 0 {
		return message{}, false
	}
	var v message
	json.Unmarshal(b.sent[len(b.sent)-1].raw, &v)
	return v, true
}

// A node of the next release, speaking 1..2, answers a node of this release
// in 1 and one of its own in 2, and declares its range in every answer.
func TestControlMessagesAcrossReleases(t *testing.T) {
	m, b := versionManager(t, wire.Range{Min: 1, Max: 2})
	for _, c := range []struct {
		peer            string
		v, low, high    int
		want            int
		told            wire.Range
		answered        bool
		describeTheCase string
	}{
		{"old", 1, 0, 0, 1, wire.Range{Min: 1, Max: 1}, true, "a node from before the range was declared"},
		{"old", 1, 1, 1, 1, wire.Range{Min: 1, Max: 1}, true, "a node of this release"},
		{"new", 1, 1, 2, 2, wire.Range{Min: 1, Max: 2}, true, "a node of the next release, before it knew this one"},
		{"new", 2, 1, 2, 2, wire.Range{Min: 1, Max: 2}, true, "a node of the next release"},
		{"newer", 3, 2, 3, 2, wire.Range{Min: 2, Max: 3}, false, "a node two releases on, in a version this one lacks"},
	} {
		before := len(b.sent)
		m.receive(ping(t, c.peer, c.v, c.low, c.high, nil))
		v, _ := lastSent(b)
		if answered := len(b.sent) > before; answered != c.answered {
			t.Fatalf("%s: answered %v", c.describeTheCase, answered)
		}
		if c.answered && (v.Type != "pong" || v.Version != c.want || v.Min != 1 || v.Max != 2) {
			t.Fatalf("%s: answer %s in %d declaring %d..%d, want pong in %d declaring 1..2", c.describeTheCase, v.Type, v.Version, v.Min, v.Max, c.want)
		}
		if b.told[c.peer] != c.told {
			t.Fatalf("%s: transport told %v, want %v", c.describeTheCase, b.told[c.peer], c.told)
		}
		if got := m.sendVersion(c.peer, time.Now()); got != c.want {
			t.Fatalf("%s: next message in %d, want %d", c.describeTheCase, got, c.want)
		}
	}
	if s := m.protocolSnapshot(time.Now()); s["old"] != 1 || s["new"] != 2 {
		t.Fatalf("snapshot %v", s)
	}

	// A route advertisement declares no range; the probes' stays.
	adv := map[string]any{"v": 2, "mesh": "n", "rev": 1, "from": "new", "to": "m", "type": msgAdvert, "advert": json.RawMessage(`{}`)}
	raw, _ := json.Marshal(adv)
	m.receive(Received{Peer: "new", Path: Path{ID: "p-new", Generation: 1}, At: time.Now(), Payload: raw})
	if m.sendVersion("new", time.Now()) != 2 {
		t.Fatal("an advertisement reset the range")
	}

	// Nothing from new for protocolExpiry: it may have restarted as an
	// older release, so this node goes back to its oldest.
	m.expireProtocols(time.Now().Add(protocolExpiry))
	if got := m.sendVersion("new", time.Now()); got != 1 || b.told["new"] != (wire.Range{}) {
		t.Fatalf("after the expiry: version %d, transport told %v", got, b.told["new"])
	}
}

// A node of this release, speaking 1 only: what a newer node sends before it
// knows that, in version 2, is dropped, but its range is learned, so the
// answer to its next message, in 1, is in 1. Message types and fields this
// node does not know are ignored without an answer or an error.
func TestThisReleaseIgnoresWhatItDoesNotKnow(t *testing.T) {
	m, b := versionManager(t, wire.Range{Min: 1, Max: 1})
	m.receive(ping(t, "new", 2, 1, 2, nil))
	if len(b.sent) != 0 {
		t.Fatal("answered a message in a version it does not speak")
	}
	if m.sendVersion("new", time.Now()) != 1 || b.told["new"] != (wire.Range{Min: 1, Max: 2}) {
		t.Fatal("range not learned from the dropped message")
	}
	m.receive(ping(t, "new", 1, 1, 2, map[string]any{"addedLater": map[string]any{"x": 1}}))
	if v, ok := lastSent(b); !ok || v.Type != "pong" || v.Version != 1 {
		t.Fatalf("a ping with an unknown field: %+v", v)
	}
	before := len(b.sent)
	unknown := map[string]any{"v": 1, "vmin": 1, "vmax": 2, "mesh": "n", "rev": 1, "from": "new", "to": "m", "type": "added-later", "whatever": []int{1}}
	raw, _ := json.Marshal(unknown)
	m.receive(Received{Peer: "new", Path: Path{ID: "p-new", Generation: 1}, At: time.Now(), Payload: raw})
	if len(b.sent) != before {
		t.Fatal("answered an unknown message type")
	}
}
