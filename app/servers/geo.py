"""Country of a server's public address, for the flag shown next to it.
Lookups go to public GeoIP services only when the address changes."""
from __future__ import annotations

import asyncio
import ipaddress
import re
import time
from collections import OrderedDict

import aiohttp

_ISO = re.compile(r'[A-Z]{2}')


def public_address(ip):
    try:
        value = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return value.is_global


async def lookup(http, ip):
    if not public_address(ip):
        return None
    sources = (
        (f'https://get.geojs.io/v1/ip/country/{ip}.json', lambda d: (d.get('country'), d.get('name'))),
        (f'https://ipinfo.io/{ip}/json', lambda d: (d.get('country'), '')),
        (f'https://ipwho.is/{ip}', lambda d: (d.get('country_code'), d.get('country'))),
    )
    for url, pick in sources:
        try:
            async with http.get(url, headers={'User-Agent': 'openbear-servers'}, timeout=aiohttp.ClientTimeout(total=8)) as response:
                if response.status != 200:
                    continue
                data = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        code, name = pick(data)
        code = str(code or '').upper()
        if _ISO.fullmatch(code):
            return {'ip': ip, 'country': code, 'name': str(name or '')[:60]}
    return None


# ----- network and operator of trace hops: batched, cached in memory -----
GEOJS_BATCH = 'https://get.geojs.io/v1/ip/geo.json'
BATCH = 50
_HEADERS = {'User-Agent': 'openbear-servers'}


def _asn(value):
    """A public AS number, else None: geojs answers 64512, a private one, for networks it
    does not know, CN2's 59.43.0.0/16 among them."""
    text = str(value or '').upper().removeprefix('AS').split(' ')[0]
    asn = int(text) if text.isdigit() else 0
    if not 0 < asn < 2 ** 32 or asn in (23456, 65535) or 64512 <= asn <= 65534 or asn >= 4200000000:
        return None
    return asn


def network_info(country, country_name, asn, org):
    """{country, countryName, asn, org}, or None when nothing useful is known."""
    code = str(country or '').upper()
    code = code if _ISO.fullmatch(code) else ''
    asn = _asn(asn)
    org = str(org or '').strip()
    org = '' if org.lower() == 'unknown' else org[:100]
    if not code and asn is None and not org:
        return None
    return {'country': code, 'countryName': str(country_name or '')[:60] if code else '', 'asn': asn, 'org': org}


async def _get_json(http, url):
    async with http.get(url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=8)) as response:
        if response.status != 200:
            raise aiohttp.ClientError(f'http {response.status}')
        return await response.json(content_type=None)


async def fetch_networks(http, ips):
    """{ip: info} for the addresses found: one geojs batch query, then ipwho.is one by one for
    any it did not answer or named no operator for, keeping geojs's country when ipwho.is
    knows nothing either. Addresses left out were not found."""
    found, partial = {}, {}
    try:
        data = await _get_json(http, f"{GEOJS_BATCH}?ip={','.join(ips)}")
    except (aiohttp.ClientError, TimeoutError, ValueError):
        data = []
    for row in data if isinstance(data, list) else [data]:
        if isinstance(row, dict) and row.get('ip') in ips:
            info = network_info(row.get('country_code'), row.get('country'), row.get('asn'), row.get('organization_name'))
            if info and info['org']:
                found[row['ip']] = info
            elif info:
                partial[row['ip']] = info
    gate = asyncio.Semaphore(4)

    async def one(ip):
        async with gate:
            try:
                row = await _get_json(http, f'https://ipwho.is/{ip}')
            except (aiohttp.ClientError, TimeoutError, ValueError):
                return
        if isinstance(row, dict) and row.get('success') is not False:
            connection = row.get('connection') if isinstance(row.get('connection'), dict) else {}
            info = network_info(row.get('country_code'), row.get('country'), connection.get('asn'), connection.get('org') or connection.get('isp'))
            if info and (info['org'] or ip not in partial):
                found[ip] = info
    await asyncio.gather(*(one(ip) for ip in ips if ip not in found))
    return {**partial, **found}


class NetworkLookups:
    """Country, ASN and operator of public addresses. get() answers from the cache only;
    want() starts a lookup for the addresses missing from it, in batches, each address at
    most once at a time. Found ones are kept a day, failed ones ten minutes, about LIMIT in all.
    Each server may have at most QUOTA addresses sent out per QUOTA_WINDOW: the services
    limit this controller's IP, and the region of its servers is looked up there too."""
    LIMIT = 5000
    TTL = 24 * 3600
    FAIL_TTL = 600
    QUOTA = 200
    QUOTA_WINDOW = 3600

    def __init__(self, fetch, spawn, clock=time.monotonic):
        self.fetch, self.spawn, self.clock = fetch, spawn, clock
        self.cache = OrderedDict()  # ip -> (expires, info or None)
        self.pending = set()
        self.spent = {}  # server -> (window start, addresses sent out)

    def _cached(self, ip):
        hit = self.cache.get(ip)
        if hit and hit[0] <= self.clock():
            del self.cache[ip]
            return None
        return hit

    def get(self, ip):
        hit = self._cached(ip)
        return hit[1] if hit else None

    def want(self, ips, server=None):
        """The addresses left unasked because server used up its quota."""
        missing = [ip for ip in dict.fromkeys(ips) if ip not in self.pending and not self._cached(ip)]
        refused = []
        if server is not None and missing:
            now = self.clock()
            start, used = self.spent.get(server, (now, 0))
            if now - start >= self.QUOTA_WINDOW:
                start, used = now, 0
            room = max(0, self.QUOTA - used)
            missing, refused = missing[:room], missing[room:]
            self.spent[server] = (start, used + len(missing))
        if missing:
            self.pending.update(missing)
            self.spawn(self._run(missing))
        return set(refused)

    async def _run(self, ips):
        try:
            for start in range(0, len(ips), BATCH):
                chunk = ips[start:start + BATCH]
                try:
                    found = await self.fetch(chunk)
                except Exception:  # an outside service: any failure is a miss, tried again later
                    found = {}
                found = found if isinstance(found, dict) else {}
                now = self.clock()
                for ip in chunk:
                    info = found.get(ip)
                    self.cache[ip] = (now + (self.TTL if info else self.FAIL_TTL), info or None)
                    self.cache.move_to_end(ip)
                while len(self.cache) > self.LIMIT:
                    self.cache.popitem(last=False)
        finally:
            self.pending.difference_update(ips)
