"""Spaceship API v1: https://docs.spaceship.dev/

Records have no id: they are identified by name, type and address. Saving a record with a
new address adds it, so a change saves the new value first and then deletes the old one.
"""
from __future__ import annotations

from app.ddns.providers.base import Caps, Field, Provider, ProviderError, Record, register


@register
class Spaceship(Provider):
    type = 'spaceship'
    name = 'Spaceship'
    tier = 'A'
    fields = (Field('key', 'API Key', hint='在 API Manager 创建，权限勾选 dnsrecords:read 和 dnsrecords:write（列域名需 domains:read）'),
              Field('secret', 'API Secret', 'password'))
    caps = Caps(by_id=False, ttl_min=60, ttl_max=3600)
    docs = 'https://www.spaceship.com/application/api-manager/'
    endpoint = 'https://spaceship.dev/api'

    async def _api(self, method, path, **kwargs):
        headers = {'X-API-Key': self.creds['key'], 'X-API-Secret': self.creds['secret']}
        response = await self.request(method, path, ok=range(200, 600), headers=headers, **kwargs)
        if response.status_code < 300:
            return self.json(response) if response.content else {}
        try:
            body = response.json()
            message = '; '.join([str(body.get('detail'))] + [f"{d.get('field')}: {d.get('details')}" for d in body.get('data') or []])
        except (ValueError, AttributeError):
            message = ''
        message = f"{response.headers.get('spaceship-error-code', '')} {message}".strip()
        if response.status_code == 404 and path.startswith('/v1/dns/records/'):
            raise ProviderError('zone_not_found', message, status=404)
        raise self.http_error(response, message)

    async def _pages(self, path):
        out = []
        while True:
            body = await self._api('GET', path, params={'take': 500 if 'dns' in path else 100, 'skip': len(out)})
            items = body.get('items') or []
            out += items
            if not items or len(out) >= int(body.get('total') or 0):
                return out

    async def zones(self):
        return sorted(d['name'].lower() for d in await self._pages('/v1/domains'))

    async def records(self, zone, name, rtype):
        host = self.relative(zone, name)
        return [Record('', name, rtype, i['address'], i.get('ttl')) for i in await self._pages(f'/v1/dns/records/{zone}')
                if i['name'].lower() == host and i['type'] == rtype and i.get('address')]

    async def _save(self, zone, name, rtype, value, ttl):
        item = {'type': rtype, 'name': self.relative(zone, name), 'address': value}
        if ttl:
            item['ttl'] = ttl
        await self._api('PUT', f'/v1/dns/records/{zone}', json={'force': False, 'items': [item]})

    async def create(self, zone, name, rtype, value, ttl, opts):
        await self._save(zone, name, rtype, value, self.ttl_for(ttl))
        return ''

    async def update(self, zone, record, value, ttl, opts):
        await self._save(zone, record.name, record.type, value, self.ttl_for(ttl) or record.ttl)
        if value != record.value:
            await self.delete(zone, record)

    async def delete(self, zone, record):
        item = {'type': record.type, 'name': self.relative(zone, record.name), 'address': record.value}
        await self._api('DELETE', f'/v1/dns/records/{zone}', json=[item])
