"""延迟监测: each node probes a few targets on an interval and sends back every finished minute (sent,
lost, average/min/max), kept in the metrics file. Targets come from a library: the three carriers of
each province (Zstatic CDN's directory, refreshed daily with a copy built in), addresses built in (DNS,
AI platforms, common services and sites, in China and abroad) and the user's own. Three default lists
say what a server measures by where it is: in China, abroad on an optimised return, abroad on ordinary
lines only; a server can have its own list, which may name other servers (their public address as it is
when sent) and which of its targets its card shows. The carriers and the built-in addresses are probed
the settings' way (TCPing or ICMP); the user's own and other servers each their own way, HTTP among them
where the node has latency.http. A front reaches nothing but its exits, so it measures those alone. Like
the DDNS probes the node keeps its targets in memory only: they are sent again on every hello."""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import re
import secrets
import time
from urllib.parse import urlsplit

from app.instances.contracts import InstanceError
from app.instances.servers import LOCAL
from app.servers import front as fronts
from app.servers.checks import DOMESTIC, server_region
from app.servers.metrics import RANGES
from app.servers.route import retier
from app.webhooks.repository import many, one

log = logging.getLogger(__name__)

FEATURE = 'latency'
HTTP_FEATURE = 'latency.http'
MODES = ('tcp', 'icmp', 'http')
MAX_URL = 2048
DIRECTORY_URL = 'https://www.zstaticcdn.com/api/v1/DescribeAllNodes'
REFRESH_S = 86400
PUSHES = 8
# Minutes a node kept while it had no session are taken back to this far.
KEPT_S = 2 * 86400
# The directory's provinces as built in (2026-09): name and the code its hosts start with.
PROVINCES = (('北京', 'bj'), ('天津', 'tj'), ('上海', 'sh'), ('重庆', 'cq'), ('河北', 'he'), ('山西', 'sx'), ('内蒙古', 'nm'),
             ('辽宁', 'ln'), ('吉林', 'jl'), ('黑龙江', 'hl'), ('江苏', 'js'), ('浙江', 'zj'), ('安徽', 'ah'), ('福建', 'fj'),
             ('江西', 'jx'), ('山东', 'sd'), ('河南', 'ha'), ('湖北', 'hb'), ('湖南', 'hn'), ('广东', 'gd'), ('广西', 'gx'),
             ('海南', 'hi'), ('四川', 'sc'), ('贵州', 'gz'), ('云南', 'yn'), ('西藏', 'xz'), ('陕西', 'sn'), ('甘肃', 'gs'),
             ('青海', 'qh'), ('宁夏', 'nx'), ('新疆', 'xj'))
