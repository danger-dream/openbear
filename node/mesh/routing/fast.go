package routing

import (
	"slices"
	"strings"
)

// FastPaths are up to k paths after this node to dest, each ending at dest:
// the route in use first, as the relays on the way advertise it, then the
// shortest others by cost (Yen's k shortest paths over the links that are
// up), no-transit tolls included. Nodes allow rejects (such as older
// releases without fast relay) and nodes in avoid are never crossed, and a
// member only ends a path.
func (r *Router) FastPaths(dest string, k int, allow func(string) bool, avoid map[string]bool) [][]string {
	r.mu.Lock()
	defer r.mu.Unlock()
	g := r.graph
	ok := func(n string) bool { return !avoid[n] && (allow == nil || allow(n)) }
	if g == nil || k <= 0 || dest == g.self || !ok(dest) {
		return nil
	}
	var out [][]string
	seen := map[string]bool{}
	add := func(p []string) {
		if key := strings.Join(p, "\x00"); len(out) < k && !seen[key] {
			seen[key] = true
			out = append(out, p)
		}
	}
	if rt := r.routes[dest]; rt != nil && rt.nextHop != "" {
		if p, _, found := g.path(rt.nextHop, dest); found && len(p) > 1 && p[len(p)-1] == dest && !slices.ContainsFunc(p[1:], func(n string) bool { return !ok(n) }) {
			add(slices.Clone(p[1:]))
		}
	}
	for _, p := range g.kShortest(dest, k, ok) {
		add(p)
	}
	return out
}

// Linked reports whether this node has an up link to peer, as routes use them.
func (r *Router) Linked(peer string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.graph == nil {
		return false
	}
	_, ok := r.graph.link(r.graph.self, peer)
	return ok
}

type costedPath struct {
	nodes []string // from this node to the destination
	cost  key
}

// kShortest are up to k loopless paths from this node to dest in order of
// cost, without this node.
func (g *graph) kShortest(dest string, k int, ok func(string) bool) [][]string {
	first, found := g.shortest(g.self, dest, nil, nil, ok)
	if !found {
		return nil
	}
	accepted := []costedPath{{first, g.cost(first)}}
	var pending []costedPath
	has := func(list []costedPath, p []string) bool {
		return slices.ContainsFunc(list, func(c costedPath) bool { return slices.Equal(c.nodes, p) })
	}
	for len(accepted) < k {
		last := accepted[len(accepted)-1].nodes
		for i := 0; i < len(last)-1; i++ {
			root := last[:i+1]
			edges := map[[2]string]bool{}
			for _, p := range accepted {
				if len(p.nodes) > i+1 && slices.Equal(p.nodes[:i+1], root) {
					edges[[2]string{p.nodes[i], p.nodes[i+1]}] = true
				}
			}
			nodes := map[string]bool{}
			for _, n := range root[:i] {
				nodes[n] = true
			}
			spur, found := g.shortest(last[i], dest, nodes, edges, ok)
			if !found {
				continue
			}
			p := append(slices.Clone(root[:i]), spur...)
			if !has(accepted, p) && !has(pending, p) {
				pending = append(pending, costedPath{p, g.cost(p)})
			}
		}
		if len(pending) == 0 {
			break
		}
		slices.SortStableFunc(pending, func(a, b costedPath) int {
			switch {
			case a.cost.less(b.cost):
				return -1
			case b.cost.less(a.cost):
				return 1
			}
			return 0
		})
		accepted, pending = append(accepted, pending[0]), pending[1:]
	}
	out := make([][]string, 0, len(accepted))
	for _, p := range accepted {
		out = append(out, p.nodes[1:])
	}
	return out
}

// shortest is the cheapest path from src to dst, both included, crossing
// none of nodes and taking none of edges; members only at its ends.
func (g *graph) shortest(src, dst string, nodes map[string]bool, edges map[[2]string]bool, ok func(string) bool) ([]string, bool) {
	s, t := g.id(src), g.id(dst)
	if s < 0 || t < 0 {
		return nil, false
	}
	dist := map[int32]key{s: {}}
	prev := map[int32]int32{}
	done := map[int32]bool{}
	for {
		u := int32(-1)
		for n, d := range dist {
			if !done[n] && (u < 0 || d.less(dist[u]) || (d == dist[u] && n < u)) {
				u = n
			}
		}
		if u < 0 {
			return nil, false
		}
		if u == t {
			break
		}
		done[u] = true
		if u != s && g.leaf[u] {
			continue
		}
		for _, e := range g.adj[u] {
			v, name := e.to, g.nodes[e.to]
			if done[v] || nodes[name] || edges[[2]string{g.nodes[u], name}] || (v != t && !ok(name)) {
				continue
			}
			nd := dist[u].add(e.k)
			if u != g.selfAt {
				nd = nd.add(g.toll(u, v, false))
			}
			if d, seen := dist[v]; !seen || nd.less(d) {
				dist[v], prev[v] = nd, u
			}
		}
	}
	path := []string{dst}
	for cur := t; cur != s; {
		p, found := prev[cur]
		if !found || len(path) > len(g.nodes) {
			return nil, false
		}
		cur = p
		path = append(path, g.nodes[cur])
	}
	slices.Reverse(path)
	return path, true
}

// cost is what path, from this node, adds up to.
func (g *graph) cost(path []string) key {
	var c key
	for i := 0; i+1 < len(path); i++ {
		k, linked := g.link(path[i], path[i+1])
		if !linked {
			return unreachable
		}
		c = c.add(k)
		if path[i] != g.self {
			c = c.add(g.toll(g.index[path[i]], g.index[path[i+1]], false))
		}
	}
	return c
}
