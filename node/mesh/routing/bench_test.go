package routing

import (
	"fmt"
	"maps"
	"math/rand"
	"slices"
	"testing"
	"time"
)

// bigRouter is a relay of a mesh the size of a real one, settled: 25 relays
// with uuid ids, about nine links each, 18 members on one to four relays
// each, every link up and every relay's advertisement held.
func bigRouter(tb testing.TB) (*Router, func(time.Duration)) {
	rng := rand.New(rand.NewSource(3))
	uuid := func() string {
		return fmt.Sprintf("%08x-%04x-%04x-%04x-%012x", rng.Uint32(), rng.Intn(1<<16), rng.Intn(1<<16), rng.Intn(1<<16), rng.Int63n(1<<48))
	}
	var relays, leaves []string
	for range 25 {
		relays = append(relays, uuid())
	}
	leaf := map[string]bool{}
	for range 18 {
		id := uuid()
		leaves = append(leaves, id)
		leaf[id] = true
	}
	cost := map[string]map[string]float64{}
	link := func(a, b string) {
		if a == b {
			return
		}
		c := 5 + float64(rng.Intn(2000))/10
		for _, x := range [][2]string{{a, b}, {b, a}} {
			if cost[x[0]] == nil {
				cost[x[0]] = map[string]float64{}
			}
			cost[x[0]][x[1]] = c
		}
	}
	for _, a := range relays {
		for _, j := range rng.Perm(len(relays))[:5] {
			link(a, relays[j])
		}
	}
	for _, l := range leaves {
		for _, j := range rng.Perm(len(relays))[:1+rng.Intn(4)] {
			link(l, relays[j])
		}
	}
	self := relays[0]
	own := slices.Sorted(maps.Keys(cost[self]))
	now := time.Unix(1_800_000_000, 0)
	r := New(signed(Config{Self: self, Relays: relays[1:], Leaves: leaves, Links: own}), func() time.Time { return now }, nil)
	from := ""
	for _, p := range own {
		if !leaf[p] {
			from = p
			break
		}
	}
	seq := uint64(0)
	advertise := func() {
		seq++
		for _, o := range relays[1:] {
			a := &advert{origin: o, seq: seq, links: map[string]advLink{}, members: map[string]advLink{}}
			for p, c := range cost[o] {
				if leaf[p] {
					a.members[p] = advLink{c, StateUp}
				} else {
					a.links[p] = advLink{c, StateUp}
				}
			}
			if _, _, err := r.Receive(from, signedBy(o, FormatJSON, encodeJSON(a, nil, false)), Budget{JSON: 1 << 20, Compact: 1 << 20}); err != nil {
				tb.Fatal(err)
			}
		}
	}
	probe := func(d time.Duration) {
		now = now.Add(d)
		for _, p := range own {
			r.Probe(p, time.Duration(cost[self][p]*float64(time.Millisecond)))
		}
	}
	for s := range 120 {
		probe(time.Second)
		if s%5 == 0 {
			advertise()
		}
		r.Tick()
	}
	return r, probe
}

// BenchmarkTick is a tick of a settled relay.
func BenchmarkTick(b *testing.B) {
	r, _ := bigRouter(b)
	b.ReportAllocs()
	for b.Loop() {
		r.Tick()
	}
}

// BenchmarkProbe is a probe answer that leaves the link's cost as it is.
func BenchmarkProbe(b *testing.B) {
	_, probe := bigRouter(b)
	b.ReportAllocs()
	for b.Loop() {
		probe(0)
	}
}

// BenchmarkBuild is building the graph, as a change to it does.
func BenchmarkBuild(b *testing.B) {
	r, _ := bigRouter(b)
	b.ReportAllocs()
	for b.Loop() {
		r.build()
	}
}
