"""IP 情报 beside IP 质量: what public sources say of a server's public IPv4 and its network, read by the
controller (not the node) and kept a while, each source on its own so one failing leaves the rest: the network
the address is announced from (RIPEstat), that ASN's announced IPv4 space (RIPEstat), its registration and
holder (RDAP), its own word on what it is and its traffic (PeeringDB), the share of its traffic that is bots
(Cloudflare Radar, with the token of a Cloudflare account of 动态域名), the address's reverse DNS, whether it
is a bogon, and Project Honey Pot's http:BL (with the key kept in the vault). Its open ports are probed from
the controller when asked."""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import time

import aiohttp
import dns.asyncresolver
import dns.resolver
import dns.reversename

from app.instances.contracts import InstanceError
from app.instances.servers import LOCAL
from app.webhooks.repository import one

STAT = 'https://stat.ripe.net/data/{call}/data.json?resource={resource}&sourceapp=openbear'
RDAP = 'https://rdap.org/autnum/{asn}'
PEERINGDB = 'https://www.peeringdb.com/api/net?asn={asn}'
RADAR = 'https://api.cloudflare.com/client/v4/radar/http/summary/bot_class?asn={asn}&dateRange=7d'
HTTPBL = '{key}.{reversed}.dnsbl.httpbl.org'
# How long an answer is kept: a network's a day, an address's six hours; a failed read half an hour, so a
# source out of reach is not waited for on every look.
NETWORK_S, ADDRESS_S, FAILED_S = 86400, 6 * 3600, 1800
TIMEOUT = aiohttp.ClientTimeout(total=12)
SECRET_NAME, SECRET_NOTE = 'servers/httpbl', 'IP 情报凭据'
# http:BL's visitor types, the fourth octet's bits (0: a search engine).
VISITORS = ((1, 'suspicious'), (2, 'harvester'), (4, 'spammer'))
# What an address is probed on from the controller: the ports people leave open, by what usually listens.
PORTS = ((21, 'FTP'), (22, 'SSH'), (23, 'Telnet'), (25, 'SMTP'), (53, 'DNS'), (80, 'HTTP'), (110, 'POP3'), (111, 'RPC'), (135, 'MSRPC'),
         (139, 'NetBIOS'), (143, 'IMAP'), (443, 'HTTPS'), (445, 'SMB'), (465, 'SMTPS'), (587, 'SMTP 提交'), (993, 'IMAPS'), (995, 'POP3S'),
         (1080, 'SOCKS'), (1433, 'MSSQL'), (2375, 'Docker'), (3128, 'HTTP 代理'), (3306, 'MySQL'), (3389, '远程桌面'), (5432, 'PostgreSQL'),
         (5900, 'VNC'), (6379, 'Redis'), (8080, 'HTTP 备用'), (8443, 'HTTPS 备用'), (9200, 'Elasticsearch'), (11211, 'Memcached'), (27017, 'MongoDB'))
PORT_TIMEOUT_S = 2.5


def bogon(ip):
    """Whether an IPv4 address is not one the internet routes: private, shared, loopback, link-local,
    reserved, multicast or documentation space."""
    address = ipaddress.ip_address(ip)
    return not address.is_global or address.is_multicast


def rdap_view(data):
    """RDAP's autnum as {name, org, registered}: the registrant organisation (RIPE's ORG- object over its
    maintainers; ARIN's one registrant), and the registration date unless it is the epoch some keep for unknown."""
    if not isinstance(data, dict):
        return None
    org = ''
    for entity in data.get('entities') or []:
        if not isinstance(entity, dict) or 'registrant' not in (entity.get('roles') or []):
            continue
        handle = str(entity.get('handle') or '')
        if 'MNT' in handle.upper().split('-'):
            continue
        card = entity.get('vcardArray') if isinstance(entity.get('vcardArray'), list) and len(entity['vcardArray']) > 1 else [None, []]
        name = next((str(item[3]) for item in card[1] if isinstance(item, list) and len(item) > 3 and item[0] == 'fn'), '')
        if name and (not org or handle.startswith('ORG-')):
            org = name
    registered = next((str(e.get('eventDate') or '')[:10] for e in data.get('events') or []
                       if isinstance(e, dict) and e.get('eventAction') == 'registration'), '')
    return {'name': str(data.get('name') or '')[:60], 'org': org[:120], 'registered': '' if registered.startswith('1970') else registered}


