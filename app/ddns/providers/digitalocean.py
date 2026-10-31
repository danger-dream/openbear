"""DigitalOcean API v2 domains: https://docs.digitalocean.com/reference/api/digitalocean/#tag/Domain-Records

PATCH changes only the attributes sent; type is the one required field.
"""
from __future__ import annotations

from app.ddns.providers.base import Caps, Field, Provider, ProviderError, Record, register


@register
class DigitalOcean(Provider):
    type = 'digitalocean'
    name = 'DigitalOcean'
    tier = 'A'
    fields = (Field('token', '个人访问令牌', 'password', hint='在 API → Tokens 生成，选 Custom Scopes，只勾选 domain 的 read、create、update、delete'),)
    caps = Caps(ttl_min=30, ttl_max=86400)
    docs = 'https://docs.digitalocean.com/reference/api/create-personal-access-token/'
    endpoint = 'https://api.digitalocean.com/v2'

    async def _api(self, method, path, **kwargs):
        response = await self.request(method, path, ok=range(200, 600), headers={'Authorization': 'Bearer ' + self.creds['token']}, **kwargs)
        if response.status_code < 300:
            return self.json(response) if response.content else {}
        try:
            body = response.json()
            message = f"{body.get('id', '')}: {body.get('message', '')}"
        except (ValueError, AttributeError):
            message = f'HTTP {response.status_code}'
        if response.status_code == 404 and '/records' in path and method == 'GET':
            raise ProviderError('zone_not_found', message, status=404)
        raise self.http_error(response, message)

    async def _pages(self, path, key, **params):
        out, url, params = [], path, {'per_page': 200, **params}
        while url:
            body = await self._api('GET', url, params=params)
            out += body.get(key) or []
            url, params = ((body.get('links') or {}).get('pages') or {}).get('next'), None
        return out

    async def zones(self):
        return sorted(d['name'].lower() for d in await self._pages('/domains', 'domains'))

    async def records(self, zone, name, rtype):
        items = await self._pages(f'/domains/{zone}/records', 'domain_records', name=name, type=rtype)
        return [Record(str(i['id']), name, i['type'], i['data'], i.get('ttl')) for i in items
                if self.fqdn(zone, i['name']) == name and i['type'] == rtype]

    async def create(self, zone, name, rtype, value, ttl, opts):
        body = {'type': rtype, 'name': self.relative(zone, name), 'data': value}
        if ttl := self.ttl_for(ttl):
            body['ttl'] = ttl
        return str((await self._api('POST', f'/domains/{zone}/records', json=body))['domain_record']['id'])

    async def update(self, zone, record, value, ttl, opts):
        body = {'type': record.type, 'data': value}
        if ttl := self.ttl_for(ttl):
            body['ttl'] = ttl
        await self._api('PATCH', f'/domains/{zone}/records/{record.id}', json=body)

    async def delete(self, zone, record):
        await self._api('DELETE', f'/domains/{zone}/records/{record.id}')
