"""deSEC API v1: https://desec.readthedocs.io/en/latest/dns/rrsets.html

Records are values inside an RRset addressed by subname and type, so they have no id; the
whole RRset shares one TTL.
"""
from __future__ import annotations

from app.ddns.providers.base import Caps, Field, Provider, ProviderError, Record, register


@register
class Desec(Provider):
    type = 'desec'
    name = 'deSEC'
    tier = 'A'
    fields = (Field('token', 'API 令牌', 'password', hint='在 desec.io 的 Token management 创建；可用令牌策略限制到指定域名和记录'),)
    # The lower bound is each domain's minimum_ttl (3600 usually, 60 under dedyn.io), read per zone.
    caps = Caps(by_id=False, ttl_min=60, ttl_max=86400)
    docs = 'https://desec.readthedocs.io/en/latest/auth/tokens.html'
    endpoint = 'https://desec.io/api/v1'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._min_ttl = {}

    async def _api(self, method, path, **kwargs):
        response = await self.request(method, path, ok=range(200, 600), headers={'Authorization': 'Token ' + self.creds['token']}, **kwargs)
        if response.status_code < 300:
            return response
        if response.status_code == 404:
            return None
        try:
            body = response.json()
            message = str(body.get('detail') or '') if isinstance(body, dict) else ''
        except ValueError:
            message = ''
        raise self.http_error(response, message[:300])

    async def zones(self):
        out, url = [], '/domains/?cursor='
        while url:
            response = await self._api('GET', url)
            out += [d['name'].lower() for d in self.json(response)]
            url = response.links.get('next', {}).get('url')
        return sorted(out)

    def _path(self, zone, name, rtype):
        return f'/domains/{zone}/rrsets/{self.relative(zone, name)}/{rtype}/'

    async def _rrset(self, zone, name, rtype):
        response = await self._api('GET', self._path(zone, name, rtype))
        if response is None:
            if await self._api('GET', f'/domains/{zone}/') is None:
                raise ProviderError('zone_not_found', zone, status=404)
            return None
        return self.json(response)

    async def _ttl(self, zone, ttl):
        ttl = self.ttl_for(ttl)
        if not ttl:
            return None
        if zone not in self._min_ttl:
            domain = await self._api('GET', f'/domains/{zone}/')
            if domain is None:
                raise ProviderError('zone_not_found', zone, status=404)
            self._min_ttl[zone] = int(self.json(domain).get('minimum_ttl') or 3600)
        return max(ttl, self._min_ttl[zone])

    async def records(self, zone, name, rtype):
        rrset = await self._rrset(zone, name, rtype)
        return [Record('', name, rtype, v, rrset.get('ttl')) for v in rrset.get('records') or []] if rrset else []

    async def _write(self, zone, name, rtype, values, ttl):
        body = {'records': values}
        if ttl:
            body['ttl'] = ttl
        await self._api('PATCH', self._path(zone, name, rtype), json=body)

    async def create(self, zone, name, rtype, value, ttl, opts):
        ttl = await self._ttl(zone, ttl)
        rrset = await self._rrset(zone, name, rtype)
        if rrset:
            await self._write(zone, name, rtype, [*rrset['records'], value], ttl)
        else:
            body = {'subname': self.relative(zone, name, ''), 'type': rtype, 'records': [value], 'ttl': ttl or 3600}
            await self._api('POST', f'/domains/{zone}/rrsets/', json=body)
        return ''

    async def update(self, zone, record, value, ttl, opts):
        rrset = await self._rrset(zone, record.name, record.type) or {'records': []}
        values = [value if v == record.value else v for v in rrset['records']]
        if value not in values:
            values.append(value)
        await self._write(zone, record.name, record.type, list(dict.fromkeys(values)), await self._ttl(zone, ttl))

    async def delete(self, zone, record):
        rrset = await self._rrset(zone, record.name, record.type)
        if rrset and record.value in rrset['records']:
            # An empty records list deletes the RRset.
            await self._write(zone, record.name, record.type, [v for v in rrset['records'] if v != record.value], None)
