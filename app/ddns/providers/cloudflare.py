"""Cloudflare API v4: https://developers.cloudflare.com/api/resources/dns/subresources/records/"""
from __future__ import annotations

from app.ddns.providers.base import Caps, Field, Provider, ProviderError, Record, register

# Error codes Cloudflare returns with HTTP 400 for a malformed or unknown token.
AUTH_CODES = {6003, 6111, 9106, 9109, 10000, 10001}


@register
class Cloudflare(Provider):
    type = 'cloudflare'
    name = 'Cloudflare'
    tier = 'A'
    fields = (Field('token', 'API 令牌', 'password', hint='用“编辑区域 DNS”模板创建：区域 → DNS → 编辑，区域 → 区域 → 读取'),)
    options = (Field('proxied', '经 Cloudflare 代理（橙色云朵）', 'bool', optional=True),)
    caps = Caps(ttl_min=60, ttl_max=86400, ttl_auto=True)
    docs = 'https://developers.cloudflare.com/fundamentals/api/get-started/create-token/'
    endpoint = 'https://api.cloudflare.com/client/v4'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._zone_ids = {}

    async def _api(self, method, path, **kwargs):
        response = await self.request(method, path, ok=range(200, 600), headers={'Authorization': 'Bearer ' + self.creds['token']}, **kwargs)
        if response.status_code >= 300:
            try:
                body = response.json()
            except ValueError:
                raise self.http_error(response) from None
        else:
            body = self.json(response) if response.content else {}
            if not isinstance(body, dict) or body.get('success') is not False:
                return body
        errors = (body.get('errors') or []) if isinstance(body, dict) else []
        message = '; '.join(f"{e.get('code')}: {e.get('message')}" for e in errors) or f'HTTP {response.status_code}'
        if response.status_code < 300:
            raise ProviderError('bad_request', message, status=response.status_code)
        if response.status_code == 400 and any(e.get('code') in AUTH_CODES for e in errors):
            raise ProviderError('auth_failed', message, status=400)
        raise self.http_error(response, message)

    async def _pages(self, path, params):
        page, out = 1, []
        while True:
            body = await self._api('GET', path, params={**params, 'page': page, 'per_page': 100})
            out += body.get('result') or []
            info = body.get('result_info') or {}
            if page >= (info.get('total_pages') or 1):
                return out
            page += 1

    async def zones(self):
        return sorted(z['name'].lower() for z in await self._pages('/zones', {}))

    async def _zone_id(self, zone):
        if zone not in self._zone_ids:
            found = (await self._api('GET', '/zones', params={'name': zone})).get('result') or []
            if not found:
                raise ProviderError('zone_not_found', zone)
            self._zone_ids[zone] = found[0]['id']
        return self._zone_ids[zone]

    @staticmethod
    def _record(item):
        return Record(item['id'], item['name'].lower(), item['type'], item['content'], item.get('ttl'), {'proxied': bool(item.get('proxied'))})

    async def records(self, zone, name, rtype):
        items = await self._pages(f'/zones/{await self._zone_id(zone)}/dns_records', {'type': rtype, 'name': name})
        return [self._record(i) for i in items if i['name'].lower() == name and i['type'] == rtype]

    def _ttl(self, ttl):
        return 1 if ttl == 1 else self.ttl_for(ttl)

    async def create(self, zone, name, rtype, value, ttl, opts):
        body = {'type': rtype, 'name': name, 'content': value, 'ttl': self._ttl(ttl) or 1}
        if 'proxied' in (opts or {}):
            body['proxied'] = bool(opts['proxied'])
        result = (await self._api('POST', f'/zones/{await self._zone_id(zone)}/dns_records', json=body))['result']
        return self._record(result)

    async def update(self, zone, record, value, ttl, opts):
        # PATCH keeps every field not sent, including proxied, comment and tags.
        body = {'content': value}
        if self._ttl(ttl):
            body['ttl'] = self._ttl(ttl)
        if 'proxied' in (opts or {}):
            body['proxied'] = bool(opts['proxied'])
        result = (await self._api('PATCH', f'/zones/{await self._zone_id(zone)}/dns_records/{record.id}', json=body))['result']
        return self._record(result)

    async def delete(self, zone, record):
        await self._api('DELETE', f'/zones/{await self._zone_id(zone)}/dns_records/{record.id}')