CARRIERS = (('ct', '电信', 'telecom'), ('cu', '联通', 'unicom'), ('cm', '移动', 'mobile'))
# Built-in targets: id -> (label, host, TCP port, group, kind). DNS answers TCP on 53, the rest on 443.
# Most of the platforms sit behind a CDN: a probe times the CDN's nearest edge, which is the line to them.
EXTRAS = {
    'ali-dns': ('阿里 DNS', '223.5.5.5', 53, 'cn', 'dns'), 'tencent-dns': ('腾讯 DNS', '119.29.29.29', 53, 'cn', 'dns'),
    'baidu-dns': ('百度 DNS', '180.76.76.76', 53, 'cn', 'dns'), '114-dns': ('114 DNS', '114.114.114.114', 53, 'cn', 'dns'),
    'deepseek': ('DeepSeek', 'api.deepseek.com', 443, 'cn', 'ai'), 'qwen': ('通义千问', 'dashscope.aliyuncs.com', 443, 'cn', 'ai'),
    'doubao': ('豆包', 'ark.cn-beijing.volces.com', 443, 'cn', 'ai'), 'kimi': ('Kimi', 'api.moonshot.cn', 443, 'cn', 'ai'),
    'zhipu': ('智谱', 'open.bigmodel.cn', 443, 'cn', 'ai'),
    'baidu': ('百度', 'www.baidu.com', 443, 'cn', 'site'), 'qq': ('腾讯', 'www.qq.com', 443, 'cn', 'site'), 'aliyun': ('阿里云', 'www.aliyun.com', 443, 'cn', 'site'),
    'bilibili': ('哔哩哔哩', 'www.bilibili.com', 443, 'cn', 'site'), 'douyin': ('抖音', 'www.douyin.com', 443, 'cn', 'site'),
    'jd': ('京东', 'www.jd.com', 443, 'cn', 'site'), 'huaweicloud': ('华为云', 'www.huaweicloud.com', 443, 'cn', 'site'),
    'cloudflare': ('Cloudflare', '1.1.1.1', 443, 'intl', 'dns'), 'google': ('Google DNS', '8.8.8.8', 443, 'intl', 'dns'),
    'quad9': ('Quad9', '9.9.9.9', 443, 'intl', 'dns'),
    'openai': ('OpenAI', 'api.openai.com', 443, 'intl', 'ai'), 'chatgpt': ('ChatGPT', 'chatgpt.com', 443, 'intl', 'ai'),
    'claude': ('Claude', 'api.anthropic.com', 443, 'intl', 'ai'), 'gemini': ('Gemini', 'generativelanguage.googleapis.com', 443, 'intl', 'ai'),
    'grok': ('Grok', 'api.x.ai', 443, 'intl', 'ai'),
    'github': ('GitHub', 'github.com', 443, 'intl', 'service'), 'docker': ('Docker Hub', 'registry-1.docker.io', 443, 'intl', 'service'),
    'telegram': ('Telegram', 'api.telegram.org', 443, 'intl', 'service'), 'youtube': ('YouTube', 'www.youtube.com', 443, 'intl', 'service'),
    'netflix': ('Netflix', 'www.netflix.com', 443, 'intl', 'service'),
}
# 'three' in a default list: the province's three carriers (abroad, those its return is not plainly ordinary on).
THREE = 'three'
INTL = ['cloudflare', 'google', 'openai', 'claude', 'gemini', 'github', 'telegram']
GROUPS = ('domestic', 'optimized', 'plain')
DEFAULTS = {'enabled': True, 'province': '云南', 'intervalS': 10, 'method': 'tcp', 'custom': [], 'card': 'auto',
            'defaults': {'domestic': [THREE, 'ali-dns', 'tencent-dns'], 'optimized': [THREE, *INTL], 'plain': list(INTL)}}
# What the list's latency is worked out over: the carriers when measured (else the rest), or always the one or the other, or all.
CARDS = ('auto', 'three', 'other', 'all')
MIN_INTERVAL_S, MAX_INTERVAL_S = 3, 600
MAX_TARGETS = 16
MAX_CUSTOM = 32
CUSTOM_ID = re.compile(r'^u-[0-9a-f]{8}$')
PROVINCE_ID = re.compile(r'^[a-z]{2}-(ct|cu|cm)$')
# Another server: s:<id> by ICMP, s:<id>:tcp:<port>, s:<id>:http:<port>; at its public IPv4 (else IPv6), or
# with @v6 its public IPv6, with @mesh its mesh address (s:<id>@mesh:tcp:22).
SERVER_ID = re.compile(r'^s:([A-Za-z0-9-]{1,40})(?:@(v6|mesh))?(?::(tcp|http):(\d{1,5}))?$')
VIA_NAMES = {'v6': 'IPv6', 'mesh': '组网'}
HOST = re.compile(r'^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$')
CARRIER_IDS = {c for c, _, _ in CARRIERS}
CODES = {code: name for name, code in PROVINCES}


def builtin_directory():
    return {code: {c: f'{code}-{c}-v4.ip.zstaticcdn.com:80' for c, _, _ in CARRIERS} for _, code in PROVINCES}


def parse_directory(data):
    """Zstatic's {ProvinceNodes: [{province, carriers: {telecom, unicom, mobile}}]} as {code: {ct, cu, cm}};
    a province or carrier the answer leaves out keeps the built-in one."""
    out = builtin_directory()
    names = {name: code for name, code in PROVINCES}
    for node in (data or {}).get('ProvinceNodes') or []:
        if not isinstance(node, dict) or node.get('province') not in names or not isinstance(node.get('carriers'), dict):
            continue
        code = names[node['province']]
        for carrier, _, key in CARRIERS:
            endpoint = node['carriers'].get(key)
            if isinstance(endpoint, str) and _endpoint(endpoint):
                out[code][carrier] = endpoint
    return out


def _endpoint(value):
    """host:port with a host name or an IPv4 address, else None."""
    host, _, port = value.rpartition(':')
    return (host, int(port)) if host and port.isdigit() and 0 < int(port) < 65536 and HOST.match(host) else None


