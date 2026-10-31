import xml.etree.ElementTree as ET
from urllib.parse import parse_qsl

import httpx
import pytest

from app.ddns.providers import _sign, get
from app.ddns.providers.base import ProviderError, Record
from tests.ddns.fakeapi import FakeAPI

CREDS = {'access_key_id': 'AKIDEXAMPLE', 'secret_access_key': 'route53secret'}
BASE = r'https://route53\.amazonaws\.com/2013-04-01'
XMLNS = 'https://route53.amazonaws.com/doc/2013-04-01/'
NS = '{' + XMLNS + '}'


def xml(status, body):
    return httpx.Response(status, content=f'<?xml version="1.0"?>{body}'.encode(), headers={'Content-Type': 'text/xml'})


def zone(zid, name, private=False):
    return (f'<HostedZone><Id>/hostedzone/{zid}</Id><Name>{name}</Name><CallerReference>c</CallerReference>'
            f'<Config><PrivateZone>{str(private).lower()}</PrivateZone></Config><ResourceRecordSetCount>3</ResourceRecordSetCount></HostedZone>')


ZONES = (f'<ListHostedZonesResponse xmlns="{XMLNS}"><HostedZones>{zone("Z1", "example.com.")}{zone("Z2", "internal.example.", True)}'
         f'{zone("Z3", "example.net.")}</HostedZones><IsTruncated>false</IsTruncated><MaxItems>100</MaxItems></ListHostedZonesResponse>')
BY_NAME = (f'<ListHostedZonesByNameResponse xmlns="{XMLNS}"><HostedZones>{zone("Z1", "example.com.")}</HostedZones>'
           '<IsTruncated>false</IsTruncated><MaxItems>10</MaxItems></ListHostedZonesByNameResponse>')
CHANGE = (f'<ChangeResourceRecordSetsResponse xmlns="{XMLNS}"><ChangeInfo><Id>/change/C1</Id><Status>PENDING</Status>'
          '<SubmittedAt>2026-10-29T00:00:00Z</SubmittedAt></ChangeInfo></ChangeResourceRecordSetsResponse>')


def rrset(name, rtype, values, ttl=300):
    rr = ''.join(f'<ResourceRecord><Value>{v}</Value></ResourceRecord>' for v in values)
    return f'<ResourceRecordSet><Name>{name}</Name><Type>{rtype}</Type><TTL>{ttl}</TTL><ResourceRecords>{rr}</ResourceRecords></ResourceRecordSet>'


def query(request):
    return dict(parse_qsl(request.url.query.decode()))


def signed(handler):
    def wrapper(request, match):
        h = request.headers
        headers = {'Host': h['Host'], 'X-Amz-Date': h['X-Amz-Date']}
        if request.content:
            headers['Content-Type'] = h['Content-Type']
        expected = _sign.sigv4('AKIDEXAMPLE', 'route53secret', region='us-east-1', service='route53', method=request.method,
                               path=request.url.path, query=query(request), headers=headers, body=request.content, stamp=h['X-Amz-Date'])
        assert h['Authorization'] == expected and '/us-east-1/route53/aws4_request, ' in expected
        return handler(request, match)
    return wrapper


def fake():
    api = FakeAPI()
    api.on('GET', BASE + '/hostedzonesbyname', signed(lambda r, m: xml(200, BY_NAME)))
    api.on('POST', BASE + '/hostedzone/Z1/rrset/', signed(lambda r, m: xml(200, CHANGE)))
    return api


def provider(api):
    return get('route53')(CREDS, client=api.client())


def change(request):
    root = ET.fromstring(request.content)
    c = root.find(f'{NS}ChangeBatch/{NS}Changes/{NS}Change')
    s = c.find(f'{NS}ResourceRecordSet')
    return (c.findtext(f'{NS}Action'), s.findtext(f'{NS}Name'), s.findtext(f'{NS}Type'), s.findtext(f'{NS}TTL'),
            [v.text for v in s.iter(f'{NS}Value')])


async def test_caps():
    caps = get('route53').caps
    assert (caps.by_id, caps.multi, caps.zones, caps.read, caps.delete) == (False, False, True, True, True)


async def test_check_lists_public_zones_only():
    api = FakeAPI().on('GET', BASE + '/hostedzone', signed(lambda r, m: xml(200, ZONES)))
    assert await provider(api).check() == {'zones': ['example.com', 'example.net'], 'writable': None}
    assert query(api.requests[0]) == {'maxitems': '100'}


async def test_records_reads_the_rrset_and_unescapes_names():
    wildcard = '\\052.example.com.'
    body = (f'<ListResourceRecordSetsResponse xmlns="{XMLNS}"><ResourceRecordSets>'
            f'{rrset(wildcard, "A", ["198.51.100.1", "198.51.100.2"], 60)}{rrset(wildcard, "AAAA", ["2001:db8::1"])}'
            '</ResourceRecordSets><IsTruncated>true</IsTruncated><MaxItems>100</MaxItems></ListResourceRecordSetsResponse>')
    api = fake().on('GET', BASE + '/hostedzone/Z1/rrset', signed(lambda r, m: xml(200, body)))
    records = await provider(api).records('example.com', '*.example.com', 'A')
    assert records == [Record('', '*.example.com', 'A', '198.51.100.1,198.51.100.2', 60)]
    assert query(api.requests[0]) == {'dnsname': 'example.com', 'maxitems': '10'}
    assert query(api.requests[1]) == {'name': '*.example.com.', 'type': 'A', 'maxitems': '100'}


