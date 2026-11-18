"""Fast relay in the mesh document: the fast ports (doc['fastPorts']), each a member's port whose
connections to its mesh address go hop by hop over plain TCP along the mesh's own paths, and
settings.fast (the relay port, how many paths a channel keeps warm, the header-sealing keys).
Every node of a release that has it takes part while there is a fast port: relays and the fast
ports' members listen on the relay port, and every node takes its connections to the fast ports.
A member may listen on another relay port (listen.fast) or be dialed on a mapped one
(publicPorts.fast)."""
from __future__ import annotations

import base64
import contextlib
import copy
import ipaddress
import re
import secrets

DEFAULT_PORT = 52003
DEFAULT_PATHS = 3
MAX_PATHS = 8
MAX_PORTS = 256
PROTOS = ('tcp', 'udp', 'any')
FAST_FEATURE = 'mesh.fastPorts'
APPLY_ERRORS = frozenset({'fast_member_needs_update'})
_KEY_RE = re.compile(r'[A-Za-z0-9+/]{42,46}={0,2}')
_ID = re.compile(r'[A-Za-z0-9_-]{1,32}')
ALL_SOURCES = {'all': True, 'allSubnets': True, 'members': [], 'tags': [], 'prefixes': []}


def default_settings():
    return {'port': DEFAULT_PORT, 'paths': DEFAULT_PATHS, 'keys': []}


def new_key():
    return base64.b64encode(secrets.token_bytes(32)).decode('ascii')


def _int(value, low, high):
    return type(value) is int and low <= value <= high


def _valid_key(value):
    if not isinstance(value, str) or not _KEY_RE.fullmatch(value.strip()):
        return False
    try:
        return len(base64.b64decode(value.strip(), validate=True)) == 32
    except Exception:
        return False


def clean_settings(raw, prior, errors):
    out = {**default_settings(), **{k: v for k, v in (prior or {}).items() if k in ('port', 'paths', 'keys')}}
    if raw is None:
        return out
    if not isinstance(raw, dict) or set(raw) - {'port', 'paths', 'keys', 'rotateKey'}:
        errors.add('settings.fast', 'invalid_fast_settings', '快速中转设置格式不正确')
        return out
    if 'port' in raw:
        if not _int(raw['port'], 1, 65535):
            errors.add('settings.fast.port', 'invalid_port', '端口须在 1～65535 之间')
        else:
            out['port'] = raw['port']
    if 'paths' in raw:
        if not _int(raw['paths'], 1, MAX_PATHS):
            errors.add('settings.fast.paths', 'invalid_fast_paths', f'路径条数须在 1～{MAX_PATHS} 之间')
        else:
            out['paths'] = raw['paths']
    if 'keys' in raw:
        keys = raw['keys']
        if not isinstance(keys, list) or len(keys) > 2 or not all(_valid_key(k) for k in keys):
            errors.add('settings.fast.keys', 'invalid_fast_keys', '快速密钥格式不正确')
        else:
            out['keys'] = [k.strip() for k in keys]
    if raw.get('rotateKey') is True:
        prev = out['keys'][:1]
        out['keys'] = [new_key(), *prev][:2]
    return out


def clean_ports(raw, errors):
    """doc['fastPorts']: [{id, serverId, port, proto (tcp, udp or any), src (a rule's source side,
    every member and shared subnet when left out), note}]."""
    from app.servers.mesh_model import _side
    if not isinstance(raw, list) or len(raw) > MAX_PORTS:
        errors.add('fastPorts', 'invalid_fast_ports', f'快速端口最多 {MAX_PORTS} 个')
        return []
    out = []
    for i, r in enumerate(raw):
        q = f'fastPorts[{i}]'
        if not isinstance(r, dict) or set(r) - {'id', 'serverId', 'port', 'proto', 'src', 'note'}:
            errors.add(q, 'invalid_fast_port', '快速端口格式不正确')
            continue
        port = {'id': r.get('id', ''), 'serverId': r.get('serverId', ''), 'port': r.get('port'), 'proto': r.get('proto', 'any'),
                'src': _side(r['src'], f'{q}.src', 'prefixes', errors) if 'src' in r else dict(ALL_SOURCES), 'note': r.get('note', '')}
        if not isinstance(port['id'], str) or port['id'] and not _ID.fullmatch(port['id']):
            errors.add(f'{q}.id', 'invalid_fast_port', '快速端口 id 格式不正确')
            port['id'] = ''
        if not isinstance(port['serverId'], str) or not port['serverId']:
            errors.add(f'{q}.serverId', 'invalid_fast_port', '选择快速端口所在的节点')
        if not _int(port['port'], 1, 65535):
            errors.add(f'{q}.port', 'invalid_port', '端口须在 1～65535 之间')
        if port['proto'] not in PROTOS:
            errors.add(f'{q}.proto', 'invalid_proto', '协议只能是 tcp、udp 或 any（两者）')
        if not isinstance(port['note'], str) or len(port['note']) > 200:
            errors.add(f'{q}.note', 'invalid_note', '备注最多 200 字')
        out.append(port)
    used = {p['id'] for p in out if p['id']}
    if len(used) != len([p for p in out if p['id']]):
        errors.add('fastPorts', 'fast_port_id_duplicate', '快速端口的 id 重复；新端口不带 id 即可自动分配')
    n = 1
    for port in out:
        if not port['id']:
            while f'f{n}' in used:
                n += 1
            port['id'] = f'f{n}'
            used.add(port['id'])
    return out


