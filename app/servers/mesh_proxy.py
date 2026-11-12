"""The mesh's exit proxy in the mesh document: settings.proxy (the tunnel port between nodes),
each member's proxy (its exit and its proxy ports) and exitGroups.

An exit is a SOCKS5 / HTTP proxy on the member's own mesh address (exit.port, its own username
and password) that every member, and optionally the networks behind gateways, may use: what they
send goes out from the exit's machine. An exit group is a set of exits (a region, home broadband,
an unlocked service). A proxy port is a port on a member's loopback, or its LAN too, that leads
to one exit or one exit group (picked in order, by the lowest round trip or pinned) through the
tunnel: connections opened ahead of clients, the target and first data sent together, the client
answered at once, so a new connection costs no round trip across the mesh before its data. A
port on loopback alone takes clients without a password; one on the LAN asks for its own.

On the node, the exit's own proxy and each proxy port are entries (proxy.entries); an exit's
entry leads to the exit itself through its own tunnel port. A node from before entries
(ENTRIES_FEATURE) runs the exit's as its one entry, and no proxy ports; one from before the
proxy (PROXY_FEATURES: it came with standby gateways) has none."""

import hashlib
import hmac
import ipaddress
import re
import secrets

DEFAULT_PORT = 51080
DEFAULT_EXIT_PORT = 1081
DEFAULT_ENTRY_PORT = 1080
LISTEN = ('local', 'lan')
NAME = re.compile(r'[a-z0-9][a-z0-9._-]{0,31}')
IFACE = re.compile(r'[A-Za-z0-9_.:@-]{1,15}')
MAX_GROUPS = 64
MAX_GROUP_MEMBERS = 32
MAX_PORTS = 16
SELECTS = ('order', 'manual', 'latency')
GROUP_ID = re.compile(r'[A-Za-z0-9_-]{1,32}')
ENTRIES_FEATURE = 'mesh.proxy.entries'
PROXY_FEATURES = frozenset({ENTRIES_FEATURE, 'mesh.standby'})
# What the draft keeps and apply refuses until the node is updated.
APPLY_ERRORS = frozenset({'proxy_ports_need_update', 'proxy_exit_needs_update'})
PORT_USER = 'proxy'
EXIT_ENTRY = 'exit'


def default_settings():
    # password: the one every entry took before exits had their own; kept to derive theirs from.
    return {'port': DEFAULT_PORT, 'password': ''}


def new_password():
    return secrets.token_urlsafe(18)


def _int(value, low, high):
    return type(value) is int and low <= value <= high


def _password(value):
    return isinstance(value, str) and len(value) <= 128 and not any(not c.isprintable() or c in ' :' for c in value)


def clean_settings(raw, prior, errors):
    out = dict(prior)
    if not isinstance(raw, dict) or set(raw) - {'port', 'password'}:
        errors.add('settings.proxy', 'invalid_proxy_settings', '代理设置格式不正确')
        return out
    if 'port' in raw:
        if not _int(raw['port'], 1, 65535):
            errors.add('settings.proxy.port', 'invalid_port', '端口须在 1～65535 之间')
        else:
            out['port'] = raw['port']
    if 'password' in raw:
        if not _password(raw['password']):
            errors.add('settings.proxy.password', 'invalid_proxy_password', '密码最多 128 个字符，不能有空格、冒号或控制字符')
        else:
            out['password'] = raw['password']
    return out


