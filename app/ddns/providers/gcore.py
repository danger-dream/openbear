"""Gcore Managed DNS API v2: https://gcore.com/docs/api-reference/dns

Records live in an RRset addressed by name and type, and PUT replaces the whole RRset; meta
(such as geodns_link) and pickers must be sent back or they are dropped.
"""
from __future__ import annotations

from app.ddns.providers.base import Caps, Field, Provider, ProviderError, Record, register


@register
class Gcore(Provider):
    type = 'gcore'
    name = 'Gcore'
    tier = 'A'
    fields = (Field('token', '永久 API 令牌', 'password', hint='在 Gcore 控制台 → Profile → API tokens 创建，角色选择能管理 DNS 的最小角色'),)
    caps = Caps(by_id=False, ttl_min=60, ttl_max=86400)
    docs = 'https://gcore.com/docs/account-settings/create-use-or-delete-a-permanent-api-token'
    endpoint = 'https://api.gcore.com/dns/v2'

    async def _api(self, method, path, **kwargs):
        response = await self.request(method, path, ok=range(200, 600), headers={'Authorization': 'APIKey ' + self.creds['token']}, **kwargs)
        if response.status_code < 300:
            return self.json(response) if response.content else {}
        if response.status_code == 404 and method == 'GET':
            return None
        try:
            body = response.json()
            message = str(body.get('error') or body.get('message') or '') if isinstance(body, dict) else ''
        except ValueError:
            message = ''
        raise self.http_error(response, message[:300])

    async def zones(self):
        out = []
        while True:
            body = await self._api('GET', '/zones', params={'limit': 1000, 'offset': len(out)}) or {}
            page = body.get('zones') or []
            out += [z['name'].lower() for z in page]
            if not page or len(out) >= int(body.get('total_amount') or 0):
                return sorted(out)

    def _path(self, zone, name, rtype):
        return f'/zones/{zone}/{name}/{rtype}'

    async def _rrset(self, zone, name, rtype):
        rrset = await self._api('GET', self._path(zone, name, rtype))
        if rrset is None and await self._api('GET', f'/zones/{zone}') is None:
            raise ProviderError('zone_not_found', zone, status=404)
        return rrset

    async def records(self, zone, name, rtype):
        rrset = await self._rrset(zone, name, rtype) or {}
        return [Record('', name, rtype, str(r['content'][0]), rrset.get('ttl')) for r in rrset.get('resource_records') or [] if r.get('content')]

    async def _put(self, zone, name, rtype, rrset, records, ttl):
        body = {'resource_records': [{k: r[k] for k in ('content', 'enabled', 'meta') if k in r} for r in records]}
        for key in ('meta', 'pickers'):
            if rrset.get(key):
                body[key] = rrset[key]
        if ttl or rrset.get('ttl'):
            body['ttl'] = ttl or rrset['ttl']
        await self._api('PUT', self._path(zone, name, rtype), json=body)

    async def create(self, zone, name, rtype, value, ttl, opts):
        rrset = await self._rrset(zone, name, rtype)
        if rrset:
            await self._put(zone, name, rtype, rrset, [*rrset['resource_records'], {'content': [value]}], self.ttl_for(ttl))
        else:
            body = {'resource_records': [{'content': [value]}], **({'ttl': self.ttl_for(ttl)} if self.ttl_for(ttl) else {})}
            await self._api('POST', self._path(zone, name, rtype), json=body)
        return ''

    async def update(self, zone, record, value, ttl, opts):
        rrset = await self._rrset(zone, record.name, record.type) or {'resource_records': []}
        records = rrset['resource_records']
        target = next((r for r in records if r.get('content') and str(r['content'][0]) == record.value), None)
        if target is None:
            records.append(target := {})
        target['content'] = [value]
        await self._put(zone, record.name, record.type, rrset, records, self.ttl_for(ttl))

    async def delete(self, zone, record):
        rrset = await self._rrset(zone, record.name, record.type)
        if not rrset:
            return
        rest = [r for r in rrset['resource_records'] if not r.get('content') or str(r['content'][0]) != record.value]
        if rest:
            await self._put(zone, record.name, record.type, rrset, rest, None)
        else:
            await self._api('DELETE', self._path(zone, record.name, record.type))