def upgrade(doc):
    """Drafts from before fast ports: a member's participation flag, the fast address prefix and
    the SOCKS5 fast-mode proxy ports all went with them."""
    s = doc['settings'].setdefault('fast', default_settings())
    s.pop('prefix', None)
    s.setdefault('paths', DEFAULT_PATHS)
    doc.setdefault('fastPorts', [])
    for m in doc['members']:
        m.pop('fast', None)
        ports = (m.get('proxy') or {}).get('ports')
        if ports:
            m['proxy']['ports'] = [p for p in ports if p.get('mode', 'exit') != 'fast']
            for p in m['proxy']['ports']:
                p.pop('mode', None)


def strip(doc, server_id):
    out = []
    for p in doc.get('fastPorts') or []:
        if p['serverId'] == server_id:
            continue
        p['src']['members'] = [sid for sid in p['src']['members'] if sid != server_id]
        # An empty selector grants nobody access; do not turn it into ALL_SOURCES.
        out.append(p)
    doc['fastPorts'] = out


def unstrip(doc, before, server_id, present):
    current = {p['id']: p for p in doc.get('fastPorts') or []}
    for i, old in enumerate(before.get('fastPorts') or []):
        if old['serverId'] not in present:
            continue
        if old['serverId'] == server_id and old['id'] not in current:
            back = copy.deepcopy(old)
            back['src']['members'] = [sid for sid in back['src']['members'] if sid in present]
            doc['fastPorts'].insert(min(i, len(doc['fastPorts'])), back)
            current[back['id']] = back
        elif server_id in old['src']['members'] and old['id'] in current:
            p = current[old['id']]
            # Do not graft old permissions onto a replacement port reusing the same id.
            if all(p[k] == old[k] for k in ('serverId', 'port', 'proto')) and server_id not in p['src']['members']:
                at = old['src']['members'].index(server_id)
                p['src']['members'].insert(min(at, len(p['src']['members'])), server_id)


def live_ports(doc, live, facts=None, *, own=False):
    """The fast ports whose member is in the mesh: the user's and, unless own, those fast port
    forwards make (mesh_forward.fast_ports)."""
    from app.servers import mesh_forward
    out = [p for p in doc.get('fastPorts') or [] if p['serverId'] in live]
    return out if own else out + mesh_forward.fast_ports(doc, live, facts)


def ensure_secrets(doc):
    s = doc['settings'].setdefault('fast', default_settings())
    keys = [k for k in (s.get('keys') or []) if _valid_key(k)]
    if not keys and (doc.get('fastPorts') or any(f['line'] == 'fast' and f['enabled'] for f in doc.get('forwards') or [])):
        keys = [new_key()]
    s['keys'] = keys[:2]


def listen_port(member, settings, doc=None, facts=None):
    """Effective relay port; an explicit listener or public mapping is never moved."""
    if doc is not None:
        from app.servers.mesh_ports import plan
        port = plan(doc, facts)['fast'].get(member['serverId'])
        if port:
            return port
    return (member.get('listen') or {}).get('fast') or ((settings.get('fast') or {}).get('port')) or DEFAULT_PORT


def public_port(member, settings, doc=None, facts=None):
    """External fast TCP port others dial on member (with port mapping if set)."""
    mapped = (member.get('publicPorts') or {}).get('fast') or 0
    return mapped or listen_port(member, settings, doc, facts)


