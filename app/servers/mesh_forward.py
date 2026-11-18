"""Port forwards in the mesh document (doc['forwards']): a port on one member, the entry, that takes
clients from anywhere (or those its allow list names) and hands each to another member, the target,
which connects to an address and port from its machine: its loopback, its own services, or any
address it reaches. It replaces a forwarder like realm on a line's entry machine.

The line is the mesh (the tunnel to the target's forward port, encrypted, along the mesh's routes)
or fast (to the target's fast forward port, which the fast relay carries hop by hop over plain TCP:
a fast port of the target's made for it here, that only the entries of its fast forwards may use).
Both tunnel ports (settings.forward) are on the target's mesh address. The target dials only what
its forwards name, for the entry each comes from.

An allow list takes networks and addresses, and provinces of China, whose networks the controller
keeps from ip2region (mesh_geo) and gives the entry as named sets."""
from __future__ import annotations

import copy
import ipaddress
import re

from app.servers import mesh_geo

DEFAULT_PORT = 51082
DEFAULT_FAST_PORT = 51083
FEATURE = 'mesh.forwards'
MAX_FORWARDS = 256
MAX_ALLOW = 1000
MAX_NETWORKS = 200000  # what a node takes, in all of its forwards' lists
PROTOS = ('any', 'tcp', 'udp')
LINES = ('mesh', 'fast')
APPLY_ERRORS = frozenset({'forward_member_needs_update', 'forward_geo_missing'})
_ID = re.compile(r'[A-Za-z0-9_-]{1,32}')
_HOST = re.compile(r'(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?')
KEYS = {'id', 'entry', 'listen', 'proto', 'target', 'host', 'port', 'line', 'proxyProtocol', 'allow', 'enabled', 'note'}
SET_PREFIX = 'province:'


def default_settings():
    return {'port': DEFAULT_PORT, 'fastPort': DEFAULT_FAST_PORT}


def _int(value, low, high):
    return type(value) is int and low <= value <= high


def clean_settings(raw, prior, errors):
    out = {**default_settings(), **(prior or {})}
    if raw is None:
        return out
    if not isinstance(raw, dict) or set(raw) - {'port', 'fastPort'}:
        errors.add('settings.forward', 'invalid_forward_settings', '端口转发设置格式不正确')
        return out
    for key in ('port', 'fastPort'):
        if key in raw:
            if not _int(raw[key], 1, 65535):
                errors.add(f'settings.forward.{key}', 'invalid_port', '端口须在 1～65535 之间')
            else:
                out[key] = raw[key]
    return out


