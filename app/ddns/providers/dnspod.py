"""DNSPod legacy API (dnsapi.cn) with a "ID,Token" login token: https://docs.dnspod.cn/api/"""
from __future__ import annotations

from app.ddns.providers.base import Caps, Field, Provider, ProviderError, Record, register

LINES = ('默认', '电信', '联通', '移动', '教育网', '境内', '境外')
AUTH = {'-1', '-8'}
DENIED = {'-7', '83', '85'}


@register
class Dnspod(Provider):
    type = 'dnspod'
    name = 'DNSPod（Token）'
    tier = 'A'
    fields = (Field('id', 'Token ID', hint='DNSPod 控制台 → 账号中心 → API 密钥 → DNSPod Token 的 ID'),
              Field('token', 'Token', 'password', hint='只能由主账号创建，对账号下全部域名有完整权限'))
    options = (Field('line', '解析线路', 'select', options=tuple(f'{x}:{x}' for x in LINES), optional=True, default='默认'),)
    # Free plans accept a TTL of 600 at least; paid plans go lower.
    caps = Caps(ttl_min=600, ttl_max=604800)
    docs = 'https://docs.dnspod.cn/account/dnspod-token/'
    endpoint = 'https://dnsapi.cn'

    async def _api(self, action, empty=(), **params):
        data = {'login_token': f"{self.creds['id']},{self.creds['token']}", 'format': 'json', 'lang': 'cn', 'error_on_empty': 'no', **params}
        body = self.json(await self.request('POST', '/' + action, data=data))
        status = body.get('status') or {}
        code, message = str(status.get('code')), f"{status.get('code')}: {status.get('message')}"
        if code == '1' or code in empty:
            return body
        if code in AUTH:
            raise ProviderError('auth_failed', message)
        if code in DENIED:
            raise ProviderError('forbidden', message)
        if code == '-2':
            raise ProviderError('rate_limited', message)
        if action != 'Domain.List' and code in ('6', '7', '8'):
            raise ProviderError('zone_not_found', message)
        raise ProviderError('bad_request', message)

    async def zones(self):
        out, offset = [], 0
        while True:
            body = await self._api('Domain.List', empty=('9',), type='all', offset=offset, length=3000)
            domains = body.get('domains') or []
            out += [(d.get('punycode') or d['name']).lower() for d in domains]
            offset += len(domains)
            if not domains or offset >= int((body.get('info') or {}).get('domain_total') or 0):
                return sorted(out)

    @staticmethod
    def _record(zone, item):
        extra = {'line': item.get('line') or '默认', 'status': 'disable' if str(item.get('enabled')) == '0' else 'enable'}
        if item.get('weight') is not None:
            extra['weight'] = item['weight']
        return Record(str(item['id']), Provider.fqdn(zone, item['name']), item['type'], item['value'], int(item['ttl']) if item.get('ttl') else None, extra)

    async def records(self, zone, name, rtype):
        out, offset, sub = [], 0, self.relative(zone, name)
        while True:
            body = await self._api('Record.List', empty=('10',), domain=zone, sub_domain=sub, record_type=rtype, offset=offset, length=3000)
            items = body.get('records') or []
            out += [self._record(zone, i) for i in items if i['name'].lower() == sub and i['type'] == rtype]
            offset += len(items)
            if not items or offset >= int((body.get('info') or {}).get('record_total') or 0):
                return out

    async def create(self, zone, name, rtype, value, ttl, opts):
        params = {'domain': zone, 'sub_domain': self.relative(zone, name), 'record_type': rtype, 'value': value,
                  'record_line': (opts or {}).get('line') or '默认'}
        if self.ttl_for(ttl):
            params['ttl'] = self.ttl_for(ttl)
        body = await self._api('Record.Create', **params)
        return str((body.get('record') or {}).get('id') or '')

    async def update(self, zone, record, value, ttl, opts):
        # Record.Modify requires the line, and resets an omitted TTL, weight and status to their defaults.
        params = {'domain': zone, 'record_id': record.id, 'sub_domain': self.relative(zone, record.name), 'record_type': record.type,
                  'value': value, 'record_line': (opts or {}).get('line') or record.extra.get('line') or '默认',
                  'status': record.extra.get('status') or 'enable'}
        if self.ttl_for(ttl) or record.ttl:
            params['ttl'] = self.ttl_for(ttl) or record.ttl
        if record.extra.get('weight') is not None:
            params['weight'] = record.extra['weight']
        await self._api('Record.Modify', **params)

    async def delete(self, zone, record):
        await self._api('Record.Remove', domain=zone, record_id=record.id)
