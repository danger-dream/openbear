package routing

import (
	"encoding/base64"
	"fmt"
	"maps"
	"slices"
	"strings"
	"testing"
	"time"
)

// bigMesh is a relay among n with 36-character ids and control host
// numbers that take three bytes, linked to every other relay, each up at
// a different cost, with a configured next hop to every relay that is not
// the relay itself.
func bigMesh(t *testing.T, n int) (*Router, []string, *time.Time) {
	ids := make([]string, n)
	numbers := map[string]uint32{}
	for i := range ids {
		ids[i] = fmt.Sprintf("%08x-0000-4000-8000-%012x", i, i*7919)
		numbers[ids[i]] = uint32(20000 + i*997)
	}
	hops := map[string]string{}
	for i, d := range ids[1:] {
		hops[d] = ids[1+(i+1)%(n-1)]
	}
	now := time.Unix(1_800_000_000, 0)
	r := New(signed(Config{Self: ids[0], Relays: ids[1:], Links: ids[1:], NextHops: hops, Numbers: numbers}), func() time.Time { return now }, nil)
	for i, p := range ids[1:] {
		for range warmSamples {
			r.Probe(p, time.Duration(1000+i*317)*time.Millisecond/10)
		}
	}
	return r, ids, &now
}

// compactBudget is the least a control message holds for a signed compact
// advertisement with 36-character ids, forwarded between any two relays
// (health's TestAdvertBudget).
const compactBudget = 726

// TestCompactAdvertHolds32Relays: 32 relays, every advertisement with all
// 31 links and all 31 routes, fits one control message in compact form;
// in JSON not even the links do.
func TestCompactAdvertHolds32Relays(t *testing.T) {
	r, ids, _ := bigMesh(t, 32)
	msgs := r.Advert(Budget{JSON: compactBudget, Compact: compactBudget}, ids[1:2])
	if len(msgs) != 1 || msgs[0].Format != FormatCompact {
		t.Fatalf("%d messages: %+v", len(msgs), msgs)
	}
	a, err := decodeCompact(msgs[0].Raw, r.ids, r.known)
	if err != nil {
		t.Fatal(err)
	}
	if len(a.links) != 31 || len(a.routes) != 31 || a.origin != ids[0] {
		t.Fatalf("%d links, %d routes", len(a.links), len(a.routes))
	}
	for d, nh := range a.routes {
		if want, _ := r.NextHop(d); nh != want {
			t.Fatalf("route to %s through %s, want %s", d, nh, want)
		}
	}
	for p, l := range a.links {
		if want := r.links[p]; l.state != StateUp || l.cost != want.advCost {
			t.Fatalf("link %s: %+v, want %v", p, l, want.advCost)
		}
	}
	plain := encodeJSON(a, r.order, false)
	t.Logf("32 relays: compact %d bytes with routes; JSON %d bytes without", len(msgs[0].Raw), len(plain))
	if len(plain) <= compactBudget {
		t.Fatal("JSON fits after all")
	}
	// How far it goes: the largest mesh whose full advertisement fits.
	most := 0
	for n := 32; n <= 64; n++ {
		r, ids, _ := bigMesh(t, n)
		m := r.Advert(Budget{Compact: compactBudget}, ids[1:2])
		if len(m) == 0 {
			break
		}
		if a, _ := decodeCompact(m[0].Raw, r.ids, r.known); a == nil || a.routes == nil {
			break
		}
		most = n
	}
	t.Logf("full advertisements fit up to %d relays", most)
	if most < 32 {
		t.Fatal(most)
	}
}

