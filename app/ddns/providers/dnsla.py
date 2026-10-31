"""DNS.LA open API: https://www.dns.la/docs/ApiDoc

Records belong to a domain id looked up from the domain list; record types are numeric.
Responses carry their own code (200 on success). A change resends the record's line, group
and weight so they stay as they were.
"""
from __future__ import annotations

import base64

from app.ddns.providers.base import Caps, Field, Provider, ProviderError, Record, register

TYPES = {'A': 1, 'AAAA': 28}
PAGE = 100


@register
class Dnsla(Provider):
    type = 'dnsla'
    name = 'DNS.LA'
    tier = 'A'
    fields = (Field('api_id', 'APIID', hint='在 DNS.LA「我的账户 → API 密钥」中获取'), Field('secret', 'API 密钥', 'password'))
    # The documented range is 1~4294967295; plans may enforce a higher minimum.
    caps = Caps(ttl_min=1, ttl_max=86400)
    options = (Field('line', '线路 ID', optional=True, default='', hint='留空为默认线路；其他线路填 DNS.LA 可用线路里的线路 ID。只在新建时生效'),)
    docs = 'https://www.dns.la/docs/ApiDoc'
    endpoint = 'https://api.dns.la'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ids = None

    async def _api(self, method, path, **kwargs):
        token = base64.b64encode(f"{self.creds['api_id']}:{self.creds['secret']}".encode()).decode()
        body = self.json(await self.request(method, path, headers={'Authorization': 'Basic ' + token}, **kwargs))
        code = body.get('code') if isinstance(body, dict) else None
        if code == 200:
            return body.get('data')
        message = str(body.get('msg') or code) if isinstance(body, dict) else 'the answer is not an object'
        if code is None:
            raise ProviderError('invalid_response', message[:200])
        if code == 401:
            raise ProviderError('auth_failed', message)
        if code == 500:
            raise ProviderError('server_error', message)
        raise ProviderError('bad_request', f'{code}: {message}')

    async def _pages(self, path, **params):
        out, page = [], 1
        while True:
            data = await self._api('GET', path, params={'pageIndex': page, 'pageSize': PAGE, **params}) or {}
            items = data.get('results') or []
            out += items
            if len(items) < PAGE or len(out) >= int(data.get('total') or 0):
                return out
            page += 1

    async def _domains(self):
        if self._ids is None:
            self._ids = {d['domain'].lower().rstrip('.'): d['id'] for d in await self._pages('/api/domainList')}
        return self._ids

    async def zones(self):
        return sorted(await self._domains())

    async def _domain_id(self, zone):
        domain_id = (await self._domains()).get(zone)
        if domain_id is None:
            raise ProviderError('zone_not_found', zone)
        return domain_id

    async def _list(self, zone, name, rtype):
        host = self.relative(zone, name)
        items = await self._pages('/api/recordList', domainId=await self._domain_id(zone), host=host, type=TYPES[rtype])
        return [i for i in items if i['host'].lower() == host and i['type'] == TYPES[rtype]]

    @staticmethod
    def _line(item):
        """The record's line as the line option names it: '' for the default line."""
        line = str(item.get('lineId') or '')
        return '' if line in ('', '0') or str(item.get('lineCode') or '').lower() == 'default' else line

    async def records(self, zone, name, rtype):
        return [Record(i['id'], name, rtype, i['data'], i.get('ttl'),
                       {'line': self._line(i), 'lineId': i.get('lineId', ''), 'groupId': i.get('groupId', ''), 'weight': i.get('weight')})
                for i in await self._list(zone, name, rtype)]

    async def create(self, zone, name, rtype, value, ttl, opts):
        body = {'domainId': await self._domain_id(zone), 'type': TYPES[rtype], 'host': self.relative(zone, name), 'data': value,
                'ttl': self.ttl_for(ttl) or 600}
        if (opts or {}).get('line'):
            body['lineId'] = opts['line']
        return str((await self._api('POST', '/api/record', json=body) or {}).get('id') or '')

    async def update(self, zone, record, value, ttl, opts):
        extra, old_ttl = record.extra, record.ttl
        if 'lineId' not in extra:
            current = next((i for i in await self._list(zone, record.name, record.type) if i['id'] == record.id), {})
            extra = {'lineId': current.get('lineId', ''), 'groupId': current.get('groupId', ''), 'weight': current.get('weight')}
            old_ttl = old_ttl or current.get('ttl')
        body = {'id': record.id, 'type': TYPES[record.type], 'host': self.relative(zone, record.name), 'data': value,
                'ttl': self.ttl_for(ttl) or old_ttl or 600, 'lineId': extra.get('lineId') or '', 'groupId': extra.get('groupId') or ''}
        if extra.get('weight'):
            body['weight'] = extra['weight']
        await self._api('PUT', '/api/record', json=body)

    async def delete(self, zone, record):
        await self._api('DELETE', '/api/record', params={'id': record.id})