def _migrate(prior):
    """Settings from before the library: cn and abroad were the extras of servers in and out of China.
    The old built-in pair abroad becomes today's defaults; a list of one's own is kept."""
    prior = dict(prior or {})
    cn, abroad = prior.pop('cn', None), prior.pop('abroad', None)
    if 'defaults' not in prior and (cn is not None or abroad is not None):
        intl = list(INTL) if abroad in (None, ['cloudflare', 'google']) else list(abroad)
        domestic = list(cn) if cn is not None else DEFAULTS['defaults']['domestic'][1:]
        prior['defaults'] = {'domestic': [THREE, *domestic], 'optimized': [THREE, *intl], 'plain': intl}
    return prior


def _clean_custom(items):
    """The user's own addresses: [{id?, label, host, port, group, mode?, url?}]; one without an id is new and
    gets one. Without a mode it is probed the settings' way, so it has a port; icmp needs none, http a URL
    (its host and port are the URL's)."""
    if not isinstance(items, list) or len(items) > MAX_CUSTOM:
        raise InstanceError('invalid_latency_custom')
    out, ids = [], set()
    for item in items:
        if not isinstance(item, dict) or set(item) - {'id', 'label', 'host', 'port', 'group', 'mode', 'url'}:
            raise InstanceError('invalid_latency_custom')
        label = item.get('label').strip() if isinstance(item.get('label'), str) else ''
        mode, group = item.get('mode'), item.get('group')
        if mode is not None and mode not in MODES:
            raise InstanceError('invalid_latency_custom')
        if mode == 'http':
            url, host, port = clean_url(item.get('url'))
        else:
            url, host = '', item.get('host').strip().lower() if isinstance(item.get('host'), str) else ''
            port = item.get('port') if mode != 'icmp' else item.get('port') or 0
            if type(port) is not int or not (0 < port < 65536 or mode == 'icmp' and port == 0):
                raise InstanceError('invalid_latency_custom')
        if not 0 < len(label) <= 24 or not (HOST.match(host) or _ip(host) or _ip6(host)) or _private(host) or group not in ('cn', 'intl'):
            raise InstanceError('invalid_latency_custom')
        target = item.get('id') or f'u-{secrets.token_hex(4)}'
        if not isinstance(target, str) or not CUSTOM_ID.match(target) or target in ids:
            raise InstanceError('invalid_latency_custom')
        ids.add(target)
        out.append({'id': target, 'label': label, 'host': host, 'port': port, 'group': group, **({'mode': mode} if mode else {}), **({'url': url} if url else {})})
    return out


def clean_url(value):
    """(url, host, port) of an http(s) address to time, or invalid_latency_custom."""
    parts = urlsplit(value.strip()) if isinstance(value, str) and len(value.strip()) <= MAX_URL else None
    try:
        port = parts.port if parts else None
    except ValueError:
        parts = None
    host = (parts.hostname or '').lower() if parts else ''
    if not parts or parts.scheme not in ('http', 'https') or parts.username or parts.password or not (HOST.match(host) or _ip(host) or _ip6(host)) or _private(host):
        raise InstanceError('invalid_latency_custom')
    return parts.geturl(), host, port or (443 if parts.scheme == 'https' else 80)


def _ip(host):
    with contextlib.suppress(ValueError):
        return ipaddress.ip_address(host).version == 4
    return False


def _ip6(host):
    with contextlib.suppress(ValueError):
        return ipaddress.ip_address(host).version == 6
    return False


def clean_settings(data, prior=None):
    value = {**DEFAULTS, **_migrate(prior)}
    value['defaults'] = {**DEFAULTS['defaults'], **value['defaults']}
    if not isinstance(data, dict) or set(data) - set(DEFAULTS):
        raise InstanceError('invalid_latency_settings')
    if 'enabled' in data:
        if not isinstance(data['enabled'], bool):
            raise InstanceError('invalid_latency_settings')
        value['enabled'] = data['enabled']
    if 'province' in data:
        if data['province'] not in {name for name, _ in PROVINCES}:
            raise InstanceError('invalid_latency_province')
        value['province'] = data['province']
    if 'intervalS' in data:
        if type(data['intervalS']) is not int or not MIN_INTERVAL_S <= data['intervalS'] <= MAX_INTERVAL_S:
            raise InstanceError('invalid_latency_interval')
        value['intervalS'] = data['intervalS']
    if 'method' in data:
        if data['method'] not in ('tcp', 'icmp'):
            raise InstanceError('invalid_latency_settings')
        value['method'] = data['method']
    if 'card' in data:
        if data['card'] not in CARDS:
            raise InstanceError('invalid_latency_settings')
        value['card'] = data['card']
    if 'custom' in data:
        value['custom'] = _clean_custom(data['custom'])
    known = {THREE, *EXTRAS, *(c['id'] for c in value['custom'])}
    if 'defaults' in data:
        given = data['defaults']
        if not isinstance(given, dict) or set(given) - set(GROUPS):
            raise InstanceError('invalid_latency_targets')
        for group, ids in given.items():
            if not isinstance(ids, list) or len(ids) > MAX_TARGETS or len(set(ids)) != len(ids) \
                    or any(not isinstance(i, str) or i not in known and not _province(i) for i in ids):
                raise InstanceError('invalid_latency_targets')
            value['defaults'][group] = list(ids)
    # An address of one's own taken out goes from the lists too.
    value['defaults'] = {g: [i for i in ids if i in known or _province(i)] for g, ids in value['defaults'].items()}
    return value