def clean_member(raw, p, errors):
    """A member's proxy: None, or {'exit': None | {...}, 'ports': [...]}."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {'exit', 'ports'}:
        errors.add(p, 'invalid_proxy', '代理设置格式不正确')
        return None
    ports = raw.get('ports') or []
    if not isinstance(ports, list) or len(ports) > MAX_PORTS:
        errors.add(f'{p}.ports', 'invalid_proxy_ports', f'一台最多 {MAX_PORTS} 个代理端口')
        ports = []
    out = {'exit': _exit(raw.get('exit'), f'{p}.exit', errors), 'ports': [x for i, e in enumerate(ports) if (x := _port(e, f'{p}.ports[{i}]', errors))]}
    used, n = {x['id'] for x in out['ports'] if x['id']}, 1
    for x in out['ports']:
        if not x['id']:
            while f'p{n}' in used:
                n += 1
            x['id'] = f'p{n}'
            used.add(x['id'])
    return out if out['exit'] or out['ports'] else None


def _exit(raw, p, errors):
    """A member's exit: its own proxy and what it lets that proxy reach and how it goes out."""
    if raw is None:
        return None
    keys = {'lan', 'interface', 'source', 'user', 'password', 'port', 'subnets'}
    if not isinstance(raw, dict) or set(raw) - keys:
        errors.add(p, 'invalid_proxy_exit', '出口设置格式不正确')
        return None
    out = {'user': '', 'password': '', 'port': DEFAULT_EXIT_PORT, 'subnets': False, 'lan': False, 'interface': '', 'source': ''}
    for key in ('lan', 'subnets'):
        if type(raw.get(key, False)) is not bool:
            errors.add(f'{p}.{key}', 'invalid_proxy_exit', '须是开或关')
        else:
            out[key] = raw.get(key, False)
    user = raw.get('user', '')
    user = user.strip().lower() if isinstance(user, str) else None
    if user is None or user and not NAME.fullmatch(user):
        errors.add(f'{p}.user', 'invalid_proxy_name', '用户名须是 1～32 个小写字母、数字、点、下划线或连字符，以字母或数字开头')
    else:
        out['user'] = user
    if not _password(raw.get('password', '')):
        errors.add(f'{p}.password', 'invalid_proxy_password', '密码最多 128 个字符，不能有空格、冒号或控制字符')
    else:
        out['password'] = raw.get('password', '')
    if not _int(raw.get('port', DEFAULT_EXIT_PORT), 1, 65535):
        errors.add(f'{p}.port', 'invalid_port', '端口须在 1～65535 之间')
    else:
        out['port'] = raw.get('port', DEFAULT_EXIT_PORT)
    iface = raw.get('interface', '')
    if not isinstance(iface, str) or iface and not IFACE.fullmatch(iface.strip()):
        errors.add(f'{p}.interface', 'invalid_proxy_interface', '网卡名最多 15 个字符，例如 eth0')
    else:
        out['interface'] = iface.strip()
    source = raw.get('source', '')
    if source:
        try:
            out['source'] = str(ipaddress.ip_address(source.strip() if isinstance(source, str) else source))
        except ValueError:
            errors.add(f'{p}.source', 'invalid_proxy_source', '出站源地址须是本机的一个 IP 地址')
    return out


def _port(raw, p, errors):
    keys = {'id', 'listen', 'address', 'port', 'exit', 'group', 'select', 'pinned', 'strict', 'user', 'password'}
    if not isinstance(raw, dict) or set(raw) - keys:
        errors.add(p, 'invalid_proxy_port', '代理端口格式不正确')
        return None
    out = {'id': raw.get('id', ''), 'listen': 'local', 'address': '', 'port': DEFAULT_ENTRY_PORT, 'exit': '', 'group': '', 'select': 'order',
           'pinned': '', 'strict': False, 'user': PORT_USER, 'password': ''}
    if not isinstance(out['id'], str) or out['id'] and not NAME.fullmatch(out['id']) or out['id'] == EXIT_ENTRY:
        errors.add(f'{p}.id', 'invalid_proxy_port', '代理端口 id 格式不正确')
        out['id'] = ''
    if raw.get('listen', 'local') not in LISTEN:
        errors.add(f'{p}.listen', 'invalid_proxy_listen', '监听范围只能是本机或局域网')
    else:
        out['listen'] = raw.get('listen', 'local')
    address = raw.get('address', '')
    if address:
        try:
            ip = ipaddress.ip_address(address.strip() if isinstance(address, str) else address)
            if ip.version != 4 or ip.is_loopback or ip.is_unspecified or ip.is_multicast:
                raise ValueError
            out['address'] = str(ip)
        except ValueError:
            errors.add(f'{p}.address', 'invalid_proxy_address', '局域网地址须是本机的一个 IPv4 地址，例如 192.168.1.10')
    if not _int(raw.get('port', DEFAULT_ENTRY_PORT), 1, 65535):
        errors.add(f'{p}.port', 'invalid_port', '端口须在 1～65535 之间')
    else:
        out['port'] = raw.get('port', DEFAULT_ENTRY_PORT)
    for key in ('exit', 'group', 'pinned'):
        if not isinstance(raw.get(key, ''), str):
            errors.add(f'{p}.{key}', 'invalid_proxy_port', '格式不正确')
        else:
            out[key] = raw.get(key, '')
    if raw.get('select', 'order') not in SELECTS:
        errors.add(f'{p}.select', 'invalid_exit_group_select', '选择方式只能是按顺序、手动指定或延迟最低')
    else:
        out['select'] = raw.get('select', 'order')
    if type(raw.get('strict', False)) is not bool:
        errors.add(f'{p}.strict', 'invalid_proxy_port', '严格应答须是开或关')
    else:
        out['strict'] = raw.get('strict', False)
    user = raw.get('user', PORT_USER)
    user = user.strip().lower() if isinstance(user, str) else ''
    if not NAME.fullmatch(user):
        errors.add(f'{p}.user', 'invalid_proxy_name', '用户名须是 1～32 个小写字母、数字、点、下划线或连字符，以字母或数字开头')
    else:
        out['user'] = user
    if not _password(raw.get('password', '')):
        errors.add(f'{p}.password', 'invalid_proxy_password', '密码最多 128 个字符，不能有空格、冒号或控制字符')
    else:
        out['password'] = raw.get('password', '')
    return out