def supports_fast(facts, sid):
    features = (facts.get(sid) or {}).get('features')
    return features is not None and FAST_FEATURE in features


def listeners(doc, live, facts):
    """The nodes that listen on their relay port: relays and the fast ports' members, of a
    release that takes part; none while there is no fast port."""
    ports = live_ports(doc, live, facts)
    if not ports:
        return {}
    hosts = {p['serverId'] for p in ports}
    return {sid: m for sid, m in live.items() if m.get('address') and supports_fast(facts, sid) and (m['role'] == 'relay' or sid in hosts)}


def validate(doc, facts, live, index, name, errors, *, applying=False):
    s = doc['settings']
    ports = doc.get('fastPorts') or []
    from app.servers.mesh_ports import plan
    if plan(doc, facts)['exhausted']:
        errors.add('settings.fast', 'mesh_internal_ports_exhausted', '内部隧道已没有可安全分配的端口')
    for i, p in enumerate(ports):
        q = f'fastPorts[{i}]'
        m = live.get(p['serverId'])
        if m is None:
            errors.add(f'{q}.serverId', 'fast_port_member', '快速端口所在的节点不在组网里')
            continue
        features = (facts.get(p['serverId']) or {}).get('features')
        if (applying and features is None) or (features is not None and FAST_FEATURE not in features):
            errors.add(q, 'fast_member_needs_update', f'{name(p["serverId"])} 的节点版本不支持快速端口，请先在服务器页更新节点')
        if p['port'] in (v for v in m['listen'].values() if v) or p['port'] == listen_port(m, s, doc, facts):
            errors.add(f'{q}.port', 'fast_port_conflict', f'端口 {p["port"]} 是 {name(p["serverId"])} 的组网或快速中转监听端口')
    for sid in live:
        if supports_fast(facts, sid) and live[sid].get('address') and len(targets_for(doc, sid, live, facts)) > MAX_PORTS:
            errors.add('fastPorts', 'fast_targets_too_many', f'{name(sid)} 去重后的快速目标超过节点容量 {MAX_PORTS} 个（含快速转发）')
    for sid, m in listeners(doc, live, facts).items():
        port = listen_port(m, s, doc, facts)
        if port == (s.get('proxy') or {}).get('port'):
            errors.add(f'members[{index[sid]}].listen.fast', 'fast_port_conflict', '显式快速监听和代理隧道端口冲突，未改动该端口或映射')
        if port in (v for t, v in m['listen'].items() if v and t != 'fast'):
            errors.add(f'members[{index[sid]}].listen.fast' if (m['listen'] or {}).get('fast') else 'settings.fast.port',
                       'fast_port_conflict', f'快速中转端口 {port} 和 {name(sid)} 的组网监听端口相同')
        exit_cfg = (m.get('proxy') or {}).get('exit')
        if exit_cfg and exit_cfg['port'] == port:
            errors.add(f'members[{index[sid]}].proxy.exit.port', 'fast_port_conflict', f'出口端口和 {name(sid)} 的快速中转端口 {port} 相同')
        for i, pt in enumerate((m.get('proxy') or {}).get('ports') or []):
            if pt['port'] == port:
                errors.add(f'members[{index[sid]}].proxy.ports[{i}].port', 'fast_port_conflict', f'代理端口不能和这台的快速中转端口 {port} 相同')


def _lan_host(src_member, dst_member, facts):
    """dst_member's LAN IPv4 address when src_member shares that LAN subnet, else ''."""
    from app.servers.mesh_config import _lan_addresses
    mine = _lan_addresses(facts, src_member['serverId'])
    if not mine:
        return ''
    for theirs in _lan_addresses(facts, dst_member['serverId']):
        if any(m.ip != theirs.ip and m.network.overlaps(theirs.network) for m in mine):
            return str(theirs.ip)
    return ''


def _publicly_dialable(target, facts):
    """Whether target accepts inbound TCP on its external fast port from outside its LAN."""
    from app.servers.mesh_config import endpoint_host, front_exits, is_site
    sid = target['serverId']
    if is_site(target) or target.get('announce') is False or front_exits(facts, sid):
        return False
    host = endpoint_host(target, facts)
    if not host:
        return False
    if target['publicEndpoint'] or (target.get('publicPorts') or {}).get('fast'):
        return True
    if target['listen'].get('tcp') or target['listen'].get('tls'):
        return True
    f = facts.get(sid) or {}
    if f.get('verdict') == 'public' and f.get('publicIp') and f.get('publicIp') == host:
        return True
    for raw in f.get('localAddresses') or []:
        with contextlib.suppress(TypeError, ValueError):
            if str(ipaddress.ip_interface(raw).ip) == host:
                return True
    return False


