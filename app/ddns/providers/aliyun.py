"""Alibaba Cloud DNS (Alidns 2015-01-09), RPC style with signature version 1.0."""
from __future__ import annotations

import time
import uuid

from app.ddns.providers import _sign
from app.ddns.providers.base import Caps, Field, Provider, ProviderError, Record, register

ERRORS = {
    'InvalidAccessKeyId': 'auth_failed', 'SignatureDoesNotMatch': 'auth_failed', 'IncompleteSignature': 'auth_failed',
    'InvalidTimeStamp': 'auth_failed', 'Forbidden': 'forbidden', 'InvalidDomainName': 'zone_not_found',
    'IncorrectDomainUser': 'zone_not_found', 'DomainRecordDuplicate': 'record_conflict', 'Throttling': 'rate_limited',
    'ServiceUnavailable': 'server_error', 'InternalError': 'server_error',
}
LINES = ('default:默认', 'telecom:电信', 'unicom:联通', 'mobile:移动', 'edu:教育网', 'drpeng:鹏博士', 'btvn:广电网', 'oversea:境外')


def error_code(code):
    """Alidns codes are dotted ('InvalidAccessKeyId.NotFound'); the head decides the class."""
    return ERRORS.get(code) or ERRORS.get(code.split('.')[0])


@register
class Aliyun(Provider):
    type = 'aliyun'
    name = '阿里云'
    tier = 'B'
    fields = (Field('access_key_id', 'AccessKey ID'), Field('access_key_secret', 'AccessKey Secret', 'password'))
    options = (Field('line', '解析线路', 'select', options=LINES, optional=True, default='default'),)
    caps = Caps(ttl_min=1, ttl_max=86400)
    docs = 'https://help.aliyun.com/zh/dns/permission-management'
    endpoint = 'https://alidns.aliyuncs.com'

    async def call(self, action, **params):
        params = {k: v for k, v in params.items() if v is not None}
        params.update(Action=action, Format='JSON', Version='2015-01-09', AccessKeyId=self.creds['access_key_id'],
                      SignatureMethod='HMAC-SHA1', SignatureVersion='1.0', SignatureNonce=uuid.uuid4().hex,
                      Timestamp=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
        signature = _sign.aliyun_rpc('GET', params, self.creds['access_key_secret'])
        query = _sign.query_string(params) + '&Signature=' + _sign.encode(signature)
        response = await self.request('GET', '/?' + query, ok=range(100, 600))
        body = self.json(response) if response.content else {}
        code = body.get('Code') if isinstance(body, dict) else None
        if code:
            mapped = error_code(code)
            message = f'{code}: {body.get("Message", "")}'
            if mapped:
                raise ProviderError(mapped, message, status=response.status_code)
            raise self.http_error(response, message) if response.status_code >= 300 else ProviderError('bad_request', message)
        if response.status_code >= 300:
            raise self.http_error(response)
        return body

    async def zones(self):
        out, page = [], 1
        while True:
            body = await self.call('DescribeDomains', PageNumber=page, PageSize=100)
            domains = (body.get('Domains') or {}).get('Domain') or []
            out += [(d.get('PunyCode') or d['DomainName']).lower().rstrip('.') for d in domains]
            if not domains or len(out) >= int(body.get('TotalCount') or 0):
                return out
            page += 1

    def _record(self, zone, item):
        return Record(str(item['RecordId']), self.fqdn(zone, item['RR']), item['Type'], item['Value'], item.get('TTL'),
                      {'line': item.get('Line') or 'default'})

    async def records(self, zone, name, rtype):
        rr = self.relative(zone, name)
        out, page = [], 1
        while True:
            body = await self.call('DescribeSubDomainRecords', DomainName=zone, SubDomain=name, Type=rtype,
                                   PageNumber=page, PageSize=100)
            items = (body.get('DomainRecords') or {}).get('Record') or []
            out += [self._record(zone, i) for i in items if i.get('RR', '').lower() == rr and i.get('Type') == rtype]
            if len(items) < 100:
                return out
            page += 1

    async def create(self, zone, name, rtype, value, ttl, opts):
        line = (opts or {}).get('line') or None
        body = await self.call('AddDomainRecord', DomainName=zone, RR=self.relative(zone, name), Type=rtype, Value=value,
                               TTL=self.ttl_for(ttl), Line=line)
        if not body.get('RecordId'):
            raise ProviderError('invalid_response', 'AddDomainRecord returned no RecordId')
        return Record(str(body['RecordId']), name, rtype, value, self.ttl_for(ttl), {'line': line or 'default'})

    async def update(self, zone, record, value, ttl, opts):
        # UpdateDomainRecord resets omitted TTL and Line to their defaults, so the current ones are sent back.
        line = (opts or {}).get('line') or record.extra.get('line') or 'default'
        new_ttl = self.ttl_for(ttl) or record.ttl
        body = await self.call('UpdateDomainRecord', RecordId=record.id, RR=self.relative(zone, record.name), Type=record.type,
                               Value=value, TTL=new_ttl, Line=line)
        if not body.get('RecordId'):
            raise ProviderError('invalid_response', 'UpdateDomainRecord returned no RecordId')
        return Record(record.id, record.name, record.type, value, new_ttl, dict(record.extra, line=line))

    async def delete(self, zone, record):
        await self.call('DeleteDomainRecord', RecordId=record.id)