def clean_groups(raw, errors):
    if not isinstance(raw, list) or len(raw) > MAX_GROUPS:
        errors.add('exitGroups', 'invalid_exit_groups', f'出口组最多 {MAX_GROUPS} 个')
        return []
    out, used = [], set()
    for i, g in enumerate(raw):
        q = f'exitGroups[{i}]'
        if not isinstance(g, dict) or set(g) - {'id', 'name', 'members'}:
            errors.add(q, 'invalid_exit_group', '出口组格式不正确')
            continue
        group = {'id': g.get('id', ''), 'name': g.get('name', ''), 'members': []}
        if not isinstance(group['id'], str) or group['id'] and not GROUP_ID.fullmatch(group['id']) or group['id'] and group['id'] in used:
            errors.add(f'{q}.id', 'invalid_exit_group', '出口组 id 格式不正确或重复')
            group['id'] = ''
        if not isinstance(group['name'], str) or not 1 <= len(group['name'].strip()) <= 40:
            errors.add(f'{q}.name', 'invalid_exit_group_name', '出口组名称 1～40 字')
        else:
            group['name'] = group['name'].strip()
        members = g.get('members', [])
        if not isinstance(members, list) or len(members) > MAX_GROUP_MEMBERS or not all(isinstance(x, str) and x for x in members):
            errors.add(f'{q}.members', 'invalid_exit_group_members', f'出口组最多 {MAX_GROUP_MEMBERS} 台出口')
        else:
            group['members'] = list(dict.fromkeys(members))
        if group['id']:
            used.add(group['id'])
        out.append(group)
    n = 1
    for group in out:
        if not group['id']:
            while f'g{n}' in used:
                n += 1
            group['id'] = f'g{n}'
            used.add(group['id'])
    return out


# ----- documents from before -----
def _derived(secret, server_id):
    """An exit's password made from the one all entries shared: the same on every load."""
    return hmac.new(secret.encode(), f'exit:{server_id}'.encode(), hashlib.sha256).hexdigest()[:24]


