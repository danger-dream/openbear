"""Vercel REST API: https://vercel.com/docs/rest-api/reference/endpoints/dns"""
from __future__ import annotations

from app.ddns.providers.base import Caps, Field, Provider, ProviderError, Record, register


@register
class Vercel(Provider):
    type = 'vercel'
    name = 'Vercel'
    tier = 'A'
    fields = (Field('token', '访问令牌', 'password', hint='在 Account Settings → Tokens 创建，Scope 选域名所在的团队或个人账户'),
              Field('team_id', '团队 ID', optional=True, hint='域名属于团队时填写（team_ 开头），个人账户留空'))
    caps = Caps(ttl_min=60, ttl_max=2147483647)
    docs = 'https://vercel.com/kb/guide/how-do-i-use-a-vercel-api-access-token'
    endpoint = 'https://api.vercel.com'

    async def _api(self, method, path, params=None, **kwargs):
        params = {**(params or {}), **({'teamId': self.creds['team_id']} if self.creds.get('team_id') else {})}
        response = await self.request(method, path, ok=range(200, 600), params=params,
                                      headers={'Authorization': 'Bearer ' + self.creds['token']}, **kwargs)
        if response.status_code < 300:
            return self.json(response) if response.content else {}
        try:
            error = response.json().get('error') or {}
        except (ValueError, AttributeError):
            raise self.http_error(response) from None
        message = f"{error.get('code')}: {error.get('message')}"
        if error.get('invalidToken'):
            raise ProviderError('auth_failed', message, status=response.status_code)
        if response.status_code == 404 and error.get('code') in ('not_found', 'domain_not_found'):
            raise ProviderError('zone_not_found', message, status=404)
        raise self.http_error(response, message)

    async def _pages(self, path, key):
        out, until = [], None
        while True:
            body = await self._api('GET', path, params={'limit': 100, **({'until': until} if until else {})})
            out += body.get(key) or []
            until = (body.get('pagination') or {}).get('next')
            if not until:
                return out

    async def zones(self):
        return sorted(d['name'].lower() for d in await self._pages('/v5/domains', 'domains'))

    async def records(self, zone, name, rtype):
        sub = self.relative(zone, name, '')
        items = await self._pages(f'/v5/domains/{zone}/records', 'records')
        return [Record(i['id'], name, i['type'], i['value'], i.get('ttl')) for i in items if (i.get('name') or '') == sub and i['type'] == rtype]

    async def create(self, zone, name, rtype, value, ttl, opts):
        body = {'name': self.relative(zone, name, ''), 'type': rtype, 'value': value}
        if self.ttl_for(ttl):
            body['ttl'] = self.ttl_for(ttl)
        return (await self._api('POST', f'/v2/domains/{zone}/records', json=body)).get('uid') or ''

    async def update(self, zone, record, value, ttl, opts):
        # PATCH keeps the fields not sent, including the comment.
        body = {'value': value}
        if self.ttl_for(ttl):
            body['ttl'] = self.ttl_for(ttl)
        await self._api('PATCH', f'/v1/domains/records/{record.id}', json=body)

    async def delete(self, zone, record):
        await self._api('DELETE', f'/v2/domains/{zone}/records/{record.id}')
