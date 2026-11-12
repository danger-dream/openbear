"""Own routes (自建路线): traffic from one machine to another goes through the relays listed, in
that order, and back the same relays reversed (the data plane's fixedpath.go).

The routes between the same two machines, whichever way round they are written, are that pair's
routes in their order, at most MAX_PER_PAIR: the nodes take the first one whose links are all up,
the next when it goes down, the routes when none is up, and an earlier one again once it has been
up for two minutes.

The ends are relays, or members routing over their upstreams, each linked to the relay next to
it; the relays between are linked in that order. A pair with a node on its routes whose data
plane takes a single route per pair gets its first route alone. Nodes get the routes they are the
source or a relay of, both ways; a destination with no data plane for routes answers by the routes.

What the mesh is like now (a link missing, an old data plane, an end not routing) does not keep
the draft from being saved: it is shown on the route and refused at apply."""
from __future__ import annotations

import re

from app.servers import mesh_config
from app.servers.mesh_status import fresh_status

FEATURE = mesh_config.PATHS_FEATURE
BACKUP_FEATURE = mesh_config.PATHS_BACKUP_FEATURE
MAX_PATHS = 100
MAX_PER_PAIR = 3
# The data plane takes at most 8 relays on a path.
MAX_VIA = 8
KEYS = {'id', 'on', 'from', 'to', 'via'}
_ID = re.compile(r'[A-Za-z0-9_-]{1,32}')
BACK_SUFFIX = '~back'
# Errors about the mesh rather than the route: kept in the draft, refused at apply.
APPLY_ERRORS = frozenset({'path_via_not_relay', 'path_link_missing', 'path_node_outdated', 'path_no_source', 'path_no_destination'})
FRESH_MS = 150_000


def _sid(v):
    return isinstance(v, str) and 0 < len(v) <= 128


def nodes_of(path):
    return [path['from'], *path['via'], path['to']]


def pair_of(path):
    return frozenset((path['from'], path['to']))


def clean(raw, errors):
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_PATHS:
        errors.add('paths', 'invalid_paths', f'自建路线最多 {MAX_PATHS} 条')
        return []
    out, used, pairs = [], set(), {}
    for i, r in enumerate(raw):
        q = f'paths[{i}]'
        if not isinstance(r, dict) or set(r) - KEYS:
            errors.add(q, 'invalid_path', '自建路线格式不正确')
            continue
        path = {'id': r.get('id', ''), 'on': r.get('on', True), 'from': r.get('from', ''), 'to': r.get('to', ''), 'via': []}
        if not isinstance(path['id'], str) or path['id'] and not _ID.fullmatch(path['id']) or path['id'] in used:
            errors.add(f'{q}.id', 'invalid_path', '自建路线 id 格式不正确或重复')
        if type(path['on']) is not bool:
            errors.add(f'{q}.on', 'invalid_path', '须是开或关')
        ends = True
        for side, what in (('from', '起点'), ('to', '终点')):
            if not _sid(path[side]):
                errors.add(f'{q}.{side}', 'path_side_empty', f'{what}不能为空')
                ends = False
        if ends and path['from'] == path['to']:
            errors.add(q, 'path_same_ends', '起点和终点不能是同一台')
            ends = False
        via = r.get('via')
        if not isinstance(via, list) or not 1 <= len(via) <= MAX_VIA or not all(_sid(v) for v in via):
            errors.add(f'{q}.via', 'invalid_path_via', f'途经中转须是 1～{MAX_VIA} 台')
        elif len(set(via)) != len(via):
            errors.add(f'{q}.via', 'path_via_repeated', '同一台中转不能经过两次')
        elif ends and {path['from'], path['to']} & set(via):
            errors.add(f'{q}.via', 'path_end_on_via', '起点和终点不能同时是途经中转')
        else:
            path['via'] = list(via)
        if path['id']:
            used.add(path['id'])
        if ends and path['via']:
            chain = tuple(nodes_of(path))
            same = pairs.setdefault(pair_of(path), [])
            if len(same) >= MAX_PER_PAIR:
                errors.add(q, 'path_pair_full', f'同一对节点最多 {MAX_PER_PAIR} 条自建路线')
            elif chain in same or chain[::-1] in same:
                errors.add(q, 'path_repeated', '和前面的一条自建路线相同')
            same.append(chain)
        out.append(path)
    n = 1
    for path in out:
        if not path['id']:
            while f'p{n}' in used:
                n += 1
            path['id'] = f'p{n}'
            used.add(path['id'])
    return out


