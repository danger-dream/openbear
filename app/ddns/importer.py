"""Reading a ddns-go config (config.yaml): its providers become accounts and its domains
records. Nothing here talks to a provider or a node; the service does that."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl

import yaml

from app.ddns import model, providers

TYPES = {'alidns': 'aliyun', 'tencentcloud': 'tencent', 'dnspod': 'dnspod', 'cloudflare': 'cloudflare', 'huaweicloud': 'huawei',
         'callback': 'callback', 'baiducloud': 'baidu', 'porkbun': 'porkbun', 'godaddy': 'godaddy', 'namecheap': 'namecheap',
         'namesilo': 'namesilo', 'vercel': 'vercel', 'dynadot': 'dynadot', 'dynv6': 'dynv6', 'spaceship': 'spaceship', 'gcore': 'gcore',
         'nsone': 'ns1', 'name_com': 'namecom', 'rainyun': 'rainyun', 'cloudns': 'cloudns', 'desec': 'desec', 'dnsla': 'dnsla',
         'trafficroute': 'volc'}
UNSUPPORTED = {'aliesa', 'edgeone', 'nowcn', 'eranet', 'tnethk', 'hipmdnsmgr'}
LINE_KEYS = ('record_line', 'RecordLine', 'line')
ID_KEYS = ('RecordId', 'record_id', 'id')
# Where ddns-go's two credential fields go when the provider names its fields like this.
ID_NAMES = ('id', 'key_id', 'access_key_id', 'secret_id', 'access_key', 'api_key', 'key', 'username', 'user', 'email', 'login',
            'account', 'client_id', 'url')
SECRET_NAMES = ('secret', 'token', 'api_token', 'access_key_secret', 'secret_key', 'secret_access_key', 'password', 'api_secret',
                'key_secret', 'body')
CALLBACK_VARS = {'#{ip}': '{ip}', '#{domain}': '{domain}', '#{recordType}': '{type}', '#{ttl}': '{ttl}'}
# Each alias repeats all it names, so a few nested ones make a short file hundreds of thousands
# of records; ddns-go itself writes none.
MAX_ALIASES, MAX_DOMAINS = 50, 500


class _Loader(yaml.SafeLoader):
    aliases = 0

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            self.aliases += 1
            if self.aliases > MAX_ALIASES:
                model.fail('import_too_large', message=f'ddns-go 配置里的 YAML 别名（*引用）超过 {MAX_ALIASES} 个，不能导入；请用 ddns-go 导出的原始配置')
        return super().compose_node(parent, index)


def parse(text):
    """{accounts: [{key, type, alias, credentials, ttl}], records: [{account, name, zone, type, method, params,
    options, recordId, ttl}], unsupported: [{item, reason}], warnings: [..]}."""
    try:
        doc = yaml.load(text, Loader=_Loader) if isinstance(text, str) else None  # noqa: S506 - a SafeLoader
    except yaml.YAMLError:
        doc = None
    if not isinstance(doc, dict):
        model.fail('invalid_yaml')
    confs = doc.get('dnsconf')
    if confs is None and isinstance(doc.get('dns'), dict):
        confs = [doc]  # before dnsconf, one provider sat at the top level
    if not isinstance(confs, list):
        model.fail('invalid_yaml')
    out = {'accounts': [], 'records': [], 'unsupported': [], 'warnings': []}
    webhook = doc.get('webhook') if isinstance(doc.get('webhook'), dict) else {}
    if webhook.get('webhookurl'):
        out['warnings'].append('ddns-go 的 Webhook 通知不会导入；OpenBear 通过提醒渠道发送动态域名告警')
    seen, count = {}, 0
    for index, conf in enumerate(confs, 1):
        if not isinstance(conf, dict):
            continue
        dns = conf.get('dns') if isinstance(conf.get('dns'), dict) else {}
        source = str(dns.get('name') or '').strip()
        label = str(conf.get('name') or '').strip() or f'第 {index} 组配置'
        domains = _domains(conf)
        if not domains:
            continue
        kind = TYPES.get(source)
        if source in UNSUPPORTED or not kind:
            out['unsupported'].append({'item': f'{label}（{source or "未填写服务商"}）', 'reason': '暂不支持这个服务商'})
            continue
        cls = providers.get(kind)
        if cls is None:
            out['unsupported'].append({'item': f'{label}（{source}）', 'reason': 'OpenBear 还没有这个服务商的适配'})
            continue
        creds, notes = _credentials(cls, kind, str(dns.get('id') or ''), str(dns.get('secret') or ''))
        out['warnings'] += [f'{label}：{n}' for n in notes]
        ttl = _ttl(conf.get('ttl'))
        identity = (kind, tuple(sorted(creds.items())))
        if identity not in seen:
            alias = str(conf.get('name') or '').strip() or cls.name
            taken = {a['alias'] for a in out['accounts'] if a['type'] == kind}
            alias = alias if alias not in taken else f'{alias} {len(taken) + 1}'
            seen[identity] = len(out['accounts'])
            out['accounts'].append({'key': seen[identity], 'type': kind, 'alias': alias[:40], 'credentials': creds, 'ttl': ttl})
        account = seen[identity]
        for family, rtype in (('ipv4', 'A'), ('ipv6', 'AAAA')):
            block = conf.get(family) if isinstance(conf.get(family), dict) else {}
            if not block.get('enable'):
                continue
            method, params, notes = _method(block, model.TYPES[rtype])
            out['warnings'] += [f'{label}：{n}' for n in notes]
            for raw in block.get('domains') or []:
                count += 1
                if count > MAX_DOMAINS:
                    model.fail('import_too_large', message=f'ddns-go 配置里的域名超过 {MAX_DOMAINS} 条（IPv4、IPv6 分别计数），不能一次导入；请拆成几份分别导入')
                if not isinstance(raw, str) or not raw.strip():
                    continue
                record = _record(raw.strip(), cls, out['warnings'])
                if record:
                    out['records'].append({**record, 'account': account, 'type': rtype, 'method': method, 'params': params, 'ttl': ttl})
    return out


def _domains(conf):
    return [d for f in ('ipv4', 'ipv6') if isinstance(conf.get(f), dict) and conf[f].get('enable')
            for d in conf[f].get('domains') or [] if isinstance(d, str) and d.strip()]


def _ttl(raw):
    try:
        value = int(str(raw or '').strip() or 0)
    except ValueError:
        return None
    return value if 1 < value <= 604800 else None


def _credentials(cls, kind, ident, secret):
    fields = [f for f in cls.fields if f.kind != 'bool']
    notes = []
    if kind == 'callback':
        return _callback(fields, ident, secret)
    if kind == 'dynadot':
        # ddns-go uses Dynadot's dynamic DNS password; the API needs an API Key, another credential.
        return {}, ['ddns-go 里的 Dynadot 动态解析密码不能用于 API：请在 Dynadot 后台 Tools → API 生成 API Key，导入后填入']
    keys = [f.key for f in fields]
    required = [f.key for f in fields if not f.optional]
    # ddns-go's rainyun id is the domain's id; the secret is the API key.
    if len(required) == 1 and (not ident or kind == 'rainyun'):
        return ({required[0]: secret} if secret else {}), notes
    id_key = next((k for k in keys if k in ID_NAMES), None)
    secret_key = next((k for k in keys if k in SECRET_NAMES and k != id_key), None)
    if id_key is None or secret_key is None:
        # The provider names its fields otherwise: ddns-go's order, id first.
        rest = [k for k in keys if k not in (id_key, secret_key)]
        id_key = id_key or (rest.pop(0) if rest and ident else None)
        secret_key = secret_key or (rest.pop(0) if rest else None)
        notes.append('凭据按顺序填入了 ' + '、'.join(f.label for f in fields if f.key in (id_key, secret_key)) + '，请导入后测试连接')
    creds = {k: v for k, v in ((id_key, ident), (secret_key, secret)) if k and v}
    missing = [f.label for f in fields if not f.optional and not creds.get(f.key)]
    if missing:
        notes.append('缺少凭据字段：' + '、'.join(missing) + '，请导入后补全')
    return creds, notes


def _callback(fields, url, body):
    for old, new in CALLBACK_VARS.items():
        url, body = url.replace(old, new), body.replace(old, new)
    keys = {f.key for f in fields}
    values = {'url': url, 'method': 'POST' if body else 'GET', 'body': body}
    creds = {k: v for k, v in values.items() if k in keys and v}
    notes = [] if 'url' in keys else ['回调的 URL 和 Body 没有对应的字段，请导入后手动填写']
    return creds, notes


def _method(block, family):
    kind = str(block.get('gettype') or 'url')
    params = model.default_params()
    notes = []
    if kind == 'netInterface':
        params['iface'] = str(block.get('netinterface') or '')[:64]
        reg = str(block.get('ipv6reg') or '').strip()
        if family == 'v6' and reg:
            match = re.fullmatch(r'@(\d{1,3})', reg)
            params['v6Select'] = {**model.DEFAULT_V6_SELECT, **({'mode': 'index', 'value': match.group(1)} if match else {'mode': 'regex', 'value': reg})}
        return 'nic', params, notes
    if kind == 'cmd':
        cmd = str(block.get('cmd') or '')
        if len(cmd) > model.MAX_CMD:
            notes.append('命令超过 1000 个字符，已截断')
        params['cmd'] = cmd[:model.MAX_CMD]
        return 'cmd', params, notes
    urls = [u.strip() for u in str(block.get('url') or '').split(',') if u.strip().startswith(('http://', 'https://'))]
    if len(urls) > model.MAX_URLS:
        notes.append(f'查询地址超过 {model.MAX_URLS} 个，只保留前 {model.MAX_URLS} 个')
    params['urls'] = urls[:model.MAX_URLS]
    return 'url', params, notes


def _record(raw, cls, warnings):
    main, _, query = raw.partition('?')
    try:
        name, zone = model.clean_name(main)
    except model.DdnsError:
        warnings.append(f'域名 {raw} 格式不正确，已跳过')
        return None
    options, record_id = {}, ''
    fields = {f.key: f for f in cls.options}
    for key, value in parse_qsl(query, keep_blank_values=True):
        if key == 'proxied' and 'proxied' in fields:
            options['proxied'] = value.lower() in ('true', '1', 'yes')
        elif key in LINE_KEYS and 'line' in fields:
            code = model.option_code(fields['line'], value)
            if code is None:
                warnings.append(f'域名 {name} 的解析线路 {value} 无法识别，已忽略')
            else:
                options['line'] = code
        elif key in ID_KEYS:
            record_id = value
        else:
            warnings.append(f'域名 {name} 的参数 {key}={value} 没有导入')
    return {'name': name, 'zone': zone, 'options': options, 'recordId': record_id}