def upgrade(doc):
    """Documents from before exits had their own proxy: run on every load, a no-op after.

    Before exit groups, an exit had a username and backups: each became a group of its name. Then
    a group had a username clients picked it by at an entry, the one port of a node. Now an exit
    takes its own old username or its single-exit group's (else its server's) and a password derived from the
    shared one; groups of one exit, made for each exit, go; a node's entry becomes its first proxy
    port, leading where its clients without a username went, nowhere when they were refused."""
    if 'exitGroups' not in doc:
        groups, used = [], set()
        for m in doc['members']:
            x = (m.get('proxy') or {}).get('exit')
            if not x:
                continue
            name = x.get('name') or m['serverId'][:8]
            gid = re.sub(r'[^A-Za-z0-9_-]', '-', name)[:30] or 'g'
            while gid in used:
                gid += '-'
            used.add(gid)
            groups.append({'id': gid, 'name': name, 'user': name, 'members': [m['serverId'], *[b for b in x.get('backups') or [] if b != m['serverId']]],
                           'select': 'order', 'pinned': ''})
        doc['exitGroups'] = groups
    old = any('user' in g for g in doc['exitGroups']) or any('entry' in (m.get('proxy') or {}) for m in doc['members'])
    if not old:
        return doc
    shared = (doc['settings'].get('proxy') or {}).get('password') or ''
    by_user = {g.get('user'): g for g in doc['exitGroups']}
    users = set()
    for m in doc['members']:
        x = (m.get('proxy') or {}).get('exit')
        if not x:
            continue
        own = (x.get('name') or '').lower() or next((g['user'] for g in doc['exitGroups'] if g.get('members') == [m['serverId']] and g.get('user')), '')
        own = own if NAME.fullmatch(own) else ''
        user = own if own and own not in users else re.sub(r'[^a-z0-9._-]', '-', m['serverId'][:8].lower())
        while user in users:
            user += '-'
        users.add(user)
        m['proxy']['exit'] = {'user': user, 'password': _derived(shared, m['serverId']) if shared else '', 'port': DEFAULT_EXIT_PORT,
                              'subnets': False, 'lan': x.get('lan', False), 'interface': x.get('interface', ''), 'source': x.get('source', '')}
    kept = [g for g in doc['exitGroups'] if len(g.get('members') or []) > 1]
    for m in doc['members']:
        proxy = m.get('proxy')
        if not proxy or 'entry' not in proxy:
            continue
        e, ports = proxy.pop('entry'), []
        if e:
            port = {'id': 'p1', 'listen': e.get('listen', 'local'), 'address': e.get('address', ''), 'port': e.get('port', DEFAULT_ENTRY_PORT),
                    'exit': '', 'group': '', 'select': 'order', 'pinned': '', 'strict': e.get('strict', False), 'user': PORT_USER,
                    'password': shared if e.get('listen') == 'lan' else ''}
            g = by_user.get(e.get('default') or None)
            if g and g in kept:
                port.update(group=g['id'], select=g.get('select', 'order'), pinned=g.get('pinned', ''))
            elif g and g.get('members'):
                port['exit'] = g['members'][0]
            ports.append(port)
        proxy['ports'] = ports
        if not proxy.get('exit') and not ports:
            m['proxy'] = None
    doc['exitGroups'] = [{'id': g['id'], 'name': g['name'], 'members': list(g['members'])} for g in kept]
    return doc


def ensure_secrets(doc):
    """What a draft leaves for the controller to fill: each exit's username and password, and the
    password of each port open to the LAN."""
    users = {(m['proxy'].get('exit') or {}).get('user') for m in doc['members'] if m.get('proxy')}
    for m in doc['members']:
        x = (m.get('proxy') or {}).get('exit')
        if x and not x['user']:
            user = re.sub(r'[^a-z0-9._-]', '-', m['serverId'][:8].lower())
            while user in users:
                user += '-'
            users.add(user)
            x['user'] = user
        if x and not x['password']:
            x['password'] = new_password()
        for p in (m.get('proxy') or {}).get('ports') or []:
            if p['listen'] == 'lan' and not p['password']:
                p['password'] = new_password()


# ----- what the document means -----
def exits(live):
    """{serverId: member} of the live members that are exits."""
    return {sid: m for sid, m in live.items() if (m.get('proxy') or {}).get('exit')}


def ports_of(m):
    return (m.get('proxy') or {}).get('ports') or []


def targets(doc, port, live):
    """The exits, serverIds in order, a proxy port leads to; [] while it leads nowhere."""
    xs = exits(live)
    if port['exit']:
        return [port['exit']] if port['exit'] in xs else []
    group = next((g for g in doc.get('exitGroups') or [] if g['id'] == port['group']), None) if port['group'] else None
    if not group:
        return []
    if port['select'] == 'manual':
        return [port['pinned']] if port['pinned'] in group['members'] and port['pinned'] in xs else []
    return [x for x in group['members'] if x in xs]


def _legacy(facts, sid):
    features = (facts.get(sid) or {}).get('features')
    return features is not None and ENTRIES_FEATURE not in features


def _no_proxy(facts, sid):
    features = (facts.get(sid) or {}).get('features')
    return features is not None and not PROXY_FEATURES & frozenset(features)