def _province(target):
    code, _, carrier = target.partition('-')
    return code in CODES and carrier in CARRIER_IDS


def library(settings):
    """id -> (label, host, port, group, kind, mode, url) of every address there is to measure but the provinces'
    carriers and the servers; mode None for the settings' way."""
    return {**{k: (*v, None, '') for k, v in EXTRAS.items()},
            **{c['id']: (c['label'], c['host'], c.get('port') or 0, c['group'], 'custom', c.get('mode'), c.get('url') or '') for c in settings.get('custom') or []}}


def server_target(target):
    """(server id, mode, port, via) of another server's target, else None; via '', 'v6' or 'mesh'."""
    m = SERVER_ID.match(target) if isinstance(target, str) else None
    if not m or m.group(3) and not 0 < int(m.group(4)) < 65536:
        return None
    return m.group(1), m.group(3) or 'icmp', int(m.group(4) or 0), m.group(2) or ''


def server_address(info, via=''):
    """Where another server's target points, by its node's info (meshAddress added for a mesh member)."""
    if via == 'v6':
        return str(info.get('ipv6') or '')
    if via == 'mesh':
        return str(info.get('meshAddress') or '')
    return str(info.get('ipv4') or info.get('ipv6') or '')


def clean_targets(value):
    """A server's own {targets?: [id], card?: [id]}: targets left out follow the defaults; card names the
    ones its card shows (none: the settings' rule). None for neither. An id is a province's carrier
    (yn-ct), a built-in target (ali-dns), an address of the user's library (u-…), another server
    (s:…), or c:host:port. A library address or a server taken out later is passed over."""
    if value is None:
        return None
    if not isinstance(value, dict) or not value or set(value) - {'targets', 'card'}:
        raise InstanceError('invalid_latency_targets')
    out = {}
    for key in ('targets', 'card'):
        if value.get(key) is None:
            continue
        ids = value[key]
        if not isinstance(ids, list) or len(ids) > MAX_TARGETS:
            raise InstanceError('invalid_latency_targets')
        out[key] = []
        for target in ids:
            if not isinstance(target, str) or target in out[key] or not _known(target):
                raise InstanceError('invalid_latency_targets')
            out[key].append(target)
    if not out.get('card'):
        out.pop('card', None)
    return out or None


def _known(target):
    if target in EXTRAS or CUSTOM_ID.match(target) or _province(target) or server_target(target):
        return True
    if target.startswith('c:'):
        parsed = _endpoint(target[2:])
        return parsed is not None and not _private(parsed[0])
    return False


def _private(host):
    with contextlib.suppress(ValueError):
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_unspecified or ip.is_multicast
    return host.lower() == 'localhost'


def province_ids(settings):
    code = {name: c for name, c in PROVINCES}[settings['province']]
    return [f'{code}-{c}' for c, _, _ in CARRIERS]


def label(target, lib=None, names=None):
    """How a target is named on the page: 云南电信, 阿里 DNS, one's own by its name or host, another server by
    its name (and the address and way when not its public one by ICMP), 出口 for a front."""
    lib = lib or EXTRAS
    if target in lib:
        return lib[target][0]
    if CUSTOM_ID.match(target):
        return '已删除的地址'
    if (server := server_target(target)) is not None:
        sid, mode, port, via = server
        return ((names or {}).get(sid, '已删除的服务器') + (f' · {VIA_NAMES[via]}' if via else '')
                + (f' · {mode.upper()} {port}' if mode != 'icmp' else ''))
    if target.startswith('exit-'):
        return '出口' if target == 'exit-1' else '备用出口'
    if target.startswith('c:'):
        return target[2:].rpartition(':')[0]
    code, _, carrier = target.partition('-')
    return CODES.get(code, code) + {c: name for c, name, _ in CARRIERS}.get(carrier, carrier)


