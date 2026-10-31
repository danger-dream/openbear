"""Hetzner DNS through the Hetzner Cloud API: https://docs.hetzner.cloud/reference/cloud#zone-rrsets

The old DNS Console API (dns.hetzner.com) was shut down in May 2026; zones now live in Hetzner
Console projects. Records are values inside an RRSet addressed by name and type, so they have
no id; the RRSet shares one TTL (null means the zone default). Writes are asynchronous actions,
and a second action on the same zone is refused while one is running.
"""
from __future__ import annotations

import asyncio

from app.ddns.providers.base import Caps, Field, Provider, ProviderError, Record, register

PAGE = 100
WAIT = (0.2, 0.5, 1, 1, 2, 2, 3)


@register
class Hetzner(Provider):
    type = 'hetzner'
    name = 'Hetzner DNS'
    tier = 'A'
    fields = (Field('token', 'API 令牌', 'password',
                    hint='Hetzner Console → 项目 → Security → API Tokens 生成「Read & Write」令牌。令牌作用于整个项目，建议把域名放在单独的项目里。'
                         '旧版 dns.hetzner.com 的令牌已不可用'),)
    caps = Caps(by_id=False, ttl_min=60, ttl_max=86400)
    docs = 'https://docs.hetzner.com/cloud/api/getting-started/generating-api-token/'
    endpoint = 'https://api.hetzner.cloud/v1'

    async def _api(self, method, path, *, missing_ok=False, **kwargs):
        response = await self.request(method, path, ok=range(200, 600), headers={'Authorization': 'Bearer ' + self.creds['token']}, **kwargs)
        if response.status_code < 300:
            return self.json(response) if response.content else {}
        try:
            error = response.json().get('error') or {}
            code, message = error.get('code', ''), f"{error.get('code', '')}: {error.get('message', '')}"
        except (ValueError, AttributeError):
            code, message = '', f'HTTP {response.status_code}'
        if response.status_code == 404 and missing_ok:
            return None
        if code in ('locked', 'conflict'):
            raise ProviderError('record_conflict', message, status=response.status_code)
        if code == 'incorrect_zone_mode':
            raise ProviderError('unsupported', message, status=response.status_code)
        if response.status_code == 404:
            raise ProviderError('zone_not_found', message, status=404)
        raise self.http_error(response, message)

    async def zones(self):
        out, page = [], 1
        while page:
            body = await self._api('GET', '/zones', params={'mode': 'primary', 'page': page, 'per_page': PAGE})
            out += [z['name'].lower() for z in body.get('zones') or []]
            page = ((body.get('meta') or {}).get('pagination') or {}).get('next_page')
        return sorted(out)

    def _path(self, zone, name, rtype):
        return f'/zones/{zone}/rrsets/{self.relative(zone, name)}/{rtype}'

    async def _rrset(self, zone, name, rtype):
        body = await self._api('GET', self._path(zone, name, rtype), missing_ok=True)
        if body is None:
            await self._api('GET', f'/zones/{zone}')
            return None
        return body['rrset']

    async def _action(self, path, body):
        action = (await self._api('POST', path, json=body)).get('action') or {}
        for delay in WAIT:
            if action.get('status') != 'running':
                break
            await asyncio.sleep(delay)
            action = (await self._api('GET', f"/zones/actions/{action['id']}")).get('action') or {}
        if action.get('status') == 'error':
            error = action.get('error') or {}
            raise ProviderError('bad_request', f"{error.get('code', '')}: {error.get('message', '')}")

    async def records(self, zone, name, rtype):
        rrset = await self._rrset(zone, name, rtype)
        return [Record('', name, rtype, r['value'], rrset.get('ttl')) for r in rrset.get('records') or []] if rrset else []

    async def create(self, zone, name, rtype, value, ttl, opts):
        body = {'records': [{'value': value}]}
        if ttl := self.ttl_for(ttl):
            body['ttl'] = ttl
        # Creates the RRSet when it does not exist yet.
        await self._action(self._path(zone, name, rtype) + '/actions/add_records', body)
        return ''

    async def update(self, zone, record, value, ttl, opts):
        rrset = await self._rrset(zone, record.name, record.type)
        if not rrset:
            await self.create(zone, record.name, record.type, value, ttl, opts)
            return
        path = self._path(zone, record.name, record.type)
        records = [dict(r) for r in rrset.get('records') or []]
        target = next((r for r in records if r['value'] == record.value), None)
        if target is None:
            records.append(target := {})
        target['value'] = value
        records = list({r['value']: r for r in records}.values())
        if records != rrset.get('records'):
            await self._action(path + '/actions/set_records', {'records': records})
        ttl = self.ttl_for(ttl)
        if ttl and ttl != rrset.get('ttl'):
            await self._action(path + '/actions/change_ttl', {'ttl': ttl})

    async def delete(self, zone, record):
        # Removing the last value deletes the RRSet.
        await self._action(self._path(zone, record.name, record.type) + '/actions/remove_records', {'records': [{'value': record.value}]})