def _host(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return value.lower() if _HOST.fullmatch(value) else None


def _allow(raw, q, errors):
    out = {'prefixes': [], 'provinces': []}
    if raw is None:
        return out
    if not isinstance(raw, dict) or set(raw) - {'prefixes', 'provinces'}:
        errors.add(q, 'invalid_forward_allow', '白名单格式不正确')
        return out
    prefixes = raw.get('prefixes', [])
    if not isinstance(prefixes, list) or len(prefixes) > MAX_ALLOW:
        errors.add(f'{q}.prefixes', 'invalid_forward_allow', f'IP 和网段最多 {MAX_ALLOW} 个')
        prefixes = []
    for k, value in enumerate(prefixes):
        try:
            net = ipaddress.ip_network(value.strip(), strict=False) if isinstance(value, str) else None
        except ValueError:
            net = None
        if net is None:
            errors.add(f'{q}.prefixes[{k}]', 'invalid_forward_allow', f'{value} 不是 IP 地址或网段，例如 1.2.3.4、1.2.3.0/24、2001:db8::/32')
            continue
        out['prefixes'].append(str(net))
    provinces = raw.get('provinces', [])
    if not isinstance(provinces, list) or any(p not in mesh_geo.PROVINCES for p in provinces):
        errors.add(f'{q}.provinces', 'invalid_forward_allow', '省份须是列表中的名称，例如 广东省、北京市')
        provinces = []
    out['prefixes'] = list(dict.fromkeys(out['prefixes']))
    out['provinces'] = [p for p in mesh_geo.PROVINCES if p in provinces]
    return out


def clean(raw, errors):
    """doc['forwards']: [{id, entry, listen, proto (any, tcp or udp), target, host (127.0.0.1 when left
    out), port, line (mesh or fast), proxyProtocol (0, 1 or 2), allow {prefixes, provinces} (every
    client when both are empty), enabled, note}]."""
    if not isinstance(raw, list) or len(raw) > MAX_FORWARDS:
        errors.add('forwards', 'invalid_forwards', f'端口转发最多 {MAX_FORWARDS} 条')
        return []
    out = []
    for i, r in enumerate(raw):
        q = f'forwards[{i}]'
        if not isinstance(r, dict) or set(r) - KEYS:
            errors.add(q, 'invalid_forward', '端口转发格式不正确')
            continue
        f = {'id': r.get('id', ''), 'entry': r.get('entry', ''), 'listen': r.get('listen'), 'proto': r.get('proto', 'any'),
             'target': r.get('target', ''), 'host': r.get('host', '127.0.0.1'), 'port': r.get('port'), 'line': r.get('line', 'mesh'),
             'proxyProtocol': r.get('proxyProtocol', 0), 'allow': _allow(r.get('allow'), f'{q}.allow', errors),
             'enabled': r.get('enabled', True), 'note': r.get('note', '')}
        if not isinstance(f['id'], str) or f['id'] and not _ID.fullmatch(f['id']):
            errors.add(f'{q}.id', 'invalid_forward', '端口转发 id 格式不正确')
            f['id'] = ''
        for key, text in (('entry', '选择入口节点'), ('target', '选择目标节点')):
            if not isinstance(f[key], str) or not f[key]:
                errors.add(f'{q}.{key}', 'invalid_forward', text)
        for key in ('listen', 'port'):
            if not _int(f[key], 1, 65535):
                errors.add(f'{q}.{key}', 'invalid_port', '端口须在 1～65535 之间')
        if f['proto'] not in PROTOS:
            errors.add(f'{q}.proto', 'invalid_proto', '协议只能是 any（TCP 和 UDP）、tcp 或 udp')
        host = _host(f['host'])
        if host is None:
            errors.add(f'{q}.host', 'invalid_forward_host', '目标地址须是 IP 地址或域名，例如 127.0.0.1')
        else:
            f['host'] = host
        if f['line'] not in LINES:
            errors.add(f'{q}.line', 'invalid_forward_line', '线路只能是 mesh（组网）或 fast（快速通道）')
        if f['proxyProtocol'] not in (0, 1, 2) or type(f['proxyProtocol']) is not int:
            errors.add(f'{q}.proxyProtocol', 'invalid_forward_proxy_protocol', 'PROXY protocol 只能是 0（不用）、1 或 2')
        elif f['proxyProtocol'] and f['proto'] == 'udp':
            errors.add(f'{q}.proxyProtocol', 'invalid_forward_proxy_protocol', 'PROXY protocol 只用于 TCP')
        if type(f['enabled']) is not bool:
            errors.add(f'{q}.enabled', 'invalid_forward', 'enabled 须是 true 或 false')
        if not isinstance(f['note'], str) or len(f['note']) > 200:
            errors.add(f'{q}.note', 'invalid_note', '备注最多 200 字')
        out.append(f)
    used = {f['id'] for f in out if f['id']}
    if len(used) != len([f for f in out if f['id']]):
        errors.add('forwards', 'forward_id_duplicate', '端口转发的 id 重复；新转发不带 id 即可自动分配')
    n = 1
    for f in out:
        if not f['id']:
            while f'fw{n}' in used:
                n += 1
            f['id'] = f'fw{n}'
            used.add(f['id'])
    return out


def upgrade(doc):
    doc['settings'].setdefault('forward', default_settings())
    doc.setdefault('forwards', [])


def strip(doc, server_id):
    """A member's removal takes the forwards it is the entry or the target of."""
    doc['forwards'] = [f for f in doc.get('forwards') or [] if server_id not in (f['entry'], f['target'])]


def unstrip(doc, before, server_id, present):
    ids = {f['id'] for f in doc.get('forwards') or []}
    for i, f in enumerate(before.get('forwards') or []):
        if server_id in (f['entry'], f['target']) and {f['entry'], f['target']} <= present and f['id'] not in ids:
            doc['forwards'].insert(min(i, len(doc['forwards'])), copy.deepcopy(f))
            ids.add(f['id'])


def supports(facts, sid):
    features = (facts.get(sid) or {}).get('features')
    return features is not None and FEATURE in features


def running(doc, live, facts=None):
    """The forwards that run: enabled, both ends in the mesh with an address and, with facts, of a
    release that has forwards."""
    out = []
    for f in doc.get('forwards') or []:
        if not f['enabled'] or f['entry'] not in live or f['target'] not in live:
            continue
        if not live[f['entry']].get('address') or not live[f['target']].get('address'):
            continue
        if f['line'] == 'fast' and f['entry'] == f['target']:
            continue
        if facts is not None and not (supports(facts, f['entry']) and supports(facts, f['target'])):
            continue
        out.append(f)
    return out


def kinds(proto):
    return ('tcp', 'udp') if proto == 'any' else (proto,)


def effective_settings(doc, facts=None):
    from app.servers.mesh_ports import plan
    return plan(doc, facts)['forward']


def held_ports(doc, sid, live, facts):
    """{(kind, port): what} of the ports the mesh itself takes on sid's machine, that a forward
    listening on every address of it would clash with."""
    from app.servers import mesh_fast
    s = doc['settings']
    m = live[sid]
    out = {}
    if m['listen'].get('udp'):
        out[('udp', m['listen']['udp'])] = '组网 UDP 监听端口'
    for t in ('tcp', 'tls'):
        if m['listen'].get(t):
            out[('tcp', m['listen'][t])] = f'组网 {t.upper()} 监听端口'
    if sid in mesh_fast.listeners(doc, live, facts):
        out[('tcp', mesh_fast.listen_port(m, s, doc, facts))] = '快速中转端口'
    proxy = m.get('proxy') or {}
    if proxy.get('exit'):
        out[('tcp', proxy['exit']['port'])] = '代理出口端口'
    for p in proxy.get('ports') or []:
        out[('tcp', p['port'])] = '代理端口'
    if (s.get('proxy') or {}).get('port'):
        out[('tcp', s['proxy']['port'])] = '代理隧道端口'
    sf = effective_settings(doc, facts)
    for f in running(doc, live, facts):
        if f['target'] == sid:
            key = 'fastPort' if f['line'] == 'fast' else 'port'
            out[('tcp', sf[key])] = '端口转发的内部隧道端口'
    return out


def validate(doc, facts, live, index, name, errors, *, applying=False):
    from app.servers import mesh_fast
    forwards = doc.get('forwards') or []
    taken = {}
    networks = {}
    for i, f in enumerate(forwards):
        q = f'forwards[{i}]'
        missing = False
        for key, role in (('entry', '入口'), ('target', '目标')):
            if f[key] not in live:
                errors.add(f'{q}.{key}', 'forward_member', f'端口转发的{role}节点不在组网里')
                missing = True
        if missing:
            continue
        for sid in dict.fromkeys((f['entry'], f['target'])):
            features = (facts.get(sid) or {}).get('features')
            if (applying and features is None) or (features is not None and FEATURE not in features):
                errors.add(q, 'forward_member_needs_update', f'{name(sid)} 的节点版本不支持端口转发，请先在服务器页更新节点')
        if f['line'] == 'fast' and f['entry'] == f['target']:
            errors.add(f'{q}.line', 'forward_fast_self', '入口和目标是同一台时没有快速通道可走，请选组网线路')
        if not f['enabled']:
            continue
        held = held_ports(doc, f['entry'], live, facts)
        for kind in kinds(f['proto']):
            if (kind, f['listen']) in held:
                errors.add(f'{q}.listen', 'forward_port_conflict', f'{name(f["entry"])} 的 {kind.upper()} 端口 {f["listen"]} 是{held[(kind, f["listen"])]}')
                break
            other = taken.get((f['entry'], kind, f['listen']))
            if other is not None:
                errors.add(f'{q}.listen', 'forward_port_duplicate', f'和第 {other + 1} 条转发在 {name(f["entry"])} 上的 {kind.upper()} 端口 {f["listen"]} 重复')
                break
            taken[(f['entry'], kind, f['listen'])] = i
        for p in mesh_fast.live_ports(doc, live, own=True):
            if p['serverId'] == f['entry'] and p['port'] == f['listen'] and set(kinds(p['proto'])) & set(kinds(f['proto'])):
                errors.add(f'{q}.listen', 'forward_port_conflict', f'{name(f["entry"])} 的端口 {f["listen"]} 是一个快速端口')
                break
        if f['allow']['provinces']:
            missing_sets = [p for p in f['allow']['provinces'] if mesh_geo.prefixes(p) is None]
            if applying and missing_sets:
                errors.add(f'{q}.allow.provinces', 'forward_geo_missing', f'省份数据还没下载好（{"、".join(missing_sets[:3])}），稍后再应用')
        count = len(f['allow']['prefixes']) + sum(len(mesh_geo.prefixes(p) or []) for p in f['allow']['provinces'])
        networks[f['entry']] = networks.get(f['entry'], 0) + count
    for sid, count in networks.items():
        if count > MAX_NETWORKS:
            errors.add('forwards', 'forward_allow_too_large', f'{name(sid)} 上各转发的白名单合计 {count} 个网段，最多 {MAX_NETWORKS} 个')


def fast_ports(doc, live, facts=None):
    """The fast ports fast forwards make: each target's fast forward port, for its entries."""
    targets = {}
    for f in running(doc, live, facts):
        if f['line'] == 'fast':
            targets.setdefault(f['target'], []).append(f['entry'])
    port = effective_settings(doc, facts)['fastPort']
    return [{'id': f'forward-{sid}', 'serverId': sid, 'port': port, 'proto': 'tcp', 'note': '',
             'src': {'all': False, 'allSubnets': False, 'members': sorted(set(entries)), 'tags': [], 'prefixes': []}}
            for sid, entries in sorted(targets.items())]


def rules_for(doc, sid, live, facts):
    """Inbound rules on sid as a target: the forward port for the entries of its mesh forwards (its
    fast ones come in by the fast port, mesh_fast.rules_for)."""
    me = live[sid]
    entries = sorted({live[f['entry']]['address'] for f in running(doc, live, facts) if f['target'] == sid and f['line'] == 'mesh'},
                     key=ipaddress.ip_address)
    if not entries or not me.get('address'):
        return []
    port = effective_settings(doc, facts)['port']
    return [{'src': [f'{a}/32' for a in entries], 'dst': [f"{me['address']}/32"], 'proto': 'tcp', 'ports': [[port, port]]}]


def config(doc, sid, live, facts):
    """configs[sid]['forward'], or None while sid is neither an entry nor a target."""
    if not supports(facts, sid):
        return None
    forwards = running(doc, live, facts)
    mine = [f for f in forwards if f['entry'] == sid]
    theirs = [f for f in forwards if f['target'] == sid]
    if not mine and not theirs:
        return None
    sf = effective_settings(doc, facts)
    out = {'port': sf['port']}
    if any(f['line'] == 'fast' for f in forwards if sid in (f['entry'], f['target'])):
        out['fastPort'] = sf['fastPort']
    sets = {}
    entries = []
    for f in mine:
        e = {'id': f['id'], 'listen': f['listen'], 'proto': f['proto'], 'node': live[f['target']]['address'], 'host': f['host'], 'port': f['port']}
        if f['line'] == 'fast':
            e['fast'] = True
        if f['proxyProtocol']:
            e['proxyProtocol'] = f['proxyProtocol']
        if f['allow']['prefixes']:
            e['allow'] = f['allow']['prefixes']
        if f['allow']['provinces']:
            e['allowSets'] = [SET_PREFIX + p for p in f['allow']['provinces']]
            for p in f['allow']['provinces']:
                sets[SET_PREFIX + p] = list(mesh_geo.prefixes(p) or [])
        entries.append(e)
    if entries:
        out['forwards'] = entries
    targets = {}
    for f in theirs:
        t = {'from': live[f['entry']]['address'], 'host': f['host'], 'port': f['port'], 'proto': f['proto'], **({'fast': True} if f['line'] == 'fast' else {})}
        targets[tuple(sorted(t.items()))] = t
    if targets:
        out['targets'] = [targets[k] for k in sorted(targets)]
    if sets:
        out['sets'] = dict(sorted(sets.items()))
    return out


def listen_ports(doc, sid, live, facts=None):
    """{'tcp': [...], 'udp': [...]} of the ports sid's forwards listen on."""
    out = {'tcp': set(), 'udp': set()}
    for f in running(doc, live, facts):
        if f['entry'] == sid:
            for kind in kinds(f['proto']):
                out[kind].add(f['listen'])
    return {k: sorted(v) for k, v in out.items()}
