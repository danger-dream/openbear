package transport

import (
	"testing"
	"time"
)

// A peer heard from a fourth address replaces the one silent longest, if
// that has been silent a while and is not in use.
func TestEvictStaleKeepsPathsInUse(t *testing.T) {
	b := newBind(t, Options{})
	now := time.Now()
	add := func(id string, ago time.Duration, outbound bool) *endpoint {
		ep := &endpoint{b: b, id: id, key: "udp://192.0.2." + id[1:] + ":1", transport: "udp", peer: "m", outbound: outbound}
		ep.lastAuthenticated[0] = now.Add(-ago)
		b.ids[id] = ep
		b.paths[ep.key] = ep
		return ep
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	b.peers["m"] = &peerPaths{}
	active := add("p1", time.Hour, false)
	b.peers["m"].active = PathRef{ID: "p1", Generation: 1}
	dialed := add("p2", time.Hour, true)
	recent := add("p3", 5*time.Second, false)
	if b.evictStale("m", nil) {
		t.Fatal("evicted the active path, a dialed one or one heard from 5 s ago")
	}
	old := add("p4", time.Minute, false)
	older := add("p5", 2*time.Minute, false)
	if !b.evictStale("m", nil) || !older.retired || b.ids["p5"] != nil || b.paths[older.key] != nil {
		t.Fatal("the path silent longest stayed")
	}
	if active.retired || dialed.retired || recent.retired || old.retired {
		t.Fatal("evicted more than one path, or the wrong one")
	}
}
