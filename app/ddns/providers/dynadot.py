"""Dynadot API3 (key only): https://www.dynadot.com/domain/api-commands

Records have no id and one TTL covers the whole domain. set_dns2 either appends
(add_dns_to_current_setting=1) or replaces every record of the domain, so changing or removing
one record rewrites the whole set; that is refused when the domain holds record kinds whose
extra fields (forwarding, stealth, email, SRV...) cannot be written back unchanged.
"""
from __future__ import annotations

from app.ddns.providers.base import Caps, Field, Provider, ProviderError, Record, register

PLAIN = {'a', 'aaaa', 'cname', 'txt', 'mx'}


@register
class Dynadot(Provider):
    type = 'dynadot'
    name = 'Dynadot'
    tier = 'A'
    fields = (Field('key', 'API Key', 'password', hint='在 Tools → API 生成；若设置了 IP 白名单需放行主控的出口 IP。域名需使用 Dynadot DNS'),)
    caps = Caps(by_id=False, ttl_min=60, ttl_max=86400, whole_zone=True)
    docs = 'https://www.dynadot.com/domain/api-commands'
    endpoint = 'https://api.dynadot.com'

    async def _api(self, command, **params):
        body = self.json(await self.request('GET', '/api3.json', params={'key': self.creds['key'], 'command': command, **params}))
        inner = next(iter(body.values()), {}) if isinstance(body, dict) and body else {}
        if not isinstance(inner, dict):
            raise ProviderError('invalid_response', 'the answer is not an API3 response')
        if str(inner.get('ResponseCode')) == '0' and inner.get('Status', 'success') == 'success':
            return inner
        message = str(inner.get('Error') or inner.get('Status') or f"ResponseCode {inner.get('ResponseCode')}")[:300]
        lowered = message.lower()
        if 'key' in lowered and ('invalid' in lowered or 'not' in lowered):
            raise ProviderError('auth_failed', message)
        if 'ip' in lowered and 'allow' in lowered:
            raise ProviderError('forbidden', message)
        if 'not found' in lowered or 'not in your account' in lowered or 'not owned' in lowered:
            raise ProviderError('zone_not_found', message)
        raise ProviderError('bad_request', message)

    async def zones(self):
        domains = (await self._api('list_domain')).get('MainDomains') or []
        return sorted(d['Name'].lower() for d in domains)

    async def _settings(self, zone):
        inner = await self._api('get_dns', domain=zone)
        settings = (inner.get('GetDns') or inner.get('GetDnsContent') or {}).get('NameServerSettings') or {}
        if settings.get('Type') and settings['Type'] != 'Dynadot DNS':
            raise ProviderError('bad_request', f"{zone} does not use Dynadot DNS ({settings['Type']})")
        main = [{'host': '', **r} for r in settings.get('MainDomains') or []]
        sub = [{'host': (r.get('Subhost') or '').lower(), **r} for r in settings.get('SubDomains') or []]
        return main + sub, int(settings['TTL']) if settings.get('TTL') else None

    async def records(self, zone, name, rtype):
        items, ttl = await self._settings(zone)
        host = self.relative(zone, name, '')
        return [Record('', name, rtype, i['Value'], ttl) for i in items if i['host'] == host and i['RecordType'].upper() == rtype]

    @staticmethod
    def _params(items, ttl):
        params, main, sub = {}, 0, 0
        for i in items:
            kind = i['RecordType'].lower()
            if i['host']:
                params.update({f'subdomain{sub}': i['host'], f'sub_record_type{sub}': kind, f'sub_record{sub}': i['Value']})
                if i.get('Value2'):
                    params[f'sub_recordx{sub}'] = i['Value2']
                sub += 1
            else:
                params.update({f'main_record_type{main}': kind, f'main_record{main}': i['Value']})
                if i.get('Value2'):
                    params[f'main_recordx{main}'] = i['Value2']
                main += 1
        if ttl:
            params['ttl'] = ttl
        return params

    async def create(self, zone, name, rtype, value, ttl, opts):
        item = {'host': self.relative(zone, name, ''), 'RecordType': rtype, 'Value': value}
        await self._api('set_dns2', domain=zone, add_dns_to_current_setting=1, **self._params([item], self.ttl_for(ttl)))
        return ''

    async def _replace(self, zone, items, ttl):
        odd = sorted({i['RecordType'].lower() for i in items} - PLAIN)
        if odd:
            raise ProviderError('unsupported', f'{zone} has {", ".join(odd)} records, which cannot be rewritten safely through API3')
        await self._api('set_dns2', domain=zone, **self._params(items, ttl))

    async def update(self, zone, record, value, ttl, opts):
        items, current_ttl = await self._settings(zone)
        host = self.relative(zone, record.name, '')
        mine = [i for i in items if i['host'] == host and i['RecordType'].upper() == record.type]
        target = next((i for i in mine if i['Value'] == record.value), None) or (mine[0] if len(mine) == 1 else None)
        if target is None:
            items.append(target := {'host': host, 'RecordType': record.type})
        target['Value'] = value
        await self._replace(zone, items, self.ttl_for(ttl) or current_ttl)

    async def delete(self, zone, record):
        items, current_ttl = await self._settings(zone)
        host = self.relative(zone, record.name, '')
        rest = [i for i in items if not (i['host'] == host and i['RecordType'].upper() == record.type and i['Value'] == record.value)]
        if len(rest) < len(items):
            await self._replace(zone, rest, current_ttl)