// TestCompactDecodeChecks: the compact form is held to the JSON form's
// rules, and malformed input is refused.
func TestCompactDecodeChecks(t *testing.T) {
	known := map[string]bool{"A": true, "B": true, "C": true}
	ids := map[uint32]string{1: "A", 2: "B", 300: "C"}
	num := map[string]uint32{"A": 1, "B": 2, "C": 300}
	order := []string{"A", "B", "C"}
	good := &advert{origin: "C", seq: 1_800_000_000_123_456, links: map[string]advLink{"A": {12.3, StateUp}, "B": {0, StateSuppressed}},
		routes: map[string]string{"A": "A", "B": "A"}}
	raw, ok := encodeCompact(good, num, order, true)
	if !ok {
		t.Fatal("not encoded")
	}
	a, err := decodeCompact(raw, ids, known)
	if err != nil || a.origin != "C" || a.seq != good.seq || !maps.Equal(a.links, good.links) || !maps.Equal(a.routes, good.routes) {
		t.Fatalf("%+v %v", a, err)
	}
	noRoutes, _ := encodeCompact(good, num, order, false)
	if a, err := decodeCompact(noRoutes, ids, known); err != nil || a.routes != nil {
		t.Fatalf("without routes: %+v %v", a, err)
	}
	bin, _ := base64.RawStdEncoding.DecodeString(string(raw))
	enc := func(b []byte) []byte { return []byte(base64.RawStdEncoding.EncodeToString(b)) }
	bad := map[string][]byte{
		"not base64":        []byte("!!"),
		"another version":   enc(append([]byte{3}, bin[1:]...)),
		"truncated":         enc(bin[:len(bin)-1]),
		"trailing bytes":    enc(append(slices.Clone(bin), 0)),
		"unknown origin":    enc([]byte{2, 9, 1, 0, 0}),
		"no seq":            enc([]byte{2, 1, 0, 0, 0}),
		"link to itself":    enc([]byte{2, 1, 1, 1, 1, 1, 5, 0}),
		"unknown link peer": enc([]byte{2, 1, 1, 1, 9, 1, 5, 0}),
		"unknown state":     enc([]byte{2, 1, 1, 1, 2, 7, 0}),
		"repeated link":     enc([]byte{2, 1, 1, 2, 2, 0, 2, 0, 0}),
		"too many links":    enc([]byte{2, 1, 1, 4, 2, 0, 2, 0, 2, 0, 2, 0, 0}),
		"cost too large":    enc(append([]byte{2, 1, 1, 1, 2, 1}, 0xff, 0xff, 0xff, 0xff, 0x0f, 0)),
		"route via origin":  enc([]byte{2, 1, 1, 0, 1, 1, 2, 1}),
		"route unknown hop": enc([]byte{2, 1, 1, 0, 1, 1, 2, 9}),
		"bad route flag":    enc([]byte{2, 1, 1, 0, 2}),
	}
	for name, raw := range bad {
		if _, err := decodeCompact(raw, ids, known); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	// A router without every relay's number neither sends nor takes them.
	r := New(signed(Config{Self: "A", Relays: []string{"B"}, Links: []string{"B"}, Numbers: map[string]uint32{"A": 1}}), nil, nil)
	for range warmSamples {
		r.Probe("B", 10*time.Millisecond)
	}
	if m := r.Advert(Budget{JSON: 1000, Compact: 1000}, []string{"B"}); len(m) != 1 || m[0].Format != FormatJSON {
		t.Fatalf("%+v", m)
	}
	if _, _, err := r.Receive("B", signedBy("B", FormatCompact, raw), Budget{1000, 1000}); err != errNoNumbers {
		t.Fatal(err)
	}
}

// TestRollingRestarts: four relays are restarted one by one, following
// every relay's next hops every 10 ms. Every pair keeps a working path
// except for a few seconds around a restart. In this sequence no loop forms
// toward an online relay; TestLiveDestinationConvergence covers simultaneous
// link failures to an online destination. Toward the relay going down, its two neighbors each
// still believe the other's link to it up for one advertisement's delay: a
// loop of tens of milliseconds whose traffic is lost either way.
func TestRollingRestarts(t *testing.T) {
	t.Logf("10 ms steps with a loop toward the relay going down: %d", rollingRestarts(t))
}

// rollingRestarts restarts A, B, C and D in turn and returns the steps with
// a loop toward the relay restarting.
func rollingRestarts(t *testing.T) int {
	links := map[string]time.Duration{"A-B": 20 * time.Millisecond, "B-C": 5 * time.Millisecond, "C-D": 30 * time.Millisecond, "A-D": 10 * time.Millisecond, "B-D": 40 * time.Millisecond}
	rtt := map[string]float64{"A-B": 40, "B-C": 10, "C-D": 60, "A-D": 20, "B-D": 80}
	n := newNet(t, links, nil)
	names := []string{"A", "B", "C", "D"}
	restarting := map[string]bool{}
	quietUntil := n.now.Add(10 * time.Second)
	loops, dying, broken := 0, 0, 0
	check := func() {
		for _, a := range names {
			for _, b := range names {
				if a == b {
					continue
				}
				p, ok := n.path(a, b)
				if !ok && len(p) > 1 && slices.Contains(p[:len(p)-1], p[len(p)-1]) {
					if restarting[b] {
						dying++
						continue
					}
					loops++
					t.Errorf("loop %v at %v", p, n.now)
				}
				if !ok && n.now.After(quietUntil) && !restarting[a] && !restarting[b] {
					broken++
					if broken < 5 {
						t.Errorf("%s to %s broken at %v: %v", a, b, n.now, p)
					}
				}
			}
		}
	}
	second := func() {
		for l, v := range rtt {
			a, b, _ := strings.Cut(l, "-")
			if restarting[a] || restarting[b] {
				n.probe(a, b, -1)
				n.cut(a, b, true)
			} else {
				n.cut(a, b, false)
				n.probe(a, b, v)
			}
		}
		n.run(time.Second, check)
	}
	for range 40 {
		second()
	}
	for _, x := range names {
		// Down for 4 s, then a new process; paths through it may break
		// until about 6 s after.
		restarting[x] = true
		quietUntil = n.now.Add(10 * time.Second)
		for range 4 {
			second()
		}
		restarting[x] = false
		n.r[x] = New(n.config(x, nil), func() time.Time { return n.now }, func(kind, detail string) {
			n.events[x] = append(n.events[x], kind+" "+detail)
		})
		for range 30 {
			second()
		}
	}
	if loops != 0 || broken != 0 {
		t.Fatalf("%d steps with a loop, %d broken", loops, broken)
	}
	return dying
}

// A compact advertisement of a later version is ignored: no error event,
// no change. A malformed one of this version is still an error.
func TestCompactAdvertOfALaterVersionIsIgnored(t *testing.T) {
	r, ids, _ := bigMesh(t, 4)
	later := base64.RawStdEncoding.EncodeToString([]byte{compactVersion + 1, 1, 2, 3})
	forward, changed, err := r.Receive(ids[1], signedBy(ids[1], FormatCompact, []byte(later)), Budget{JSON: compactBudget, Compact: compactBudget})
	if err != nil || changed || len(forward) != 0 {
		t.Fatalf("later version: %v %v %v", forward, changed, err)
	}
	bad := base64.RawStdEncoding.EncodeToString([]byte{compactVersion, 0xff})
	if _, _, err := r.Receive(ids[1], signedBy(ids[1], FormatCompact, []byte(bad)), Budget{JSON: compactBudget, Compact: compactBudget}); err == nil {
		t.Fatal("a malformed advertisement passed")
	}
}