def label(path, facts):
    """What a route is called: its machines in order."""
    def name(sid):
        return (facts.get(sid) or {}).get('name') or sid
    return ' → '.join(name(n) for n in nodes_of(path))


class _Mesh:
    """The links routes can use: relay links in effect, and routed members' upstream links."""

    def __init__(self, doc, facts):
        self.facts = facts
        self.live = {m['serverId']: m for m in doc['members'] if not m['removing']}
        self.routed = {sid for sid, m in self.live.items() if mesh_config.routed_member(m, self.live, facts)}
        self.edges = {frozenset((p['a'], p['b'])) for p in mesh_config.relay_pairs(doc, facts)
                      if p['a'] in self.live and p['b'] in self.live and p['a'] != p['b']}
        for sid in self.routed:
            for r in mesh_config.member_relays(self.live[sid], self.live):
                self.edges.add(frozenset((sid, r)))

    def name(self, sid):
        return (self.facts.get(sid) or {}).get('name') or sid

    def relay(self, sid):
        return self.live.get(sid, {}).get('role') == 'relay'

    def linked(self, a, b):
        return frozenset((a, b)) in self.edges

    def new(self, sid, feature=FEATURE):
        return mesh_config.has_feature(self.facts, sid, feature)

    def why_not_end(self, sid):
        """Why sid cannot be an end of a route, or ''."""
        if sid not in self.live:
            return '不在组网里'
        if self.relay(sid) or sid in self.routed:
            return ''
        m = self.live[sid]
        if m['entry']['mode'] != 'upstreams':
            return '的入口中转要改成「同时连上」才能作为路线的一端'
        upstreams = mesh_config.member_relays(m, self.live)
        if not upstreams:
            return '还没有入口中转'
        if not all(self.new(x, mesh_config.MEMBERS_ROUTING_FEATURE) for x in (sid, *upstreams)):
            return '或它的入口中转节点版本太旧：先在服务器页更新节点'
        return '暂未启用多中转选路（组网里的中转节点需全部更新）：先在服务器页更新旧中转节点'

    def why_not_linked(self, end, relay):
        """Why the end of a route and the relay next to it are not linked, or ''."""
        if self.linked(end, relay):
            return ''
        if self.relay(end):
            return f'和 {self.name(relay)} 之间没有线路'
        return f'的入口中转里没有 {self.name(relay)}'

    def segment(self, a, b):
        """A link of a route as the editor shows it: whether it is there, and for a member which one."""
        kind = 'relay' if self.relay(a) and self.relay(b) else 'upstream'
        out = {'a': a, 'b': b, 'kind': kind, 'exists': self.linked(a, b)}
        if kind == 'upstream':
            out['member'] = a if not self.relay(a) else b
        return out


