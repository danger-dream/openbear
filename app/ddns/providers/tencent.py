"""Tencent Cloud DNSPod, API 3.0 (2021-03-23) with TC3-HMAC-SHA256."""
from __future__ import annotations

import json
import time
from urllib.parse import urlsplit

from app.ddns.providers import _sign
from app.ddns.providers.base import Caps, Field, Provider, ProviderError, Record, register

ERRORS = {
    'AuthFailure': 'auth_failed', 'UnauthorizedOperation': 'forbidden', 'OperationDenied': 'forbidden',
    'InvalidParameterValue.DomainNotExists': 'zone_not_found', 'ResourceNotFound.NoDataOfDomain': 'zone_not_found',
    'FailedOperation.NotDomainOwner': 'zone_not_found', 'InvalidParameter.DomainRecordExist': 'record_conflict',
    'RequestLimitExceeded': 'rate_limited', 'FailedOperation.FrequencyLimit': 'rate_limited',
    'InternalError': 'server_error', 'FailedOperation.UnknowError': 'server_error',
}
# RecordLine takes the line name; DNSPod's free plan offers these.
LINES = tuple(f'{n}:{n}' for n in ('默认', '电信', '联通', '移动', '铁通', '广电网', '教育网', '境内', '境外'))


def error_code(code):
    while code:
        if code in ERRORS:
            return ERRORS[code]
        code = code.rpartition('.')[0]
    return None


@register
class Tencent(Provider):
    type = 'tencent'
    name = '腾讯云 DNSPod'
    tier = 'B'
    fields = (Field('secret_id', 'SecretId'), Field('secret_key', 'SecretKey', 'password'))
    options = (Field('line', '解析线路', 'select', options=LINES, optional=True, default='默认'),)
    caps = Caps(ttl_min=1, ttl_max=604800)
    docs = 'https://docs.dnspod.cn/dns/dnspod-cam-manage/'
    endpoint = 'https://dnspod.tencentcloudapi.com'

    async def call(self, action, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        headers = _sign.tc3(self.creds['secret_id'], self.creds['secret_key'], service='dnspod', host=urlsplit(self.base).netloc,
                            action=action, payload=body, timestamp=int(time.time()))
        headers['X-TC-Version'] = '2021-03-23'
        response = await self.request('POST', '/', content=body.encode(), headers=headers, ok=range(100, 600))
        data = self.json(response)
        result = data.get('Response') if isinstance(data, dict) else None
        if not isinstance(result, dict):
            raise self.http_error(response) if response.status_code >= 300 else ProviderError('invalid_response', 'the answer has no Response')
        error = result.get('Error')
        if error:
            code = error.get('Code', '')
            raise ProviderError(error_code(code) or 'bad_request', f'{code}: {error.get("Message", "")}', status=response.status_code)
        if response.status_code >= 300:
            raise self.http_error(response)
        return result

    async def zones(self):
        out, offset = [], 0
        while True:
            try:
                result = await self.call('DescribeDomainList', {'Offset': offset, 'Limit': 3000})
            except ProviderError as exc:
                if offset == 0 and exc.code == 'zone_not_found':
                    return []
                raise
            items = result.get('DomainList') or []
            out += [(d.get('Punycode') or d['Name']).lower().rstrip('.') for d in items]
            if len(items) < 3000:
                return out
            offset += len(items)

    def _record(self, zone, item):
        extra = {'line': item.get('Line') or '默认', 'status': item.get('Status'), 'weight': item.get('Weight')}
        return Record(str(item['RecordId']), self.fqdn(zone, item['Name']), item['Type'], item['Value'], item.get('TTL'), extra)

    async def records(self, zone, name, rtype):
        rr = self.relative(zone, name)
        out, offset = [], 0
        while True:
            result = await self.call('DescribeRecordList', {'Domain': zone, 'SubDomain': rr, 'RecordType': rtype,
                                                            'Offset': offset, 'Limit': 3000, 'ErrorOnEmpty': 'no'})
            items = result.get('RecordList') or []
            out += [self._record(zone, i) for i in items if i.get('Name', '').lower() == rr and i.get('Type') == rtype]
            if len(items) < 3000:
                return out
            offset += len(items)

    async def create(self, zone, name, rtype, value, ttl, opts):
        line = (opts or {}).get('line') or '默认'
        payload = {'Domain': zone, 'SubDomain': self.relative(zone, name), 'RecordType': rtype, 'RecordLine': line, 'Value': value}
        if self.ttl_for(ttl):
            payload['TTL'] = self.ttl_for(ttl)
        result = await self.call('CreateRecord', payload)
        if not result.get('RecordId'):
            raise ProviderError('invalid_response', 'CreateRecord returned no RecordId')
        return Record(str(result['RecordId']), name, rtype, value, self.ttl_for(ttl), {'line': line})

    async def update(self, zone, record, value, ttl, opts):
        # ModifyRecord requires RecordLine and falls back to defaults for an omitted TTL, Status or Weight,
        # so the record's current values are sent back unless the caller changes them.
        extra = record.extra
        line = (opts or {}).get('line') or extra.get('line') or '默认'
        payload = {'Domain': zone, 'RecordId': int(record.id), 'SubDomain': self.relative(zone, record.name),
                   'RecordType': record.type, 'RecordLine': line, 'Value': value}
        new_ttl = self.ttl_for(ttl) or record.ttl
        for key, current in (('TTL', new_ttl), ('Status', extra.get('status')), ('Weight', extra.get('weight'))):
            if current is not None:
                payload[key] = current
        await self.call('ModifyRecord', payload)
        return Record(record.id, record.name, record.type, value, new_ttl, dict(extra, line=line))

    async def delete(self, zone, record):
        await self.call('DeleteRecord', {'Domain': zone, 'RecordId': int(record.id)})
