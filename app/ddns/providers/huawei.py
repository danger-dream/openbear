"""Huawei Cloud DNS, record set API v2.1 (lines) with APIG AK/SK signing."""
from __future__ import annotations

import json
import time
from urllib.parse import urlsplit

from app.ddns.providers import _sign
from app.ddns.providers.base import Caps, Field, Provider, ProviderError, Record, register

LINES = ('default_view:全网默认', 'Dianxin:电信', 'Liantong:联通', 'Yidong:移动', 'Jiaoyuwang:教育网', 'Tietong:铁通',
         'Pengboshi:鹏博士', 'CN:中国大陆', 'Abroad:境外')


@register
class Huawei(Provider):
    type = 'huawei'
    name = '华为云'
    tier = 'B'
    fields = (Field('access_key_id', 'Access Key ID（AK）'), Field('secret_access_key', 'Secret Access Key（SK）', 'password'))
    options = (Field('line', '解析线路', 'select', options=LINES, optional=True, default='default_view', hint='只在新建时生效'),)
    caps = Caps(ttl_min=1, ttl_max=2147483647)
    docs = 'https://support.huaweicloud.com/usermanual-dns/dns_usermanual_0027.html'
    endpoint = 'https://dns.myhuaweicloud.com'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._zone_ids = {}

    async def call(self, method, path, query=None, payload=None):
        body = json.dumps(payload, separators=(',', ':')).encode() if payload is not None else b''
        stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
        headers = {'Content-Type': 'application/json', 'Host': urlsplit(self.base).netloc, 'X-Sdk-Date': stamp}
        prefix = urlsplit(self.base).path.rstrip('/')
        headers['Authorization'] = _sign.huawei(self.creds['access_key_id'], self.creds['secret_access_key'], method=method,
                                                path=prefix + path, query=query or {}, headers=headers, body=body, stamp=stamp)
        qs = _sign.query_string(query or {})
        response = await self.request(method, path + ('?' + qs if qs else ''), content=body or None, headers=headers,
                                      ok=range(100, 600))
        if response.status_code >= 300:
            data = self.json(response) if response.content.startswith(b'{') else {}
            code = data.get('code') or data.get('error_code') or ''
            message = data.get('message') or data.get('error_msg') or ''
            raise self.http_error(response, f'{code}: {message}' if code else message)
        return self.json(response) if response.content else {}

    async def _list_zones(self, name=None):
        out, offset = [], 0
        while True:
            query = {'type': 'public', 'limit': 500, 'offset': offset}
            if name:
                query.update(name=name, search_mode='equal')
            data = await self.call('GET', '/v2/zones', query)
            items = data.get('zones') or []
            for z in items:
                zone = z['name'].lower().rstrip('.')
                self._zone_ids.setdefault(zone, z['id'])
                out.append(zone)
            if len(items) < 500:
                return out
            offset += len(items)

    async def zones(self):
        return await self._list_zones()

    async def zone_id(self, zone):
        if zone not in self._zone_ids:
            await self._list_zones(zone)
        if zone not in self._zone_ids:
            raise ProviderError('zone_not_found', f'{zone} is not a public zone of this account')
        return self._zone_ids[zone]

    @staticmethod
    def _record(item):
        # A record set holds every value of one name, type and line; it is one Record here.
        return Record(item['id'], item['name'].lower().rstrip('.'), item['type'], ','.join(item.get('records') or []),
                      item.get('ttl'), {'line': item.get('line') or 'default_view'})

    async def records(self, zone, name, rtype):
        zid = await self.zone_id(zone)
        out, offset = [], 0
        while True:
            data = await self.call('GET', f'/v2.1/zones/{zid}/recordsets',
                                   {'name': name + '.', 'type': rtype, 'search_mode': 'equal', 'limit': 500, 'offset': offset})
            items = data.get('recordsets') or []
            out += [self._record(i) for i in items if i['name'].lower().rstrip('.') == name and i['type'] == rtype]
            if len(items) < 500:
                return out
            offset += len(items)

    async def create(self, zone, name, rtype, value, ttl, opts):
        zid = await self.zone_id(zone)
        payload = {'name': name + '.', 'type': rtype, 'records': [value], 'line': (opts or {}).get('line') or 'default_view'}
        if self.ttl_for(ttl):
            payload['ttl'] = self.ttl_for(ttl)
        data = await self.call('POST', f'/v2.1/zones/{zid}/recordsets', payload=payload)
        if not data.get('id'):
            raise ProviderError('invalid_response', 'create returned no record set id')
        return self._record(data)

    async def update(self, zone, record, value, ttl, opts):
        # Omitted ttl, weight and description keep their current values; the line cannot be changed.
        zid = await self.zone_id(zone)
        payload = {'name': record.name + '.', 'type': record.type, 'records': [value]}
        if self.ttl_for(ttl):
            payload['ttl'] = self.ttl_for(ttl)
        data = await self.call('PUT', f'/v2.1/zones/{zid}/recordsets/{record.id}', payload=payload)
        if not data.get('id'):
            raise ProviderError('invalid_response', 'update returned no record set id')
        return Record(record.id, record.name, record.type, value, data.get('ttl', record.ttl), dict(record.extra))

    async def delete(self, zone, record):
        zid = await self.zone_id(zone)
        await self.call('DELETE', f'/v2.1/zones/{zid}/recordsets/{record.id}')
