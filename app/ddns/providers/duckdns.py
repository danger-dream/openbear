"""DuckDNS update API: https://www.duckdns.org/spec.jsp

Update-only: there is no way to read a record or list domains, so records() raises
'unsupported' and the service writes without reading. Names are <sub>.duckdns.org; deeper
names share their parent's addresses. The ip parameter takes an IPv4 or IPv6 address and sets
only that family; the answer is plain text OK or KO (bad token or a domain not on the account).
Clearing removes both families at once, so single records cannot be deleted. TTL is fixed at 60.
"""
from __future__ import annotations

import uuid

from app.ddns.providers.base import Caps, Field, Provider, ProviderError, register

ZONE = 'duckdns.org'


@register
class DuckDNS(Provider):
    type = 'duckdns'
    name = 'Duck DNS'
    tier = 'A'
    fields = (Field('token', '令牌', 'password', hint='登录 duckdns.org 后在页面顶部显示的 token；令牌对账号下所有子域名有效'),)
    caps = Caps(zones=False, read=False, by_id=False, multi=False, delete=False, ttl_min=60, ttl_max=60)
    docs = 'https://www.duckdns.org/spec.jsp'
    endpoint = 'https://www.duckdns.org'

    async def check(self):
        # Any update call would change DNS, so only the token's form can be checked.
        try:
            uuid.UUID(self.creds['token'])
        except ValueError:
            raise ProviderError('auth_failed', 'a DuckDNS token is a UUID') from None
        return {'zones': [ZONE], 'writable': None}

    async def records(self, zone, name, rtype):
        raise ProviderError('unsupported', 'DuckDNS cannot read records')

    def _sub(self, zone, name):
        if zone.lower().rstrip('.') != ZONE:
            raise ProviderError('bad_request', f'DuckDNS names live under {ZONE}')
        sub = self.relative(ZONE, name, '')
        if not sub or '.' in sub:
            raise ProviderError('bad_request', f'{name} is not a <name>.{ZONE} domain')
        return sub

    async def _update(self, zone, name, value):
        params = {'domains': self._sub(zone, name), 'token': self.creds['token'], 'ip': value, 'verbose': 'true'}
        response = await self.request('GET', '/update', params=params)
        text = response.text.strip()
        if text.startswith('KO'):
            raise ProviderError('auth_failed', 'KO: wrong token or the domain is not on this account')
        if not text.startswith('OK'):
            raise ProviderError('invalid_response', 'the answer is neither OK nor KO')

    async def create(self, zone, name, rtype, value, ttl, opts):
        await self._update(zone, name, value)
        return ''

    async def update(self, zone, record, value, ttl, opts):
        await self._update(zone, record.name, value)
