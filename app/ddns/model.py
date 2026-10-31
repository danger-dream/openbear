"""What a DDNS account and record may hold, which addresses may be written, and the
probe each record asks its node to run."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from urllib.parse import urlsplit

from app.ddns import nodecheck
from app.instances.contracts import InstanceError

TYPES = {'A': 'v4', 'AAAA': 'v6'}
FAMILY_TYPE = {'v4': 'A', 'v6': 'AAAA'}
METHODS = ('auto', 'url', 'nic', 'stun', 'cmd', 'conn')
PROBED = ('url', 'nic', 'stun', 'cmd')
# What auto may settle on: a probed method, or the interface method held to one group of
# services, when a proxy routes traffic to China and abroad different ways.
RESOLVED = (*PROBED, 'url_cn', 'url_intl')
V6_MODES = ('first', 'index', 'prefix', 'regex')
# Tried in order, the first answer wins; a node takes at most MAX_URLS. Upyun's answer also
# names its own server, so it stays last: nodes before 1.1.0-dev.12 read the first address.
URL_GROUPS = {
    'cn': {'v4': ['https://myip.ipip.net/', 'https://www.ipuu.net/ipuu/user/getIP', 'https://ddns.oray.com/checkip',
                  'https://www.cz88.net/api/cz88/ip/geo?ip=', 'https://ip2city.domob.cn/json', 'https://ip.3322.net',
                  'https://data.video.iqiyi.com/v.f4v', 'https://pubstatic.b0.upaiyun.com/?_upnode'],
           'v6': ['https://v6.yinghualuo.cn/bejson', 'https://speed.neu6.edu.cn/getIP.php']},
    'intl': {'v4': ['https://api4.ipify.org', 'https://4.icanhazip.com', 'https://ipv4.ip.sb'],
             'v6': ['https://api6.ipify.org', 'https://6.icanhazip.com', 'https://v6.ident.me']}}
URL_GROUP_OF = {'url_cn': 'cn', 'url_intl': 'intl'}
DEFAULT_URLS = {'v4': ['https://api4.ipify.org', 'https://myip.ipip.net/', 'https://ddns.oray.com/checkip', 'https://4.icanhazip.com', 'https://ip.3322.net'],
                'v6': ['https://api6.ipify.org', 'https://v6.yinghualuo.cn/bejson', 'https://6.icanhazip.com', 'https://v6.ident.me']}
DEFAULT_STUN = ['stun.cloudflare.com:3478', 'stun.l.google.com:19302']
DEFAULT_INTERVAL = {'nic': 15, 'url': 60, 'stun': 60, 'cmd': 60}
MIN_INTERVAL = {'nic': 10, 'url': 30, 'stun': 30, 'cmd': 30}
MAX_INTERVAL = 3600
CONFIRM = 2
MAX_URLS, MAX_STUN, MAX_CMD = 8, 4, 1000
# A node runs at most this many probes (ddns.watch refuses more).
MAX_PROBES = 64
# The longest probe URL a node takes.
MAX_URL = 2048
_STUN = re.compile(r'(stun:)?([\w.\-]{1,253}|\[([0-9a-fA-F:.]{2,45})\])(:\d{1,5})?', re.ASCII)
# The longest v6Select value a node takes, in UTF-8 bytes.
MAX_SELECT = 256
PROXY_SCHEMES = ('http', 'https')
DEFAULT_POLICY = {'multi': 'one', 'allowPrivate': False, 'onDrift': 'revert'}
DEFAULT_V6_SELECT = {'mode': 'first', 'value': '', 'allowTemporary': False}
BACKUP_AFTER_S = 300

MESSAGES = {
    'invalid_parameters': '参数不正确', 'invalid_json': '请求内容不是合法的 JSON', 'json_object_required': '请求内容必须是 JSON 对象',
    'body_too_large': '请求内容太大', 'authentication_required': '请先登录', 'owner_required': '只有主人可以管理动态域名',
    'storage_unavailable': '数据库暂时不可用', 'controller_only': '只有主控可以管理动态域名',
    'provider_unknown': '不支持这个服务商', 'invalid_alias': '别名需要 1 到 40 个字符', 'invalid_credentials': '凭据字段不正确',
    'credentials_required': '请填写凭据，或选择凭据库里的凭据', 'credential_missing': '缺少必填的凭据字段',
    'secret_not_found': '凭据库里找不到这条凭据', 'secret_not_ddns': '只能引用动态域名自己的凭据（凭据库里 ddns/ 开头的条目）',
    'endpoint_web_only': 'API 端点只能在网页上设置和修改', 'secret_ref_web_only': '回调账户只能在网页上引用凭据库里的凭据',
    'secret_missing': '账户引用的凭据已被删除或归档', 'invalid_secret_ref': '凭据引用不正确',
    'secret_save_failed': '凭据保存失败', 'invalid_settings': '账户设置不正确', 'invalid_endpoint': 'API 端点必须是 http 或 https 地址',
    'invalid_proxy': '代理地址必须是 http 或 https 地址', 'invalid_ttl': 'TTL 需要是 1 到 604800 之间的整数',
    'account_not_found': '找不到这个服务商账户', 'account_in_use': '这个账户下还有解析配置，确认后会一起停止管理',
    'record_not_found': '找不到这条解析配置', 'invalid_record': '解析配置不正确', 'invalid_name': '域名格式不正确',
    'invalid_type': '记录类型只能是 A 或 AAAA', 'type_unsupported': '这个服务商只能更新 IPv4（A）记录', 'invalid_source': '地址来源不正确', 'invalid_method': '取 IP 方式不正确',
    'invalid_urls': '查询地址必须是 http 或 https 地址，最多 8 个', 'invalid_stun': 'STUN 服务器写成 主机 或 主机:端口（可带 stun: 前缀），最多 4 个',
    'invalid_cmd': '命令最长 1000 个字符', 'too_many_probes': f'一台节点最多同时运行 {MAX_PROBES} 个取 IP 探测，请合并取 IP 方式或参数', 'cmd_required': '命令方式需要填写命令', 'invalid_iface': '网卡名称不正确',
    'invalid_interval': '探测间隔不正确', 'invalid_v6_select': 'IPv6 地址选择规则不正确', 'invalid_fixed': '固定值必须是对应类型的 IP 地址',
    'invalid_backup': '备用来源不正确', 'invalid_policy': '高级策略不正确', 'invalid_options': '服务商专用选项不正确',
    'server_required': '请选择来源节点', 'server_not_found': '找不到这台服务器，或它没有安装节点', 'invalid_rows': '请至少填写一个域名',
    'node_offline': '节点离线', 'node_connection_lost': '和节点的连接断开了', 'node_outdated': '节点版本过旧，不支持动态域名，请先更新节点',
    'node_protocol_outdated': '节点版本过旧，请先更新节点', 'controller_protocol_outdated': '主控版本过旧，请先更新主控',
    'invalid_family': '地址族只能是 v4 或 v6', 'invalid_yaml': '无法解析 ddns-go 配置', 'no_account': '没有能管理这个域名的服务商账户',
    'account_ambiguous': '有多个账户能管理这个域名，请选择一个', 'delete_unsupported': '这个服务商不支持删除记录',
    'auth_failed': '鉴权失败，请检查凭据', 'forbidden': '凭据没有权限', 'zone_not_found': '服务商上找不到这个域名',
    'bad_request': '服务商拒绝了请求', 'unsupported': '服务商不支持这个操作', 'rate_limited': '服务商限流，稍后重试',
    'network': '连接服务商失败', 'server_error': '服务商出错，稍后重试', 'invalid_response': '服务商返回了无法识别的内容',
    'record_conflict': '服务商上的记录冲突', 'verify_failed': '写入后回读的值不对', 'timeout': '操作超时',
    'record_ambiguous': '服务商上同名的记录有多条，认不出哪条由 OpenBear 管理', 'internal_error': '同步时出现意外错误',
    'import_too_large': 'ddns-go 配置太大',
}


class DdnsError(InstanceError):
    def __init__(self, error, status=400, message=''):
        super().__init__(error, status)
        self.message = message or MESSAGES.get(error, '')


def fail(error, status=400, message=''):
    raise DdnsError(error, status, message)


def message_of(exc):
    return getattr(exc, 'message', '') or MESSAGES.get(getattr(exc, 'error', ''), '') or '操作失败'


# ----- addresses -----
def _nets(*items):
    return tuple(ipaddress.ip_network(n) for n in items)


V4_PRIVATE = _nets('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16')
V4_RESERVED = _nets('0.0.0.0/8', '224.0.0.0/4', '240.0.0.0/4', '192.0.0.0/24', '198.18.0.0/15')
V6_PRIVATE = _nets('fc00::/7', 'fe80::/10', '::1/128')
V6_GLOBAL = ipaddress.ip_network('2000::/3')
CGNAT = ipaddress.ip_network('100.64.0.0/10')
REASONS = {'family': '地址族不对', 'private': '内网、CGNAT 或链路本地地址', 'reserved': '组播或保留地址', 'invalid': '不是 IP 地址'}


def check_address(value, family, allow_private=False):
    """(address, '') when value may be written for family, else ('', reason). Without
    allowPrivate, private, CGNAT, link-local and reserved addresses are refused.
    Unspecified and multicast addresses are always refused."""
    try:
        ip = ipaddress.ip_address(str(value or '').strip())
    except ValueError:
        return '', 'invalid'
    if (ip.version == 4) != (family == 'v4') or (ip.version == 6 and ip.ipv4_mapped):
        return '', 'family'
    if ip.is_unspecified or ip.is_multicast:
        return '', 'reserved'
    if allow_private:
        return str(ip), ''
    if ip.version == 4:
        if any(ip in n for n in V4_PRIVATE):
            return '', 'private'
        if any(ip in n for n in V4_RESERVED):
            return '', 'reserved'
    elif any(ip in n for n in V6_PRIVATE):
        return '', 'private'
    elif ip not in V6_GLOBAL:
        return '', 'reserved'
    return str(ip), ''


# ----- names -----
_LABEL = re.compile(r'\*|(?!-)[a-z0-9_-]{1,63}(?<!-)')


def _ascii(text):
    labels = text.split('.')
    out = []
    for label in labels:
        if not label:
            fail('invalid_name')
        if not label.isascii():
            try:
                label = label.encode('idna').decode()
            except UnicodeError:
                fail('invalid_name')
        if not _LABEL.fullmatch(label):
            fail('invalid_name')
        out.append(label)
    name = '.'.join(out)
    if len(name) > 253 or len(out) < 2 or '*' in out[1:]:
        fail('invalid_name')
    return name


def clean_name(raw):
    """(name, zone hint): a full name in ASCII lower case without the final dot; 'sub:zone'
    names its zone, which is '' otherwise."""
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 300:
        fail('invalid_name')
    text = raw.strip().lower().rstrip('.')
    zone = ''
    if ':' in text:
        sub, _, zone = text.partition(':')
        zone = _ascii(zone.strip().rstrip('.'))
        sub = sub.strip().strip('.')
        return (zone if sub in ('', '@') else _ascii(sub + '.' + zone)), zone
    return _ascii(text), zone


def in_zone(name, zone):
    return name == zone or name.endswith('.' + zone)


# ----- account settings and credentials -----
def _url(value, schemes, error, limit=500):
    if value in (None, ''):
        return ''
    if not isinstance(value, str) or len(value) > limit or any(c.isspace() for c in value):
        fail(error)
    parts = urlsplit(value)
    if parts.scheme not in schemes or not parts.hostname:
        fail(error)
    return value


def clean_ttl(value):
    if value in (None, '', 0):
        return None
    if type(value) is not int or not 1 <= value <= 604800:
        fail('invalid_ttl')
    return value


def clean_settings(data, prior=None):
    value = {'endpoint': '', 'proxy': '', 'ttl': None, **(prior or {})}
    if data is None:
        return value
    if not isinstance(data, dict) or set(data) - {'endpoint', 'proxy', 'ttl'}:
        fail('invalid_settings')
    if 'endpoint' in data:
        value['endpoint'] = _url(data['endpoint'], ('http', 'https'), 'invalid_endpoint')
    if 'proxy' in data:
        value['proxy'] = _url(data['proxy'], PROXY_SCHEMES, 'invalid_proxy')
    if 'ttl' in data:
        value['ttl'] = clean_ttl(data['ttl'])
    return value


def clean_alias(value, default):
    if value in (None, ''):
        return default
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 40:
        fail('invalid_alias')
    return value.strip()


def credential_fields(cls):
    return [f for f in cls.fields]


def clean_credentials(cls, data, *, partial=False):
    """Credential values by field key; partial keeps empty values out (they keep what is stored)."""
    keys = {f.key for f in cls.fields}
    if not isinstance(data, dict) or set(data) - keys:
        fail('invalid_credentials')
    values = {}
    for key, raw in data.items():
        if isinstance(raw, bool):
            raw = 'true' if raw else 'false'
        if isinstance(raw, (int, float)):
            raw = str(raw)
        if raw is None:
            continue
        if not isinstance(raw, str) or len(raw) > 20000:
            fail('invalid_credentials')
        if raw != '' or not partial:
            values[key] = raw
    if not partial:
        missing_fields(cls, values)
    return values


def missing_fields(cls, values):
    missing = [f.label for f in cls.fields if not f.optional and f.kind != 'bool' and not values.get(f.key)]
    if missing:
        fail('credential_missing', message='缺少必填的凭据字段：' + '、'.join(missing))


def clean_secret_ref(cls, data):
    if not isinstance(data, dict) or set(data) - {'secretId', 'map'} or type(data.get('secretId')) is not int:
        fail('invalid_secret_ref')
    mapping = data.get('map')
    keys = {f.key for f in cls.fields}
    if mapping is None:
        mapping = {k: k for k in keys}
    if not isinstance(mapping, dict) or set(mapping) - keys or not all(isinstance(v, str) and 0 < len(v) <= 200 for v in mapping.values()):
        fail('invalid_secret_ref')
    return data['secretId'], dict(mapping)


# ----- records -----
def default_params():
    return {'urls': [], 'iface': '', 'bindIface': '', 'stun': [], 'cmd': '', 'v6Select': dict(DEFAULT_V6_SELECT), 'intervalS': None}


def _iface(value):
    if value in (None, ''):
        return ''
    if not isinstance(value, str) or len(value) > 64 or any(c.isspace() or ord(c) < 32 for c in value):
        fail('invalid_iface')
    return value


def clean_params(raw, method):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or set(raw) - set(default_params()):
        fail('invalid_source')
    out = default_params()
    urls = raw.get('urls') or []
    if not isinstance(urls, list) or len(urls) > MAX_URLS:
        fail('invalid_urls')
    if not all(u is None or isinstance(u, str) for u in urls):
        fail('invalid_urls')
    out['urls'] = [u.strip() for u in urls if u and u.strip()]
    # As the node's Go code parses them: one it refuses stops every probe on the node.
    if not all(nodecheck.url_ok(u, MAX_URL) for u in out['urls']):
        fail('invalid_urls')
    stun = raw.get('stun') or []
    if not isinstance(stun, list) or len(stun) > MAX_STUN or not all(isinstance(s, str) and _stun_ok(s.strip()) for s in stun):
        fail('invalid_stun')
    out['stun'] = [s.strip() for s in stun]
    out['iface'], out['bindIface'] = _iface(raw.get('iface')), _iface(raw.get('bindIface'))
    cmd = raw.get('cmd') or ''
    if not isinstance(cmd, str) or len(cmd) > MAX_CMD:
        fail('invalid_cmd')
    out['cmd'] = cmd
    if method == 'cmd' and not cmd.strip():
        fail('cmd_required')
    select = raw.get('v6Select')
    if select is not None:
        if not isinstance(select, dict) or set(select) - set(DEFAULT_V6_SELECT):
            fail('invalid_v6_select')
        select = {**DEFAULT_V6_SELECT, **select}
        if select['mode'] not in V6_MODES or not isinstance(select['value'], str) or len(select['value']) > 200 \
                or not isinstance(select['allowTemporary'], bool):
            fail('invalid_v6_select')
        try:
            size = len(select['value'].encode('utf-8'))
        except UnicodeError:
            size = MAX_SELECT + 1
        if size > MAX_SELECT:
            fail('invalid_v6_select')
        # Counted from 1, as ddns-go's @1.
        if select['mode'] == 'index' and not re.fullmatch(r'[1-9][0-9]{0,2}', select['value']):
            fail('invalid_v6_select')
        if select['mode'] == 'prefix' and not select['value'].strip():
            fail('invalid_v6_select')
        # The node compiles it with Go's regexp (RE2), which has no lookaround or backreferences.
        if select['mode'] == 'regex' and not (select['value'] and nodecheck.regex_ok(select['value'])):
            fail('invalid_v6_select')
        out['v6Select'] = select
    interval = raw.get('intervalS')
    if interval is not None:
        low = MIN_INTERVAL.get(method, min(MIN_INTERVAL.values()))
        if type(interval) is not int or not low <= interval <= MAX_INTERVAL:
            fail('invalid_interval')
        out['intervalS'] = interval
    return out


def _stun_ok(text):
    """host, host:port, [v6]:port, with or without stun:; the node defaults the port to 3478."""
    match = _STUN.fullmatch(text)
    if not match or match.group(4) and not 0 < int(match.group(4)[1:]) < 65536:
        return False
    if match.group(3):
        # The node takes a bracketed host only as an IP address.
        try:
            ipaddress.ip_address(match.group(3))
        except ValueError:
            return False
    return True


def clean_source(data):
    """The stored source: {kind, serverId, method, resolvedMethod, params, fixed}. methodAuto
    is not stored: it is method == 'auto'; resolvedMethod is kept only for auto."""
    if not isinstance(data, dict) or set(data) - {'kind', 'serverId', 'method', 'methodAuto', 'resolvedMethod', 'params', 'fixed'}:
        fail('invalid_source')
    kind = data.get('kind') or 'node'
    if kind == 'fixed':
        fixed = data.get('fixed')
        if not isinstance(fixed, str) or not fixed.strip():
            fail('invalid_fixed')
        return {'kind': 'fixed', 'serverId': '', 'method': '', 'resolvedMethod': '', 'params': default_params(), 'fixed': fixed.strip()}
    if kind != 'node':
        fail('invalid_source')
    server = data.get('serverId')
    if not isinstance(server, str) or not 0 < len(server) <= 128:
        fail('server_required')
    method = data.get('method') or 'auto'
    if method not in METHODS:
        fail('invalid_method')
    resolved = data.get('resolvedMethod') if method == 'auto' else ''
    if resolved not in ('', None, *RESOLVED):
        fail('invalid_method')
    return {'kind': 'node', 'serverId': server, 'method': method, 'resolvedMethod': resolved or '',
            'params': clean_params(data.get('params'), method), 'fixed': ''}


def check_fixed(source, family):
    if source['kind'] == 'fixed' and check_address(source['fixed'], family, True)[1]:
        fail('invalid_fixed')


def clean_policy(data, prior=None):
    value = {**DEFAULT_POLICY, **(prior or {})}
    if data is None:
        return value
    if not isinstance(data, dict) or set(data) - set(DEFAULT_POLICY):
        fail('invalid_policy')
    value.update(data)
    if value['multi'] not in ('one', 'all') or value['onDrift'] not in ('revert', 'alert') or not isinstance(value['allowPrivate'], bool):
        fail('invalid_policy')
    return value


def clean_backup(data, primary):
    if data in (None, False):
        return None
    if not isinstance(data, dict) or set(data) - {'serverId', 'afterS'}:
        fail('invalid_backup')
    server, after = data.get('serverId'), data.get('afterS', BACKUP_AFTER_S)
    if not isinstance(server, str) or not 0 < len(server) <= 128 or server == primary:
        fail('invalid_backup')
    if type(after) is not int or not 60 <= after <= 86400:
        fail('invalid_backup')
    return {'serverId': server, 'afterS': after}


def option_code(field, value):
    """A select option's code, from its code or its Chinese name ("telecom:电信")."""
    for item in field.options:
        code, _, label = str(item).partition(':')
        if value in (code, label):
            return code
    return None