def catalog():
    """What the settings and the editor offer."""
    return {'provinces': [{'name': name, 'code': code} for name, code in PROVINCES],
            'carriers': [{'id': c, 'name': name} for c, name, _ in CARRIERS],
            'extras': [{'id': i, 'label': v[0], 'host': v[1], 'port': v[2], 'group': v[3], 'kind': v[4]} for i, v in EXTRAS.items()],
            'defaultLists': DEFAULTS['defaults'], 'maxTargets': MAX_TARGETS, 'maxCustom': MAX_CUSTOM}


def plain_carriers(route):
    """The carriers a server's 三网回程 found on ordinary lines only (163, 4837, CMNET, CMI …): no premium or
    middle line to any of the three cities, and at least one line made out. Not checked, or nothing made
    out: none."""
    results = (route or {}).get('results') or {}
    plain = set()
    for carrier, _, _ in CARRIERS:
        tiers = {(results.get(f'{city}-{carrier}') or {}).get('tier') or '' for city in ('bj', 'sh', 'gd')}
        if 'plain' in tiers and not tiers & {'gold', 'silver'}:
            plain.add(carrier)
    return plain


def server_group(domestic, route=None):
    """Which default list a server follows, and the carriers to leave out of its 'three': in China the
    domestic one; abroad the plain one when all three returns are ordinary, else the optimised one
    without the carriers that are (a return not checked counts as optimised)."""
    if domestic:
        return 'domestic', set()
    plain = plain_carriers(route)
    return ('plain', set()) if plain == CARRIER_IDS else ('optimized', plain)


def default_ids(settings, domestic, route=None):
    """What a server measures following the settings: its group's list, 'three' spelt out."""
    group, plain = server_group(domestic, route)
    out = []
    for target in settings['defaults'][group]:
        out += [t for t in province_ids(settings) if t.rpartition('-')[2] not in plain] if target == THREE else [target]
    return list(dict.fromkeys(out))


def card_targets(card, ids, lib=None, pins=None, names=None):
    """Of a server's targets, the ones its card's latency is worked out over, and what they are called:
    those it picked (pins) and still measures, else by the settings' rule the carriers (三网), the rest
    (国内, 国外, 出口 or 其他地址), or all of them."""
    lib = lib or EXTRAS
    picked = [t for t in ids if t in (pins or ())]
    if picked:
        return picked, label(picked[0], lib, names) if len(picked) == 1 else f'所选 {len(picked)} 个'
    three = [t for t in ids if PROVINCE_ID.match(t)]
    other = [t for t in ids if not PROVINCE_ID.match(t)]
    pick = {'auto': 'three' if three else 'other', 'three': 'three', 'other': 'other', 'all': 'all'}[card]
    if pick == 'three' and three:
        return three, '三网'
    if pick == 'other' and other:
        groups = frozenset('exit' if t.startswith('exit-') else (lib.get(t) or ('', '', 0, 'own'))[3] for t in other)
        return other, {frozenset({'intl'}): '国外', frozenset({'cn'}): '国内', frozenset({'exit'}): '出口'}.get(groups, '其他地址')
    return list(ids), '全部'


def own_ids(profile):
    """The server's own list, or None when it follows the defaults."""
    own = (profile or {}).get('latency') or {}
    return own.get('targets')


def wanted_ids(settings, profile, domestic, route=None):
    """The ids a server without exits measures: its own list, or the defaults."""
    ids = own_ids(profile)
    return default_ids(settings, domestic, route) if ids is None else ids