def validate(doc, facts, live, index, name, errors):
    s = doc['settings'].get('proxy') or default_settings()
    xs = exits(live)
    groups = {g['id']: g for g in doc.get('exitGroups') or []}
    for i, g in enumerate(doc.get('exitGroups') or []):
        for k, sid in enumerate(g['members']):
            if sid not in xs:
                errors.add(f'exitGroups[{i}].members[{k}]', 'proxy_group_not_exit', f'{name(sid)} 不是出口，不能放进出口组')
    if not xs and not any(ports_of(m) for m in live.values()):
        return
    if s['port'] in doc['settings']['ports'].values():
        errors.add('settings.proxy.port', 'proxy_port_conflict', '代理隧道端口不能和组网的传输端口相同')
    users = {}
    for sid, m in xs.items():
        x, p = m['proxy']['exit'], f'members[{index[sid]}].proxy.exit'
        if _no_proxy(facts, sid):
            errors.add(p, 'proxy_exit_needs_update', f'{name(sid)} 的节点版本不支持出口代理，请先在服务器页更新节点')
        if x['user'] in users:
            errors.add(f'{p}.user', 'proxy_name_duplicate', f'用户名和 {name(users[x["user"]])} 的出口相同')
        users.setdefault(x['user'], sid)
        if x['interface'] and (facts.get(sid) or {}).get('platform') != 'linux':
            errors.add(f'{p}.interface', 'proxy_interface_requires_linux', '只有 Linux 可以指定出站网卡')
        if s['port'] in m['listen'].values():
            errors.add(p, 'proxy_port_conflict', f'代理隧道端口 {s["port"]} 和这台的组网监听端口相同')
        if x['port'] in m['listen'].values() or x['port'] == s['port']:
            errors.add(f'{p}.port', 'proxy_port_conflict', '出口端口不能和组网的监听端口或代理隧道端口相同')
    for sid, m in live.items():
        seen = set()
        for i, port in enumerate(ports_of(m)):
            p = f'members[{index[sid]}].proxy.ports[{i}]'
            if i == 0 and _legacy(facts, sid):
                errors.add(p, 'proxy_ports_need_update', f'{name(sid)} 的节点版本不支持代理端口，请先在服务器页更新节点')
            if port['port'] in seen:
                errors.add(f'{p}.port', 'proxy_port_conflict', '这台已经有一个代理端口用了这个端口号')
            seen.add(port['port'])
            if port['port'] in m['listen'].values() or port['port'] == s['port']:
                errors.add(f'{p}.port', 'proxy_port_conflict', '代理端口不能和组网的监听端口或代理隧道端口相同')
            if port['listen'] == 'lan' and not port['address']:
                errors.add(f'{p}.address', 'proxy_lan_address_missing', '监听局域网时要填本机的局域网地址')
            if bool(port['exit']) == bool(port['group']):
                errors.add(p, 'proxy_port_target', '选这个端口走哪个出口或出口组')
            elif port['exit'] and port['exit'] not in xs:
                errors.add(f'{p}.exit', 'proxy_port_not_exit', f'{name(port["exit"])} 不是出口')
            elif port['group'] and port['group'] not in groups:
                errors.add(f'{p}.group', 'proxy_port_group', '这个出口组不存在了')
            elif port['group'] and not groups[port['group']]['members']:
                errors.add(f'{p}.group', 'proxy_port_group', '这个出口组里还没有出口')
            elif port['group'] and port['select'] == 'manual' and port['pinned'] not in groups[port['group']]['members']:
                errors.add(f'{p}.pinned', 'proxy_group_pinned', '手动指定时要选组里的一台')


def _entry_of_exit(m):
    x = m['proxy']['exit']
    return {'listen': [f'{m["address"]}:{x["port"]}'], 'password': x['password'], 'users': [{'name': x['user'], 'exits': [m['address']]}]}


