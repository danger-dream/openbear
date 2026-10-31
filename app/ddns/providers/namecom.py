"""name.com Core API v1: https://docs.name.com/api/v1/reference"""
from __future__ import annotations

from app.ddns.providers.base import Caps, Field, Provider, ProviderError, Record, register


@register
class NameCom(Provider):
    type = 'namecom'
    name = 'Name.com'
    tier = 'A'
    fields = (Field('username', '用户名', hint='name.com 账户用户名（不是邮箱）'),
              Field('token', 'API 令牌', 'password', hint='在 Account Settings → API Settings 生成 Production 令牌；开启了两步验证的账户还要在 Security 里允许 API 访问'))
    caps = Caps(ttl_min=300, ttl_max=86400)
    docs = 'https://docs.name.com/guides/getting-started'
    endpoint = 'https://api.name.com'

    async def _api(self, method, path, **kwargs):
        response = await self.request(method, path, ok=range(200, 600), auth=(self.creds['username'], self.creds['token']), **kwargs)
        if response.status_code < 300:
            return self.json(response) if response.content else {}
        try:
            body = response.json()
            message = f"{body.get('message')}: {body.get('details', '')}".rstrip(': ')
        except (ValueError, AttributeError):
            raise self.http_error(response) from None
        if response.status_code == 404 and '/records' in path and method == 'GET':
            raise ProviderError('zone_not_found', message, status=404)
        raise self.http_error(response, message)

    async def _pages(self, path, key):
        out, page = [], 1
        while True:
            body = await self._api('GET', path, params={'perPage': 1000, 'page': page})
            out += body.get(key) or []
            if not body.get('nextPage'):
                return out
            page = body['nextPage']

    async def zones(self):
        return sorted(d['domainName'].lower() for d in await self._pages('/core/v1/domains', 'domains'))

    @staticmethod
    def _record(zone, item):
        name = (item.get('fqdn') or Provider.fqdn(zone, item.get('host'))).lower().rstrip('.')
        return Record(str(item['id']), name, item['type'], item['answer'], item.get('ttl'))

    async def records(self, zone, name, rtype):
        items = await self._pages(f'/core/v1/domains/{zone}/records', 'records')
        return [r for r in (self._record(zone, i) for i in items) if r.name == name and r.type == rtype]

    async def create(self, zone, name, rtype, value, ttl, opts):
        body = {'host': self.relative(zone, name, ''), 'type': rtype, 'answer': value}
        if self.ttl_for(ttl):
            body['ttl'] = self.ttl_for(ttl)
        return self._record(zone, await self._api('POST', f'/core/v1/domains/{zone}/records', json=body))

    async def update(self, zone, record, value, ttl, opts):
        # PUT overwrites the whole record: host, type and ttl are sent back as they were.
        body = {'host': self.relative(zone, record.name, ''), 'type': record.type, 'answer': value,
                'ttl': self.ttl_for(ttl) or record.ttl or self.caps.ttl_min}
        return self._record(zone, await self._api('PUT', f'/core/v1/domains/{zone}/records/{record.id}', json=body))

    async def delete(self, zone, record):
        await self._api('DELETE', f'/core/v1/domains/{zone}/records/{record.id}')
