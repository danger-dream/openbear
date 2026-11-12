"""Connection limits: hard rules on which machines may connect, and how, across a boundary.

A limit has two sides (the selectors of line rules: any machine, regions, tags or machines) and
the conditions a connection between them must meet: at least one end a relay, one of the listed
transports, at least one end among the listed machines, set by hand rather than by the mesh. A
connection with one end on each side crosses the limit. Line rules say how machines connect;
limits say whether they may. Every connection the user lists must keep to them (the draft check
names each one that does not); the mesh's own links (full and auto mode, auto transports) skip or
adapt to them and never break one."""
from __future__ import annotations

import re

from app.servers import mesh_auto

TRANSPORTS = ('udp', 'tcp', 'tls')
NAMES = {'udp': 'UDP', 'tcp': 'TCP', 'tls': 'TLS'}
MAX_LIMITS = 50
MAX_VIA = 100
KEYS = {'id', 'name', 'on', 'a', 'b', 'relayEnd', 'transports', 'via', 'manualOnly'}
_ID = re.compile(r'[A-Za-z0-9_-]{1,32}')


def clean(raw, errors):
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_LIMITS:
        errors.add('limits', 'invalid_limits', f'连接限制最多 {MAX_LIMITS} 条')
        return []
    out, used = [], set()
    for i, r in enumerate(raw):
        q = f'limits[{i}]'
        if not isinstance(r, dict) or set(r) - KEYS:
            errors.add(q, 'invalid_limit', '连接限制格式不正确')
            continue
        limit = {'id': r.get('id', ''), 'name': r.get('name', ''), 'on': r.get('on', True),
                 'a': mesh_auto._selector(r.get('a', {'kind': 'any'}), f'{q}.a', errors),
                 'b': mesh_auto._selector(r.get('b', {'kind': 'any'}), f'{q}.b', errors),
                 'relayEnd': r.get('relayEnd', False), 'transports': None, 'via': None, 'manualOnly': r.get('manualOnly', False)}
        if not isinstance(limit['id'], str) or limit['id'] and not _ID.fullmatch(limit['id']) or limit['id'] and limit['id'] in used:
            errors.add(f'{q}.id', 'invalid_limit', '限制 id 格式不正确或重复')
        if not isinstance(limit['name'], str) or not 1 <= len(limit['name'].strip()) <= 60:
            errors.add(f'{q}.name', 'invalid_limit_name', '限制名称 1～60 字')
        else:
            limit['name'] = limit['name'].strip()
        for key in ('on', 'relayEnd', 'manualOnly'):
            if type(limit[key]) is not bool:
                errors.add(f'{q}.{key}', 'invalid_limit', '须是开或关')
        transports = r.get('transports')
        if transports is not None:
            if not isinstance(transports, list) or not 1 <= len(transports) <= 3 or not all(t in TRANSPORTS for t in transports):
                errors.add(f'{q}.transports', 'invalid_limit_transports', '允许的协议须是 udp、tcp、tls 中的一个或几个')
            else:
                limit['transports'] = [t for t in TRANSPORTS if t in transports]
        via = r.get('via')
        if via is not None:
            if not isinstance(via, list) or not 1 <= len(via) <= MAX_VIA or not all(isinstance(v, str) and 0 < len(v) <= 128 for v in via):
                errors.add(f'{q}.via', 'invalid_limit_via', '指定节点须是 1 台以上的机器')
            else:
                limit['via'] = list(dict.fromkeys(via))
        if not (limit['relayEnd'] or limit['transports'] or limit['via'] or limit['manualOnly']):
            errors.add(q, 'limit_without_condition', '至少勾选一个跨界条件')
        if limit['id']:
            used.add(limit['id'])
        out.append(limit)
    n = 1
    for limit in out:
        if not limit['id']:
            while f'k{n}' in used:
                n += 1
            limit['id'] = f'k{n}'
            used.add(limit['id'])
    return out


def crossing(limits, x, y, facts):
    """The enabled limits with x on one side and y on the other."""
    return [k for k in limits or [] if k.get('on', True) and (
        mesh_auto.matches(k['a'], x, facts) and mesh_auto.matches(k['b'], y, facts)
        or mesh_auto.matches(k['a'], y, facts) and mesh_auto.matches(k['b'], x, facts))]


def broken(limit, x, y, transport, automatic, name):
    """Why a connection breaks limit, or None."""
    if limit['relayEnd'] and x['role'] != 'relay' and y['role'] != 'relay':
        return '跨界连接至少一端须是中转'
    if limit['transports'] and transport not in limit['transports']:
        return f'跨界只允许 {"、".join(NAMES[t] for t in limit["transports"])}'
    if limit['via'] and x['serverId'] not in limit['via'] and y['serverId'] not in limit['via']:
        return f'跨界只能经过 {"、".join(name(v) for v in limit["via"])}'
    if limit['manualOnly'] and automatic:
        return '跨界连接只能手动建立，组网不会自动生成'
    return None


def violation(limits, x, y, transport, facts, automatic=False):
    """(limit, reason) for the first limit the connection breaks; None when it keeps to all."""
    def name(sid):
        return (facts.get(sid) or {}).get('name') or sid
    for limit in crossing(limits, x, y, facts):
        reason = broken(limit, x, y, transport, automatic, name)
        if reason:
            return limit, reason
    return None


def allowed(limits, x, y, transport, facts, automatic=True):
    return violation(limits, x, y, transport, facts, automatic) is None


def message(found, x, y, facts):
    limit, reason = found
    def name(sid):
        return (facts.get(sid) or {}).get('name') or sid
    return f'{name(x["serverId"])} 和 {name(y["serverId"])} 违反连接限制「{limit["name"]}」：{reason}'


def transports_for(limits, x, y, facts, automatic=True):
    """The transports a connection between x and y may use, in TRANSPORTS order; empty when no
    transport would do (a condition other than the transport is broken)."""
    return [t for t in TRANSPORTS if allowed(limits, x, y, t, facts, automatic)]
