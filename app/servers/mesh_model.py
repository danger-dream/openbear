"""Mesh intent: the document users edit (settings, members, links, rules), its
defaults, address allocation and the checks a draft must pass. Every error
names the field it belongs to, e.g. members[2].entry.candidates[0]."""
from __future__ import annotations

import copy
import functools
import ipaddress
import itertools
import re

from app.instances.contracts import InstanceError
from app.instances.servers import LOCAL
from app.servers import mesh_config
from app.servers.mesh_config import (
    BRIDGE_NETS,
    HEALTH,
    LAN_NETS,
    TRANSPORTS,
    can_dial,
    config_schema,
    effective,
    endpoint_host,
    is_site,
    next_hops,
    punch_host,
    reflectors,
    relay_pairs,
    schemas_of,
)

DEFAULT_PREFIX = '100.96.0.0/16'
DEFAULT_MTU = 1400
DEFAULT_PORTS = {'udp': 51999, 'tcp': 52000, 'tls': 52001}
PROTOS = ('any', 'tcp', 'udp', 'icmp')
PLATFORMS = ('linux', 'darwin', 'windows')
MAX_MEMBERS = 250
MAX_CANDIDATES = 8
MAX_LINKS = 500
MAX_RULES = 200
MAX_STUN = 8
BIAS_SPAN = 10000
RELAY_MODES = ('public', 'site')
MEMBER_KEYS = {'serverId', 'role', 'relayMode', 'address', 'controlAddress', 'publicEndpoint', 'listen', 'tags', 'gateway', 'uploadLimitMbps', 'entry',
               'removing'}
# Read-only fields GET adds to each member; a draft sent back with them is accepted.
VIEW_KEYS = {'name', 'platform', 'publicEndpointAuto', 'subnetSuggestions'}
# Never behind a gateway: this host, link-local, multicast and reserved space.
SPECIAL_NETS = tuple(ipaddress.ip_network(n) for n in ('0.0.0.0/8', '127.0.0.0/8', '169.254.0.0/16', '224.0.0.0/4', '240.0.0.0/4'))
MIN_SUBNET_PREFIX = 8
_HOST = re.compile(r'(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?')
_ID = re.compile(r'[A-Za-z0-9_-]{1,32}')
TRANSPORT_NAMES = {'udp': 'UDP', 'tcp': 'TCP', 'tls': 'TLS'}
KEY_NAMES = {'signKey': '签名密钥', 'e2eKey': '端到端密钥'}


class MeshInvalid(InstanceError):
    def __init__(self, errors):
        super().__init__('mesh_invalid', 400)
        self.errors = list(errors)


class Errors(list):
    def add(self, path, error, message):
        self.append({'path': path, 'error': error, 'message': message})


def control_prefix(prefix):
    """The last /24 of the mesh prefix carries control addresses."""
    return str(ipaddress.ip_network(f'{ipaddress.ip_network(prefix).broadcast_address}/24', strict=False))


def default_settings():
    return {'prefix': DEFAULT_PREFIX, 'controlPrefix': control_prefix(DEFAULT_PREFIX), 'mtu': DEFAULT_MTU, 'ports': dict(DEFAULT_PORTS),
            'health': dict(HEALTH), 'reachability': {'stun': []}}


def empty_doc():
    return {'settings': default_settings(), 'members': [], 'relayLinks': {'mode': 'full', 'pairs': []}, 'directLinks': [], 'rules': []}


def new_member(server_id, settings):
    return {'serverId': server_id, 'role': 'member', 'relayMode': 'public', 'address': '', 'controlAddress': '', 'publicEndpoint': '',
            'listen': {'udp': settings['ports']['udp'], 'tcp': 0, 'tls': 0}, 'tags': [], 'gateway': None, 'uploadLimitMbps': None,
            'entry': {'mode': 'fixed', 'candidates': [], 'autoReturn': True}, 'removing': False}


def active(doc):
    return [m for m in doc['members'] if not m['removing']]


def upgrade(doc):
    """Fields added after a document was stored, with their defaults, so an old document
    compares equal to the same one saved again."""
    doc['settings'].setdefault('reachability', {'stun': []})
    for m in doc['members']:
        m.setdefault('relayMode', 'public')
    for pair in doc['relayLinks']['pairs']:
        pair.setdefault('bias', 0)
    for rule in doc['rules']:
        for side in ('src', 'dst'):
            rule[side].setdefault('all', False)
            rule[side].setdefault('allSubnets', False)
    return doc


def _int(value, low, high):
    return type(value) is int and low <= value <= high


def _ip(value):
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    return ip if ip.version == 4 else None