async def test_records_none():
    body = (f'<ListResourceRecordSetsResponse xmlns="{XMLNS}"><ResourceRecordSets>{rrset("zzz.example.com.", "A", ["1.1.1.1"])}'
            '</ResourceRecordSets><IsTruncated>false</IsTruncated><MaxItems>100</MaxItems></ListResourceRecordSetsResponse>')
    api = fake().on('GET', BASE + '/hostedzone/Z1/rrset', signed(lambda r, m: xml(200, body)))
    assert await provider(api).records('example.com', 'home.example.com', 'A') == []


async def test_create_is_upsert():
    api = fake()
    record = await provider(api).create('example.com', 'home.example.com', 'A', '198.51.100.9', None, {})
    assert change(api.requests[-1]) == ('UPSERT', 'home.example.com.', 'A', '300', ['198.51.100.9'])
    assert api.requests[-1].headers['Content-Type'] == 'application/xml'
    assert record == Record('', 'home.example.com', 'A', '198.51.100.9', 300)


async def test_update_keeps_ttl_and_replaces_the_set():
    api = fake()
    current = Record('', 'home.example.com', 'AAAA', '2001:db8::1,2001:db8::2', 60)
    updated = await provider(api).update('example.com', current, '2001:db8::9', None, {})
    assert change(api.requests[-1]) == ('UPSERT', 'home.example.com.', 'AAAA', '60', ['2001:db8::9'])
    assert updated.ttl == 60 and updated.value == '2001:db8::9'


async def test_delete_repeats_the_current_set():
    api = fake()
    await provider(api).delete('example.com', Record('', 'home.example.com', 'A', '198.51.100.1,198.51.100.2', 60))
    assert change(api.requests[-1]) == ('DELETE', 'home.example.com.', 'A', '60', ['198.51.100.1', '198.51.100.2'])


async def test_unknown_zone():
    empty = f'<ListHostedZonesByNameResponse xmlns="{XMLNS}"><HostedZones/><IsTruncated>false</IsTruncated></ListHostedZonesByNameResponse>'
    api = FakeAPI().on('GET', BASE + '/hostedzonesbyname', signed(lambda r, m: xml(200, empty)))
    with pytest.raises(ProviderError) as err:
        await provider(api).create('example.org', 'example.org', 'A', '198.51.100.9', None, {})
    assert err.value.code == 'zone_not_found'


@pytest.mark.parametrize('status,body,expected', [
    (403, '<ErrorResponse><Error><Type>Sender</Type><Code>SignatureDoesNotMatch</Code><Message>m</Message></Error></ErrorResponse>', 'auth_failed'),
    (403, f'<ErrorResponse xmlns="{XMLNS}"><Error><Type>Sender</Type><Code>InvalidClientTokenId</Code><Message>m</Message></Error></ErrorResponse>', 'auth_failed'),
    (403, f'<ErrorResponse xmlns="{XMLNS}"><Error><Code>AccessDenied</Code><Message>m</Message></Error></ErrorResponse>', 'forbidden'),
    (404, f'<ErrorResponse xmlns="{XMLNS}"><Error><Code>NoSuchHostedZone</Code><Message>m</Message></Error></ErrorResponse>', 'zone_not_found'),
    (400, f'<ErrorResponse xmlns="{XMLNS}"><Error><Code>Throttling</Code><Message>Rate exceeded</Message></Error></ErrorResponse>', 'rate_limited'),
    (400, f'<InvalidChangeBatch xmlns="{XMLNS}"><Messages><Message>m</Message></Messages></InvalidChangeBatch>', 'bad_request'),
    (503, 'Service Unavailable', 'server_error'),
])
async def test_errors(status, body, expected):
    api = FakeAPI().on('POST', BASE + '/hostedzone/Z1/rrset/', lambda r, m: xml(status, body))
    api.on('GET', BASE + '/hostedzonesbyname', lambda r, m: xml(200, BY_NAME))
    with pytest.raises(ProviderError) as err:
        await provider(api).create('example.com', 'home.example.com', 'A', '198.51.100.9', None, {})
    assert err.value.code == expected


async def test_http_200_without_change_info_is_invalid():
    api = FakeAPI().on('GET', BASE + '/hostedzonesbyname', lambda r, m: xml(200, BY_NAME))
    api.on('POST', BASE + '/hostedzone/Z1/rrset/', lambda r, m: xml(200, f'<Other xmlns="{XMLNS}"/>'))
    with pytest.raises(ProviderError) as err:
        await provider(api).create('example.com', 'home.example.com', 'A', '198.51.100.9', None, {})
    assert err.value.code == 'invalid_response'
