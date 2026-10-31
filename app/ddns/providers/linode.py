"""Linode (Akamai Cloud) API v4 domains: https://techdocs.akamai.com/linode-api/reference/put-domain-record

Records belong to a numeric domain id looked up from the domain list. PUT changes only the
fields sent. ttl_sec is rounded by the API to its nearest allowed value; 0 means the domain default.
"""
from __future__ import annotations

from app.ddns.providers.base import Caps, Field, Provider, ProviderError, Record, register

PAGE = 500


@register
class Linode(Provider):
    type = 'linode'
    name = 'Linode (Akamai Cloud)'
    tier = 'A'
    fields = (Field('token', '个人访问令牌', 'password', hint='在 Cloud Manager → API Tokens 创建，只给 Domains 读写权限，其余设为 None'),)
    caps = Caps(ttl_min=300, ttl_max=2419200)
    docs = 'https://techdocs.akamai.com/cloud-computing/docs/manage-personal-access-tokens'
    endpoint = 'https://api.linode.com/v4'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ids = None

    async def _api(self, method, path, **kwargs):
        response = await self.request(method, path, ok=range(200, 600), headers={'Authorization': 'Bearer ' + self.creds['token']}, **kwargs)
        if response.status_code < 300:
            return self.json(response)
        try:
            message = '; '.join(f"{e.get('field', '')}: {e.get('reason', '')}".lstrip(': ') for e in response.json().get('errors') or [])
        except (ValueError, AttributeError):
            message = ''
        raise self.http_error(response, message)

    async def _pages(self, path):
        out, page = [], 1
        while True:
            body = await self._api('GET', path, params={'page': page, 'page_size': PAGE})
            out += body.get('data') or []
            if page >= int(body.get('pages') or 1):
                return out
            page += 1

    async def _domains(self):
        if self._ids is None:
            self._ids = {d['domain'].lower(): d['id'] for d in await self._pages('/domains') if d.get('type', 'master') == 'master'}
        return self._ids

    async def zones(self):
        return sorted(await self._domains())

    async def _path(self, zone):
        domain_id = (await self._domains()).get(zone)
        if domain_id is None:
            raise ProviderError('zone_not_found', zone)
        return f'/domains/{domain_id}/records'

    async def records(self, zone, name, rtype):
        host = self.relative(zone, name, '')
        return [Record(str(i['id']), name, rtype, i['target'], i.get('ttl_sec') or None) for i in await self._pages(await self._path(zone))
                if (i.get('name') or '').lower() == host and i['type'] == rtype]

    async def create(self, zone, name, rtype, value, ttl, opts):
        body = {'type': rtype, 'name': self.relative(zone, name, ''), 'target': value}
        if ttl := self.ttl_for(ttl):
            body['ttl_sec'] = ttl
        return str((await self._api('POST', await self._path(zone), json=body))['id'])

    async def update(self, zone, record, value, ttl, opts):
        body = {'target': value}
        if ttl := self.ttl_for(ttl):
            body['ttl_sec'] = ttl
        await self._api('PUT', f'{await self._path(zone)}/{record.id}', json=body)

    async def delete(self, zone, record):
        await self._api('DELETE', f'{await self._path(zone)}/{record.id}')
