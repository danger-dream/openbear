"""Namecheap Dynamic DNS: https://www.namecheap.com/support/knowledgebase/article.aspx/29/11/how-to-dynamically-update-the-hosts-ip-with-an-https-request/

Update-only and IPv4-only: records cannot be read or listed, so records() raises 'unsupported'
and the service writes without reading. The password belongs to one domain. Only GET works.
The host must already exist as an A record; the call cannot create one, set a TTL or delete.
Host and domain are matched case-sensitively, so the domain is sent as entered. The answer is
XML with an ErrCount and Err1..n messages.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from app.ddns.providers.base import Caps, Field, Provider, ProviderError, register


@register
class Namecheap(Provider):
    type = 'namecheap'
    name = 'Namecheap（动态 DNS）'
    tier = 'A'
    fields = (Field('domain', '域名', hint='与 Namecheap 后台显示的大小写一致，例如 example.com'),
              Field('password', '动态 DNS 密码', 'password',
                    hint='Domain List → Manage → Advanced DNS → Dynamic DNS 开启后显示；不是账号密码。仅支持 IPv4，主机需先在后台建好 A 记录'))
    caps = Caps(zones=False, read=False, by_id=False, multi=False, delete=False, types=('A',))
    docs = 'https://www.namecheap.com/support/knowledgebase/article.aspx/595/11/how-do-i-enable-dynamic-dns-for-a-domain/'
    endpoint = 'https://dynamicdns.park-your-domain.com'

    async def check(self):
        # Any update call would change DNS, so only the configuration can be checked.
        return {'zones': [self.creds['domain'].lower().rstrip('.')], 'writable': None}

    async def records(self, zone, name, rtype):
        raise ProviderError('unsupported', 'Namecheap dynamic DNS cannot read records')

    async def _update(self, zone, name, rtype, value):
        if rtype != 'A':
            raise ProviderError('unsupported', 'Namecheap dynamic DNS only updates A records')
        domain = self.creds['domain'].rstrip('.')
        if domain.lower() != zone.lower().rstrip('.'):
            raise ProviderError('bad_request', f'this password belongs to {domain}, not {zone}')
        params = {'host': self.relative(zone, name), 'domain': domain, 'password': self.creds['password'], 'ip': value}
        response = await self.request('GET', '/update', params=params)
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError:
            raise ProviderError('invalid_response', f'not XML (HTTP {response.status_code})') from None
        if (root.findtext('ErrCount') or '').strip() == '0':
            return
        node = root.find('errors')
        errors = '; '.join((e.text or '').strip() for e in (node if node is not None else []))
        lowered = errors.lower()
        if 'password' in lowered:
            raise ProviderError('auth_failed', errors)
        if 'domain name not found' in lowered:
            raise ProviderError('zone_not_found', errors)
        raise ProviderError('bad_request', errors or 'the update was refused without an error message')

    async def create(self, zone, name, rtype, value, ttl, opts):
        await self._update(zone, name, rtype, value)
        return ''

    async def update(self, zone, record, value, ttl, opts):
        await self._update(zone, record.name, record.type, value)
