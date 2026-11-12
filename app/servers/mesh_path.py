"""The path traffic takes between two mesh nodes right now, worked out from what each node
last reported: a punched direct link when it is up, else source -> its entry relay -> the
relays on that relay's route -> the relay the destination is attached to -> destination."""
from __future__ import annotations

import heapq
import math

from app.servers import mesh_paths
from app.servers.mesh_status import direct_up, entry_rtt, fresh_status, records

# Reports come at most a minute apart; older ones no longer describe the path.
FRESH_MS = 150_000
MAX_HOPS = 16


def find(src, dst, roles, statuses, configs, now):
    """roles: {id: 'member'|'relay'}; statuses: {id: {'status', 'at'}} as kept for reports;
    configs: the applied configs, whose nextHops relays use before routes are known or in
    static mode. Returns {hops: [{node, via, rttMs}], rttMs, stale}: via says how a hop is
    reached from the one before ('' for the source), rttMs is that segment's round trip.
    stale is true when a report needed is missing or old, or a segment has no round trip;
    rttMs is then null. A path that cannot be followed to the end stops where it breaks.
    fixed, when the source reports a fixed path for this pair: its id, nodes and state; while
    it is pinned the hops are its nodes."""
    stale = False

    def fresh(sid):
        return fresh_status(statuses, sid, now, FRESH_MS)

    def segment(a, b, kind):
        mine, theirs = records(fresh(a), b), records(fresh(b), a)
        rtt = None
        if kind == 'entry':
            # A relay reports 0 for the members it carries: it does not measure them. The member
            # measures its entry itself, so its side decides. A routed leaf member measures its
            # upstreams in routing.links instead.
            member, relay = (a, b) if roles.get(a) == 'member' else (b, a)
            rtt = entry_rtt(fresh(member), relay)
        if rtt is None and kind in ('relay', 'entry'):
            for x, y in ((a, b), (b, a)):
                routing = (fresh(x) or {}).get('routing') or {}
                link = next((link for link in routing.get('links', []) if link['peer'] == y and link['rttMs']), None)
                if link:
                    rtt = link['rttMs']
                    break
        if rtt is None:
            rtt = next((p['rttMs'] for p in (*mine, *theirs) if p['rttMs']), None)
        lan = any((p.get('direct') or {}).get('via') == 'lan' and p['direct']['state'] == 'direct' for p in (*mine, *theirs))
        return {'via': 'lan' if lan else kind, 'rttMs': rtt}

    hops = [{'node': src, 'via': '', 'rttMs': None}]
    fixed = mesh_paths.fixed_of(fresh(src), src, dst)

    def done(complete):
        rtts = [h['rttMs'] for h in hops[1:]]
        broken = stale or not complete or any(r is None for r in rtts)
        return {'hops': hops, 'rttMs': None if broken else round(sum(rtts), 1), 'stale': broken, **({'fixed': fixed} if fixed else {})}

    def add(node, kind):
        if any(h['node'] == node for h in hops) or len(hops) >= MAX_HOPS:
            return False
        hops.append({'node': node, **segment(hops[-1]['node'], node, kind)})
        return True

    if fixed and fixed['state'] == 'pinned':
        for step in fixed['nodes'][1:]:
            if not add(step, 'relay' if roles.get(hops[-1]['node']) == 'relay' and roles.get(step) == 'relay' else 'entry'):
                stale = True
                return done(False)
        return done(True)

    if roles.get(src) == 'member' and roles.get(dst) == 'member':
        for a, b in ((src, dst), (dst, src)):
            if any(direct_up(record) for record in records(fresh(a), b)):
                add(dst, 'direct')
                return done(True)

    def routed_to(node, target):
        routing = (fresh(node) or {}).get('routing')
        if not routing or routing.get('mode') == 'static' or not routing.get('routes'):
            return None
        route = next((r for r in routing['routes'] if r.get('dest') == target), None)
        path = route.get('path') if route else None
        return path[1:] if path and len(path) >= 2 and path[0] == node and path[-1] == target else None

    def hop_kind(a, b):
        if roles.get(a) == 'member' and roles.get(b) == 'member':
            return 'direct'
        if roles.get(a) == 'member' or roles.get(b) == 'member':
            return 'entry'
        return 'relay'

    direct_route = routed_to(src, dst)
    if direct_route is not None:
        for step in direct_route:
            if not add(step, hop_kind(hops[-1]['node'], step)):
                stale = True
                return done(False)
        return done(True)

    if roles.get(src) == 'relay':
        start = src
    else:
        status = fresh(src)
        start = ((status or {}).get('entry') or {}).get('relay')
        if not start or not add(start, 'entry'):
            stale = True
            return done(False)
        tail = routed_to(start, dst)
        if tail is not None:
            for step in tail:
                if not add(step, hop_kind(hops[-1]['node'], step)):
                    stale = True
                    return done(False)
            return done(True)

    if roles.get(dst) == 'relay':
        home = dst
    else:
        owner = ((fresh(start) or {}).get('owners') or {}).get(dst) or {}
        home = owner.get('relay') or ((fresh(dst) or {}).get('entry') or {}).get('relay')
        if not home:
            stale = True
            return done(False)

    if home != start:
        relays = _relay_path(start, home, fresh(start), configs)
        if relays is None:
            stale = True
            return done(False)
        for relay in relays:
            if not add(relay, 'relay'):
                stale = True
                return done(False)
    if home != dst and not add(dst, 'entry'):
        stale = True
        return done(False)
    return done(True)