def _direct_fast_target(src_member, dst_member, doc, facts, *, is_pair_dialer=True):
    """host:port for src_member to dial dst_member's fast port directly, or '' when it must use mesh."""
    from app.servers import mesh_auto, mesh_limits
    from app.servers.mesh_config import address, endpoint_host, front_exits
    s = doc['settings']
    src_id, dst_id = src_member['serverId'], dst_member['serverId']
    exits = front_exits(facts, src_id)
    if exits:
        if dst_id in exits and exits[dst_id]:
            return address(exits[dst_id], listen_port(dst_member, s, doc, facts))
        return ''
    lan = _lan_host(src_member, dst_member, facts)
    if lan:
        return address(lan, listen_port(dst_member, s, doc, facts))
    limits = doc.get('limits') or []
    if limits:
        if 'tcp' not in mesh_limits.transports_for(limits, src_member, dst_member, facts):
            return ''
        if not is_pair_dialer and any(k.get('manualOnly') for k in mesh_limits.crossing(limits, src_member, dst_member, facts)):
            return ''
    line_rules = doc.get('lineRules') or []
    found = mesh_auto.rule_for(line_rules, src_member, dst_member, 'lines', 'dialer', facts)
    if found:
        rule, side_a, side_b = found
        chosen = (side_a if rule['dialer'] == 'a' else side_b)['serverId']
        if chosen != src_id:
            return ''
    if not _publicly_dialable(dst_member, facts):
        return ''
    host = endpoint_host(dst_member, facts)
    return address(host, public_port(dst_member, s, doc, facts)) if host else ''


def _sources(port, live):
    from app.servers.mesh_config import _sources
    return _sources(port['src'], live)


def _touches(sid, live, sources):
    """Whether sources include sid's own address or a gateway subnet of its."""
    me = live[sid]
    mine = [ipaddress.ip_network(f"{me['address']}/32")] + [ipaddress.ip_network(s) for s in (me.get('gateway') or {}).get('subnets') or []]
    return any(ipaddress.ip_network(raw).overlaps(n) for raw in sources for n in mine)


def rules_for(doc, sid, live, facts):
    """Inbound rules on sid: each fast port of its for the sources it names, whichever way its
    connections come; and, on a node listening on the relay port, that port for the other
    nodes taking part, which dial it over the mesh where they cannot directly."""
    me = live[sid]
    if not me.get('address'):
        return []
    out = []
    for p in live_ports(doc, live, facts):
        if p['serverId'] != sid:
            continue
        sources = _sources(p, live)
        if sources:
            out.append({'src': sources, 'dst': [f"{me['address']}/32"], 'proto': p['proto'], 'ports': [[p['port'], p['port']]]})
    if sid in listeners(doc, live, facts):
        others = sorted((f"{m['address']}/32" for other, m in live.items()
                         if other != sid and m.get('address') and supports_fast(facts, other)), key=ipaddress.ip_network)
        if others:
            port = listen_port(me, doc['settings'], doc, facts)
            out.append({'src': others, 'dst': [f"{me['address']}/32"], 'proto': 'tcp', 'ports': [[port, port]]})
    return out


def targets_for(doc, sid, live, facts):
    """Canonical per-node targets, merging only the permissions already granted per protocol.
    TCP and UDP with different sources stay separate; an empty selector never becomes any-source."""
    merged = {}
    for p in live_ports(doc, live, facts):
        if p['serverId'] == sid or not live.get(p['serverId'], {}).get('address') or not supports_fast(facts, p['serverId']):
            continue
        sources = _sources(p, live)
        if not _touches(sid, live, sources):
            continue
        for proto in ('tcp', 'udp') if p['proto'] == 'any' else (p['proto'],):
            key = (p['serverId'], p['port'], proto)
            if p['src'].get('all') and p['src'].get('allSubnets'):
                merged[key] = None
            elif key not in merged:
                merged[key] = set(sources)
            elif merged[key] is not None:
                merged[key].update(sources)
    for key, sources in merged.items():
        if sources is not None:
            merged[key] = [str(n) for n in ipaddress.collapse_addresses(ipaddress.ip_network(s) for s in sources)]
    out = []
    for node, port in sorted({(n, p) for n, p, _ in merged}):
        tcp, udp = (node, port, 'tcp'), (node, port, 'udp')
        protos = ['any'] if tcp in merged and udp in merged and merged[tcp] == merged[udp] else [k for k in ('tcp', 'udp') if (node, port, k) in merged]
        for proto in protos:
            sources = merged[(node, port, 'tcp' if proto == 'any' else proto)]
            out.append({'node': node, 'port': port, 'proto': proto, **({'src': sources} if sources is not None else {})})
    return out