def clean_options(cls, data, *, strict=True):
    """Options the provider declares; with strict off, ones it does not are dropped
    (one request may add records at several providers)."""
    if data is None:
        return {}
    if not isinstance(data, dict):
        fail('invalid_options')
    fields = {f.key: f for f in (cls.options if cls else ())}
    out = {}
    for key, value in data.items():
        field = fields.get(key)
        if field is None:
            if strict:
                fail('invalid_options')
            continue
        if value is None:
            continue
        if field.kind == 'bool':
            if not isinstance(value, bool):
                fail('invalid_options')
        elif field.kind == 'select':
            value = option_code(field, value) if isinstance(value, str) else None
            if value is None:
                fail('invalid_options')
        elif field.kind == 'number':
            if type(value) is not int:
                fail('invalid_options')
        elif not isinstance(value, str) or len(value) > 200:
            fail('invalid_options')
        out[key] = value
    return out


# ----- probes -----
def method_of(source, family):
    if source['kind'] != 'node':
        return ''
    if source['method'] != 'auto':
        return source['method']
    return source.get('resolvedMethod') or ('nic' if family == 'v6' else 'url')


def probe_method(method):
    """What the node runs for a method auto settled on."""
    return 'url' if method in URL_GROUP_OF else method


def probe_spec(source, family, allow_private, method=None):
    """The ddns.watch entry for one record's source; key hashes method, family and the
    parameters that change what is seen, so records sharing them share one probe."""
    group = URL_GROUP_OF.get(method or method_of(source, family))
    method = probe_method(method or method_of(source, family))
    p = source['params']
    urls = p['urls'] or (URL_GROUPS[group][family] if group else DEFAULT_URLS[family])
    spec = {'family': family, 'method': method,
            'intervalS': max(MIN_INTERVAL[method], p.get('intervalS') or DEFAULT_INTERVAL[method]), 'confirm': CONFIRM,
            'urls': urls if method == 'url' else [],
            'bindIface': p['bindIface'] if method in ('url', 'stun') else '',
            'iface': p['iface'] if method == 'nic' else '',
            'stun': (p['stun'] or DEFAULT_STUN) if method == 'stun' else [],
            'cmd': p['cmd'] if method == 'cmd' else '',
            'v6Select': dict(p['v6Select']) if method == 'nic' and family == 'v6' else dict(DEFAULT_V6_SELECT),
            # Only a nic probe for v6 filters by it (ULA); the controller filters everything else.
            'allowPrivate': bool(allow_private) if method == 'nic' and family == 'v6' else False}
    identity = {k: v for k, v in spec.items() if k not in ('intervalS', 'confirm')}
    spec['key'] = 'p_' + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:10]
    return spec


