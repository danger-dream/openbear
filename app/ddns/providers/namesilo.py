"""NameSilo API v1: https://www.namesilo.com/api-reference

Every successful dnsUpdateRecord gives the record a new id, so ids are only used within one
read-then-write and the record is otherwise located by name and type (by_id=False).
"""
from __future__ import annotations

from app.ddns.providers.base import Caps, Field, Provider, ProviderError, Record, register

AUTH = {110}
DENIED = {112, 113}


@register
class NameSilo(Provider):
    type = 'namesilo'
    name = 'NameSilo'
    tier = 'A'
    fields = (Field('key', 'API Key', 'password', hint='在 Account → API Manager 生成；若设置了 IP 白名单，需放行主控的出口 IP'),)
    caps = Caps(by_id=False, ttl_min=3600, ttl_max=2592000)
    docs = 'https://www.namesilo.com/account/api-manager'
    endpoint = 'https://www.namesilo.com/api'

    async def _api(self, operation, **params):
        # Every operation must be called with GET.
        response = await self.request('GET', '/' + operation, params={'version': 1, 'type': 'json', 'key': self.creds['key'], **params})
        reply = (self.json(response) or {}).get('reply') or {}
        code = int(reply.get('code') or 0)
        if code in (300, 301, 302):
            return reply
        message = f"{code}: {reply.get('detail')}"
        if code in AUTH:
            raise ProviderError('auth_failed', message)
        if code in DENIED:
            raise ProviderError('forbidden', message)
        if code == 200:
            raise ProviderError('zone_not_found', message)
        raise ProviderError('server_error' if code in (115, 201) else 'bad_request', message)

    async def zones(self):
        out, page = [], 1
        while True:
            reply = await self._api('listDomains', page=page, pageSize=100)
            domains = reply.get('domains') or []
            if isinstance(domains, dict):  # older shape: {"domain": [...] | "..."}
                domains = domains.get('domain') or []
            domains = domains if isinstance(domains, list) else [domains]
            out += [(d['domain'] if isinstance(d, dict) else d).lower() for d in domains]
            pager = reply.get('pager') or {}
            if not domains or not pager or page * int(pager.get('pageSize') or 100) >= int(pager.get('total') or 0):
                return sorted(out)
            page += 1

    async def records(self, zone, name, rtype):
        items = (await self._api('dnsListRecords', domain=zone)).get('resource_record') or []
        items = items if isinstance(items, list) else [items]
        return [Record(i['record_id'], i['host'].lower(), i['type'], i['value'], int(i['ttl']) if i.get('ttl') else None)
                for i in items if i['host'].lower() == name and i['type'] == rtype]

    async def create(self, zone, name, rtype, value, ttl, opts):
        params = {'domain': zone, 'rrtype': rtype, 'rrhost': self.relative(zone, name, ''), 'rrvalue': value}
        if self.ttl_for(ttl):
            params['rrttl'] = self.ttl_for(ttl)
        return (await self._api('dnsAddRecord', **params)).get('record_id') or ''

    async def update(self, zone, record, value, ttl, opts):
        # An omitted rrttl resets the TTL to 7207.
        params = {'domain': zone, 'rrid': record.id, 'rrhost': self.relative(zone, record.name, ''), 'rrvalue': value}
        if self.ttl_for(ttl) or record.ttl:
            params['rrttl'] = self.ttl_for(ttl) or record.ttl
        return (await self._api('dnsUpdateRecord', **params)).get('record_id') or ''

    async def delete(self, zone, record):
        await self._api('dnsDeleteRecord', domain=zone, rrid=record.id)
