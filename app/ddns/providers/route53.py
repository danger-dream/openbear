"""Amazon Route 53 REST API (2013-04-01, XML) with SigV4.

A record set is identified by name and type; writes UPSERT the whole set."""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

from app.ddns.providers import _sign
from app.ddns.providers.base import Caps, Field, Provider, ProviderError, Record, register

NS = '{https://route53.amazonaws.com/doc/2013-04-01/}'
ERRORS = {
    'InvalidClientTokenId': 'auth_failed', 'SignatureDoesNotMatch': 'auth_failed', 'IncompleteSignature': 'auth_failed',
    'MissingAuthenticationToken': 'auth_failed', 'InvalidSignatureException': 'auth_failed', 'AccessDenied': 'forbidden',
    'NoSuchHostedZone': 'zone_not_found', 'Throttling': 'rate_limited', 'PriorRequestNotComplete': 'rate_limited',
}
DEFAULT_TTL = 300


def unescape_name(name):
    """Route 53 returns names with a trailing dot and octal escapes such as \\052 for '*'."""
    out, i = [], 0
    while i < len(name):
        if name[i] == '\\' and name[i + 1:i + 4].isdigit():
            out.append(chr(int(name[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(name[i])
            i += 1
    return ''.join(out).lower().rstrip('.')


@register
class Route53(Provider):
    type = 'route53'
    name = 'AWS Route 53'
    tier = 'B'
    fields = (Field('access_key_id', 'Access Key ID'), Field('secret_access_key', 'Secret Access Key', 'password'))
    caps = Caps(by_id=False, multi=False, ttl_min=1, ttl_max=2147483647)
    docs = 'https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/r53-api-permissions-ref.html'
    endpoint = 'https://route53.amazonaws.com'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._zone_ids = {}

    async def call(self, method, path, query=None, body=b''):
        stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
        headers = {'Host': urlsplit(self.base).netloc, 'X-Amz-Date': stamp}
        if body:
            headers['Content-Type'] = 'application/xml'
        headers['Authorization'] = _sign.sigv4(self.creds['access_key_id'], self.creds['secret_access_key'], region='us-east-1',
                                               service='route53', method=method, path=path, query=query or {}, headers=headers,
                                               body=body, stamp=stamp)
        qs = _sign.query_string(query or {})
        response = await self.request(method, path + ('?' + qs if qs else ''), content=body or None, headers=headers,
                                      ok=range(100, 600))
        try:
            root = ET.fromstring(response.content) if response.content else None
        except ET.ParseError:
            root = None
        if response.status_code >= 300:
            # Errors raised before the service (signature, credentials) may come without the namespace.
            fields = {el.tag.rpartition('}')[2]: el.text for el in root.iter()} if root is not None else {}
            code = fields.get('Code')
            message = f'{code}: {fields.get("Message") or ""}' if code else ''
            if code in ERRORS:
                raise ProviderError(ERRORS[code], message, status=response.status_code)
            raise self.http_error(response, message)
        if root is None:
            raise ProviderError('invalid_response', f'not XML (HTTP {response.status_code})')
        return root

    async def _hosted_zones(self):
        out, marker = [], None
        while True:
            root = await self.call('GET', '/2013-04-01/hostedzone', {'maxitems': 100, **({'marker': marker} if marker else {})})
            for z in root.iter(f'{NS}HostedZone'):
                if z.findtext(f'{NS}Config/{NS}PrivateZone') == 'true':
                    continue
                zone = unescape_name(z.findtext(f'{NS}Name'))
                self._zone_ids.setdefault(zone, z.findtext(f'{NS}Id').rsplit('/', 1)[-1])
                out.append(zone)
            marker = root.findtext(f'{NS}NextMarker')
            if root.findtext(f'{NS}IsTruncated') != 'true' or not marker:
                return out

    async def zones(self):
        return list(dict.fromkeys(await self._hosted_zones()))

    async def zone_id(self, zone):
        if zone not in self._zone_ids:
            root = await self.call('GET', '/2013-04-01/hostedzonesbyname', {'dnsname': zone, 'maxitems': 10})
            for z in root.iter(f'{NS}HostedZone'):
                if unescape_name(z.findtext(f'{NS}Name')) == zone and z.findtext(f'{NS}Config/{NS}PrivateZone') != 'true':
                    self._zone_ids[zone] = z.findtext(f'{NS}Id').rsplit('/', 1)[-1]
                    break
            else:
                raise ProviderError('zone_not_found', f'{zone} is not a public hosted zone of this account')
        return self._zone_ids[zone]

    async def records(self, zone, name, rtype):
        zid = await self.zone_id(zone)
        root = await self.call('GET', f'/2013-04-01/hostedzone/{zid}/rrset', {'name': name + '.', 'type': rtype, 'maxitems': 100})
        out = []
        for rrset in root.iter(f'{NS}ResourceRecordSet'):
            if unescape_name(rrset.findtext(f'{NS}Name')) != name or rrset.findtext(f'{NS}Type') != rtype:
                continue
            if rrset.find(f'{NS}SetIdentifier') is not None or rrset.find(f'{NS}AliasTarget') is not None:
                raise ProviderError('unsupported', 'Route 53 目前只支持 simple 记录，不支持路由策略或 Alias 记录')
            values = [v.text for v in rrset.iter(f'{NS}Value')]
            ttl = rrset.findtext(f'{NS}TTL')
            extra = {}
            out.append(Record('', name, rtype, ','.join(values), int(ttl) if ttl else None, extra))
        return out

    async def change(self, zone, action, name, rtype, values, ttl):
        zid = await self.zone_id(zone)
        records = ''.join(f'<ResourceRecord><Value>{escape(v)}</Value></ResourceRecord>' for v in values)
        body = ('<?xml version="1.0" encoding="UTF-8"?>'
                '<ChangeResourceRecordSetsRequest xmlns="https://route53.amazonaws.com/doc/2013-04-01/"><ChangeBatch><Changes>'
                f'<Change><Action>{action}</Action><ResourceRecordSet><Name>{escape(name)}.</Name><Type>{rtype}</Type>'
                f'<TTL>{ttl}</TTL><ResourceRecords>{records}</ResourceRecords></ResourceRecordSet></Change>'
                '</Changes></ChangeBatch></ChangeResourceRecordSetsRequest>').encode()
        root = await self.call('POST', f'/2013-04-01/hostedzone/{zid}/rrset/', body=body)
        if root.find(f'{NS}ChangeInfo') is None:
            raise ProviderError('invalid_response', 'ChangeResourceRecordSets returned no ChangeInfo')

    async def create(self, zone, name, rtype, value, ttl, opts):
        ttl = self.ttl_for(ttl) or DEFAULT_TTL
        await self.change(zone, 'UPSERT', name, rtype, [value], ttl)
        return Record('', name, rtype, value, ttl)

    async def update(self, zone, record, value, ttl, opts):
        ttl = self.ttl_for(ttl) or record.ttl or DEFAULT_TTL
        await self.change(zone, 'UPSERT', record.name, record.type, [value], ttl)
        return Record('', record.name, record.type, value, ttl, dict(record.extra))

    async def delete(self, zone, record):
        # DELETE must repeat the record set exactly as it is now.
        await self.change(zone, 'DELETE', record.name, record.type, record.value.split(','), record.ttl or DEFAULT_TTL)
