package routing

import (
	"fmt"
	"math/rand"
	"slices"
	"testing"
)

// refGraph is the graph as it was kept before nodes were numbered, by name
// in maps, with its search: the reference the numbered one must match.
type refGraph struct {
	self      string
	nodes     []string
	leaves    map[string]bool
	noTransit map[string]bool
	adj       map[string]map[string]key
}

func (g *refGraph) toll(u, v string, fromMember bool) key {
	if g.noTransit[u] && !fromMember && !g.leaves[v] {
		return lastResort
	}
	return key{}
}

func (g *refGraph) dijkstra(src string) (map[string]key, map[string]string) {
	dist := map[string]key{}
	first := map[string]string{}
	done := map[string]bool{}
	for _, n := range g.nodes {
		dist[n] = unreachable
	}
	dist[src] = key{}
	for {
		u := ""
		for _, n := range g.nodes {
			if !done[n] && !dist[n].unreachable() && (u == "" || dist[n].less(dist[u])) {
				u = n
			}
		}
		if u == "" {
			return dist, first
		}
		done[u] = true
		if u != src && g.leaves[u] {
			continue
		}
		for v, k := range g.adj[u] {
			if done[v] {
				continue
			}
			nd := dist[u].add(k)
			hop := first[u]
			if u == src {
				hop = v
			} else {
				nd = nd.add(g.toll(u, v, g.leaves[src] && first[u] == u))
			}
			if nd.less(dist[v]) || (nd == dist[v] && hop < first[v]) {
				dist[v], first[v] = nd, hop
			}
		}
	}
}

func (g *refGraph) shortest(src, dst string, nodes map[string]bool, edges map[[2]string]bool, ok func(string) bool) ([]string, bool) {
	dist := map[string]key{src: {}}
	prev := map[string]string{}
	done := map[string]bool{}
	for {
		u := ""
		for n, d := range dist {
			if !done[n] && (u == "" || d.less(dist[u]) || (d == dist[u] && n < u)) {
				u = n
			}
		}
		if u == "" {
			return nil, false
		}
		if u == dst {
			break
		}
		done[u] = true
		if u != src && g.leaves[u] {
			continue
		}
		for v, k := range g.adj[u] {
			if done[v] || nodes[v] || edges[[2]string{u, v}] || (v != dst && !ok(v)) {
				continue
			}
			nd := dist[u].add(k)
			if u != g.self {
				nd = nd.add(g.toll(u, v, false))
			}
			if d, seen := dist[v]; !seen || nd.less(d) {
				dist[v], prev[v] = nd, u
			}
		}
	}
	path := []string{dst}
	for cur := dst; cur != src; {
		cur = prev[cur]
		if cur == "" || len(path) > len(g.nodes) {
			return nil, false
		}
		path = append(path, cur)
	}
	slices.Reverse(path)
	return path, true
}

// randomGraphs are both forms of the same graph: costs and tie-breakers
// from a few values so that equal paths are common, some no-transit
// relays, and members, this node one at times.
func randomGraphs(rng *rand.Rand) (*refGraph, *graph) {
	n := 2 + rng.Intn(40)
	ref := &refGraph{leaves: map[string]bool{}, noTransit: map[string]bool{}, adj: map[string]map[string]key{}}
	for i := range n {
		id := fmt.Sprintf("%c%d", 'a'+rng.Intn(26), i)
		ref.nodes = append(ref.nodes, id)
		ref.adj[id] = map[string]key{}
		switch rng.Intn(5) {
		case 0:
			ref.leaves[id] = true
		case 1:
			ref.noTransit[id] = true
		}
	}
	ref.self = ref.nodes[rng.Intn(n)]
	delete(ref.noTransit, ref.self)
	density := rng.Float64()
	for i, a := range ref.nodes {
		for _, b := range ref.nodes[i+1:] {
			if rng.Float64() < density {
				k := key{w: 1 + int64(rng.Intn(4)), tie: int64(rng.Intn(3))}
				ref.adj[a][b], ref.adj[b][a] = k, k
			}
		}
	}
	nodes := slices.Sorted(slices.Values(ref.nodes))
	g := &graph{self: ref.self, nodes: nodes, index: map[string]int32{}, leaves: ref.leaves, leaf: make([]bool, n), noTransit: make([]bool, n),
		adj: make([][]edge, n), dist: make([][]key, n), first: make([][]int32, n)}
	for i, id := range nodes {
		g.index[id] = int32(i)
		g.leaf[i], g.noTransit[i] = ref.leaves[id], ref.noTransit[id]
	}
	g.selfAt = g.index[ref.self]
	for i, a := range nodes {
		for _, b := range nodes[i+1:] {
			if k, ok := ref.adj[a][b]; ok {
				g.adj[i] = append(g.adj[i], edge{g.index[b], k})
				g.adj[g.index[b]] = append(g.adj[g.index[b]], edge{int32(i), k})
			}
		}
	}
	g.solve()
	return ref, g
}

// TestNumberedGraphMatchesNames: numbering the nodes changes no distance,
// next hop or k-shortest spur path.
func TestNumberedGraphMatchesNames(t *testing.T) {
	rng := rand.New(rand.NewSource(1))
	pairs, spurs := 0, 0
	for range 3000 {
		ref, g := randomGraphs(rng)
		for _, src := range ref.nodes {
			if src != ref.self && ref.leaves[src] {
				if g.dist[g.index[src]] != nil {
					t.Fatalf("searched from member %s", src)
				}
				continue
			}
			dist, first := ref.dijkstra(src)
			for _, d := range ref.nodes {
				if got := g.distance(src, d); got != dist[d] {
					t.Fatalf("%s to %s: cost %v, want %v", src, d, got, dist[d])
				}
				if got := g.hop(src, d); got != first[d] {
					t.Fatalf("%s to %s: next hop %q, want %q", src, d, got, first[d])
				}
				pairs++
			}
		}
		for range 5 {
			src, dst := ref.nodes[rng.Intn(len(ref.nodes))], ref.nodes[rng.Intn(len(ref.nodes))]
			nodes, edges := map[string]bool{}, map[[2]string]bool{}
			for _, x := range ref.nodes {
				if x != src && x != dst && rng.Intn(6) == 0 {
					nodes[x] = true
				}
				for y := range ref.adj[x] {
					if rng.Intn(8) == 0 {
						edges[[2]string{x, y}] = true
					}
				}
			}
			ok := func(x string) bool { return len(x)%3 != 0 }
			want, wantFound := ref.shortest(src, dst, nodes, edges, ok)
			got, found := g.shortest(src, dst, nodes, edges, ok)
			if found != wantFound || !slices.Equal(got, want) {
				t.Fatalf("%s to %s: %v %v, want %v %v", src, dst, got, found, want, wantFound)
			}
			spurs++
		}
	}
	t.Logf("%d pairs, %d spur paths", pairs, spurs)
}
