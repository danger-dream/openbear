"""Rainyun (雨云) API v2: https://s.apifox.cn/a4595cc8-44c5-4678-a2a3-eed7738dab03/api-153559362

Records are addressed by the numeric domain id, which is looked up from the domain list.
A change must resend the whole record (host, type, line, level, ttl), otherwise it is
created as a new one; responses carry their own code (200 on success).
"""
from __future__ import annotations

import json

from app.ddns.providers.base import Caps, Field, Provider, ProviderError, Record, register

PAGE = 100


@register
class Rainyun(Provider):
    type = 'rainyun'
    name = '雨云'
    tier = 'A'
    fields = (Field('key', 'API 密钥', 'password', hint='在雨云控制台「账户设置 → API 密钥」生成；该密钥对整个账号有效，雨云不提供按域名限权'),)
    caps = Caps(ttl_min=60, ttl_max=86400)
    docs = 'https://app.rainyun.com/account/settings/api-key'
    endpoint = 'https://api.v2.rainyun.com'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ids = None

    async def _api(self, method, path, **kwargs):
        response = await self.request(method, path, ok=range(200, 600), headers={'x-api-key': self.creds['key']}, **kwargs)
        try:
            body = response.json()
        except ValueError:
            body = None
        code = body.get('code') if isinstance(body, dict) else None
        if response.status_code < 300 and code == 200:
            return body.get('data')
        message = str(body.get('message') or f'code {code}') if isinstance(body, dict) else f'HTTP {response.status_code}'
        if response.status_code >= 300:
            raise self.http_error(response, message)
        if code == 401:
            raise ProviderError('auth_failed', message)
        if code == 403:
            raise ProviderError('forbidden', message)
        if code is None:
            raise ProviderError('invalid_response', message[:200])
        raise ProviderError('bad_request', message)

    async def _domains(self):
        if self._ids is None:
            ids, page = {}, 1
            while True:
                options = json.dumps({'columnFilters': {'domains.Domain': ''}, 'sort': [], 'page': page, 'perPage': PAGE}, separators=(',', ':'))
                data = await self._api('GET', '/product/domain/', params={'options': options}) or {}
                items = data.get('Records') or []
                ids.update({d['domain'].lower(): d['id'] for d in items})
                if len(items) < PAGE or len(ids) >= int(data.get('TotalRecords') or 0):
                    break
                page += 1
            self._ids = ids
        return self._ids

    async def zones(self):
        return sorted(await self._domains())

    async def _path(self, zone):
        domain_id = (await self._domains()).get(zone)
        if domain_id is None:
            raise ProviderError('zone_not_found', zone)
        return f'/product/domain/{domain_id}/dns'

    async def _list(self, zone):
        path, out, page = await self._path(zone), [], 1
        while True:
            data = await self._api('GET', path + '/', params={'limit': PAGE, 'page_no': page}) or {}
            items = data.get('Records') or []
            out += items
            if len(items) < PAGE or len(out) >= int(data.get('TotalRecords') or 0):
                return out
            page += 1

    async def records(self, zone, name, rtype):
        host = self.relative(zone, name)
        return [Record(str(r['record_id']), name, rtype, r['value'], r.get('ttl'), {'line': r.get('line'), 'level': r.get('level')})
                for r in await self._list(zone) if r['host'].lower() == host and r['type'] == rtype]

    async def create(self, zone, name, rtype, value, ttl, opts):
        body = {'record_id': 0, 'host': self.relative(zone, name), 'type': rtype, 'value': value, 'line': 'DEFAULT', 'level': 10,
                'ttl': self.ttl_for(ttl) or 600}
        return str(await self._api('POST', await self._path(zone), json=body))

    async def update(self, zone, record, value, ttl, opts):
        current = {}
        if 'line' not in record.extra or 'level' not in record.extra:
            current = next((r for r in await self._list(zone) if str(r['record_id']) == record.id), {})
        body = {'record_id': int(record.id), 'host': self.relative(zone, record.name), 'type': record.type, 'value': value,
                'line': record.extra.get('line') or current.get('line') or 'DEFAULT',
                'level': record.extra.get('level') or current.get('level') or 10,
                'ttl': self.ttl_for(ttl) or record.ttl or current.get('ttl') or 600}
        await self._api('PATCH', await self._path(zone), json=body)

    async def delete(self, zone, record):
        # The documentation puts record_id in the body; it is also sent as a query parameter.
        await self._api('DELETE', await self._path(zone) + '/', params={'record_id': record.id}, json={'record_id': int(record.id)})
