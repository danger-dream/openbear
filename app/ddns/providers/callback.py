"""Generic HTTP callback: one templated request per write, for services without a dedicated adapter."""
from __future__ import annotations

import json
import re
from urllib.parse import quote

from app.ddns.providers.base import Caps, Field, Provider, ProviderError, Record, register

VARIABLE = re.compile(r'\{(ip|domain|type|ttl|zone)\}')
METHODS = ('GET', 'POST', 'PUT', 'PATCH')


def render(template, values, escape):
    return VARIABLE.sub(lambda m: escape(values[m.group(1)]), template)


@register
class Callback(Provider):
    type = 'callback'
    name = 'HTTP 回调'
    tier = 'C'
    fields = (
        Field('method', '请求方法', 'select', options=tuple(f'{m}:{m}' for m in METHODS), optional=True, default='GET'),
        Field('url', 'URL 模板', hint='可用变量 {ip} {domain} {type} {ttl} {zone}，代入时做 URL 编码'),
        Field('headers', '请求头', 'textarea', optional=True, hint='每行一个，例如 Authorization: Bearer xxx'),
        Field('body', 'Body 模板', 'textarea', optional=True, hint='变量同 URL；Body 是 JSON 时按 JSON 字符串转义代入'),
        Field('zones', '可管理的域', optional=True, hint='逗号分隔；不填则不能列出域'),
    )
    caps = Caps(zones=False, read=False, by_id=False, multi=False, delete=False)

    def __init__(self, creds, settings=None, *, client=None):
        super().__init__(creds, settings, client=client)
        self.method = self.creds.get('method', 'GET').upper()
        if self.method not in METHODS:
            raise ProviderError('bad_request', f'unsupported method: {self.method}')
        if not self.creds['url'].lower().startswith(('http://', 'https://')):
            raise ProviderError('bad_request', 'the URL must start with http:// or https://')
        self.headers = {}
        for line in self.creds.get('headers', '').splitlines():
            if not line.strip():
                continue
            key, sep, value = line.partition(':')
            if not sep or not key.strip():
                raise ProviderError('bad_request', f'invalid header line: {line.strip()[:80]}')
            self.headers[key.strip()] = value.strip()
        self.managed = [z.strip().lower().rstrip('.') for z in self.creds.get('zones', '').split(',') if z.strip()]
        if self.managed:
            self.caps = Caps(zones=True, read=False, by_id=False, multi=False, delete=False)

    def _is_json(self):
        content_type = next((v for k, v in self.headers.items() if k.lower() == 'content-type'), None)
        if content_type is not None:
            return 'json' in content_type.lower()
        return self.creds.get('body', '').lstrip()[:1] in ('{', '[')

    async def zones(self):
        if not self.managed:
            raise ProviderError('unsupported', 'no zones configured')
        return list(self.managed)

    async def check(self):
        # A call would change DNS, so only the configuration can be checked.
        return {'zones': list(self.managed), 'writable': None}

    async def records(self, zone, name, rtype):
        raise ProviderError('unsupported', 'a callback cannot read records')

    async def send(self, zone, name, rtype, value, ttl):
        ttl = self.ttl_for(ttl)
        values = {'ip': value, 'domain': name, 'type': rtype, 'ttl': '' if ttl is None else str(ttl), 'zone': zone}
        url = render(self.creds['url'], values, lambda v: quote(v, safe=''))
        headers = dict(self.headers)
        content = None
        if self.creds.get('body'):
            if self._is_json():
                content = render(self.creds['body'], values, lambda v: json.dumps(v)[1:-1])
                if not any(k.lower() == 'content-type' for k in headers):
                    headers['Content-Type'] = 'application/json'
            else:
                content = render(self.creds['body'], values, str)
            content = content.encode()
        await self.request(self.method, url, content=content, headers=headers, ok=range(200, 300))
        return Record('', name, rtype, value, ttl)

    async def create(self, zone, name, rtype, value, ttl, opts):
        return await self.send(zone, name, rtype, value, ttl)

    async def update(self, zone, record, value, ttl, opts):
        return await self.send(zone, record.name, record.type, value, ttl)