def peeringdb_view(data):
    """PeeringDB's net for an ASN as {name, type, traffic, scope}; None when the network is not in it."""
    nets = data.get('data') if isinstance(data, dict) else None
    if not isinstance(nets, list) or not nets or not isinstance(nets[0], dict):
        return None
    net = nets[0]
    types = net.get('info_types') if isinstance(net.get('info_types'), list) else []
    return {'name': str(net.get('name') or '')[:80], 'type': str(net.get('info_type') or (types[0] if types else ''))[:40],
            'traffic': str(net.get('info_traffic') or '')[:30], 'scope': str(net.get('info_scope') or '')[:30]}


def radar_view(data):
    """Radar's bot class summary as {human, bot} percentages; None without one."""
    summary = ((data or {}).get('result') or {}).get('summary_0') if isinstance(data, dict) else None
    try:
        return {'human': round(float(summary['human']), 1), 'bot': round(float(summary['bot']), 1)}
    except (TypeError, KeyError, ValueError):
        return None


def httpbl_view(answer):
    """An http:BL answer 127.days.threat.type as {listed, days, threat, kinds}; a search engine is not listed."""
    parts = [int(p) for p in answer.split('.')] if answer else []
    if len(parts) != 4 or parts[0] != 127 or parts[3] == 0:
        return {'listed': False}
    return {'listed': True, 'days': parts[1], 'threat': parts[2], 'kinds': [name for bit, name in VISITORS if parts[3] & bit]}


# Asked of DNS itself: the system's lookup would answer from /etc/hosts first (the machine's own name).
async def _first(name, kind):
    try:
        answer = await dns.asyncresolver.resolve(name, kind, lifetime=6)
    # No such name, no record, or no server able to say (a reverse zone never delegated answers SERVFAIL): none.
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        return ''
    return str(answer[0]) if len(answer) else ''


async def lookup_ptr(ip):
    """The address's reverse DNS name; '' when it has none."""
    return (await _first(dns.reversename.from_address(ip), 'PTR')).rstrip('.')[:200]


async def lookup_a(name):
    """A name's first IPv4 address; '' when it has none."""
    return await _first(name, 'A')