def plan(doc, facts):
    """Every route, in order: [{path, rank, problems, used, single, back}]. rank is its place among
    its pair's routes (from 1, the disabled ones counted); problems [(code, text)] what keeps it
    from applying; used whether the nodes get it (enabled, no problem, and the first usable one of
    a pair with an old node on its routes); single the old nodes that leave it out; back whether
    the destination takes the way back."""
    mesh = _Mesh(doc, facts)
    out, ranks = [], {}
    for path in doc.get('paths') or []:
        pair = pair_of(path)
        ranks[pair] = ranks.get(pair, 0) + 1
        e = {'path': path, 'rank': ranks[pair], 'problems': [], 'used': False, 'single': [], 'back': False}
        out.append(e)
        if not path['on'] or not path['via']:
            continue
        via, s, d, problems = path['via'], path['from'], path['to'], e['problems']
        for v in via:
            if not mesh.relay(v):
                problems.append(('path_via_not_relay', f'{mesh.name(v)} 不是中转，途经节点只能是中转'))
            elif not mesh.new(v):
                problems.append(('path_node_outdated', f'{mesh.name(v)} 的节点版本不支持自建路线：先在服务器页更新节点'))
        for a, b in zip(via, via[1:]):
            if not mesh.linked(a, b):
                problems.append(('path_link_missing', f'{mesh.name(a)} 和 {mesh.name(b)} 之间没有线路'))
        why = mesh.why_not_end(s) or ('' if mesh.new(s) else '的节点版本不支持自建路线：先在服务器页更新节点') or mesh.why_not_linked(s, via[0])
        if why:
            problems.append(('path_no_source', f'起点 {mesh.name(s)} {why}'))
        why = mesh.why_not_end(d) or mesh.why_not_linked(d, via[-1])
        if why:
            problems.append(('path_no_destination', f'终点 {mesh.name(d)} {why}'))
        e['back'] = mesh.new(d)
    usable = {}
    for e in out:
        if e['path']['on'] and e['path']['via'] and not e['problems']:
            usable.setdefault(pair_of(e['path']), []).append(e)
    for routes in usable.values():
        old = [] if len(routes) == 1 else sorted({n for r in routes for n in nodes_of(r['path'])
                                                 if n in mesh.live and not mesh.new(n, BACKUP_FEATURE)}, key=mesh.name)
        for i, e in enumerate(routes):
            e['used'] = i == 0 or not old
            e['single'] = [] if e['used'] else old
    return out


def _forward(path, rank):
    return {'id': path['id'], 'from': [path['from']], 'to': [path['to']], 'via': list(path['via']), 'rank': rank}


def _backward(path, rank):
    return {'id': path['id'] + BACK_SUFFIX, 'from': [path['to']], 'to': [path['from']], 'via': path['via'][::-1], 'rank': rank}


def validate(doc, facts, errors):
    """The draft check: what keeps a route from applying, at the route."""
    for i, e in enumerate(plan(doc, facts)):
        for code, text in e['problems']:
            errors.add(f'paths[{i}]', code, f'自建路线 {label(e["path"], facts)}：{text}')


def hints(doc, facts, out):
    """A route an old node leaves out, a destination that answers by the routes."""
    mesh = _Mesh(doc, facts)
    for i, e in enumerate(plan(doc, facts)):
        title = f'自建路线 {label(e["path"], facts)}'
        if e['single']:
            names = '、'.join(mesh.name(n) for n in e['single'][:3]) + (' 等' if len(e['single']) > 3 else '')
            out.add(f'paths[{i}]', 'path_backup_outdated', f'{title}不会生效：{names} 的节点版本只用每对的第 1 条，先在服务器页更新节点')
        elif e['used'] and not e['back']:
            out.add(f'paths[{i}]', 'path_end_skipped', f'{title}的终点 {mesh.name(e["path"]["to"])} 节点版本不支持自建路线，回程走自动选路')


def configs(doc, facts):
    """{serverId: [path for config.json]}: each node's routes, the ones it is the source or a
    relay of, both ways, each pair's in their order with its rank, which the source marks its packets
    with and the relays go by."""
    out = {}
    for e in plan(doc, facts):
        if not e['used']:
            continue
        path = e['path']
        forward = _forward(path, e['rank'])
        ways = [(forward, [path['from'], *path['via']])]
        if e['back']:
            ways.append((_backward(path, e['rank']), [path['to'], *path['via']]))
        else:
            # An older destination does not answer the source's probes: its links alone say it is up.
            forward['noProbe'] = True
        for item, nodes in ways:
            for sid in nodes:
                out.setdefault(sid, []).append(item)
    return out


