"""The path traffic takes between two mesh nodes right now, worked out from what each node
last reported: a punched direct link when it is up, else source -> its entry relay -> the
relays on that relay's route -> the relay the destination is attached to -> destination."""
from __future__ import annotations

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
    rttMs is then null. A path that cannot be followed to the end stops where it breaks."""
    stale = False

    def fresh(sid):
        return fresh_status(statuses, sid, now, FRESH_MS)

    def segment(a, b, kind):
        mine, theirs = records(fresh(a), b), records(fresh(b), a)
        rtt = None
        if kind == 'entry':
            # A relay reports 0 for the members it carries: it does not measure them. The member
            # measures its entry itself, so its side decides.
            member, relay = (a, b) if roles.get(a) == 'member' else (b, a)
            rtt = entry_rtt(fresh(member), relay)
        elif kind == 'relay':
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

    def done(complete):
        rtts = [h['rttMs'] for h in hops[1:]]
        broken = stale or not complete or any(r is None for r in rtts)
        return {'hops': hops, 'rttMs': None if broken else round(sum(rtts), 1), 'stale': broken}

    def add(node, kind):
        if any(h['node'] == node for h in hops) or len(hops) >= MAX_HOPS:
            return False
        hops.append({'node': node, **segment(hops[-1]['node'], node, kind)})
        return True

    if roles.get(src) == 'member' and roles.get(dst) == 'member':
        for a, b in ((src, dst), (dst, src)):
            if any(direct_up(record) for record in records(fresh(a), b)):
                add(dst, 'direct')
                return done(True)

    if roles.get(src) == 'relay':
        start = src
    else:
        status = fresh(src)
        start = ((status or {}).get('entry') or {}).get('relay')
        if not start or not add(start, 'entry'):
            stale = True
            return done(False)

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