class IpIntel:
    def __init__(self, service):
        self.service = service
        self.reading = {}  # key -> its read under way, joined by any other asking meanwhile (two pages, two servers of one network)

    # ----- the vault's http:BL key -----
    def _memory(self):
        from app.memory.builtin import BuiltinMemoryClient
        identity = getattr(getattr(getattr(self.service.host, 'config', None), 'memory', None), 'identity', None) or 'openbear'
        return BuiltinMemoryClient(self.service.db, identity=identity)

    async def _secret(self):
        return await one(self.service.db.conn, 'SELECT id, kv_json FROM memory_secrets WHERE name=? AND archived=0', (SECRET_NAME,))

    async def httpbl_key(self):
        row = await self._secret()
        with contextlib.suppress(ValueError, TypeError):
            for item in json.loads(row['kv_json'] or '[]') if row else []:
                if isinstance(item, dict) and item.get('key') == 'key' and isinstance(item.get('value'), str):
                    return item['value'].strip()
        return ''

    async def save_httpbl_key(self, key):
        """Keep the key (12 lowercase letters; '' to drop it) in the vault through the credentials page's writer."""
        key = key.strip() if isinstance(key, str) else None
        if key is None or key and not (len(key) == 12 and key.isalpha() and key.islower()):
            raise InstanceError('invalid_httpbl_key')
        row = await self._secret()
        args = {'action': 'set', 'kvJson': json.dumps([{'key': 'key', 'value': key}])}
        args.update({'id': row['id']} if row else {'name': SECRET_NAME, 'note': SECRET_NOTE})
        result = await self._memory().tool_call('secret', args)
        if not result.get('ok'):
            raise InstanceError('secret_save_failed', 500)

    async def view(self, owner):
        """What the settings show of it: whether the key is there and which 动态域名 account Radar reads with."""
        account = await self.service.ddns.cloudflare_account(owner)
        return {'httpbl': bool(await self.httpbl_key()), 'cloudflare': account['alias'] if account else ''}

    # ----- reading, kept a while per address and per network -----
    async def _kept(self, key, max_age, read):
        """read()'s value, as kept under key when younger than max_age; None when it failed (kept FAILED_S)."""
        with contextlib.suppress(Exception):
            raw = await self.service.metrics.meta(key)
            if raw:
                kept = json.loads(raw)
                if time.time() - kept.get('at', 0) < (FAILED_S if kept.get('failed') else max_age):
                    return kept['value']
        task = self.reading.get(key)
        if task is None:
            task = self.reading[key] = self.service.spawn(self._read(key, read))
            task.add_done_callback(lambda t: self.reading.pop(key, None) if self.reading.get(key) is t else None)
        return await asyncio.shield(task)

    async def _read(self, key, read):
        try:
            value, failed = await read(), False
        except Exception:
            value, failed = None, True
        with contextlib.suppress(Exception):
            await self.service.metrics.set_meta(key, json.dumps({'at': int(time.time()), 'value': value, **({'failed': True} if failed else {})},
                                                                ensure_ascii=False, separators=(',', ':')))
        return value

    async def _json(self, url, headers=None, missing=()):
        async with self.service.http.get(url, headers=headers or {}, timeout=TIMEOUT) as response:
            if response.status in missing:
                return None
            response.raise_for_status()
            return await response.json(content_type=None)

    async def _stat(self, call, resource):
        data = await self._json(STAT.format(call=call, resource=resource))
        if not isinstance(data, dict) or not isinstance(data.get('data'), dict):
            raise ValueError(f'RIPEstat {call}')
        return data['data']

    async def _asn(self, ip):
        async def read():
            asns = (await self._stat('network-info', ip)).get('asns') or []
            return int(asns[0]) if asns and str(asns[0]).isdigit() else 0
        return await self._kept(f'intel:asn:{ip}', NETWORK_S, read)

    async def _space(self, asn):
        async def read():
            v4 = ((await self._stat('routing-status', f'AS{asn}')).get('announced_space') or {}).get('v4') or {}
            return {'prefixes': int(v4.get('prefixes') or 0), 'ips': int(v4.get('ips') or 0)}
        return await self._kept(f'intel:space:{asn}', NETWORK_S, read)

    async def _rdap(self, asn):
        async def read():
            view = rdap_view(await self._json(RDAP.format(asn=asn)))
            if view is None:
                raise ValueError('RDAP')
            return view
        return await self._kept(f'intel:rdap:{asn}', NETWORK_S, read)

    async def _peeringdb(self, asn):
        # Not in PeeringDB is an answer too: kept as {} (shown as not registered there).
        async def read():
            return peeringdb_view(await self._json(PEERINGDB.format(asn=asn))) or {}
        return await self._kept(f'intel:peeringdb:{asn}', NETWORK_S, read)

    async def _radar(self, owner, asn):
        account = await self.service.ddns.cloudflare_account(owner)
        if not account:
            return {'state': 'unconfigured'}

        async def read():
            view = radar_view(await self._json(RADAR.format(asn=asn), {'Authorization': f'Bearer {account["token"]}'}, missing=(400, 404)))
            return view or {}
        value = await self._kept(f'intel:radar:{asn}', NETWORK_S, read)
        return None if value is None else {'state': 'ok', **value} if value else {'state': 'none'}

    async def _rdns(self, ip):
        return await self._kept(f'intel:rdns:{ip}', ADDRESS_S, lambda: lookup_ptr(ip))

    async def _httpbl(self, ip):
        key = await self.httpbl_key()
        if not key:
            return {'state': 'unconfigured'}

        async def read():
            return httpbl_view(await lookup_a(HTTPBL.format(key=key, reversed='.'.join(reversed(ip.split('.'))))))
        value = await self._kept(f'intel:httpbl:{ip}', ADDRESS_S, read)
        return None if value is None else {'state': 'ok', **value}

    async def _address(self, owner, server_id):
        server = await self.service.registry.get(owner, server_id)
        node = await one(self.service.db.conn, 'SELECT info_json FROM server_nodes WHERE server_id=? AND owner=?', (server_id, owner))
        info = (json.loads(node['info_json'] or '{}').get('info') or {}) if node else {}
        ip = str(info.get('ipv4') or '')
        with contextlib.suppress(ValueError):
            if ipaddress.ip_address(ip).version == 4:
                return server, ip
        return server, ''

    async def report(self, owner, server_id):
        """{ip, bogon, rdns, httpbl, asn, network: {space, rdap, peeringdb, radar}, ports}: a source that could
        not be read is None; one not set up says so ({state: 'unconfigured'})."""
        _, ip = await self._address(owner, server_id)
        ports = await self.last_ports(server_id)
        # Probed before the address changed: not this address's.
        ports = ports if ports and ports.get('ip') == ip else None
        if not ip:
            return {'ok': True, 'ip': '', 'ports': ports}
        if bogon(ip):
            return {'ok': True, 'ip': ip, 'bogon': True, 'ports': ports}
        asn, rdns, httpbl = await asyncio.gather(self._asn(ip), self._rdns(ip), self._httpbl(ip))
        network = None
        if asn:
            space, rdap, peeringdb, radar = await asyncio.gather(self._space(asn), self._rdap(asn), self._peeringdb(asn), self._radar(owner, asn))
            network = {'space': space, 'rdap': rdap, 'peeringdb': peeringdb, 'radar': radar}
        return {'ok': True, 'ip': ip, 'bogon': False, 'rdns': rdns, 'httpbl': httpbl, 'asn': asn or None, 'network': network, 'ports': ports}

    # ----- open ports, probed from the controller when asked -----
    async def last_ports(self, server_id):
        with contextlib.suppress(Exception):
            raw = await self.service.metrics.meta(f'intel:ports:{server_id}')
            if raw:
                return json.loads(raw)
        return None

    async def scan(self, owner, server_id):
        server, ip = await self._address(owner, server_id)
        if not ip:
            raise InstanceError('no_public_ipv4', 409)
        ports = dict(PORTS)
        ssh = int((server.get('port') if server.get('auth') != 'none' else 0) or 0)
        if ssh and ssh not in ports:
            ports[ssh] = 'SSH'
        gate = asyncio.Semaphore(16)

        async def probe(port):
            async with gate:
                try:
                    _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), PORT_TIMEOUT_S)
                except (OSError, asyncio.TimeoutError):
                    return False
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
                return True
        found = await asyncio.gather(*(probe(p) for p in ports))
        result = {'atMs': int(time.time() * 1000), 'ip': ip, 'checked': len(ports), 'fromSelf': server_id == LOCAL,
                  'open': [{'port': p, 'name': ports[p]} for p, up in zip(ports, found) if up]}
        await self.service.metrics.set_meta(f'intel:ports:{server_id}', json.dumps(result, ensure_ascii=False, separators=(',', ':')))
        return {'ok': True, 'ports': result}