def config(doc, sid, live, facts):
    """This node's part of the proxy for its config.json, None when it is neither exit nor proxy port."""
    s = doc['settings'].get('proxy') or default_settings()
    me = live[sid]
    if _no_proxy(facts, sid):
        return None
    x = (me.get('proxy') or {}).get('exit')
    out = {}
    if x and me['address']:
        out['exit'] = {'clients': sorted(_clients(doc, sid, live), key=ipaddress.ip_address)}
        for key in ('lan', 'interface', 'source'):
            if x[key]:
                out['exit'][key] = x[key]
    serves = bool(x and me['address'] and x['user'] and x['password'])
    if _legacy(facts, sid):
        if serves:
            out['entry'] = _entry_of_exit(me)
    else:
        entries = [{'name': EXIT_ENTRY, **_entry_of_exit(me)}] if serves else []
        for port in ports_of(me):
            addresses = [live[t]['address'] for t in targets(doc, port, live) if live[t]['address'] and not _no_proxy(facts, t)]
            lan = port['listen'] == 'lan' and port['address']
            if not addresses or lan and not port['password']:
                continue
            listen = [f'127.0.0.1:{port["port"]}', *([f'{port["address"]}:{port["port"]}'] if lan else [])]
            user = {'name': port['user'], 'exits': addresses[:MAX_GROUP_MEMBERS]}
            if port['group'] and port['select'] == 'latency' and len(addresses) > 1:
                user['select'] = 'latency'
            entry = {'name': port['id'], 'listen': listen, 'password': port['password'] if lan else '', 'users': [user]}
            if not lan:
                entry['default'] = port['user']
            if port['strict']:
                entry['strict'] = True
            entries.append(entry)
        if entries:
            out['entries'] = entries
    if not out:
        return None
    out['port'] = s['port']
    return out


def _clients(doc, sid, live):
    """The mesh addresses that open tunnels to an exit: those of the members with a port leading
    to it, and its own for its own proxy."""
    out = {live[sid]['address']}
    for other, m in live.items():
        if m['address'] and any(sid in targets(doc, port, live) for port in ports_of(m)):
            out.add(m['address'])
    return out


def rules_for(doc, sid, live):
    """An exit's rules: its proxy port from every member (and the networks behind gateways when it
    lets them), tcp and udp, and its tunnel port from the members with a port leading to it."""
    me = live[sid]
    x = (me.get('proxy') or {}).get('exit')
    if not x or not me['address']:
        return []
    key = lambda n: ipaddress.ip_network(n)
    dst = [f'{me["address"]}/32']
    out = []
    sources = {f'{m["address"]}/32' for other, m in live.items() if other != sid and m['address']}
    if x['subnets']:
        sources |= {n for other, m in live.items() if other != sid for n in (m['gateway'] or {}).get('subnets', [])}
    if sources:
        src = sorted(sources, key=key)
        out += [{'src': src, 'dst': dst, 'proto': proto, 'ports': [[x['port'], x['port']]]} for proto in ('tcp', 'udp')]
    tunnel = sorted({f'{a}/32' for a in _clients(doc, sid, live) if a != me['address']}, key=key)
    if tunnel:
        port = (doc['settings'].get('proxy') or default_settings())['port']
        out.append({'src': tunnel, 'dst': dst, 'proto': 'tcp', 'ports': [[port, port]]})
    return out


# ----- a member leaving -----
def strip(doc, server_id):
    """Take a member out of the exit groups and out of the ports leading to it; a port pinned to
    it is left without a pin, for the user to choose another."""
    for g in doc.get('exitGroups') or []:
        g['members'] = [v for v in g['members'] if v != server_id]
    for m in doc['members']:
        for port in ports_of(m):
            if port['exit'] == server_id:
                port['exit'] = ''
            if port['pinned'] == server_id:
                port['pinned'] = ''


def unstrip(doc, before, server_id, present):
    """What strip(before, server_id) took out, back where it still fits."""
    groups = {g['id']: g for g in doc.get('exitGroups') or []}
    for g in before.get('exitGroups') or []:
        now = groups.get(g['id'])
        if now is not None and server_id in g['members'] and server_id not in now['members']:
            now['members'].insert(min(g['members'].index(server_id), len(now['members'])), server_id)
    prior = {m['serverId']: {p['id']: p for p in ports_of(m)} for m in before['members']}
    for m in doc['members']:
        if m['serverId'] not in present:
            continue
        for port in ports_of(m):
            old = prior.get(m['serverId'], {}).get(port['id'])
            if not old:
                continue
            if old['exit'] == server_id and not port['exit'] and not port['group']:
                port['exit'] = server_id
            if old['pinned'] == server_id and not port['pinned'] and port['group'] == old['group']:
                port['pinned'] = server_id