def config(doc, sid, live, facts):
    """configs[sid]['fast'], or None while there is no fast port, sid's release does not take
    part, or sid neither listens nor has a fast port to take connections to."""
    from app.servers.mesh_config import relay_pairs, routed_member
    ports = live_ports(doc, live, facts)
    me = live[sid]
    if not ports or not me.get('address') or not supports_fast(facts, sid):
        return None
    s = doc['settings']
    sf = s.get('fast') or default_settings()
    keys = [k for k in (sf.get('keys') or []) if _valid_key(k)]
    if not keys:
        return None
    capable = {other: m for other, m in live.items() if m.get('address') and supports_fast(facts, other)}
    listening = listeners(doc, live, facts)

    targets = targets_for(doc, sid, live, facts)
    if len(targets) > MAX_PORTS:
        # Reconcile also builds directly when capabilities change. Keep its last working config
        # rather than issuing an oversized config or silently dropping arbitrary user targets.
        from app.servers.mesh_model import MeshInvalid
        raise MeshInvalid([{'path': 'fastPorts', 'error': 'fast_targets_too_many',
                            'message': f'{(facts.get(sid) or {}).get("name") or sid} 去重后的快速目标超过节点容量 {MAX_PORTS} 个（含快速转发）'}])
    if sid not in listening and not targets:
        return None

    mesh_ports = {other: port for other, m in sorted(listening.items()) if (port := listen_port(m, s, doc, facts)) != sf['port']}

    pairs = relay_pairs(doc, facts)
    routed = {other for other, m in live.items() if routed_member(m, live, facts)}
    linked_pairs = set()
    for p in pairs:
        if p['a'] in live and p['b'] in live and p['a'] != p['b']:
            linked_pairs.add((p['a'], p['b'], p.get('punch', False), p.get('dialer', 'a')))
    for p in doc.get('directLinks') or []:
        if p['a'] in live and p['b'] in live and p['a'] != p['b']:
            linked_pairs.add((p['a'], p['b'], p['transport'] == 'udp', p.get('dialer', 'a')))
    for m_sid, m in live.items():
        if m['role'] == 'member':
            for c in m['entry']['candidates']:
                r_sid = c['relay']
                if r_sid in live and live[r_sid]['role'] == 'relay':
                    linked_pairs.add((m_sid, r_sid, False, 'a'))

    dial_edges = set()
    next_targets = {}
    for a_id, b_id, is_punched, dialer_side in linked_pairs:
        explicit_dialer = a_id if dialer_side == 'a' else b_id
        for u_id, v_id in ((a_id, b_id), (b_id, a_id)):
            if u_id not in capable or v_id not in listening:
                continue
            target_hp = '' if (is_punched and not _lan_host(live[u_id], live[v_id], facts)) else _direct_fast_target(
                live[u_id], live[v_id], doc, facts, is_pair_dialer=(u_id == explicit_dialer)
            )
            if target_hp:
                if u_id in routed or live[u_id]['role'] == 'relay' or v_id in routed or live[v_id]['role'] == 'relay':
                    dial_edges.add((u_id, v_id))
                if u_id == sid:
                    next_targets[v_id] = target_hp

    out = {
        'meshPort': sf['port'],
        'keys': list(keys[:2]),
        'nodes': {other: m['address'] for other, m in sorted(capable.items())},
        'paths': sf.get('paths') or DEFAULT_PATHS,
    }
    if sid in listening:
        out['port'] = listen_port(me, s, doc, facts)
    if mesh_ports:
        out['meshPorts'] = mesh_ports
    if next_targets:
        out['next'] = dict(sorted(next_targets.items()))
    if dial_edges:
        out['dial'] = [list(e) for e in sorted(dial_edges)]
    if targets:
        out['targets'] = sorted(targets, key=lambda t: (t['node'], t['port'], t['proto']))
        out['capture'] = True
    return out