def targets_for(settings, profile, domestic, directory, front_port, route=None, servers=None, self_id=None):
    """[{id, host, mode, port?, url?}] a server's node is to probe, [] for none. servers: {id: info} of the
    owner's servers, at whose address (server_address) another server's target is; one without it, the
    server itself, or the mesh address of one when this server is not on the mesh is passed over."""
    exits = fronts.exits(profile)
    if exits:
        found = [{'id': f'exit-{i + 1}', 'host': e['address'], 'port': front_port} for i, e in enumerate(exits[:2])]
    else:
        ids = wanted_ids(settings, profile, domestic, route)
        found, lib = [], library(settings)
        for target in ids:
            if target in lib:
                _, host, port, _, _, mode, url = lib[target]
                found.append({'id': target, 'host': host, **({'port': port} if port else {}), **({'mode': mode} if mode else {}), **({'url': url} if url else {})})
            elif (server := server_target(target)) is not None:
                sid, mode, port, via = server
                ip = server_address((servers or {}).get(sid) or {}, via)
                if sid == self_id or not ip or via == 'mesh' and not ((servers or {}).get(self_id) or {}).get('meshAddress'):
                    continue
                entry = {'id': target, 'host': ip, 'mode': mode, **({'port': port} if port else {})}
                if mode == 'http':
                    where = f'[{ip}]' if ':' in ip else ip
                    entry['url'] = f'https://{where}/' if port == 443 else f'http://{where}:{port}/'
                found.append(entry)
            elif CUSTOM_ID.match(target):
                continue
            elif target.startswith('c:'):
                host, port = _endpoint(target[2:])
                found.append({'id': target, 'host': host, 'port': port})
            else:
                code, _, carrier = target.partition('-')
                endpoint = (directory.get(code) or {}).get(carrier)
                if not endpoint or not _endpoint(endpoint):
                    continue
                host, port = _endpoint(endpoint)
                found.append({'id': target, 'host': host, 'port': port})
    # Without a way of its own a target is probed the settings' way; a TCPing needs its port.
    out = []
    for t in found:
        t = {**t, 'mode': t.get('mode') or settings['method']}
        if t['mode'] != 'tcp' or t.get('port'):
            out.append(t)
    return out


