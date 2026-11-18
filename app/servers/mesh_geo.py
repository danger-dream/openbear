"""Province networks for port forwards' allow lists, from ip2region (github.com/lionsoul2014/ip2region):
its IPv4 and IPv6 xdb files, each segment a range of addresses with "country|province|city|ISP|code".
The controller checks the files once a day and downloads them when they changed; the ranges of each
province of China are merged and summarized into networks, kept on disk (provinces.json) and given
to the nodes whose forwards allow a province (mesh_forward). Nodes never download anything.

A province is named as the data names it: 广东省, 北京市, 广西, 香港特别行政区. Names the data
spells otherwise in a few IPv6 segments (a city where the province goes) are left out."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import struct
import tempfile
import time
from pathlib import Path

import aiohttp

SOURCES = {
    'v4': 'https://raw.githubusercontent.com/lionsoul2014/ip2region/master/data/ip2region_v4.xdb',
    'v6': 'https://raw.githubusercontent.com/lionsoul2014/ip2region/master/data/ip2region_v6.xdb',
}
CHECK_S = 24 * 3600
RETRY_S = 3600
MAX_FILE = 128 << 20
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=900, sock_connect=20, sock_read=120)
HEADER = 256
COUNTRY = '中国'
# In the order of their administrative codes.
PROVINCES = ('北京市', '天津市', '河北省', '山西省', '内蒙古', '辽宁省', '吉林省', '黑龙江省', '上海市', '江苏省', '浙江省', '安徽省',
             '福建省', '江西省', '山东省', '河南省', '湖北省', '湖南省', '广东省', '广西', '海南省', '重庆市', '四川省', '贵州省', '云南省',
             '西藏', '陕西省', '甘肃省', '青海省', '宁夏', '新疆', '台湾省', '香港特别行政区', '澳门特别行政区')


class GeoError(Exception):
    pass


def segments(data):
    """(first, last, region) of each segment of an xdb file, addresses as integers."""
    if len(data) < HEADER:
        raise GeoError('xdb too short')
    version, _policy, _created, start, end, ip_version, ptr_bytes = struct.unpack_from('<HHIIIHH', data, 0)
    if version == 2:
        ip_version, ptr_bytes = 4, 4
    if version not in (2, 3) or ip_version not in (4, 6) or ptr_bytes != 4:
        raise GeoError(f'xdb version {version}, ip version {ip_version}')
    width = 4 if ip_version == 4 else 16
    size = 2 * width + 6
    if not HEADER <= start <= end or end + size > len(data) or (end - start) % size:
        raise GeoError('xdb index out of the file')
    for p in range(start, end + 1, size):
        if width == 4:
            first, last = struct.unpack_from('<II', data, p)
        else:
            first, last = int.from_bytes(data[p:p + 16], 'big'), int.from_bytes(data[p + 16:p + 32], 'big')
        length, ptr = struct.unpack_from('<HI', data, p + 2 * width)
        if ptr + length > len(data):
            raise GeoError('xdb region out of the file')
        yield first, last, data[ptr:ptr + length].decode('utf-8', 'replace')


def provinces(data, version):
    """{province: [network, ...]} of one xdb file (version 4 or 6)."""
    ranges = {}
    for first, last, region in segments(data):
        country, _, rest = region.partition('|')
        name = rest.partition('|')[0]
        if country == COUNTRY and name in PROVINCES:
            ranges.setdefault(name, []).append((first, last))
    address = ipaddress.IPv4Address if version == 4 else ipaddress.IPv6Address
    out = {}
    for name, rs in ranges.items():
        rs.sort()
        merged = []
        for first, last in rs:
            if merged and first <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], last)
            else:
                merged.append([first, last])
        out[name] = [str(n) for first, last in merged for n in ipaddress.summarize_address_range(address(first), address(last))]
    return out


class Store:
    """The province networks the controller has, on disk under root."""

    def __init__(self, root):
        self.root = Path(root)
        self.sets = {}  # province -> [network], IPv4 first
        self.meta = {}  # updatedMs, checkedMs, etags {v4, v6}, digest, error
        self.lock = asyncio.Lock()

    @property
    def path(self):
        return self.root / 'provinces.json'

    def load(self):
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        if isinstance(data, dict) and isinstance(data.get('sets'), dict):
            self.sets = {k: v for k, v in data['sets'].items() if k in PROVINCES and isinstance(v, list)}
            self.meta = data.get('meta') if isinstance(data.get('meta'), dict) else {}

    def _save(self):
        self.root.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=self.root, prefix='.provinces-')
        with os.fdopen(fd, 'w') as f:
            json.dump({'meta': self.meta, 'sets': self.sets}, f, ensure_ascii=False, separators=(',', ':'))
        os.replace(temp, self.path)

    def prefixes(self, name):
        """name's networks, None while the data has none for it (not downloaded yet)."""
        return self.sets.get(name)

    def view(self):
        def count(nets, colon):
            return sum(1 for n in nets if (':' in n) == colon)
        return {'provinces': [{'name': p, 'v4': count(self.sets.get(p, []), False), 'v6': count(self.sets.get(p, []), True)} for p in PROVINCES],
                'ready': bool(self.sets), 'updatedAt': self.meta.get('updatedMs'), 'checkedAt': self.meta.get('checkedMs'),
                'error': self.meta.get('error', ''), 'source': 'ip2region'}

    def due(self, now_ms):
        if not self.sets:
            last = self.meta.get('checkedMs') if self.meta.get('error') else None
            return last is None or now_ms - last >= RETRY_S * 1000
        wait = RETRY_S if self.meta.get('error') else CHECK_S
        return now_ms - (self.meta.get('checkedMs') or 0) >= wait * 1000

    async def refresh(self, http, now_ms):
        """Downloads what changed and makes the sets again; True when they changed."""
        async with self.lock:
            etags = dict(self.meta.get('etags') or {})
            files = {}
            try:
                for key, url in SOURCES.items():
                    got = await _download(http, url, etags.get(key) if self.sets else None)
                    if got is None:
                        continue
                    files[key], etags[key] = got
            except GeoError as e:
                self.meta = {**self.meta, 'checkedMs': now_ms, 'error': str(e)[:300]}
                self._save_quietly()
                return False
            if not files:
                self.meta = {**self.meta, 'checkedMs': now_ms, 'error': ''}
                self._save_quietly()
                return False
            if len(files) < len(SOURCES):
                # One changed: the other is needed again to make the sets, whatever its tag says.
                for key, url in SOURCES.items():
                    if key not in files:
                        try:
                            files[key], etags[key] = await _download(http, url, None)
                        except GeoError as e:
                            self.meta = {**self.meta, 'checkedMs': now_ms, 'error': str(e)[:300]}
                            self._save_quietly()
                            return False
            try:
                sets = await asyncio.to_thread(_build, files)
            except (GeoError, ValueError, struct.error) as e:
                self.meta = {**self.meta, 'checkedMs': now_ms, 'error': f'xdb: {e}'[:300]}
                self._save_quietly()
                return False
            digest = hashlib.sha256(json.dumps(sets, sort_keys=True).encode()).hexdigest()
            changed = digest != self.meta.get('digest')
            self.sets = sets
            self.meta = {'updatedMs': now_ms if changed else self.meta.get('updatedMs', now_ms), 'checkedMs': now_ms, 'etags': etags,
                         'digest': digest, 'error': ''}
            self._save_quietly()
            return changed

    def _save_quietly(self):
        try:
            self._save()
        except OSError:
            pass