def _net(value):
    try:
        net = ipaddress.ip_network(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return net if net and net.version == 4 else None


# ----- shape -----
def clean_doc(data, prior):
    """Types and shape over the prior draft; references and conflicts are validate's job."""
    errors = Errors()
    if not isinstance(data, dict) or set(data) - {'settings', 'members', 'relayLinks', 'directLinks', 'rules'}:
        raise MeshInvalid([{'path': '', 'error': 'invalid_draft', 'message': '草稿格式不正确'}])
    settings = _settings(data.get('settings', prior['settings']), prior['settings'], errors)
    members = _members(data.get('members', prior['members']), prior['members'], settings, errors)
    if settings['prefix'] != prior['settings']['prefix']:
        for m in members:
            m['address'] = m['controlAddress'] = ''
    doc = {'settings': settings, 'members': members, 'relayLinks': _relay_links(data.get('relayLinks', prior['relayLinks']), errors),
           'directLinks': _direct_links(data.get('directLinks', prior['directLinks']), errors), 'rules': _rules(data.get('rules', prior['rules']), errors)}
    if errors:
        raise MeshInvalid(errors)
    return doc


def _settings(raw, prior, errors):
    out = copy.deepcopy(prior)
    if not isinstance(raw, dict) or set(raw) - {'prefix', 'controlPrefix', 'mtu', 'ports', 'health', 'reachability'}:
        errors.add('settings', 'invalid_settings', '设置格式不正确')
        return out
    if 'prefix' in raw:
        net = _net(raw['prefix'])
        if not net or not 8 <= net.prefixlen <= 22:
            errors.add('settings.prefix', 'invalid_prefix', '地址段须是 IPv4 网段，前缀长度 8～22，例如 100.96.0.0/16')
        else:
            out['prefix'] = str(net)
    out['controlPrefix'] = control_prefix(out['prefix'])
    if 'mtu' in raw:
        # Inner MTU: 1500 minus the IPv6, UDP and WireGuard headers leaves 1420.
        if not _int(raw['mtu'], 1280, 1420):
            errors.add('settings.mtu', 'invalid_mtu', 'MTU 须在 1280～1420 之间')
        else:
            out['mtu'] = raw['mtu']
    if 'ports' in raw:
        ports = raw['ports']
        if not isinstance(ports, dict) or set(ports) - set(TRANSPORTS):
            errors.add('settings.ports', 'invalid_ports', '端口格式不正确')
        else:
            out['ports'] = {**out['ports'], **ports}
            for t in TRANSPORTS:
                if not _int(out['ports'][t], 1, 65535):
                    errors.add(f'settings.ports.{t}', 'invalid_port', '端口须在 1～65535 之间')
    if 'health' in raw:
        health = raw['health']
        if not isinstance(health, dict) or set(health) - set(HEALTH):
            errors.add('settings.health', 'invalid_health', '健康探测参数不正确')
        else:
            for key, value in health.items():
                if not _int(value, 1, 3600000):
                    errors.add(f'settings.health.{key}', 'invalid_health', '须是 1～3600000 的整数')
                else:
                    out['health'][key] = value
            _health_relations(out['health'], errors)
    if 'reachability' in raw:
        reach = raw['reachability']
        stun = reach.get('stun', []) if isinstance(reach, dict) and set(reach) <= {'stun'} else None
        if not isinstance(stun, list) or len(stun) > MAX_STUN:
            errors.add('settings.reachability.stun', 'invalid_stun', f'额外观测点（STUN）最多 {MAX_STUN} 个')
        else:
            servers = []
            for k, value in enumerate(stun):
                server = _host_port(value)
                if not server:
                    errors.add(f'settings.reachability.stun[{k}]', 'invalid_stun', 'STUN 服务器须是 主机:端口，例如 stun.example.com:3478')
                else:
                    servers.append(server)
            out['reachability'] = {'stun': list(dict.fromkeys(servers))}
    return out


def _host_port(value):
    """host:port with a host name, an IPv4 or a bracketed IPv6 address; '' when it is not one."""
    if not isinstance(value, str) or ':' not in value:
        return ''
    host, _, port = value.strip().rpartition(':')
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        return ''
    if host.startswith('[') and host.endswith(']') and _v6(host[1:-1]):
        return f'{host}:{int(port)}'
    return f'{host}:{int(port)}' if _ip(host) or _HOST.fullmatch(host) else ''


def _health_relations(h, errors):
    """The data plane rejects the whole config when these fail, so they are checked here first."""
    h = {**HEALTH, **h}
    if not h['minTimeoutMs'] <= h['initialTimeoutMs'] <= h['maxTimeoutMs']:
        errors.add('settings.health.initialTimeoutMs', 'invalid_health', '须满足 最小超时 ≤ 初始超时 ≤ 最大超时')
    if h['leaseRefreshMs'] >= h['leaseMs']:
        errors.add('settings.health.leaseRefreshMs', 'invalid_health', '刷新间隔须小于租约时长')
    if h['coldRetryMs'] > 300000:
        errors.add('settings.health.coldRetryMs', 'invalid_health', '冷备轮询间隔不能超过 300000 毫秒')
    for key in ('consecutiveFailures', 'readySuccesses'):
        if not 1 <= h[key] <= 16:
            errors.add(f'settings.health.{key}', 'invalid_health', '须是 1～16 的整数')
    if 1 <= h['readySuccesses'] <= 16 and h['activeIntervalMs'] * h['readySuccesses'] > 10000:
        errors.add('settings.health.activeIntervalMs', 'invalid_health', '探测间隔 × 就绪所需成功次数不能超过 10000 毫秒')


def _members(raw, prior, settings, errors):
    if not isinstance(raw, list) or len(raw) > MAX_MEMBERS:
        errors.add('members', 'invalid_members', '成员列表格式不正确')
        return copy.deepcopy(prior)
    before = {m['serverId']: m for m in prior}
    out, seen = [], set()
    for i, item in enumerate(raw):
        member = _member(item, f'members[{i}]', before, errors)
        if member is None:
            continue
        if member['serverId'] in seen:
            errors.add(f'members[{i}].serverId', 'member_duplicate', '成员重复')
            continue
        seen.add(member['serverId'])
        out.append(member)
    missing = [sid for sid in before if sid not in seen]
    if missing and not errors:
        errors.add('members', 'member_missing', '成员只能通过“移除”删除，草稿里缺少已添加的成员')
    return out


def _member(raw, p, before, errors):
    if not isinstance(raw, dict) or set(raw) - MEMBER_KEYS - VIEW_KEYS:
        errors.add(p, 'invalid_member', '成员格式不正确')
        return None
    prior = before.get(raw['serverId']) if isinstance(raw.get('serverId'), str) else None
    if prior is None:
        errors.add(f'{p}.serverId', 'member_not_installed', '只能编辑已添加的成员；新成员请先通过“添加成员”加入')
        return None
    m = copy.deepcopy(prior)
    if 'role' in raw:
        if raw['role'] not in ('member', 'relay'):
            errors.add(f'{p}.role', 'invalid_role', '角色只能是 member 或 relay')
        else:
            m['role'] = raw['role']
    if 'relayMode' in raw:
        if raw['relayMode'] not in RELAY_MODES:
            errors.add(f'{p}.relayMode', 'invalid_relay_mode', '中转类型只能是 public（公网中转）或 site（站点中转）')
        else:
            m['relayMode'] = raw['relayMode']
    for key in ('address', 'controlAddress'):
        if key in raw:
            value = raw[key]
            if not isinstance(value, str) or value and not _ip(value):
                errors.add(f'{p}.{key}', 'invalid_address', '须是 IPv4 地址，留空表示自动分配')
            else:
                m[key] = str(_ip(value)) if value else ''
    if 'publicEndpoint' in raw:
        value = raw['publicEndpoint']
        # Pasted addresses often carry a space or a line break at either end.
        value = value.strip() if isinstance(value, str) else value
        if not isinstance(value, str) or value and not (_ip(value) or _HOST.fullmatch(value) or _v6(value)):
            errors.add(f'{p}.publicEndpoint', 'invalid_endpoint', '对外地址须是 IP 或域名，不带端口；留空表示自动')
        else:
            m['publicEndpoint'] = value.strip()
    if 'listen' in raw:
        listen = raw['listen']
        if not isinstance(listen, dict) or set(listen) - set(TRANSPORTS):
            errors.add(f'{p}.listen', 'invalid_listen', '监听端口格式不正确')
        else:
            for t, value in listen.items():
                if not _int(value, 0, 65535):
                    errors.add(f'{p}.listen.{t}', 'invalid_port', '端口须在 1～65535 之间，0 表示不监听')
                else:
                    m['listen'][t] = value
    if 'tags' in raw:
        tags = raw['tags']
        if not isinstance(tags, list) or len(tags) > 20 or not all(isinstance(t, str) and 1 <= len(t.strip()) <= 40 for t in tags):
            errors.add(f'{p}.tags', 'invalid_tags', '标签最多 20 个，每个 1～40 字')
        else:
            m['tags'] = list(dict.fromkeys(t.strip() for t in tags))
    if 'gateway' in raw:
        m['gateway'] = _gateway(raw['gateway'], f'{p}.gateway', errors)
    if 'uploadLimitMbps' in raw:
        value = raw['uploadLimitMbps']
        if value is not None and (type(value) not in (int, float) or not 0 < value <= 100000):
            errors.add(f'{p}.uploadLimitMbps', 'invalid_rate', '发送速率须在 0～100000 Mbit/s 之间，留空表示不限')
        else:
            m['uploadLimitMbps'] = value
    if 'entry' in raw:
        m['entry'] = _entry(raw['entry'], f'{p}.entry', m['entry'], errors)
    if 'removing' in raw:
        if type(raw['removing']) is not bool:
            errors.add(f'{p}.removing', 'invalid_member', '格式不正确')
        else:
            m['removing'] = raw['removing']
    return m


def _v6(value):
    try:
        return ipaddress.ip_address(value).version == 6
    except ValueError:
        return False


def _gateway(raw, p, errors):
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {'subnets', 'masquerade'} or type(raw.get('masquerade', True)) is not bool:
        errors.add(p, 'invalid_gateway', '网关格式不正确')
        return None
    subnets = raw.get('subnets')
    if not isinstance(subnets, list) or not 1 <= len(subnets) <= 32:
        errors.add(f'{p}.subnets', 'invalid_subnets', '网关至少要有一个网段，最多 32 个')
        return None
    out = []
    for k, value in enumerate(subnets):
        net = _net(value)
        if not net:
            errors.add(f'{p}.subnets[{k}]', 'invalid_subnet', '须是 IPv4 网段（网络地址/前缀长度），例如 192.168.1.0/24')
        elif net.prefixlen < MIN_SUBNET_PREFIX:
            errors.add(f'{p}.subnets[{k}]', 'invalid_subnet', f'网段太大，前缀长度至少 {MIN_SUBNET_PREFIX}，例如 192.168.1.0/24')
        elif any(net.overlaps(s) for s in SPECIAL_NETS):
            errors.add(f'{p}.subnets[{k}]', 'invalid_subnet', '不能是本机回环、链路本地（169.254.0.0/16）、组播或保留地址段')
        else:
            out.append(str(net))
    return {'subnets': out, 'masquerade': raw.get('masquerade', True)}


def _entry(raw, p, prior, errors):
    if not isinstance(raw, dict) or set(raw) - {'mode', 'candidates', 'autoReturn'}:
        errors.add(p, 'invalid_entry', '入口格式不正确')
        return prior
    out = {'mode': raw.get('mode', prior['mode']), 'candidates': [], 'autoReturn': raw.get('autoReturn', prior['autoReturn'])}
    if out['mode'] not in ('fixed', 'failover'):
        errors.add(f'{p}.mode', 'invalid_entry_mode', '模式只能是 fixed 或 failover')
    if type(out['autoReturn']) is not bool:
        errors.add(f'{p}.autoReturn', 'invalid_entry', '格式不正确')
    candidates = raw.get('candidates', prior['candidates'])
    if not isinstance(candidates, list) or len(candidates) > MAX_CANDIDATES:
        errors.add(f'{p}.candidates', 'invalid_candidates', f'入口候选最多 {MAX_CANDIDATES} 个')
        return out
    for j, c in enumerate(candidates):
        q = f'{p}.candidates[{j}]'
        if (not isinstance(c, dict) or set(c) - {'id', 'relay', 'transport'} or not isinstance(c.get('relay'), str)
                or c.get('transport', 'udp') not in TRANSPORTS or 'id' in c and not (isinstance(c['id'], str) and _ID.fullmatch(c['id']))):
            errors.add(q, 'invalid_candidate', '入口候选须指定中转和传输方式（udp、tcp、tls）')
            continue
        out['candidates'].append({'id': c.get('id', ''), 'relay': c['relay'], 'transport': c.get('transport', 'udp')})
    used = {c['id'] for c in out['candidates'] if c['id']}
    if len(used) != len([c for c in out['candidates'] if c['id']]):
        errors.add(f'{p}.candidates', 'candidate_id_duplicate', '入口候选的 id 重复')
    n = 1
    for c in out['candidates']:
        if not c['id']:
            while f'c{n}' in used:
                n += 1
            c['id'] = f'c{n}'
            used.add(c['id'])
    return out


def _pair(raw, q, keys, errors):
    if (not isinstance(raw, dict) or set(raw) - keys or not isinstance(raw.get('a'), str) or not isinstance(raw.get('b'), str)
            or raw.get('transport', 'udp') not in TRANSPORTS or raw.get('dialer', 'a') not in ('a', 'b')
            or 'cost' in keys and not _int(raw.get('cost', 1), 1, 1000)):
        errors.add(q, 'invalid_link', '连接须指定两端、传输方式（udp、tcp、tls）、主动方（a 或 b），代价为 1～1000')
        return None
    if 'bias' in keys and not _int(raw.get('bias', 0), -BIAS_SPAN, BIAS_SPAN):
        errors.add(f'{q}.bias', 'invalid_bias', f'偏好须是 -{BIAS_SPAN}～{BIAS_SPAN} 的整数（毫秒）')
        return None
    out = {'a': raw['a'], 'b': raw['b'], 'transport': raw.get('transport', 'udp')}
    if 'cost' in keys:
        out['cost'] = raw.get('cost', 1)
    out['dialer'] = raw.get('dialer', 'a')
    if 'bias' in keys:
        out['bias'] = raw.get('bias', 0)
    return out


def _relay_links(raw, errors):
    if not isinstance(raw, dict) or set(raw) - {'mode', 'pairs'} or raw.get('mode', 'full') not in ('full', 'custom'):
        errors.add('relayLinks', 'invalid_relay_links', '中转连接格式不正确')
        return {'mode': 'full', 'pairs': []}
    pairs = raw.get('pairs', [])
    if not isinstance(pairs, list) or len(pairs) > MAX_LINKS:
        errors.add('relayLinks.pairs', 'invalid_relay_links', '中转连接格式不正确')
        return {'mode': raw.get('mode', 'full'), 'pairs': []}
    out = [_pair(p, f'relayLinks.pairs[{i}]', {'a', 'b', 'transport', 'cost', 'dialer', 'bias'}, errors) for i, p in enumerate(pairs)]
    return {'mode': raw.get('mode', 'full'), 'pairs': [p for p in out if p]}


def _direct_links(raw, errors):
    if not isinstance(raw, list) or len(raw) > MAX_LINKS:
        errors.add('directLinks', 'invalid_direct_links', '直连格式不正确')
        return []
    out = [_pair(p, f'directLinks[{i}]', {'a', 'b', 'transport', 'dialer'}, errors) for i, p in enumerate(raw)]
    return [p for p in out if p]


def _rules(raw, errors):
    if not isinstance(raw, list) or len(raw) > MAX_RULES:
        errors.add('rules', 'invalid_rules', f'访问规则最多 {MAX_RULES} 条')
        return []
    out = []
    for i, r in enumerate(raw):
        q = f'rules[{i}]'
        if not isinstance(r, dict) or set(r) - {'id', 'src', 'dst', 'proto', 'ports', 'note'}:
            errors.add(q, 'invalid_rule', '规则格式不正确')
            continue
        rule = {'id': r.get('id', ''), 'src': _side(r.get('src'), f'{q}.src', 'prefixes', errors),
                'dst': _side(r.get('dst'), f'{q}.dst', 'subnets', errors), 'proto': r.get('proto', 'any'), 'ports': [], 'note': r.get('note', '')}
        if not isinstance(rule['id'], str) or rule['id'] and not _ID.fullmatch(rule['id']):
            errors.add(f'{q}.id', 'invalid_rule', '规则 id 格式不正确')
        if rule['proto'] not in PROTOS:
            errors.add(f'{q}.proto', 'invalid_proto', '协议只能是 any、tcp、udp、icmp')
        if not isinstance(rule['note'], str) or len(rule['note']) > 200:
            errors.add(f'{q}.note', 'invalid_note', '备注最多 200 字')
        ports = r.get('ports', [])
        if not isinstance(ports, list) or len(ports) > 32:
            errors.add(f'{q}.ports', 'invalid_ports', '端口区间最多 32 个')
        else:
            for k, span in enumerate(ports):
                if not (isinstance(span, list) and len(span) == 2 and _int(span[0], 1, 65535) and _int(span[1], 1, 65535) and span[0] <= span[1]):
                    errors.add(f'{q}.ports[{k}]', 'invalid_port_range', '端口区间须是 [起, 止]，在 1～65535 之间')
                else:
                    rule['ports'].append(list(span))
        out.append(rule)
    given = [r['id'] for r in out if isinstance(r['id'], str) and r['id']]
    used = set(given)
    if len(used) != len(given):
        # The page finds a rule by its id: two with one id would edit as one.
        errors.add('rules', 'rule_id_duplicate', '访问规则的 id 重复；新规则不带 id 即可自动分配')
    n = 1
    for rule in out:
        if not rule['id']:
            while f'r{n}' in used:
                n += 1
            rule['id'] = f'r{n}'
            used.add(rule['id'])
    return out


def _side(raw, p, nets, errors):
    """One side of a rule. all: true stands for every member, those that join later and local
    included (not its gateway subnets): members and tags named with it add nothing and are
    dropped; prefixes / subnets still count. allSubnets: true stands for every gateway subnet
    of the members not being removed, those announced later included; it adds to the rest."""
    keys = ('members', 'tags', nets)
    out = {'all': False, 'allSubnets': False, **{k: [] for k in keys}}
    if not isinstance(raw, dict) or set(raw) - {'all', 'allSubnets', *keys}:
        errors.add(p, 'invalid_rule_side', '格式不正确')
        return out
    for flag in ('all', 'allSubnets'):
        if type(raw.get(flag, False)) is not bool:
            errors.add(f'{p}.{flag}', 'invalid_rule_side', '格式不正确')
        elif raw.get(flag):
            out[flag] = True
    for key in keys:
        values = raw.get(key, [])
        if not isinstance(values, list) or len(values) > 100 or not all(isinstance(v, str) and 0 < len(v) <= 128 for v in values):
            errors.add(f'{p}.{key}', 'invalid_rule_side', '格式不正确')
            continue
        for k, value in enumerate(values):
            if key == nets:
                net = _net(value)
                if not net:
                    errors.add(f'{p}.{key}[{k}]', 'invalid_subnet', '须是 IPv4 网段')
                    continue
                value = str(net)
            if value not in out[key]:
                out[key].append(value)
    if out['all']:
        out['members'], out['tags'] = [], []
    return out


# ----- addresses -----
def allocate(doc):
    """Fill empty addresses with the lowest free ones. A control address mirrors the
    business address's offset when that slot is free, so the two are easy to pair up."""
    net = ipaddress.ip_network(doc['settings']['prefix'])
    ctrl = ipaddress.ip_network(doc['settings']['controlPrefix'])
    used = {m['address'] for m in doc['members'] if m['address']}
    used_ctrl = {m['controlAddress'] for m in doc['members'] if m['controlAddress']}

    def free(network, taken, skip=None):
        for value in range(int(network.network_address) + 1, int(network.broadcast_address)):
            ip = ipaddress.ip_address(value)
            if skip is not None and ip in skip:
                continue
            if str(ip) not in taken:
                return str(ip)
        return ''
    for m in doc['members']:
        if not m['address']:
            m['address'] = free(net, used, ctrl)
            used.add(m['address'])
        if not m['controlAddress']:
            mirrored = ''
            if _ip(m['address']) and _ip(m['address']) in net:
                value = int(ctrl.network_address) + int(_ip(m['address'])) - int(net.network_address)
                if int(ctrl.network_address) < value < int(ctrl.broadcast_address):
                    mirrored = str(ipaddress.ip_address(value))
            m['controlAddress'] = mirrored if mirrored and mirrored not in used_ctrl else free(ctrl, used_ctrl)
            used_ctrl.add(m['controlAddress'])
    return doc


# ----- references and conflicts -----
def strip(doc, server_id):
    """Drop everything that points at one member: entry candidates, links and rule references.
    A rule left with no source or no target was only about that member and goes too."""
    for m in doc['members']:
        m['entry']['candidates'] = [c for c in m['entry']['candidates'] if c['relay'] != server_id]
    doc['relayLinks']['pairs'] = [p for p in doc['relayLinks']['pairs'] if server_id not in (p['a'], p['b'])]
    doc['directLinks'] = [p for p in doc['directLinks'] if server_id not in (p['a'], p['b'])]
    rules = []
    for rule in doc['rules']:
        for side in ('src', 'dst'):
            rule[side]['members'] = [v for v in rule[side]['members'] if v != server_id]
        if any(rule['src'].values()) and any(rule['dst'].values()):
            rules.append(rule)
    doc['rules'] = rules
    return doc


def unstrip(doc, before, server_id):
    """What strip(before, server_id) took out, back into doc where what it needs is still there:
    an undone removal keeps the entry candidates, links and rule references it had."""
    present = {m['serverId'] for m in doc['members'] if not m['removing']}
    prior = {m['serverId']: m for m in before['members']}

    def fresh(used, prefix):
        n = 1
        while f'{prefix}{n}' in used:
            n += 1
        return f'{prefix}{n}'
    for m in doc['members']:
        old = prior.get(m['serverId'])
        if m['removing'] or not old:
            continue
        candidates = m['entry']['candidates']
        have, ids = {(c['relay'], c['transport']) for c in candidates}, {c['id'] for c in candidates}
        for i, c in enumerate(old['entry']['candidates']):
            if c['relay'] == server_id and (c['relay'], c['transport']) not in have:
                c = {**c, 'id': fresh(ids, 'c') if c['id'] in ids else c['id']}
                ids.add(c['id'])
                candidates.insert(min(i, len(candidates)), c)
    for links, old in ((doc['relayLinks']['pairs'], before['relayLinks']['pairs']), (doc['directLinks'], before['directLinks'])):
        pairs = {frozenset((x['a'], x['b'])) for x in links}
        for link in old:
            pair = frozenset((link['a'], link['b']))
            if server_id in pair and pair <= present and pair not in pairs:
                links.append(copy.deepcopy(link))
                pairs.add(pair)
    current = {r['id']: r for r in doc['rules']}
    for i, rule in enumerate(before['rules']):
        sides = [side for side in ('src', 'dst') if server_id in rule[side]['members']]
        if not sides:
            continue
        if rule['id'] in current:
            for side in sides:
                if server_id not in current[rule['id']][side]['members']:
                    current[rule['id']][side]['members'].append(server_id)
            continue
        back = copy.deepcopy(rule)
        for side in ('src', 'dst'):
            back[side]['members'] = [v for v in back[side]['members'] if v in present]
        doc['rules'].insert(min(i, len(doc['rules'])), back)
        current[back['id']] = back
    return doc


def prefix_overlaps(doc, facts):
    """(member, network): each live member with a network on its machine (an interface, its
    address, a route) overlapping the mesh prefix, the first such one."""
    net = ipaddress.ip_network(doc['settings']['prefix'])
    out = []
    for m in active(doc):
        f = facts.get(m['serverId']) or {}
        reported = [*(f.get('localPrefixes') or []), *([f['ipv4']] if f.get('ipv4') else []), *(r['net'] for r in f.get('hostRoutes') or [])]
        for raw in reported:
            try:
                other = ipaddress.ip_network(raw, strict=False)
            except ValueError:
                continue
            if other.version == 4 and other.overlaps(net):
                out.append((m['serverId'], other))
                break
    return out


def overlap_blocks(sid, doc, applied, applying=False):
    """Whether a member's overlap with the prefix is an error: always on the controller's machine
    (its own routes) and for a prefix being changed; for a member being added only when applying,
    so other edits still save meanwhile. Otherwise it is what a node says of itself, only warned of."""
    if applied is None or sid == LOCAL or doc['settings']['prefix'] != applied['settings']['prefix']:
        return True
    return applying and sid not in {m['serverId'] for m in active(applied)}


def _added(sid, doc, applied):
    return (applied is not None and sid != LOCAL and doc['settings']['prefix'] == applied['settings']['prefix']
            and sid not in {m['serverId'] for m in active(applied)})


def validate(doc, facts, *, applying=False, applied=None):
    """Every problem in one pass. applying adds what only matters when configs are built:
    the TLS fingerprints, which are fetched from relays at apply time. applied: the applied
    document, against which a member's overlap with the prefix is judged (overlap_blocks)."""
    errors = Errors()
    s = doc['settings']
    net, ctrl = ipaddress.ip_network(s['prefix']), ipaddress.ip_network(s['controlPrefix'])
    live = {m['serverId']: m for m in active(doc)}
    index = {m['serverId']: i for i, m in enumerate(doc['members'])}

    def name(sid):
        return (facts.get(sid) or {}).get('name') or sid
    for t in TRANSPORTS:
        if not _int(s['ports'][t], 1, 65535):
            errors.add(f'settings.ports.{t}', 'invalid_port', '端口须在 1～65535 之间')
    if len(set(s['ports'].values())) < len(TRANSPORTS):
        errors.add('settings.ports', 'port_duplicate', '三个默认端口不能相同')
    for sid, other in prefix_overlaps(doc, facts):
        if overlap_blocks(sid, doc, applied, applying):
            if _added(sid, doc, applied):
                errors.add(f'members[{index[sid]}]', 'prefix_conflict',
                           f'{name(sid)} 的本机网段 {other} 和组网地址段 {s["prefix"]} 重叠：移除它，或者换一个组网地址段')
            else:
                errors.add('settings.prefix', 'prefix_conflict', f'地址段和 {name(sid)} 的本机网段 {other} 冲突')

    addresses, controls = {}, {}
    for sid, m in live.items():
        p = f'members[{index[sid]}]'
        f = facts.get(sid) or {}
        if not f.get('publicKey'):
            errors.add(p, 'member_not_installed', f'{name(sid)} 还没有安装组网')
        ip = _ip(m['address'])
        if not m['address']:
            errors.add(f'{p}.address', 'address_exhausted', '地址段已用完')
        elif ip in (net.network_address, net.broadcast_address):
            errors.add(f'{p}.address', 'address_outside', f'不能用 {net} 的网络地址或广播地址')
        elif not ip or ip not in net or ip in ctrl:
            errors.add(f'{p}.address', 'address_outside', f'地址须在 {net} 内，且不在控制地址段 {ctrl} 内')
        elif m['address'] in addresses:
            errors.add(f'{p}.address', 'address_duplicate', f'地址和 {name(addresses[m["address"]])} 重复')
        else:
            addresses[m['address']] = sid
        cip = _ip(m['controlAddress'])
        if cip in (ctrl.network_address, ctrl.broadcast_address):
            errors.add(f'{p}.controlAddress', 'address_outside', f'不能用 {ctrl} 的网络地址或广播地址')
        elif not cip or cip not in ctrl:
            errors.add(f'{p}.controlAddress', 'address_outside', f'控制地址须在 {ctrl} 内')
        elif m['controlAddress'] in controls:
            errors.add(f'{p}.controlAddress', 'address_duplicate', f'控制地址和 {name(controls[m["controlAddress"]])} 重复')
        else:
            controls[m['controlAddress']] = sid
        linux = f.get('platform') == 'linux'
        if m['role'] == 'relay' and not linux:
            errors.add(f'{p}.role', 'relay_requires_linux', '中转只能是 Linux')
        if m['gateway'] and not linux:
            errors.add(f'{p}.gateway', 'gateway_requires_linux', '网关只能是 Linux')
        ports = [(t, v) for t, v in m['listen'].items() if v]
        seen = {}
        for t, v in ports:
            if v in seen:
                errors.add(f'{p}.listen.{t}', 'port_duplicate', f'和 {TRANSPORT_NAMES[seen[v]]} 端口相同')
            seen.setdefault(v, t)
        if m['role'] == 'relay' and not ports:
            errors.add(f'{p}.listen', 'relay_needs_listener', '中转至少要开启一种传输的监听')
        if m['role'] != 'relay' and m['listen']['tls']:
            errors.add(f'{p}.listen.tls', 'tls_relay_only', '只有中转可以开启 TLS 监听')
        if m['role'] == 'member':
            used = set()
            for j, c in enumerate(m['entry']['candidates']):
                q = f'{p}.entry.candidates[{j}]'
                relay = live.get(c['relay'])
                if not relay or relay['role'] != 'relay':
                    errors.add(q, 'candidate_not_relay', '入口候选必须是中转')
                elif is_site(relay):
                    _check_site_entry(m, relay, c, q, errors, name)
                elif not relay['listen'][c['transport']]:
                    errors.add(q, 'transport_not_listening', f'{name(c["relay"])} 没有开启 {TRANSPORT_NAMES[c["transport"]]} 监听')
                elif not endpoint_host(relay, facts):
                    errors.add(q, 'endpoint_missing', f'{name(c["relay"])} 没有可用的对外地址')
                elif applying and c['transport'] == 'tls' and not _pins(facts, c['relay']):
                    errors.add(q, 'tls_fingerprint_missing', f'还没有取到 {name(c["relay"])} 的 TLS 证书指纹，节点需要在线')
                if (c['relay'], c['transport']) in used:
                    errors.add(q, 'candidate_duplicate', '入口候选重复')
                used.add((c['relay'], c['transport']))
            kinds = {is_site(live[c['relay']]) for c in m['entry']['candidates'] if live.get(c['relay'], {}).get('role') == 'relay'}
            if True in kinds and False not in kinds:
                errors.add(f'{p}.entry.candidates', 'mesh_entry_needs_bootstrap',
                           '入口候选里有站点中转时，还必须有一个公网中转候选（经它交换打洞地址，也作备用入口）')

    _check_schemas(live, facts, index, errors, name)

    gateways = []
    for sid, m in live.items():
        for k, raw in enumerate((m['gateway'] or {}).get('subnets', [])):
            q = f'members[{index[sid]}].gateway.subnets[{k}]'
            subnet = ipaddress.ip_network(raw)
            if subnet.overlaps(net):
                errors.add(q, 'gateway_overlaps_mesh', f'网关网段不能和网格地址段 {net} 重叠')
            for other, owner in gateways:
                if subnet.overlaps(other):
                    errors.add(q, 'gateway_overlap', f'和 {name(owner)} 的网段 {other} 重叠')
                    break
            gateways.append((subnet, sid))

    relays = [sid for sid, m in live.items() if m['role'] == 'relay']
    seen = set()
    for i, pair in enumerate(doc['relayLinks']['pairs']):
        q = f'relayLinks.pairs[{i}]'
        key = frozenset((pair['a'], pair['b']))
        if pair['a'] == pair['b'] or not all(live.get(x, {}).get('role') == 'relay' for x in key):
            errors.add(q, 'link_not_relays', '中转之间的连接两端都必须是中转')
        elif key in seen:
            errors.add(q, 'link_duplicate', '连接重复')
        else:
            _check_relay_link(effective(pair, live), live, facts, q, errors, name, applying)
        seen.add(key)
    pairs = relay_pairs(doc, facts)
    if doc['relayLinks']['mode'] == 'full':
        for pair in pairs:
            if frozenset((pair['a'], pair['b'])) not in seen:
                _check_relay_link(pair, live, facts, 'relayLinks', errors, name, applying)
    for sid in relays:
        if is_site(live[sid]) and not any(sid in (pair['a'], pair['b']) and not is_site(live[pair['b'] if pair['a'] == sid else pair['a']])
                                          for pair in pairs):
            errors.add(f'members[{index[sid]}].relayMode', 'mesh_site_relay_needs_public_link',
                       f'站点中转 {name(sid)} 至少要连一台公网中转（作打洞信令通道）')
    hops = next_hops(relays, pairs)
    for a in relays:
        missing = [b for b in relays if b != a and b not in hops[a]]
        if missing:
            errors.add('relayLinks', 'relay_unreachable', f'{name(a)} 和 {name(missing[0])} 之间没有中转连接路径')
            break

    seen = set()
    for i, pair in enumerate(doc['directLinks']):
        q = f'directLinks[{i}]'
        key = frozenset((pair['a'], pair['b']))
        if pair['a'] == pair['b'] or not all(live.get(x, {}).get('role') == 'member' for x in key):
            errors.add(q, 'direct_not_members', '直连的两端都必须是普通成员')
        elif key in seen:
            errors.add(q, 'link_duplicate', '直连重复')
        elif pair['transport'] == 'udp':
            _check_punch(pair, live, facts, q, errors)
        else:
            _check_dial(pair, live, facts, q, errors, name, applying)
        seen.add(key)

    tags = {t for m in live.values() for t in m['tags']}
    for i, rule in enumerate(doc['rules']):
        q = f'rules[{i}]'
        for side in ('src', 'dst'):
            for k, sid in enumerate(rule[side]['members']):
                if sid not in live:
                    errors.add(f'{q}.{side}.members[{k}]', 'rule_member_unknown', '不是网格成员')
            for k, tag in enumerate(rule[side]['tags']):
                if tag not in tags:
                    errors.add(f'{q}.{side}.tags[{k}]', 'rule_tag_unknown', '没有成员带这个标签')
        for k, raw in enumerate(rule['dst']['subnets']):
            if not any(ipaddress.ip_network(raw).subnet_of(g) for g, _ in gateways):
                errors.add(f'{q}.dst.subnets[{k}]', 'rule_subnet_not_gateway', '目标网段须在某个网关宣告的网段内')
        if not any(rule['src'].values()):
            errors.add(f'{q}.src', 'rule_src_empty', '来源不能为空')
        if not any(rule['dst'].values()):
            errors.add(f'{q}.dst', 'rule_dst_empty', '目标不能为空')
        if rule['ports'] and rule['proto'] not in ('tcp', 'udp'):
            errors.add(f'{q}.ports', 'rule_ports_need_proto', '只有 TCP 和 UDP 规则可以限定端口')
    return errors


def hints(doc, facts, conflicts=None, applied=None):
    """Warnings that block neither saving nor applying: punched links whose two ends both
    report a symmetric NAT (facts[sid]['verdict'], from the node's reachability check), and
    members behind one NAT listening on the same UDP port (mesh_udp_port_shared). conflicts:
    mesh_config.route_conflicts(doc, facts) when the caller has it already. With applied, a
    member's overlap with the prefix that does not block (overlap_blocks)."""
    out = Errors()
    live = {m['serverId']: m for m in active(doc)}
    index = {m['serverId']: i for i, m in enumerate(doc['members'])}

    def name(sid):
        return (facts.get(sid) or {}).get('name') or sid
    if applied is not None:
        for sid, other in prefix_overlaps(doc, facts):
            if _added(sid, doc, applied):
                out.add(f'members[{index[sid]}]', 'mesh_prefix_overlap',
                        f'{name(sid)} 的本机网段 {other} 和组网地址段 {doc["settings"]["prefix"]} 重叠，应用时会被拦下：'
                        f'移除它，或者换一个组网地址段。')
            elif not overlap_blocks(sid, doc, applied):
                out.add(f'members[{index[sid]}]', 'mesh_prefix_overlap',
                        f'{name(sid)} 上报本机网段 {other}，和组网地址段 {doc["settings"]["prefix"]} 重叠：它访问这段地址可能走错路，'
                        f'其他成员照常。确有其事时请换一个组网地址段，或调整那台机器的网络。')
        # Instances listening on a mesh address the draft takes from their server, or whose gateway's server leaves.
        kept = {(a['serverId'], a['instanceId']) for a in mesh_config.instance_access(doc, facts)}
        for a in mesh_config.instance_access(applied, facts):
            sid, gw = a['serverId'], a['gateway']
            if (sid, a['instanceId']) in kept:
                continue
            what = f'实例「{a["name"]}」（端口 {a["port"]}）'
            if sid not in live:
                text = f'{name(sid)} 移出组网后，网关访问不到它上面的{what}，实例会无法使用。'
            elif gw not in live:
                text = f'网关所在的 {name(gw)} 移出组网后，网关访问不到 {name(sid)} 上的{what}。'
            else:
                text = f'{name(sid)} 的组网地址改了，它上面的{what}还监听在原地址，网关会访问不到它：应用后到实例页点「迁移网络」，让实例改到新地址。'
            out.add(f'members[{index[sid]}]' if sid in index else 'settings.prefix', 'mesh_instance_cut', text)

    def check(q, a, b, outcome):
        if all((facts.get(x) or {}).get('verdict') == 'symmetric' for x in (a, b)) and not same_lan(a, b, facts):
            out.add(q, 'mesh_punch_impossible', f'{name(a)} 和 {name(b)} 都在对称型 NAT 后面，打洞基本不会成功，{outcome}')
    for i, pair in enumerate(doc['directLinks']):
        if pair['transport'] == 'udp' and pair['a'] != pair['b'] and all(live.get(x, {}).get('role') == 'member' for x in (pair['a'], pair['b'])):
            check(f'directLinks[{i}]', pair['a'], pair['b'], '这条直连会一直经中转')
    listed = {frozenset((p['a'], p['b'])): i for i, p in reversed(list(enumerate(doc['relayLinks']['pairs'])))}
    for pair in relay_pairs(doc, facts):
        if pair.get('punch'):
            i = listed.get(frozenset((pair['a'], pair['b'])))
            check('relayLinks' if i is None else f'relayLinks.pairs[{i}]', pair['a'], pair['b'], '这条中转链路很可能建不起来')
    for sid, m in live.items():
        if m['role'] == 'member':
            for j, c in enumerate(m['entry']['candidates']):
                if is_site(live.get(c['relay']) or {'role': ''}):
                    check(f'members[{index[sid]}].entry.candidates[{j}]', sid, c['relay'], '这个入口候选很可能用不上')
    for first, sid, port, suggested in shared_udp_ports(doc, facts):
        out.add(f'members[{index[sid]}].listen.udp', 'mesh_udp_port_shared',
                f'{name(first)} 和 {name(sid)} 在同一个 NAT 后，都用 UDP {port}。部分路由器（尤其带代理的旁路由）会因此丢包，'
                f'建议把 {name(sid)} 改成 {suggested}。')
    for sid, skipped in (mesh_config.route_conflicts(doc, facts) if conflicts is None else conflicts).items():
        for route, held, dev in skipped:
            net = ipaddress.ip_network(route)
            gateway = next((g for g in live.values() if any(net.overlaps(ipaddress.ip_network(s)) for s in (g['gateway'] or {}).get('subnets', []))), None)
            behind = f'{name(gateway["serverId"])} 的网关网段 ' if gateway else '网段 '
            via = f'（经 {dev}）' if dev else ''
            out.add(f'members[{index[sid]}]', 'mesh_gateway_route_conflict',
                    f'{name(sid)} 本机已有 {held} 的路由{via}，和{behind}{route} 重叠。组网不在 {name(sid)} 上装 {route} 的路由，'
                    f'它访问这个网段仍走原来的路由，其他组网流量照常。')
    for sid in live:
        by = {}
        for field, other in ((facts.get(sid) or {}).get('keyConflict') or {}).items():
            by.setdefault(other, []).append(KEY_NAMES.get(field, field))
        for other, keys in by.items():
            out.add(f'members[{index[sid]}]', 'mesh_key_conflict',
                    f'{name(sid)} 上报的{"、".join(keys)}和 {name(other)} 的相同，主控没有采用。它多半是从 {name(other)} 克隆出来的，'
                    f'请在 {name(sid)} 上删掉组网目录里的 keys 后重新安装组网服务；{name(other)} 和其他成员照常。')
    ours = mesh_config.SCHEMA
    for sid in sorted(mesh_config.keyless(list(live.values()), facts), key=index.get):
        # One whose data plane cannot read the configs is named by the schema check instead, one
        # that reported another's keys by the warning above.
        r = schemas_of(facts, sid)
        f = facts.get(sid) or {}
        if not all(mesh_config._key32(f.get(k)) for k in ('publicKey', 'controlKey')):
            out.add(f'members[{index[sid]}]', 'mesh_keys_invalid',
                    f'{name(sid)} 记录的组网公钥或控制密钥不是 32 字节的密钥：其余成员的配置都不包含它，它连不上组网，'
                    f'其他成员照常。请检查这台机器，再对它「重装组网」。')
        elif r[0] <= ours[1] and ours[0] <= r[1] and not f.get('keyConflict'):
            out.add(f'members[{index[sid]}]', 'mesh_keys_missing',
                    f'{name(sid)} 还没有上报签名密钥和端到端密钥：在它上报之前，其余成员的配置都不包含它，它暂时连不上组网，'
                    f'其他成员照常。它上线后会自动补上。')
    return out


def subnet_suggestions(f, prefix):
    """The LAN networks a member's machine is on (its node's localPrefixes), as gateway subnets
    it could announce: private (RFC 1918), not a default container bridge, not a single address,
    not overlapping the mesh prefix."""
    mesh = ipaddress.ip_network(prefix)
    out = []
    for net in _lans(f):
        if net.prefixlen <= 30 and not net.overlaps(mesh) and not any(net.overlaps(b) for b in BRIDGE_NETS):
            out.append(str(net))
    return list(dict.fromkeys(out))


# ----- machines behind one NAT -----


def _parse_lans(prefixes):
    """(networks, spans): the private networks among prefixes, and (first, last, prefixlen, inside a
    default container bridge) of each, for _shared."""
    nets = []
    for raw in prefixes:
        try:
            net = ipaddress.ip_network(raw, strict=False)
        except (TypeError, ValueError):
            continue
        if net.version == 4 and any(net.subnet_of(lan) for lan in LAN_NETS):
            nets.append(net)
    spans = tuple((int(n.network_address), int(n.broadcast_address), n.prefixlen, any(n.subnet_of(b) for b in BRIDGE_NETS)) for n in nets)
    return tuple(nets), spans


# Parsed once per set of prefixes: every check of every pair of members asks again.
_known_lans = functools.lru_cache(maxsize=4096)(_parse_lans)


def _lan_info(f):
    raw = f.get('localPrefixes') or ()
    try:
        return _known_lans(tuple(raw))
    except TypeError:  # an unhashable entry (never a prefix): parsed without the cache
        return _parse_lans(raw)


def _lans(f):
    """The private (RFC 1918) networks among a node's interface prefixes."""
    return list(_lan_info(f)[0])


def _shared(xs, ys):
    """Whether any of two nodes' private networks (as _known_lans spans) overlap outside the default
    container bridges. Two networks overlap only when one holds the other, so their common part is
    the smaller one."""
    for x_first, x_last, x_len, x_bridge in xs:
        for y_first, y_last, y_len, y_bridge in ys:
            if x_first <= y_last and y_first <= x_last and not (x_bridge if x_len >= y_len else y_bridge):
                return True
    return False


def same_nat(a, b, facts):
    """Two nodes are taken to share a NAT when their reachability checks saw the same public
    IP (facts[sid]['natIp']), or when they have overlapping private networks (default
    container bridges aside)."""
    fa, fb = facts.get(a) or {}, facts.get(b) or {}
    if fa.get('natIp') and fa.get('natIp') == fb.get('natIp'):
        return True
    return _shared(_lan_info(fa)[1], _lan_info(fb)[1])


def same_lan(a, b, facts):
    """Two nodes on one private network reach each other over it whatever their NATs do. A
    known public IP that differs says the two networks only share their numbers."""
    fa, fb = facts.get(a) or {}, facts.get(b) or {}
    if fa.get('natIp') and fb.get('natIp') and fa['natIp'] != fb['natIp']:
        return False
    return _shared(_lan_info(fa)[1], _lan_info(fb)[1])


def _nat_neighbours(sids, facts):
    """For each of sids (by index), the indexes of the others same_nat pairs it with, in order:
    those seen at the same public IP by group, then the pairs among those with private networks."""
    near = [set() for _ in sids]
    groups = {}
    for i, sid in enumerate(sids):
        nat = (facts.get(sid) or {}).get('natIp')
        if nat:
            groups.setdefault(nat, []).append(i)
    for group in groups.values():
        for i in group:
            near[i].update(group)
    spans = [_lan_info(facts.get(sid) or {})[1] for sid in sids]
    lan = [i for i, s in enumerate(spans) if s]
    for k, i in enumerate(lan):
        for j in lan[k + 1:]:
            if j not in near[i] and _shared(spans[i], spans[j]):
                near[i].add(j)
                near[j].add(i)
    return [sorted(n - {i}) for i, n in enumerate(near)]


def _behind_nat(member):
    """Public relays are reached at their own address and take no part in this."""
    return member['role'] != 'relay' or is_site(member)


def free_udp_port(member, others, settings):
    """The first UDP port from settings.ports.udp downwards (then upwards) that none of
    others (ports in use behind the same NAT) holds and member's own TCP/TLS do not use."""
    taken = set(others) | {member['listen']['tcp'], member['listen']['tls']}
    start = settings['ports']['udp']
    return next((p for p in itertools.chain(range(start, 0, -1), range(start + 1, 65536)) if p not in taken), 0)


def nat_udp_ports(doc, member, facts):
    """UDP ports the other members behind member's NAT listen on."""
    return {m['listen']['udp'] for m in active(doc)
            if m['serverId'] != member['serverId'] and m['listen']['udp'] and _behind_nat(m) and same_nat(m['serverId'], member['serverId'], facts)}


def shared_udp_ports(doc, facts):
    """(first, later, port, suggested) for each member whose UDP port an earlier member behind
    the same NAT already uses. Suggestions are made in member order as if each earlier one
    were taken, so following all of them leaves no two on one port."""
    out, suggested = [], {}
    behind = [m for m in active(doc) if m['listen']['udp'] and _behind_nat(m)]
    near = _nat_neighbours([m['serverId'] for m in behind], facts)
    for i, m in enumerate(behind):
        sid, port = m['serverId'], m['listen']['udp']
        peers = [(j, behind[j]) for j in near[i]]
        first = next((x['serverId'] for j, x in peers if j < i and x['listen']['udp'] == port), None)
        if first is not None:
            taken = {x['listen']['udp'] for _, x in peers} | {suggested[x['serverId']] for _, x in peers if x['serverId'] in suggested}
            suggested[sid] = free_udp_port(m, taken, doc['settings'])
            out.append((first, sid, port, suggested[sid]))
    return out


def _pins(facts, sid):
    tls = (facts.get(sid) or {}).get('tls') or {}
    return (tls.get('active') or {}).get('fingerprint')


# Members whose data plane has to be updated (or the controller): refused at apply, not when the draft is saved.
SCHEMA_ERRORS = frozenset({'config_schema_node_outdated', 'config_schema_controller_outdated', 'config_schema_mismatch'})


def schema_span(r):
    return str(r[0]) if r[0] == r[1] else f'{r[0]}～{r[1]}'


def _check_schemas(live, facts, index, errors, name):
    """Every member's data plane must read a config schema this controller writes, and one
    the others read too: all configs of a revision share it."""
    ours, blamed = mesh_config.SCHEMA, False
    for sid in live:
        r = schemas_of(facts, sid)
        if r[1] < ours[0]:
            errors.add(f'members[{index[sid]}]', 'config_schema_node_outdated',
                       f'{name(sid)} 的组网数据面只读配置格式 {schema_span(r)}，主控写的是 {schema_span(ours)}：请先更新这台的节点')
        elif r[0] > ours[1]:
            errors.add(f'members[{index[sid]}]', 'config_schema_controller_outdated',
                       f'{name(sid)} 的组网数据面要求配置格式 {schema_span(r)}，主控只会写 {schema_span(ours)}：请先更新主控')
        else:
            continue
        blamed = True
    if blamed or not live or config_schema({'members': list(live.values())}, facts) is not None:
        return
    # Each reads one the controller writes, but not a common one: the oldest is behind.
    oldest = min(live, key=lambda sid: (schemas_of(facts, sid)[1], index[sid]))
    newest = max(live, key=lambda sid: (schemas_of(facts, sid)[0], -index[sid]))
    errors.add(f'members[{index[oldest]}]', 'config_schema_mismatch',
               f'{name(oldest)} 的组网数据面只读配置格式 {schema_span(schemas_of(facts, oldest))}，{name(newest)} 的要求 '
               f'{schema_span(schemas_of(facts, newest))}，没有共同的格式：请先更新 {name(oldest)} 的节点')


def _check_punch(pair, live, facts, q, errors):
    """A UDP direct link is punched from both ends, so neither needs a public address and
    the dialer is not used; without one, a relay with UDP must show each end its own."""
    a, b = live[pair['a']], live[pair['b']]
    if not (a['listen']['udp'] and b['listen']['udp']):
        errors.add(q, 'udp_not_listening', '两端都要开启 UDP 监听才能直连')
    elif not (punch_host(a, facts) or punch_host(b, facts) or reflectors(live.values(), facts)):
        errors.add(q, 'punch_no_reflector', '没有可用来打洞的中转（需要至少一台开启 UDP 监听的中转）')


def _check_relay_link(pair, live, facts, q, errors, name, applying):
    if not pair.get('punch'):
        _check_dial(pair, live, facts, q, errors, name, applying)
    elif pair['transport'] != 'udp':
        errors.add(q, 'punch_needs_udp', f'{name(pair["a"])} 和 {name(pair["b"])} 都是站点中转，只能经 UDP 打洞连接，传输方式请选 UDP')
    elif not (live[pair['a']]['listen']['udp'] and live[pair['b']]['listen']['udp']):
        errors.add(q, 'udp_not_listening', '两台站点中转都要开启 UDP 监听才能打洞连接')


def _check_site_entry(member, relay, c, q, errors, name):
    """A member reaches a site relay by punching from its UDP socket to the relay's."""
    if c['transport'] != 'udp':
        errors.add(q, 'punch_needs_udp', f'{name(relay["serverId"])} 是站点中转，只能经 UDP 打洞接入，传输方式请选 UDP')
    elif not relay['listen']['udp']:
        errors.add(q, 'transport_not_listening', f'{name(relay["serverId"])} 没有开启 UDP 监听')
    elif not member['listen']['udp']:
        errors.add(q, 'udp_not_listening', '要开启 UDP 监听才能经打洞接入站点中转')


def _check_dial(pair, live, facts, q, errors, name, applying):
    dialer, target = (pair['a'], pair['b']) if pair['dialer'] == 'a' else (pair['b'], pair['a'])
    member = live[target]
    if not member['listen'][pair['transport']]:
        errors.add(q, 'transport_not_listening', f'{name(target)} 没有开启 {TRANSPORT_NAMES[pair["transport"]]} 监听，{name(dialer)} 无法连接')
    elif not can_dial(member, pair['transport'], facts):
        errors.add(q, 'endpoint_missing', f'{name(dialer)} 拿不到 {name(target)} 的对外地址')
    elif applying and pair['transport'] == 'tls' and not _pins(facts, target):
        errors.add(q, 'tls_fingerprint_missing', f'还没有取到 {name(target)} 的 TLS 证书指纹，节点需要在线')
