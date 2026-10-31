"""Vultr API v2 DNS: https://www.vultr.com/api/#tag/dns

Lists page with a cursor (meta.links.next). PATCH changes only the fields sent.
"""
from __future__ import annotations

from app.ddns.providers.base import Caps, Field, Provider, ProviderError, Record, register


@register
class Vultr(Provider):
    type = 'vultr'
    name = 'Vultr'
    tier = 'A'
    fields = (Field('key', 'API 密钥', 'password', hint='在 Account → API 启用并复制；Vultr 密钥无法按权限细分，务必设置 Access Control 只允许主控的出口 IP'),)
    caps = Caps(ttl_min=60, ttl_max=86400)
    docs = 'https://docs.vultr.com/platform/other/api/manage-api-access-control'
    endpoint = 'https://api.vultr.com/v2'

    async def _api(self, method, path, **kwargs):
        response = await self.request(method, path, ok=range(200, 600), headers={'Authorization': 'Bearer ' + self.creds['key']}, **kwargs)
        if response.status_code < 300:
            return self.json(response) if response.content else {}
        try:
            message = str(response.json().get('error') or '')
        except (ValueError, AttributeError):
            message = ''
        if response.status_code == 404 and method == 'GET' and path.endswith('/records'):
            raise ProviderError('zone_not_found', message, status=404)
        raise self.http_error(response, message)

    async def _pages(self, path, key):
        out, cursor = [], ''
        while True:
            params = {'per_page': 500, **({'cursor': cursor} if cursor else {})}
            body = await self._api('GET', path, params=params)
            out += body.get(key) or []
            cursor = ((body.get('meta') or {}).get('links') or {}).get('next')
            if not cursor:
                return out

    async def zones(self):
        return sorted(d['domain'].lower() for d in await self._pages('/domains', 'domains'))

    async def records(self, zone, name, rtype):
        host = self.relative(zone, name, '')
        return [Record(i['id'], name, rtype, i['data'], i.get('ttl')) for i in await self._pages(f'/domains/{zone}/records', 'records')
                if (i.get('name') or '').lower() == host and i['type'] == rtype]

    async def create(self, zone, name, rtype, value, ttl, opts):
        body = {'name': self.relative(zone, name, ''), 'type': rtype, 'data': value}
        if ttl := self.ttl_for(ttl):
            body['ttl'] = ttl
        return (await self._api('POST', f'/domains/{zone}/records', json=body))['record']['id']

    async def update(self, zone, record, value, ttl, opts):
        body = {'data': value}
        if ttl := self.ttl_for(ttl):
            body['ttl'] = ttl
        await self._api('PATCH', f'/domains/{zone}/records/{record.id}', json=body)

    async def delete(self, zone, record):
        await self._api('DELETE', f'/domains/{zone}/records/{record.id}')