def _build(files):
    sets = {}
    for key, version in (('v4', 4), ('v6', 6)):
        for name, nets in provinces(files[key], version).items():
            sets.setdefault(name, []).extend(nets)
    if not sets:
        raise GeoError('no province of China in the data')
    return {name: sets[name] for name in PROVINCES if name in sets}


async def _download(http, url, etag):
    """(bytes, etag) of url, None when it still has etag."""
    headers = {'User-Agent': 'openbear-mesh'}
    if etag:
        headers['If-None-Match'] = etag
    try:
        async with http.get(url, headers=headers, timeout=DOWNLOAD_TIMEOUT) as response:
            if response.status == 304:
                return None
            if response.status != 200:
                raise GeoError(f'{url}: HTTP {response.status}')
            body = bytearray()
            async for block in response.content.iter_chunked(1 << 16):
                body += block
                if len(body) > MAX_FILE:
                    raise GeoError(f'{url}: too large')
            return bytes(body), response.headers.get('ETag', '')
    except (aiohttp.ClientError, TimeoutError) as e:
        raise GeoError(f'{url}: {str(e) or "timed out"}') from e


STORE = None


def store():
    return STORE


def install(root):
    """The controller's store, loaded from disk."""
    global STORE
    STORE = Store(root)
    STORE.load()
    return STORE


def prefixes(name):
    return STORE.prefixes(name) if STORE else None


def now_ms():
    return int(time.time() * 1000)