def _relay_path(start, home, status, configs):
    """The relays after start up to home: from start's reported route, else by the configured
    nextHops when start reports no routes (static mode, or no advertisements yet)."""
    routing = (status or {}).get('routing')
    if routing and routing['mode'] != 'static' and routing['routes']:
        route = next((r for r in routing['routes'] if r['dest'] == home), None)
        path = route['path'] if route else []
        return path[1:] if len(path) >= 2 and path[0] == start and path[-1] == home else None
    out, at = [], start
    while at != home and len(out) < MAX_HOPS:
        at = ((configs.get(at) or {}).get('nextHops') or {}).get(home)
        if not at:
            return None
        out.append(at)
    return out if at == home else None


def links(roles, statuses, now):
    """{a: {b: (cost, rttMs, loss)}}, both ways: the links up between nodes as their latest reports
    show them, costed as the routes cost them (rtt, jitter, loss and bias, the larger of the two
    ends' figures); a member on a single entry is linked to the relay it enters by, at its round trip."""
    sides = {}
    for sid in roles:
        status = fresh_status(statuses, sid, now, FRESH_MS) or {}
        for link in (status.get('routing') or {}).get('links') or []:
            if link['peer'] in roles and link['peer'] != sid:
                sides.setdefault(frozenset((sid, link['peer'])), []).append(link)
        relay = (status.get('entry') or {}).get('relay')
        if roles[sid] == 'member' and roles.get(relay) == 'relay':
            rtt = entry_rtt(status, relay)
            sides.setdefault(frozenset((sid, relay)), []).append({'state': 'up', 'cost': rtt, 'rttMs': rtt})
    out = {}
    for pair, reports in sides.items():
        if any(r['state'] != 'up' for r in reports):
            continue
        costs = [r['cost'] if r.get('cost') is not None else r.get('rttMs') for r in reports]
        rtt = next((r['rttMs'] for r in reports if r.get('rttMs')), None)
        cost = max((c for c in costs if c is not None), default=None)
        loss = max(r.get('loss') or 0 for r in reports)
        a, b = tuple(pair)
        for x, y in ((a, b), (b, a)):
            out.setdefault(x, {})[y] = (max(cost if cost is not None else 1000.0, 0.1), rtt, loss)
    return out


