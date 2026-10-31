"""Porkbun API v3: https://porkbun.com/api/json/v3/documentation"""
from __future__ import annotations

from app.ddns.providers.base import Caps, Field, Provider, ProviderError, Record, register

AUTH = {'API_KEY_REQUIRED', 'INVALID_API_KEYS_001', 'INVALID_TOKEN', 'INVALID_USER'}
DENIED = {'INSUFFICIENT_SCOPE', 'IP_NOT_ALLOWED', 'DOMAIN_NOT_ALLOWED', 'API_ACCESS_DISABLED'}


@register
class Porkbun(Provider):
    type = 'porkbun'
    name = 'Porkbun'
    tier = 'A'
    fields = (Field('api_key', 'API Key', hint='以 pk1_ 开头'),
              Field('secret_key', 'Secret API Key', 'password', hint='以 sk1_ 开头；域名还需在域名管理里开启 API Access'))
    # The minimum TTL is an account setting, normally 600.
    caps = Caps(ttl_min=600, ttl_max=86400)
    docs = 'https://kb.porkbun.com/article/190-getting-started-with-the-porkbun-api'
    endpoint = 'https://api.porkbun.com/api/json/v3'

    async def _api(self, path, **body):
        response = await self.request('POST', path, ok=range(200, 600),
                                      json={'apikey': self.creds['api_key'], 'secretapikey': self.creds['secret_key'], **body})
        try:
            data = response.json()
        except ValueError:
            if response.status_code >= 300:
                raise self.http_error(response) from None
            raise ProviderError('invalid_response', f'not JSON (HTTP {response.status_code})') from None
        if response.status_code < 300 and data.get('status') == 'SUCCESS':
            return data
        code = data.get('code') or ''
        message = f"{code}: {data.get('message')}"
        if code in AUTH:
            raise ProviderError('auth_failed', message, status=response.status_code)
        if code in DENIED:
            raise ProviderError('forbidden', message, status=response.status_code)
        if code == 'DOMAIN_NOT_FOUND':
            raise ProviderError('zone_not_found', message, status=response.status_code)
        if response.status_code < 300:
            raise ProviderError('bad_request', message, status=response.status_code)
        raise self.http_error(response, message)

    async def zones(self):
        out = []
        while True:
            page = (await self._api('/domain/listAll', start=len(out))).get('domains') or []
            out += [d['domain'].lower() for d in page]
            if len(page) < 1000:
                return sorted(out)

    async def check(self):
        scopes = (await self._api('/ping')).get('scopes')
        writable = None if scopes is None else bool({'dns', 'full'} & set(scopes))
        return {'zones': await self.zones(), 'writable': writable}

    async def records(self, zone, name, rtype):
        sub = self.relative(zone, name, '')
        items = (await self._api(f'/dns/retrieveByNameType/{zone}/{rtype}/{sub}'.rstrip('/'))).get('records') or []
        return [Record(str(i['id']), i['name'].lower(), i['type'], i['content'], int(i['ttl']) if i.get('ttl') else None)
                for i in items if i['name'].lower() == name and i['type'] == rtype]

    async def create(self, zone, name, rtype, value, ttl, opts):
        body = {'name': self.relative(zone, name, ''), 'type': rtype, 'content': value}
        if self.ttl_for(ttl):
            body['ttl'] = self.ttl_for(ttl)
        return str((await self._api(f'/dns/create/{zone}', **body)).get('id') or '')

    async def update(self, zone, record, value, ttl, opts):
        # Fields left out of an edit keep their current value.
        body = {'content': value}
        if self.ttl_for(ttl):
            body['ttl'] = self.ttl_for(ttl)
        await self._api(f'/dns/edit/{zone}/{record.id}', **body)

    async def delete(self, zone, record):
        await self._api(f'/dns/delete/{zone}/{record.id}')
