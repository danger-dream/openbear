"""Volcengine DNS (云解析, TrafficRoute DNS suite), OpenAPI 2018-08-01 with HMAC-SHA256 signing.

ddns-go's "trafficroute" provider calls the same service and actions."""
from __future__ import annotations

import json
import time
from urllib.parse import urlsplit

from app.ddns.providers import _sign
from app.ddns.providers.base import Caps, Field, Provider, ProviderError, Record, register

ERRORS = {
    'InvalidAccessKey': 'auth_failed', 'SignatureDoesNotMatch': 'auth_failed', 'InvalidCredential': 'auth_failed',
    'InvalidAuthorization': 'auth_failed', 'InvalidSecretToken': 'auth_failed', 'InvalidTimestamp': 'auth_failed',
    'AccessDenied': 'forbidden', 'ErrZoneNotFound': 'zone_not_found', 'ErrZoneInactive': 'zone_not_found',
    'FlowLimitExceeded': 'rate_limited', 'InternalError': 'server_error', 'ErrInternalServer': 'server_error',
    'InternalServiceTimeout': 'server_error', 'ServiceUnavailableTemp': 'server_error', 'InternalServiceError': 'server_error',
}
PAGE = 100


@register
class Volc(Provider):
    type = 'volc'
    name = '火山引擎'
    tier = 'B'
    fields = (Field('access_key_id', 'Access Key ID'), Field('secret_access_key', 'Secret Access Key', 'password'))
    caps = Caps(ttl_min=1, ttl_max=86400)
    options = (Field('line', '解析线路', optional=True, default='default',
                     hint='线路代码：default 默认、domestic 中国内地、oversea 境外，其他线路的代码见火山引擎的 ListLines 接口。只在新建时生效'),)
    docs = 'https://www.volcengine.com/docs/6758/2222495'
    endpoint = 'https://dns.volcengineapi.com'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._zone_ids = {}

    async def call(self, action, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
        query = {'Action': action, 'Version': '2018-08-01'}
        stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
        headers = {'Content-Type': 'application/json', 'Host': urlsplit(self.base).netloc, 'X-Date': stamp,
                   'X-Content-Sha256': _sign.sha256_hex(body)}
        headers['Authorization'] = _sign.volc(self.creds['access_key_id'], self.creds['secret_access_key'], region='cn-beijing',
                                              service='dns', method='POST', path='/', query=query, headers=headers, body=body,
                                              stamp=stamp)
        response = await self.request('POST', '/?' + _sign.query_string(query), content=body, headers=headers, ok=range(100, 600))
        data = self.json(response) if response.content.startswith(b'{') else {}
        error = (data.get('ResponseMetadata') or {}).get('Error') or {}
        if error.get('Code'):
            message = f'{error["Code"]}: {error.get("Message", "")}'
            mapped = ERRORS.get(error['Code'])
            if mapped:
                raise ProviderError(mapped, message, status=response.status_code)
            raise self.http_error(response, message) if response.status_code >= 300 else ProviderError('bad_request', message)
        if response.status_code >= 300:
            raise self.http_error(response)
        if not isinstance(data.get('Result'), dict) and action.startswith('List'):
            raise ProviderError('invalid_response', f'{action} returned no Result')
        return data.get('Result') or {}

    async def _list_zones(self, key=None):
        out, page = [], 1
        while True:
            payload = {'PageNumber': page, 'PageSize': PAGE}
            if key:
                payload.update(Key=key, SearchMode='exact')
            result = await self.call('ListZones', payload)
            items = result.get('Zones') or []
            for z in items:
                zone = z['ZoneName'].lower().rstrip('.')
                self._zone_ids.setdefault(zone, z['ZID'])
                out.append(zone)
            if len(items) < PAGE:
                return out
            page += 1

    async def zones(self):
        return await self._list_zones()

    async def zone_id(self, zone):
        if zone not in self._zone_ids:
            await self._list_zones(zone)
        if zone not in self._zone_ids:
            raise ProviderError('zone_not_found', f'{zone} is not a zone of this account')
        return self._zone_ids[zone]

    def _record(self, zone, item):
        return Record(str(item['RecordID']), self.fqdn(zone, item['Host']), item['Type'], item['Value'], item.get('TTL'),
                      {'line': item.get('Line') or 'default'})

    async def records(self, zone, name, rtype):
        zid, host = await self.zone_id(zone), self.relative(zone, name)
        out, page = [], 1
        while True:
            result = await self.call('ListRecords', {'ZID': zid, 'Host': host, 'Type': rtype, 'SearchMode': 'exact',
                                                     'PageNumber': page, 'PageSize': PAGE})
            items = result.get('Records') or []
            out += [self._record(zone, i) for i in items if i.get('Host', '').lower() == host and i.get('Type') == rtype]
            if len(items) < PAGE:
                return out
            page += 1

    async def create(self, zone, name, rtype, value, ttl, opts):
        payload = {'ZID': await self.zone_id(zone), 'Host': self.relative(zone, name), 'Type': rtype, 'Value': value,
                   'Line': (opts or {}).get('line') or 'default'}
        if self.ttl_for(ttl):
            payload['TTL'] = self.ttl_for(ttl)
        result = await self.call('CreateRecord', payload)
        if not result.get('RecordID'):
            raise ProviderError('invalid_response', 'CreateRecord returned no RecordID')
        return self._record(zone, {'Line': payload['Line'], **result, 'Host': payload['Host'], 'Type': rtype, 'Value': value})

    async def update(self, zone, record, value, ttl, opts):
        # Host and Line are required; omitted TTL, Weight and Remark keep their current values.
        payload = {'RecordID': record.id, 'Host': self.relative(zone, record.name), 'Line': record.extra.get('line') or 'default',
                   'Type': record.type, 'Value': value}
        if self.ttl_for(ttl):
            payload['TTL'] = self.ttl_for(ttl)
        result = await self.call('UpdateRecord', payload)
        return Record(record.id, record.name, record.type, value, result.get('TTL', self.ttl_for(ttl) or record.ttl), dict(record.extra))

    async def delete(self, zone, record):
        await self.call('DeleteRecord', {'RecordID': record.id})
