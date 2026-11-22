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
    it is pinned the hops are its nodes. decision: the relay (or routed member) that chose the
    route for its next hops, as it reports it: {node, dest, cost, smoothed?, best?, bestCost?, bestSmoothed?, held?}."""
    stale = False
    decision = {}

    def decide(node, target):
        routing = (fresh(node) or {}).get('routing') or {}
        route = next((r for r in routing.get('routes') or [] if r.get('dest') == target), None)
        if route:
            decision.update(node=node, dest=target, cost=route.get('cost'),
                            **{k: route[k] for k in ('smoothed', 'best', 'bestCost', 'bestSmoothed', 'held') if k in route})

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
        return {'hops': hops, 'rttMs': None if broken else round(sum(rtts), 1), 'stale': broken, **({'fixed': fixed} if fixed else {}),
                **({'decision': decision} if decision else {})}

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
        decide(src, dst)
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
            decide(start, dst)
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
        decide(start, home)
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
    show them, costed as the routes cost them (the cost each end advertises, from rtt, jitter, loss and
    bias, the larger of the two; the measured one from a node that does not report it); a member on a
    single entry is linked to the relay it enters by, at its round trip."""
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
        costs = [r['advertised'] if r.get('advertised') is not None else r['cost'] if r.get('cost') is not None else r.get('rttMs') for r in reports]
        rtt = next((r['rttMs'] for r in reports if r.get('rttMs')), None)
        cost = max((c for c in costs if c is not None), default=None)
        loss = max(r.get('loss') or 0 for r in reports)
        a, b = tuple(pair)
        for x, y in ((a, b), (b, a)):
            out.setdefault(x, {})[y] = (max(cost if cost is not None else 1000.0, 0.1), rtt, loss)
    return out


def tolled(path, roles, no_transit):
    """The relays of no_transit a path goes on through from a relay to a relay, as the data plane
    counts them (routing.noTransit): not where it starts or ends, toward a member, or at the relay
    the member it starts from enters by."""
    return [x for i, x in enumerate(path[1:-1], 1)
            if x in no_transit and roles.get(path[i + 1]) == 'relay' and not (i == 1 and roles.get(path[0]) == 'member')]


def _shortest(graph, roles, src, dst, banned_nodes, banned_links, no_transit=frozenset(), *, root=None):
    """Fewest tolled relays, then cost. State includes the first-entry exemption: a cheaper
    arrival through another relay must not replace the source member's own entry. A Yen spur
    inherits the original root; it is not a new, toll-exempt origin."""
    root = root or [src]
    exempt = len(root) == 1 or len(root) == 2 and roles.get(root[0]) == 'member'
    start = (src, exempt)
    dist, prev, heap = {start: (0, 0.0)}, {}, [((0, 0.0), start)]
    end = None
    while heap:
        d, state = heapq.heappop(heap)
        u, exempt = state
        if d > dist[state]:
            continue
        if u == dst:
            end = state
            break
        if u != src and roles.get(u) != 'relay':
            continue
        for v, (c, *_) in sorted(graph.get(u, {}).items()):
            if v == src or v in banned_nodes or frozenset((u, v)) in banned_links:
                continue
            toll = int(not exempt and u in no_transit and roles.get(v) == 'relay')
            next_state = (v, len(root) == 1 and state == start and roles.get(src) == 'member')
            nd = (d[0] + toll, d[1] + c)
            if nd < dist.get(next_state, (math.inf, math.inf)):
                dist[next_state], prev[next_state] = nd, state
                heapq.heappush(heap, (nd, next_state))
    if end is None:
        return None
    path = [end[0]]
    while end != start:
        end = prev[end]
        path.append(end[0])
    return path[::-1]


def best_paths(graph, roles, src, dst, k, no_transit=frozenset()):
    """The k cheapest loopless paths from src to dst that only relays forward on (Yen's algorithm),
    those through fewer relays of no_transit first."""
    def cost(p):
        return len(tolled(p, roles, no_transit)), sum(graph[a][b][0] for a, b in zip(p, p[1:]))
    first = _shortest(graph, roles, src, dst, set(), set(), no_transit)
    if not first:
        return []
    found, candidates, seen = [first], [], {tuple(first)}
    while len(found) < k:
        last = found[-1]
        for i in range(len(last) - 1):
            root = last[:i + 1]
            banned = {frozenset((p[i], p[i + 1])) for p in found if p[:i + 1] == root}
            tail = _shortest(graph, roles, last[i], dst, set(root[:-1]), banned, no_transit, root=root)
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
    costs = [graph[a][b][0] for a, b in zip(nodes, nodes[1:]) if b in graph.get(a, {})]
    return {'nodes': list(nodes), 'segments': segs, 'rttMs': None if any(r is None for r in rtts) else round(sum(rtts), 1),
            'cost': round(sum(costs), 1) if len(costs) == len(segs) else None}


def options(src, dst, roles, statuses, now, k, own=(), no_transit=frozenset(), current=None):
    """The k best paths from src to dst by the latest reports, and the pair's own routes:
    {routes: [{nodes, segments [{a, b, exists, rttMs, loss?}], rttMs, cost, own (its rank, or None), noTransit?}],
    own: [{id, rank, on, nodes, segments, rttMs, cost, place (its place in routes from 1, or None)}],
    current: the path in use, current (its nodes), measured the same way, or None}.
    own: [{id, rank, on, nodes}] the own routes of the pair, nodes from src to dst. A link of an
    own route that is not up yet has no rttMs and leaves the route out of the ranking."""
    graph = links(roles, statuses, now)
    if roles.get(src) == 'member' and roles.get(dst) == 'member':
        ups = [p for a, b in ((src, dst), (dst, src)) for p in records(fresh_status(statuses, a, now, FRESH_MS), b) if direct_up(p)]
        if ups:
            rtt = next((p['rttMs'] for p in ups if p['rttMs']), None)
            graph.setdefault(src, {})[dst] = graph.setdefault(dst, {})[src] = (max(rtt or 1000.0, 0.1), rtt, 0)
    found = best_paths(graph, roles, src, dst, k, no_transit)
    places = {tuple(p): i + 1 for i, p in enumerate(found)}
    ranks = {tuple(o['nodes']): o['rank'] for o in own}

    def through(p):
        t = tolled(p, roles, no_transit)
        return {'noTransit': t} if t else {}
    return {'routes': [{**_measured(graph, p), 'own': ranks.get(tuple(p)), **through(p)} for p in found],
            'own': [{'id': o['id'], 'rank': o['rank'], 'on': o['on'], **_measured(graph, o['nodes']), 'place': places.get(tuple(o['nodes']))}
                    for o in own],
            'current': {**_measured(graph, current), **through(current)} if current else None}
