"""ClouDNS HTTP API: https://www.cloudns.net/wiki/article/41/"""
from __future__ import annotations

import bisect

from app.ddns.providers.base import Caps, Field, Provider, ProviderError, Record, register

# The only TTL values the API accepts.
TTLS = (60, 300, 900, 1800, 3600, 21600, 43200, 86400, 172800, 259200, 604800, 1209600, 2592000)
ROWS = 100


@register
class ClouDNS(Provider):
    type = 'cloudns'
    name = 'ClouDNS'
    tier = 'A'
    fields = (Field('auth_id', 'API 用户 ID', hint='在 API & Resellers 页面创建 API 用户；建议用只授权所需域名的子用户（sub-auth-id）'),
              Field('password', 'API 密码', 'password'),
              Field('kind', 'ID 类型', 'select', options=('auth-id:API 主用户 ID', 'sub-auth-id:子用户 ID', 'sub-auth-user:子用户名'),
                    optional=True, default='auth-id'))
    caps = Caps(ttl_min=60, ttl_max=2592000)
    docs = 'https://www.cloudns.net/wiki/article/42/'
    endpoint = 'https://api.cloudns.net'

    async def _api(self, path, **params):
        kind = self.creds.get('kind') or 'auth-id'
        if kind not in ('auth-id', 'sub-auth-id', 'sub-auth-user'):
            raise ProviderError('bad_request', 'unknown ClouDNS ID type: ' + kind)
        data = {kind: self.creds['auth_id'], 'auth-password': self.creds['password'], **params}
        body = self.json(await self.request('POST', path, data=data))
        if isinstance(body, dict) and body.get('status') == 'Failed':
            message = str(body.get('statusDescription') or 'Failed')
            if 'authentication' in message.lower():
                raise ProviderError('auth_failed', message)
            if 'domain' in message.lower() and ('missing' in message.lower() or 'invalid' in message.lower()):
                raise ProviderError('zone_not_found', message)
            raise ProviderError('bad_request', message)
        return body

    async def check(self):
        await self._api('/login/login.json')
        return {'zones': await self.zones(), 'writable': None}

    async def zones(self):
        out, page = [], 1
        while True:
            items = await self._api('/dns/list-zones.json', page=page, **{'rows-per-page': ROWS}) or []
            out += [z['name'].lower() for z in items if z.get('type') not in ('slave', 'parked')]
            if len(items) < ROWS:
                return sorted(out)
            page += 1

    def _ttl(self, ttl):
        ttl = self.ttl_for(ttl)
        return TTLS[min(bisect.bisect_left(TTLS, ttl), len(TTLS) - 1)] if ttl else None

    async def records(self, zone, name, rtype):
        host = self.relative(zone, name, '')
        out, page = [], 1
        while True:
            items = await self._api('/dns/records.json', **{'domain-name': zone, 'host': host, 'type': rtype, 'rows-per-page': ROWS, 'page': page})
            items = list(items.values()) if isinstance(items, dict) else items or []
            out += [Record(str(i['id']), name, i['type'], i['record'], int(i['ttl']) if i.get('ttl') else None)
                    for i in items if (i.get('host') or '').lower() == host and i['type'] == rtype]
            if len(items) < ROWS:
                return out
            page += 1

    async def create(self, zone, name, rtype, value, ttl, opts):
        params = {'domain-name': zone, 'record-type': rtype, 'host': self.relative(zone, name, ''), 'record': value, 'ttl': self._ttl(ttl) or 3600}
        body = await self._api('/dns/add-record.json', **params)
        return str((body.get('data') or {}).get('id') or '')

    async def update(self, zone, record, value, ttl, opts):
        # host and ttl are mandatory on every change.
        params = {'domain-name': zone, 'record-id': record.id, 'host': self.relative(zone, record.name, ''), 'record': value,
                  'ttl': self._ttl(ttl) or record.ttl or 3600}
        await self._api('/dns/mod-record.json', **params)

    async def delete(self, zone, record):
        await self._api('/dns/delete-record.json', **{'domain-name': zone, 'record-id': record.id})