class Latency:
    def __init__(self, service):
        self.service = service
        self.directory = builtin_directory()
        self.updated_ms = 0
        # The last try at the directory: {atMs, error}, error '' when it worked.
        self.attempt = {}
        # server_id -> the targets last sent: what a node's minutes may name, and their labels.
        self.sent = {}
        self.pushing = {}
        self.again = set()
        # Pushes at once: every node reconnecting together asks them all.
        self.pushes = asyncio.Semaphore(PUSHES)

    @property
    def hub(self):
        return self.service.hub

    async def start(self):
        with contextlib.suppress(Exception):
            raw = await self.service.metrics.meta('zstatic')
            if raw:
                stored = json.loads(raw)
                self.directory = parse_directory(stored.get('data'))
                self.updated_ms = int(stored.get('updatedAtMs') or 0)
        with contextlib.suppress(Exception):
            self.attempt = json.loads(await self.service.metrics.meta('zstatic_attempt') or '{}')
        if not os.environ.get('PYTEST_CURRENT_TEST'):
            self.service.spawn(self._refresh_loop())

    async def settings(self):
        return (await self.service.settings())['latency']

    def node_hello(self, server_id):
        self.soon(server_id)

    def soon(self, server_id):
        """Send a node its targets again; a push already on its way is followed by one more."""
        task = self.pushing.get(server_id)
        if task and not task.done():
            self.again.add(server_id)
            return
        self.pushing[server_id] = self.service.spawn(self._push_loop(server_id))

    async def _push_loop(self, server_id):
        while True:
            with contextlib.suppress(Exception):
                async with self.pushes:
                    await self.push(server_id)
            if server_id not in self.again:
                break
            self.again.discard(server_id)

    def soon_all(self):
        for server_id in list(self.hub.sessions):
            self.soon(server_id)

    async def targets(self, server_id):
        """The server's targets now, worked out from the settings and its profile."""
        row = await one(self.service.db.conn, '''SELECT n.owner, n.info_json, p.profile_json FROM server_nodes n
                                                  LEFT JOIN server_profiles p ON p.server_id = n.server_id AND p.owner = n.owner WHERE n.server_id=?''', (server_id,))
        if not row:
            return []
        settings = await self.service.settings()
        if not settings['latency']['enabled']:
            return []
        prof, node = json.loads(row['profile_json'] or '{}'), json.loads(row['info_json'] or '{}')
        domestic, route = server_region(prof, node) == DOMESTIC, retier(node.get('route'))
        # Every node's info is read only for a server that measures another: on a reconnect of them all, N times N.
        ids = [] if fronts.exits(prof) else wanted_ids(settings['latency'], prof, domestic, route)
        servers = await self._addresses(row['owner']) if any(server_target(t) is not None for t in ids) else {}
        return targets_for(settings['latency'], prof, domestic, self.directory, settings['frontPort'], route, servers, server_id)

    async def _addresses(self, owner):
        """{server id: info} of the owner's servers with a node, a mesh member's with its meshAddress: where
        another server's targets point."""
        mesh = await self.service.mesh.addresses(owner)
        return {r['server_id']: {**(json.loads(r['info_json'] or '{}').get('info') or {}), 'meshAddress': mesh.get(r['server_id'], '')}
                for r in await many(self.service.db.conn, 'SELECT server_id, info_json FROM server_nodes WHERE owner=?', (owner,))}

    async def names(self, owner):
        """{server id: name} for the labels of other servers' targets."""
        profiles = await self.service._profiles(owner)
        return {s['id']: (profiles.get(s['id'], {}).get('alias') or '本机') if s['id'] == LOCAL else s['name'] for s in await self.service.registry.all(owner)}

    async def moved(self, server_id):
        """A server's address changed or it went: the servers that measure it get their targets again."""
        rows = await many(self.service.db.conn, 'SELECT server_id, profile_json FROM server_profiles WHERE profile_json LIKE ?', (f'%"s:{server_id}%',))
        for row in rows:
            targets = own_ids(json.loads(row['profile_json'] or '{}')) or []
            if row['server_id'] != server_id and any((server_target(t) or ('',))[0] == server_id for t in targets):
                self.soon(row['server_id'])

    def mesh_moved(self, owner):
        """The applied mesh changed: the servers measuring another over it get their targets again."""
        async def run():
            rows = await many(self.service.db.conn, 'SELECT server_id, profile_json FROM server_profiles WHERE owner=? AND profile_json LIKE ?', (owner, '%@mesh%'))
            for row in rows:
                if any((server_target(t) or ('', '', 0, ''))[3] == 'mesh' for t in own_ids(json.loads(row['profile_json'] or '{}')) or []):
                    self.soon(row['server_id'])
        self.service.spawn(run())

    async def push(self, server_id):
        session = self.hub.sessions.get(server_id)
        if not session or FEATURE not in session.features or session.incompatible:
            return
        # A node without HTTP probes would refuse the whole list for one: those wait for its update.
        targets = [t for t in await self.targets(server_id) if t['mode'] != 'http' or HTTP_FEATURE in session.features]
        interval = (await self.settings())['intervalS'] * 1000
        # Known before the call: a node back from a time without a session sends the minutes it kept
        # as soon as it has its targets, which may come before its reply.
        before = self.sent.get(server_id)
        self.sent[server_id] = {t['id']: t for t in targets}
        reply = await self.hub.call(server_id, 'latency.watch', {'targets': targets, 'intervalMs': interval}, 30)
        if not reply.get('ok'):
            if before is None:
                self.sent.pop(server_id, None)
            else:
                self.sent[server_id] = before
            log.warning('node %s refused its latency targets: %s', server_id, reply.get('error'))

    async def observe(self, server_id, minutes):
        """A node's {t: latency} message: only the targets it was given are kept."""
        known = self.sent.get(server_id) or {}
        if not isinstance(minutes, list) or not known:
            return
        # A node keeps the minutes of a time without a session and sends them later; none older than
        # this, nor from a clock ahead.
        now = time.time()
        rows = []
        for m in minutes[:256]:
            try:
                if m['target'] not in known:
                    continue
                ts = int(m['ts'])
                if not now - KEPT_S <= ts <= now + 120:
                    continue
                rows.append({'ts': ts, 'target': m['target'], 'sent': int(m['sent']), 'lost': int(m['lost']),
                             'avg': m.get('avg'), 'min': m.get('min'), 'max': m.get('max')})
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
        if rows:
            await self.service.metrics.write_pings(server_id, rows)

    # ----- the directory -----
    async def refresh(self):
        """Zstatic's directory again; nodes whose targets it moved get theirs. Each try is kept with how it
        went, and answered with the directory as it stands: one that failed leaves it as it was."""
        try:
            async with self.service.http.get(DIRECTORY_URL) as response:
                response.raise_for_status()
                data = await response.json(content_type=None)
            if not isinstance(data, dict) or not isinstance(data.get('ProvinceNodes'), list):
                raise ValueError('the answer has no ProvinceNodes')
        except Exception as exc:
            await self._attempted(f'{type(exc).__name__}: {exc}'[:200])
            return {'ok': True, 'refreshed': False, 'changed': False, **self.view()}
        await self._attempted('')
        directory = parse_directory(data)
        changed = directory != self.directory
        self.directory = directory
        self.updated_ms = _now_ms()
        await self.service.metrics.set_meta('zstatic', json.dumps({'updatedAtMs': self.updated_ms, 'data': data}, ensure_ascii=False))
        if changed:
            self.soon_all()
        return {'ok': True, 'refreshed': True, 'changed': changed, **self.view()}

    async def _refresh_loop(self):
        # Once a day; one that failed is tried again within the hour.
        await asyncio.sleep(60)
        while True:
            if _now_ms() - self.updated_ms >= REFRESH_S * 1000:
                result = await self.refresh()
                if not result['refreshed']:
                    log.info('latency directory not refreshed: %s', result['directoryError'])
            await asyncio.sleep(3600)

    async def _attempted(self, error):
        self.attempt = {'atMs': _now_ms(), 'error': error}
        with contextlib.suppress(Exception):
            await self.service.metrics.set_meta('zstatic_attempt', json.dumps(self.attempt, ensure_ascii=False))

    def view(self):
        """The library as the settings show it: the built-in addresses, the directory now and how it is kept."""
        return {**catalog(), 'directoryUpdatedAtMs': self.updated_ms, 'directoryRefreshS': REFRESH_S, 'directory': self.directory,
                'directoryCheckedAtMs': self.attempt.get('atMs') or 0, 'directoryError': self.attempt.get('error') or ''}

    # ----- what the pages read -----
    async def summary(self, owner):
        """Every owned server's last half hour for the cards (one cell a minute) and its last ten minutes, over
        the targets the card setting picks of those it measures now (all it reported, for one not connected)."""
        ids = [r['server_id'] for r in await many(self.service.db.conn, 'SELECT server_id FROM server_nodes WHERE owner=?', (owner,))]
        settings, profiles, servers = await self.settings(), await self.service._profiles(owner), await self.names(owner)
        lib, wanted, names = library(settings), {}, {}
        for server_id in ids:
            sent = self.sent.get(server_id)
            if sent:
                pins = ((profiles.get(server_id) or {}).get('latency') or {}).get('card')
                wanted[server_id], names[server_id] = card_targets(settings['card'], list(sent), lib, pins, servers)
        data = await self.service.metrics.ping_summary(ids, wanted={k: set(v) for k, v in wanted.items()})
        for server_id, item in data['items'].items():
            item['group'] = names.get(server_id, '')
        return {'ok': True, **data}

    async def series(self, owner, server_id, span):
        if span not in RANGES:
            raise InstanceError('invalid_range')
        await self.service.registry.get(owner, server_id)
        data = await self.service.metrics.pings(server_id, span)
        current = {c['id']: c for c in await self.targets(server_id)}
        settings, names = await self.settings(), await self.names(owner)
        profile = (await self.service._profiles(owner)).get(server_id) or {}
        lib = library(settings)
        # The card's group is what the page draws to begin with; the rest are a tap away.
        primary, group = card_targets(settings['card'], list(current), lib, (profile.get('latency') or {}).get('card'), names)
        order = list(current) + [t for t in data['targets'] if t not in current]
        kind = lambda t: 'three' if PROVINCE_ID.match(t) else 'exit' if t.startswith('exit-') else 'server' if server_target(t) else (lib.get(t) or ('', '', 0, 'own'))[3]  # noqa: E731
        session = self.hub.sessions.get(server_id)
        http = session is None or HTTP_FEATURE in session.features
        empty = {'avg': [None] * len(data['t']), 'loss': [None] * len(data['t'])}
        return {'ok': True, **data, 'group': group, 'targets': [{
            'id': t, 'label': label(t, lib, names), 'current': t in current, 'host': (current.get(t) or {}).get('url') or (current.get(t) or {}).get('host', ''),
            'mode': (current.get(t) or {}).get('mode', ''), 'kind': kind(t), 'primary': t in primary,
            # Waiting for a node that has HTTP probes.
            **({'update': True} if (current.get(t) or {}).get('mode') == 'http' and not http else {}),
            **data['targets'].get(t, empty)} for t in order], 'supported': self._supported(server_id)}

    def _supported(self, server_id):
        session = self.hub.sessions.get(server_id)
        return None if session is None else FEATURE in session.features


def _now_ms():
    return int(time.time() * 1000)
