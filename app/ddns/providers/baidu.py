"""Baidu AI Cloud DNS (智能云解析, dns.baidubce.com v1) with bce-auth-v1 signing."""
from __future__ import annotations

import json
import time
import uuid
from urllib.parse import urlsplit

from app.ddns.providers import _sign
from app.ddns.providers.base import Caps, Field, Provider, ProviderError, Record, register

ERRORS = {
    'InvalidAccessKeyId': 'auth_failed', 'SignatureDoesNotMatch': 'auth_failed', 'RequestExpired': 'auth_failed',
    'InvalidHTTPAuthHeader': 'auth_failed', 'AccessDenied': 'forbidden',
}
# The free and discount editions' lines (智能云解析 API, record line).
LINES = ('default:默认', 'ct:电信', 'cnc:联通', 'cmnet:移动', 'edu:教育网', 'search:搜索引擎（百度）')


@register
class Baidu(Provider):
    type = 'baidu'
    name = '百度智能云'
    tier = 'B'
    fields = (Field('access_key_id', 'Access Key（AK）'), Field('secret_access_key', 'Secret Key（SK）', 'password'))
    caps = Caps(ttl_min=1, ttl_max=86400)
    options = (Field('line', '解析线路', 'select', options=LINES, optional=True, default='default', hint='只在新建时生效'),)
    docs = 'https://cloud.baidu.com/doc/DNS/s/njwvywyto'
    endpoint = 'https://dns.baidubce.com'

    async def call(self, method, path, query=None, payload=None):
        query = dict(query or {})
        if method != 'GET':
            query['clientToken'] = uuid.uuid4().hex
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode() if payload is not None else b''
        now = int(time.time())
        headers = {'Host': urlsplit(self.base).netloc, 'x-bce-date': _sign.utc(now, '%Y-%m-%dT%H:%M:%SZ')}
        if body:
            headers['Content-Type'] = 'application/json'
        headers['Authorization'] = _sign.bce(self.creds['access_key_id'], self.creds['secret_access_key'], method=method,
                                             path=path, query=query, headers=headers, timestamp=now)
        qs = _sign.query_string(query)
        response = await self.request(method, path + ('?' + qs if qs else ''), content=body or None, headers=headers,
                                      ok=range(100, 600))
        data = self.json(response) if response.content else {}
        if response.status_code >= 300:
            code = data.get('code', '') if isinstance(data, dict) else ''
            message = f'{code}: {data.get("message", "")}' if code else f'HTTP {response.status_code}'
            if code in ERRORS:
                raise ProviderError(ERRORS[code], message, status=response.status_code)
            raise self.http_error(response, message)
        if isinstance(data, dict) and data.get('code') and data.get('message'):
            raise ProviderError(ERRORS.get(data['code'], 'bad_request'), f'{data["code"]}: {data["message"]}')
        return data

    async def zones(self):
        out, marker = [], None
        while True:
            data = await self.call('GET', '/v1/dns/zone', {'maxKeys': 1000, **({'marker': marker} if marker else {})})
            out += [z['name'].lower().rstrip('.') for z in data.get('zones') or []]
            marker = data.get('nextMarker')
            if not data.get('isTruncated') or not marker:
                return out

    def _record(self, zone, item):
        return Record(str(item['id']), self.fqdn(zone, item['rr']), item['type'], item['value'], item.get('ttl'),
                      {'line': item.get('line') or 'default', 'description': item.get('description') or ''})

    async def records(self, zone, name, rtype):
        rr = self.relative(zone, name)
        out, marker = [], None
        while True:
            query = {'rr': rr, 'maxKeys': 1000, **({'marker': marker} if marker else {})}
            data = await self.call('GET', f'/v1/dns/zone/{zone}/record', query)
            out += [self._record(zone, i) for i in data.get('records') or [] if i.get('rr', '').lower() == rr and i.get('type') == rtype]
            marker = data.get('nextMarker')
            if not data.get('isTruncated') or not marker:
                return out

    async def create(self, zone, name, rtype, value, ttl, opts):
        line = (opts or {}).get('line') or 'default'
        payload = {'rr': self.relative(zone, name), 'type': rtype, 'value': value, 'line': line}
        if self.ttl_for(ttl):
            payload['ttl'] = self.ttl_for(ttl)
        await self.call('POST', f'/v1/dns/zone/{zone}/record', payload=payload)
        # The add API returns no body, so the new record's id comes from a fresh listing.
        for record in reversed(await self.records(zone, name, rtype)):
            if record.value == value and record.extra.get('line') == line:
                return record
        raise ProviderError('invalid_response', 'the new record is not listed')

    async def update(self, zone, record, value, ttl, opts):
        # The modify API has no line field; ttl and description are sent back so they are not reset.
        payload = {'rr': self.relative(zone, record.name), 'type': record.type, 'value': value}
        new_ttl = self.ttl_for(ttl) or record.ttl
        if new_ttl:
            payload['ttl'] = new_ttl
        if record.extra.get('description'):
            payload['description'] = record.extra['description']
        await self.call('PUT', f'/v1/dns/zone/{zone}/record/{record.id}', payload=payload)
        return Record(record.id, record.name, record.type, value, new_ttl, dict(record.extra))

    async def delete(self, zone, record):
        await self.call('DELETE', f'/v1/dns/zone/{zone}/record/{record.id}')
