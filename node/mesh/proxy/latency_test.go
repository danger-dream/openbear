package proxy

import (
	"net/netip"
	"slices"
	"testing"
	"time"
)

// A latency user goes to the exit with the lowest round trip, moves to a
// nearer one only once it has stayed clearly nearer, and leaves one that
// fails at once.
func TestALatencyUserTakesTheNearestExitOnlyOnceItStaysNearer(t *testing.T) {
	a, b := netip.MustParseAddr("100.96.0.2"), netip.MustParseAddr("100.96.0.3")
	p := &pool{exits: map[netip.Addr]*exitPool{}, stop: make(chan struct{})}
	set := func(x netip.Addr, ms int) {
		p.mu.Lock()
		p.exit(x).rtt = time.Duration(ms) * time.Millisecond
		p.mu.Unlock()
	}
	u := User{Name: "us", Exits: []netip.Addr{a, b}, Select: selectLatency}
	s := &entryServer{cfg: &Entry{Users: []User{u, {Name: "jp", Exits: []netip.Addr{b, a}}}}, pool: p, picks: map[string]*latencyPick{}}
	picked := func() netip.Addr {
		t.Helper()
		x, ok := s.picked("us")
		if !ok {
			t.Fatal("nothing picked")
		}
		return x
	}

	// Not measured yet: nothing to pick, the exits in their order.
	s.pick()
	if _, ok := s.picked("us"); ok || !slices.Equal(s.byLatency(&u), []netip.Addr{a, b}) {
		t.Fatal("picked before any round trip was known")
	}
	set(a, 80)
	set(b, 100)
	s.pick()
	if picked() != a {
		t.Fatalf("picked %v, not the nearer", picked())
	}
	// 5 ms nearer is about as near: kept.
	set(b, 75)
	for range 5 {
		s.pick()
	}
	if picked() != a {
		t.Fatal("moved for 5 ms")
	}
	// 30 ms nearer: taken on the third look in a row.
	set(b, 50)
	s.pick()
	s.pick()
	if picked() != a {
		t.Fatal("moved before it stayed nearer")
	}
	s.pick()
	if picked() != b || !slices.Equal(s.byLatency(&u), []netip.Addr{b, a}) {
		t.Fatalf("picked %v, order %v", picked(), s.byLatency(&u))
	}
	// A look where it is not nearer starts the count again.
	set(a, 20)
	s.pick()
	s.pick()
	set(a, 80)
	s.pick()
	set(a, 20)
	s.pick()
	s.pick()
	if picked() != b {
		t.Fatal("moved without three looks in a row")
	}
	// The picked exit fails: left at once.
	p.mu.Lock()
	p.exit(b).downAt = time.Now()
	p.mu.Unlock()
	s.pick()
	if picked() != a {
		t.Fatal("kept an exit that failed")
	}
	// A user in order is not picked for.
	if _, ok := s.picked("jp"); ok {
		t.Fatal("picked for a user in order")
	}
}
