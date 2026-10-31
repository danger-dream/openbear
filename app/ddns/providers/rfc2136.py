"""RFC 2136 dynamic update signed with TSIG (RFC 8945), for BIND, PowerDNS, Technitium, Windows DNS and the like."""
from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import socket

import dns.asyncquery
import dns.exception
import dns.message
import dns.name
import dns.rcode
import dns.rdataclass
import dns.rdatatype
import dns.tsig
import dns.update

from app.ddns.providers.base import Caps, Field, Provider, ProviderError, Record, register

ALGORITHMS = {'hmac-sha256': dns.tsig.HMAC_SHA256, 'hmac-sha512': dns.tsig.HMAC_SHA512, 'hmac-sha1': dns.tsig.HMAC_SHA1}
TIMEOUT = 10.0
DEFAULT_TTL = 300
RCODES = {dns.rcode.REFUSED: 'forbidden', dns.rcode.NOTAUTH: 'zone_not_found', dns.rcode.SERVFAIL: 'server_error'}


def split_server(text):
    """'host', 'host:port', '[v6]:port' or a bare IPv6 address -> (host, port)."""
    text = text.strip()
    if text.startswith('['):
        host, _, rest = text[1:].partition(']')
        port = rest.lstrip(':') or '53'
    elif text.count(':') == 1:
        host, port = text.split(':')
    else:
        host, port = text, '53'
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ProviderError('bad_request', f'invalid DNS server: {text}')
    return host, int(port)


@register
class Rfc2136(Provider):
    type = 'rfc2136'
    name = 'RFC 2136 动态更新'
    tier = 'C'
    fields = (
        Field('server', 'DNS 服务器', hint='主机:端口，端口默认 53；IPv6 写成 [2001:db8::53]:53'),
        Field('key_name', 'TSIG 密钥名'),
        Field('algorithm', 'TSIG 算法', 'select', options=('hmac-sha256:HMAC-SHA256', 'hmac-sha512:HMAC-SHA512', 'hmac-sha1:HMAC-SHA1'),
              optional=True, default='hmac-sha256'),
        Field('secret', 'TSIG 密钥', 'password', hint='Base64'),
        Field('protocol', '传输协议', 'select', options=('udp:UDP（响应被截断时改用 TCP）', 'tcp:TCP'), optional=True, default='udp'),
        Field('zones', '可管理的域', optional=True, hint='逗号分隔，例如 example.com,example.net；不填则不能列出域'),
    )
    caps = Caps(zones=False, by_id=False, multi=False, ttl_min=1, ttl_max=2147483647)
    docs = 'https://datatracker.ietf.org/doc/html/rfc2136'

    def __init__(self, creds, settings=None, *, client=None):
        super().__init__(creds, settings, client=client)
        self.host, self.port = split_server(self.creds['server'])
        algorithm = self.creds.get('algorithm', 'hmac-sha256').lower()
        if algorithm not in ALGORITHMS:
            raise ProviderError('bad_request', f'unsupported TSIG algorithm: {algorithm}')
        try:
            base64.b64decode(self.creds['secret'], validate=True)
            self.key = dns.tsig.Key(self.creds['key_name'], self.creds['secret'], ALGORITHMS[algorithm])
        except (binascii.Error, ValueError, dns.exception.DNSException) as exc:
            raise ProviderError('bad_request', f'invalid TSIG key: {exc}') from None
        self.protocol = self.creds.get('protocol', 'udp').lower()
        self.managed = [z.strip().lower().rstrip('.') for z in self.creds.get('zones', '').split(',') if z.strip()]
        if self.managed:
            self.caps = Caps(zones=True, by_id=False, multi=False, ttl_min=1, ttl_max=2147483647)

    async def _where(self):
        try:
            return str(ipaddress.ip_address(self.host))
        except ValueError:
            pass
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(self.host, self.port, type=socket.SOCK_DGRAM)
        except OSError as exc:
            raise ProviderError('network', f'cannot resolve {self.host}: {exc}') from None
        return infos[0][4][0]

    async def send(self, message, any_rcode=False):
        where = await self._where()
        try:
            if self.protocol == 'tcp':
                response = await dns.asyncquery.tcp(message, where, timeout=TIMEOUT, port=self.port)
            else:
                response, _ = await dns.asyncquery.udp_with_fallback(message, where, timeout=TIMEOUT, port=self.port)
        except dns.exception.Timeout:
            raise ProviderError('network', f'no answer from {self.host}:{self.port} within {TIMEOUT:g}s') from None
        except (dns.tsig.PeerError, dns.tsig.BadSignature, dns.tsig.BadTime) as exc:
            raise ProviderError('auth_failed', f'TSIG rejected: {type(exc).__name__}') from None
        except OSError as exc:
            raise ProviderError('network', f'{type(exc).__name__}: {exc}') from None
        except dns.exception.DNSException as exc:
            raise ProviderError('invalid_response', f'{type(exc).__name__}: {exc}') from None
        rcode = response.rcode()
        if not any_rcode and rcode not in (dns.rcode.NOERROR, dns.rcode.NXDOMAIN):
            raise ProviderError(RCODES.get(rcode, 'bad_request'), f'server answered {dns.rcode.to_text(rcode)}')
        return response

    async def query(self, name, rtype, any_rcode=False):
        message = dns.message.make_query(name, rtype, flags=0)
        message.use_tsig(self.key)
        return await self.send(message, any_rcode)

    async def zones(self):
        if not self.managed:
            raise ProviderError('unsupported', 'no zones configured')
        return list(self.managed)

    async def check(self):
        for zone in self.managed:
            response = await self.query(zone, 'SOA')
            if not response.get_rrset(response.answer, dns.name.from_text(zone), dns.rdataclass.IN, dns.rdatatype.SOA):
                raise ProviderError('zone_not_found', f'{self.host} is not authoritative for {zone}')
        if not self.managed:
            # Without zones only the transport and the TSIG key can be proven; any answer code will do.
            await self.query('.', 'SOA', any_rcode=True)
        return {'zones': list(self.managed), 'writable': None}

    async def records(self, zone, name, rtype):
        self.relative(zone, name)
        response = await self.query(name, rtype)
        rrset = response.get_rrset(response.answer, dns.name.from_text(name), dns.rdataclass.IN, dns.rdatatype.from_text(rtype))
        if not rrset:
            return []
        return [Record('', name, rtype, ','.join(sorted(rd.to_text() for rd in rrset)), rrset.ttl)]

    async def _update(self, zone, change):
        message = dns.update.UpdateMessage(zone, keyring=self.key)
        change(message)
        response = await self.send(message)
        if response.rcode() != dns.rcode.NOERROR:
            raise ProviderError('bad_request', f'server answered {dns.rcode.to_text(response.rcode())}')

    async def create(self, zone, name, rtype, value, ttl, opts):
        self.relative(zone, name)
        ttl = self.ttl_for(ttl) or DEFAULT_TTL
        await self._update(zone, lambda m: m.replace(dns.name.from_text(name), ttl, rtype, value))
        return Record('', name, rtype, value, ttl)

    async def update(self, zone, record, value, ttl, opts):
        self.relative(zone, record.name)
        ttl = self.ttl_for(ttl) or record.ttl or DEFAULT_TTL
        await self._update(zone, lambda m: m.replace(dns.name.from_text(record.name), ttl, record.type, value))
        return Record('', record.name, record.type, value, ttl)

    async def delete(self, zone, record):
        self.relative(zone, record.name)
        await self._update(zone, lambda m: m.delete(dns.name.from_text(record.name), record.type))