def merge_probes(specs):
    """One probe per key, at the shortest interval any record asks for."""
    out = {}
    for spec in specs:
        known = out.get(spec['key'])
        if known is None or spec['intervalS'] < known['intervalS']:
            out[spec['key']] = spec
    return [out[k] for k in sorted(out)]


def recommend(family, results, interfaces, seen=(), region=''):
    """(method, notes) for method auto, from ddns.probe answers: url_cn and url_intl are the
    interface method against services in China and abroad. seen holds the addresses mesh
    peers on a direct link see the node at; region is the one the user gave the server."""
    by = {r.get('method'): r for r in results if isinstance(r, dict)}

    def value(m):
        return str((by.get(m) or {}).get('value') or '')
    notes = []
    if family == 'v6':
        if not value('nic'):
            notes.append({'level': 'warn', 'text': '网卡上没有可用的全局 IPv6 长期地址'})
        return 'nic', notes
    nic_v4 = [a for i in interfaces if isinstance(i, dict) for a in (i.get('v4') or []) if isinstance(a, str)] + ([value('nic')] if value('nic') else [])
    if check_address(value('nic'), 'v4')[0]:
        return 'nic', [{'level': 'info', 'text': '网卡上直接配着公网 IPv4，用网卡方式'}]
    if any(_in(a, CGNAT) for a in nic_v4):
        notes.append({'level': 'warn', 'text': '网卡地址属于运营商级 NAT（CGNAT），外部可能无法直接访问这台机器'})
    cn, intl, stun = value('url_cn') or value('url'), value('url_intl') or value('url'), value('stun')

    def method_for(address):
        if address == cn == intl:
            return 'url'
        return 'url_cn' if address == cn else 'url_intl' if address == intl else 'stun'
    found = {v for v in (cn, intl, stun) if v}
    if not found:
        notes.append({'level': 'warn', 'text': '取不到公网 IPv4 地址'})
        return 'url', notes
    peers = [a for a in found if a in seen]
    if len(found) == 1:
        address = next(iter(found))
        if seen and not peers:
            notes.append({'level': 'warn', 'text': f'组网直连的对端看到这台机器的地址是 {"、".join(sorted(seen))}，和探测到的 {address} 不同，'
                                                   '这台机器的流量可能全部经过了代理'})
        return method_for(address), notes
    parts = [f'{label} {v}' for label, v in (('国内接口', cn), ('国外接口', intl), ('STUN', stun)) if v]
    notes.append({'level': 'warn', 'text': f'不同方式查到的地址不一致（{"，".join(parts)}），部分流量经过了代理'})
    abroad = bool(region) and region.upper() != 'CN'
    if len(peers) == 1:
        address = peers[0]
        notes.append({'level': 'info', 'text': f'组网直连的对端看到这台机器的地址也是 {address}，用它'})
    elif cn and intl and cn != intl:
        address = intl if abroad else cn
        notes.append({'level': 'info', 'text': f'按{"服务器资料里的地区" if abroad else "国内接口"}的结果用 {address}（去国外的流量通常经过代理）；'
                                               '不对的话，在高级选项里指定取 IP 方式'})
    elif cn and not intl and not abroad:
        address = cn
        notes.append({'level': 'info', 'text': f'国外接口取不到地址，按国内接口的结果用 {address}'})
    else:
        address = stun
        notes.append({'level': 'info', 'text': 'HTTP 很可能经过了代理，改用 STUN'})
    return method_for(address), notes


def _in(address, network):
    try:
        return ipaddress.ip_address(address) in network
    except ValueError:
        return False
