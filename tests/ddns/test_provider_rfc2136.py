"""RFC 2136 against a local fake authoritative server (UDP and TCP) that verifies TSIG; no network beyond 127.0.0.1."""
import asyncio
import base64
import struct

import dns.flags
import dns.message
import dns.name
import dns.opcode
import dns.rcode
import dns.rdataclass
import dns.rdatatype
import dns.rrset
import dns.tsig
import pytest

from app.ddns.providers import get, rfc2136
from app.ddns.providers.base import ProviderError, Record

SECRET = base64.b64encode(b'openbear-test-tsig-secret-32byte').decode()
ZONE = 'example.com'


class FakeDNS:
    def __init__(self, algorithm='hmac-sha256', key_name='ddns-key.', secret=SECRET):
        self.key = dns.tsig.Key(key_name, secret, rfc2136.ALGORITHMS[algorithm])
        self.algorithm = rfc2136.ALGORITHMS[algorithm]
        self.data = {('example.com', 'SOA'): (3600, ['ns1.example.com. admin.example.com. 1 3600 600 86400 300']),
                     ('home.example.com', 'A'): (120, ['198.51.100.1', '198.51.100.2'])}
        self.seen = []        # (transport, message) of every verified request
        self.truncate = False  # answer UDP queries with TC set
        self.silent = False
        self.rcode = None      # force an rcode on updates

    def handle(self, wire, transport):
        if self.silent:
            return None
        try:
            request = dns.message.from_wire(wire, keyring={self.key.name: self.key})
        except dns.tsig.BadSignature:
            request = dns.message.from_wire(wire, keyring=False)
            response = dns.message.make_response(request)
            response.set_rcode(dns.rcode.NOTAUTH)
            response.use_tsig(self.key, tsig_error=dns.rcode.BADSIG)
            return response.to_wire()
        assert request.had_tsig and request.keyname == self.key.name and request.keyalgorithm == self.algorithm
        self.seen.append((transport, request))
        response = dns.message.make_response(request)
        if request.opcode() == dns.opcode.UPDATE:
            self.apply(request)
            if self.rcode is not None:
                response.set_rcode(self.rcode)
        elif transport == 'udp' and self.truncate:
            response.flags |= dns.flags.TC
        else:
            q = request.question[0]
            name, rtype = q.name.to_text(omit_final_dot=True), dns.rdatatype.to_text(q.rdtype)
            if (name, rtype) in self.data:
                ttl, values = self.data[(name, rtype)]
                response.answer.append(dns.rrset.from_text_list(q.name, ttl, 'IN', rtype, values))
            elif not any(n == name for n, _ in self.data):
                response.set_rcode(dns.rcode.NXDOMAIN)
        return response.to_wire()

    def apply(self, update):
        assert update.zone[0].name == dns.name.from_text(ZONE) and update.zone[0].rdtype == dns.rdatatype.SOA
        for rrset in update.update:
            key = (rrset.name.to_text(omit_final_dot=True), dns.rdatatype.to_text(rrset.rdtype))
            if rrset.deleting == dns.rdataclass.ANY:
                self.data.pop(key, None)
            else:
                ttl, values = self.data.get(key, (rrset.ttl, []))
                self.data[key] = (rrset.ttl, values + [rd.to_text() for rd in rrset])

    async def __aenter__(self):
        loop = asyncio.get_running_loop()
        fake = self

        class UDP(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                self.transport = transport

            def datagram_received(self, data, addr):
                reply = fake.handle(data, 'udp')
                if reply:
                    self.transport.sendto(reply, addr)

        async def tcp(reader, writer):
            try:
                while True:
                    size = struct.unpack('!H', await reader.readexactly(2))[0]
                    reply = fake.handle(await reader.readexactly(size), 'tcp')
                    if reply:
                        writer.write(struct.pack('!H', len(reply)) + reply)
                        await writer.drain()
            except asyncio.IncompleteReadError:
                writer.close()

        self.tcp_server = await asyncio.start_server(tcp, '127.0.0.1', 0)
        self.port = self.tcp_server.sockets[0].getsockname()[1]
        self.udp, _ = await loop.create_datagram_endpoint(UDP, local_addr=('127.0.0.1', self.port))
        return self

    async def __aexit__(self, *exc):
        self.udp.close()
        self.tcp_server.close()
        await self.tcp_server.wait_closed()


def provider(server, **creds):
    base = {'server': f'127.0.0.1:{server.port}', 'key_name': 'ddns-key', 'secret': SECRET, 'zones': 'example.com, Example.NET.'}
    return get('rfc2136')({**base, **creds})


def test_caps_and_fields():
    cls = get('rfc2136')
    assert (cls.caps.zones, cls.caps.by_id, cls.caps.multi, cls.caps.read, cls.caps.delete) == (False, False, False, True, True)
    assert [f.key for f in cls.fields] == ['server', 'key_name', 'algorithm', 'secret', 'protocol', 'zones']


@pytest.mark.parametrize('text,expected', [('ns1.example.com', ('ns1.example.com', 53)), ('192.0.2.1:5353', ('192.0.2.1', 5353)),
                                           ('[2001:db8::53]:5300', ('2001:db8::53', 5300)), ('2001:db8::53', ('2001:db8::53', 53))])
def test_split_server(text, expected):
    assert rfc2136.split_server(text) == expected


@pytest.mark.parametrize('creds,message', [({'server': 'host:99999'}, 'invalid DNS server'), ({'algorithm': 'hmac-md5'}, 'algorithm'),
                                           ({'secret': 'not base64!'}, 'TSIG key')])
def test_bad_configuration(creds, message):
    base = {'server': '127.0.0.1', 'key_name': 'k', 'secret': SECRET}
    with pytest.raises(ProviderError) as err:
        get('rfc2136')({**base, **creds})
    assert err.value.code == 'bad_request' and message in err.value.message


async def test_zones_from_configuration():
    async with FakeDNS() as server:
        p = provider(server)
        assert p.caps.zones is True
        assert await p.zones() == ['example.com', 'example.net']
        with pytest.raises(ProviderError) as err:
            await provider(server, zones='').zones()
        assert err.value.code == 'unsupported'


async def test_check_queries_soa_of_each_zone():
    async with FakeDNS() as server:
        assert await provider(server, zones='example.com').check() == {'zones': ['example.com'], 'writable': None}
        q = server.seen[0][1].question[0]
        assert (q.name.to_text(), q.rdtype) == ('example.com.', dns.rdatatype.SOA)
        with pytest.raises(ProviderError) as err:
            await provider(server, zones='example.com,example.net').check()
        assert err.value.code == 'zone_not_found'


async def test_records_reads_the_rrset():
    async with FakeDNS() as server:
        p = provider(server)
        assert await p.records(ZONE, 'home.example.com', 'A') == [Record('', 'home.example.com', 'A', '198.51.100.1,198.51.100.2', 120)]
        assert await p.records(ZONE, 'home.example.com', 'AAAA') == []
        assert await p.records(ZONE, 'nothing.example.com', 'A') == []


@pytest.mark.parametrize('algorithm', ['hmac-sha256', 'hmac-sha512', 'hmac-sha1'])
async def test_create_replaces_the_rrset(algorithm):
    async with FakeDNS(algorithm) as server:
        record = await provider(server, algorithm=algorithm).create(ZONE, 'home.example.com', 'A', '203.0.113.5', 60, {})
        assert record == Record('', 'home.example.com', 'A', '203.0.113.5', 60)
        assert server.data[('home.example.com', 'A')] == (60, ['203.0.113.5'])
        update = server.seen[-1][1]
        assert [(r.deleting, len(r)) for r in update.update] == [(dns.rdataclass.ANY, 0), (None, 1)]


async def test_update_keeps_ttl_and_other_types():
    async with FakeDNS() as server:
        server.data[('home.example.com', 'AAAA')] = (300, ['2001:db8::1'])
        current = Record('', 'home.example.com', 'A', '198.51.100.1,198.51.100.2', 120)
        updated = await provider(server).update(ZONE, current, '198.51.100.9', None, {})
        assert updated.ttl == 120
        assert server.data[('home.example.com', 'A')] == (120, ['198.51.100.9'])
        assert server.data[('home.example.com', 'AAAA')] == (300, ['2001:db8::1'])


async def test_delete_removes_the_rrset():
    async with FakeDNS() as server:
        await provider(server).delete(ZONE, Record('', 'home.example.com', 'A', '198.51.100.1'))
        assert ('home.example.com', 'A') not in server.data


async def test_tcp_transport():
    async with FakeDNS() as server:
        await provider(server, protocol='tcp').create(ZONE, 'tcp.example.com', 'AAAA', '2001:db8::7', None, {})
        assert server.seen[-1][0] == 'tcp' and server.data[('tcp.example.com', 'AAAA')] == (300, ['2001:db8::7'])


async def test_truncated_udp_answer_falls_back_to_tcp():
    async with FakeDNS() as server:
        server.truncate = True
        records = await provider(server).records(ZONE, 'home.example.com', 'A')
        assert records[0].value == '198.51.100.1,198.51.100.2'
        assert [t for t, _ in server.seen] == ['udp', 'tcp']


async def test_wrong_secret_is_auth_failed():
    async with FakeDNS() as server:
        other = base64.b64encode(b'another-secret-of-thirty-two-byt').decode()
        with pytest.raises(ProviderError) as err:
            await provider(server, secret=other).create(ZONE, 'home.example.com', 'A', '203.0.113.5', None, {})
        assert err.value.code == 'auth_failed'
        assert server.data[('home.example.com', 'A')][1] == ['198.51.100.1', '198.51.100.2']


@pytest.mark.parametrize('rcode,expected', [(dns.rcode.REFUSED, 'forbidden'), (dns.rcode.NOTAUTH, 'zone_not_found'),
                                            (dns.rcode.SERVFAIL, 'server_error'), (dns.rcode.NXRRSET, 'bad_request')])
async def test_update_rcodes(rcode, expected):
    async with FakeDNS() as server:
        server.rcode = rcode
        with pytest.raises(ProviderError) as err:
            await provider(server).create(ZONE, 'home.example.com', 'A', '203.0.113.5', None, {})
        assert err.value.code == expected


async def test_timeout(monkeypatch):
    monkeypatch.setattr(rfc2136, 'TIMEOUT', 0.2)
    async with FakeDNS() as server:
        server.silent = True
        with pytest.raises(ProviderError) as err:
            await provider(server).records(ZONE, 'home.example.com', 'A')
        assert err.value.code == 'network'


async def test_name_outside_zone():
    async with FakeDNS() as server:
        with pytest.raises(ProviderError) as err:
            await provider(server).create(ZONE, 'home.example.net', 'A', '203.0.113.5', None, {})
        assert err.value.code == 'bad_request' and not server.seen
