"""dynv6 REST API v2: https://dynv6.github.io/api-spec/

The zone apex has no A/AAAA records: its addresses are the zone's own ipv4address and
ipv6prefix fields, written with PATCH /zones/{id}. TTLs are not exposed by the API.
"""
from __future__ import annotations

from app.ddns.providers.base import Caps, Field, Provider, ProviderError, Record, register

APEX = {'A': 'ipv4address', 'AAAA': 'ipv6prefix'}


@register
class Dynv6(Provider):
    type = 'dynv6'
    name = 'dynv6'
    tier = 'A'
    fields = (Field('token', 'HTTP 令牌', 'password', hint='在 dynv6.com/keys 创建 HTTP Token，可限定到单个 zone'),)
    caps = Caps(ttl_min=60, ttl_max=60)
    docs = 'https://dynv6.com/keys'
    endpoint = 'https://dynv6.com/api/v2'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._zone_ids = {}

    async def _api(self, method, path, **kwargs):
        headers = {'Authorization': 'Bearer ' + self.creds['token'], 'Accept': 'application/json'}
        response = await self.request(method, path, headers=headers, **kwargs)
        return self.json(response) if response.content else None

    async def zones(self):
        return sorted(z['name'].lower() for z in await self._api('GET', '/zones') or [])

    async def _zone(self, zone):
        try:
            found = await self._api('GET', f'/zones/by-name/{zone}')
        except ProviderError as exc:
            raise ProviderError('zone_not_found', zone, status=404) if exc.status == 404 else exc from None
        self._zone_ids[zone] = found['id']
        return found

    async def _zone_id(self, zone):
        return self._zone_ids.get(zone) or (await self._zone(zone))['id']

    async def records(self, zone, name, rtype):
        if name == zone:
            value = (await self._zone(zone)).get(APEX[rtype])
            return [Record('', name, rtype, value)] if value else []
        sub = self.relative(zone, name)
        items = await self._api('GET', f'/zones/{await self._zone_id(zone)}/records') or []
        return [Record(str(i['id']), name, i['type'], i['data']) for i in items if (i.get('name') or '').lower() == sub and i['type'] == rtype]

    async def create(self, zone, name, rtype, value, ttl, opts):
        zid = await self._zone_id(zone)
        if name == zone:
            await self._api('PATCH', f'/zones/{zid}', json={APEX[rtype]: value})
            return ''
        item = await self._api('POST', f'/zones/{zid}/records', json={'name': self.relative(zone, name), 'type': rtype, 'data': value})
        return str((item or {}).get('id') or '')

    async def update(self, zone, record, value, ttl, opts):
        zid = await self._zone_id(zone)
        if record.name == zone:
            await self._api('PATCH', f'/zones/{zid}', json={APEX[record.type]: value})
        else:
            await self._api('PATCH', f'/zones/{zid}/records/{record.id}', json={'data': value})

    async def delete(self, zone, record):
        zid = await self._zone_id(zone)
        if record.name == zone:
            await self._api('PATCH', f'/zones/{zid}', json={APEX[record.type]: ''})
        else:
            await self._api('DELETE', f'/zones/{zid}/records/{record.id}')