def in_use(doc, facts):
    """The links the routes in use go over, frozensets of their two ends: the mesh keeps them."""
    out = set()
    for e in plan(doc, facts):
        if e['used']:
            nodes = nodes_of(e['path'])
            out |= {frozenset(p) for p in zip(nodes, nodes[1:])}
    return out


def _rtt(statuses, a, b, now):
    for x, y in ((a, b), (b, a)):
        routing = (fresh_status(statuses, x, now, FRESH_MS) or {}).get('routing') or {}
        link = next((k for k in routing.get('links', []) if k.get('peer') == y and k.get('rttMs')), None)
        if link:
            return link['rttMs']
    return None


def _report(statuses, sid, pid, nodes, now):
    """Where the pair is on this route, as its source reports it: {state, since, until?, down?, silent?}."""
    for p in (fresh_status(statuses, sid, now, FRESH_MS) or {}).get('paths') or []:
        if p.get('id') == pid and p.get('nodes') == nodes:
            return {k: p[k] for k in ('state', 'since', 'until', 'down', 'silent') if k in p}
    return {'state': 'unknown'}


def view(doc, facts, statuses, now):
    """Each route as the pages show it: its links, measured or missing, from the source to the
    destination; why it does not apply or is left out; and, from both ends' reports, where it is
    each way (pinned in use, standby up behind an earlier one, fallback down, waiting to come back,
    starting, unknown)."""
    mesh = _Mesh(doc, facts)
    out = []
    for e in plan(doc, facts):
        path = e['path']
        nodes = nodes_of(path)
        segs = [{**mesh.segment(a, b), 'rttMs': _rtt(statuses, a, b, now)} for a, b in zip(nodes, nodes[1:])] if path['via'] else []
        item = {'id': path['id'], 'from': path['from'], 'to': path['to'], 'via': list(path['via']), 'on': path['on'], 'rank': e['rank'],
                'label': label(path, facts), 'segments': segs, 'problems': [{'error': c, 'message': t} for c, t in e['problems']],
                'used': e['used'], 'single': e['single'], 'back': e['back']}
        if e['used']:
            item['states'] = {'to': _report(statuses, path['from'], path['id'], nodes, now)}
            if e['back']:
                item['states']['back'] = _report(statuses, path['to'], path['id'] + BACK_SUFFIX, nodes[::-1], now)
        out.append(item)
    return out


def strip(doc, server_id):
    """A removed machine takes the routes it is on with it."""
    if 'paths' in doc:
        doc['paths'] = [p for p in doc['paths'] if server_id not in nodes_of(p)]


def unstrip(doc, before, server_id, present):
    """What strip(before, server_id) took out, back where everything it names is still there and
    its pair has room."""
    if not before.get('paths'):
        return
    paths = doc.setdefault('paths', [])
    for i, path in enumerate(before['paths']):
        nodes = nodes_of(path)
        if server_id not in nodes or any(p['id'] == path['id'] for p in paths) or not all(n in present for n in nodes):
            continue
        if sum(pair_of(p) == pair_of(path) for p in paths) >= MAX_PER_PAIR:
            continue
        paths.insert(min(i, len(paths)), {**path, 'via': list(path['via'])})


def fixed_of(status, src, dst):
    """The own route src reports for its traffic to dst: the one in use, else the first of the
    pair's; {id, nodes, state, since, until, down, rank}, or None."""
    found = []
    for p in (status or {}).get('paths') or []:
        nodes = p.get('nodes') or []
        if len(nodes) >= 3 and nodes[0] == src and nodes[-1] == dst:
            found.append({k: p.get(k) for k in ('id', 'nodes', 'state', 'since', 'until', 'down', 'rank')})
    if not found:
        return None
    return next((p for p in found if p['state'] == 'pinned'), min(found, key=lambda p: p['rank'] or 1))
