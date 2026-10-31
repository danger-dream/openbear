"""GoDaddy Domains API v1: https://developer.godaddy.com/doc/endpoint/domains

Records have no id: they are addressed by name and type, and the whole set for a name and
type is replaced at once. A record is told apart from its siblings by its value.
"""
from __future__ import annotations

from app.ddns.providers.base import Caps, Field, Provider, ProviderError, Record, register


@register
class GoDaddy(Provider):
    type = 'godaddy'
    name = 'GoDaddy'
    tier = 'A'
    fields = (Field('key', 'API Key', hint='在 developer.godaddy.com/keys 创建 Production 类型的密钥（OTE 测试密钥不能用）'),
              Field('secret', 'API Secret', 'password',
                    hint='账户需要有至少一个域名才能调用 DNS API（2026 年 4 月前要求 10 个以上域名或 Discount Domain Club）；不满足时返回 403'))
    caps = Caps(by_id=False, ttl_min=600, ttl_max=604800)
    docs = 'https://developer.godaddy.com/getstarted'
    endpoint = 'https://api.godaddy.com'

    async def _api(self, method, path, **kwargs):
        headers = {'Authorization': f"sso-key {self.creds['key']}:{self.creds['secret']}", 'Accept': 'application/json'}
        response = await self.request(method, path, ok=range(200, 600), headers=headers, **kwargs)
        if response.status_code < 300:
            return self.json(response) if response.content else None
        try:
            body = response.json()
        except ValueError:
            raise self.http_error(response) from None
        message = f"{body.get('code')}: {body.get('message')}" if isinstance(body, dict) else f'HTTP {response.status_code}'
        if response.status_code == 404 or (isinstance(body, dict) and body.get('code') == 'UNKNOWN_DOMAIN'):
            raise ProviderError('zone_not_found', message, status=response.status_code)
        raise self.http_error(response, message)

    async def zones(self):
        out, marker = [], None
        while True:
            params = {'statuses': 'ACTIVE', 'limit': 1000, **({'marker': marker} if marker else {})}
            page = await self._api('GET', '/v1/domains', params=params) or []
            out += [d['domain'].lower() for d in page]
            if len(page) < 1000:
                return sorted(out)
            marker = page[-1]['domain']

    def _path(self, zone, name, rtype):
        return f'/v1/domains/{zone}/records/{rtype}/{self.relative(zone, name)}'

    async def _items(self, zone, name, rtype):
        return await self._api('GET', self._path(zone, name, rtype)) or []

    async def records(self, zone, name, rtype):
        return [Record('', name, rtype, i['data'], i.get('ttl')) for i in await self._items(zone, name, rtype)]

    async def create(self, zone, name, rtype, value, ttl, opts):
        item = {'type': rtype, 'name': self.relative(zone, name), 'data': value}
        if self.ttl_for(ttl):
            item['ttl'] = self.ttl_for(ttl)
        await self._api('PATCH', f'/v1/domains/{zone}/records', json=[item])
        return ''

    async def update(self, zone, record, value, ttl, opts):
        # PUT replaces every record of this name and type: send the siblings back unchanged.
        items = await self._items(zone, record.name, record.type)
        target = next((i for i in items if i['data'] == record.value), None) or (items[0] if len(items) == 1 else None)
        if target is None:
            items.append(target := {'data': record.value, 'ttl': record.ttl})
        target['data'] = value
        if self.ttl_for(ttl):
            target['ttl'] = self.ttl_for(ttl)
        body = [{k: v for k, v in i.items() if k not in ('name', 'type') and v is not None} for i in items]
        await self._api('PUT', self._path(zone, record.name, record.type), json=body)

    async def delete(self, zone, record):
        items = await self._items(zone, record.name, record.type)
        rest = [{k: v for k, v in i.items() if k not in ('name', 'type')} for i in items if i['data'] != record.value]
        if rest and len(rest) < len(items):
            await self._api('PUT', self._path(zone, record.name, record.type), json=rest)
        elif not rest:
            await self._api('DELETE', self._path(zone, record.name, record.type))