def _shortest(graph, roles, src, dst, banned_nodes, banned_links):
    """The cheapest path from src to dst through relays alone, or None."""
    dist, prev, heap = {src: 0.0}, {}, [(0.0, src)]
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist[u]:
            continue
        if u == dst:
            break
        if u != src and roles.get(u) != 'relay':
            continue
        for v, (c, *_) in sorted(graph.get(u, {}).items()):
            if v in banned_nodes or frozenset((u, v)) in banned_links:
                continue
            if d + c < dist.get(v, math.inf):
                dist[v], prev[v] = d + c, u
                heapq.heappush(heap, (d + c, v))
    if dst not in dist:
        return None
    path = [dst]
    while path[-1] != src:
        path.append(prev[path[-1]])
    return path[::-1]


def best_paths(graph, roles, src, dst, k):
    """The k cheapest loopless paths from src to dst that only relays forward on (Yen's algorithm)."""
    def cost(p):
        return sum(graph[a][b][0] for a, b in zip(p, p[1:]))
    first = _shortest(graph, roles, src, dst, set(), set())
    if not first:
        return []
    found, candidates, seen = [first], [], {tuple(first)}
    while len(found) < k:
        last = found[-1]
        for i in range(len(last) - 1):
            root = last[:i + 1]
            banned = {frozenset((p[i], p[i + 1])) for p in found if p[:i + 1] == root}
            tail = _shortest(graph, roles, last[i], dst, set(root[:-1]), banned)
            if tail and tuple(root[:-1] + tail) not in seen:
                path = root[:-1] + tail
                seen.add(tuple(path))
                heapq.heappush(candidates, (cost(path), path))
        if not candidates:
            break
        found.append(heapq.heappop(candidates)[1])
    return found


def _measured(graph, nodes):
    segs = []
    for a, b in zip(nodes, nodes[1:]):
        _, rtt, loss = graph.get(a, {}).get(b, (None, None, 0))
        segs.append({'a': a, 'b': b, 'exists': b in graph.get(a, {}), 'rttMs': rtt, **({'loss': loss} if loss else {})})
    rtts = [s['rttMs'] for s in segs]
    return {'nodes': list(nodes), 'segments': segs, 'rttMs': None if any(r is None for r in rtts) else round(sum(rtts), 1)}


def options(src, dst, roles, statuses, now, k, own=()):
    """The k best paths from src to dst by the latest reports, and the pair's own routes:
    {routes: [{nodes, segments [{a, b, exists, rttMs, loss?}], rttMs, own (its rank, or None)}],
    own: [{id, rank, on, nodes, segments, rttMs, place (its place in routes from 1, or None)}]}.
    own: [{id, rank, on, nodes}] the own routes of the pair, nodes from src to dst. A link of an
    own route that is not up yet has no rttMs and leaves the route out of the ranking."""
    graph = links(roles, statuses, now)
    if roles.get(src) == 'member' and roles.get(dst) == 'member':
        ups = [p for a, b in ((src, dst), (dst, src)) for p in records(fresh_status(statuses, a, now, FRESH_MS), b) if direct_up(p)]
        if ups:
            rtt = next((p['rttMs'] for p in ups if p['rttMs']), None)
            graph.setdefault(src, {})[dst] = graph.setdefault(dst, {})[src] = (max(rtt or 1000.0, 0.1), rtt, 0)
    found = best_paths(graph, roles, src, dst, k)
    places = {tuple(p): i + 1 for i, p in enumerate(found)}
    ranks = {tuple(o['nodes']): o['rank'] for o in own}
    return {'routes': [{**_measured(graph, p), 'own': ranks.get(tuple(p))} for p in found],
            'own': [{'id': o['id'], 'rank': o['rank'], 'on': o['on'], **_measured(graph, o['nodes']), 'place': places.get(tuple(o['nodes']))}
                    for o in own]}
