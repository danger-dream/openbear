"""BGP 上游 on the detail page and the card: how the world reaches the network a server's public address is
in, drawn the way bgp.tools does. RIPEstat says which prefix announces the address and from which AS, and
gives the AS path each of RIPE RIS's collector peers has for it; of those the paths through a Tier 1 are
kept, from the origin up to the first Tier 1 on them (a Tier 1 met only behind another is kept with the edge
most peers came by), and every edge carries how many peers' paths use it. Results are kept a day per prefix, so
servers in one network share them; AS names a month."""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import re
import time

from app.instances.contracts import InstanceError
from app.servers.hub import clean_ip
from app.webhooks.repository import one

STAT = 'https://stat.ripe.net/data/{call}/data.json?resource={resource}&sourceapp=openbear'
KEEP_S = 86400
NAMES_S = 30 * 86400
# The Tier 1 networks as bgp.tools draws them, by the names people use.
TIER1 = {174: 'Cogent', 701: 'Verizon', 1299: 'Arelion', 2914: 'NTT', 3257: 'GTT', 3320: 'DTAG', 3356: 'Lumen', 3491: 'PCCW',
         5511: 'Orange', 6453: 'TATA', 6461: 'Zayo', 6762: 'Sparkle', 6830: 'Liberty', 7018: 'AT&T', 12956: 'Telxius'}
# Others often met on the way, whose registered names say little.
KNOWN = {1273: 'Vodafone', 4134: 'ChinaNet', 4637: 'Telstra', 4809: 'CN2', 4837: 'China Unicom', 6939: 'Hurricane Electric',
         7473: 'Singtel', 9002: 'RETN', 9808: 'China Mobile', 9929: 'China Unicom 9929', 10099: 'China Unicom Global',
         23764: 'CTG', 58453: 'CMI', 58807: 'CMIN2', 2516: 'KDDI', 2497: 'IIJ', 4826: 'Vocus', 7922: 'Comcast', 3216: 'Vimpelcom',
         1221: 'Telstra', 7713: 'Telin', 4788: 'TM', 9304: 'HGC', 4635: 'HKIX', 17676: 'SoftBank'}
SUFFIX = re.compile(r'[,\s]+(inc\.?|llc|l\.l\.c\.|ltd\.?|limited|co\.,? ltd\.?|corp\.?|corporation|gmbh|s\.?a\.?s?|sarl|b\.v\.|bv|ab|ag|plc|pte\.? ltd\.?|pty\.? ltd\.?|s\.?r\.?l\.?|oy|as)$', re.I)


def short_name(holder):
    """RIPE's 'HANDLE - Org Name' or 'HANDLE Org Name' as the org's name without its company form."""
    holder = (holder or '').strip()
    if not holder:
        return ''
    name = holder.split(' - ', 1)[1] if ' - ' in holder else holder.split(' ', 1)[1] if ' ' in holder else holder
    while True:
        cut = SUFFIX.sub('', name).strip(' ,')
        if cut == name or not cut:
            break
        name = cut
    return name[:40]


def graph(routes, origins):
    """bgp-state's routes [{source_id, path}] as nodes and edges: the paths that reach a Tier 1, each from its
    origin up to the first Tier 1, and a Tier 1 seen only behind another with the edges that led to it."""
    reach, behind, routes_via, origin_set = {}, {}, 0, set(origins)
    for route in routes:
        path = [a for i, a in enumerate(route.get('path') or []) if isinstance(a, int) and (i == 0 or a != route['path'][i - 1])]
        if len(path) < 2:
            continue
        up = path[::-1]
        first = next((i for i, a in enumerate(up) if a in TIER1), None)
        if first is None or first == 0:
            continue
        routes_via += 1
        peer = route.get('source_id') or ''
        for a, b in zip(up[:first], up[1:first + 1]):
            reach.setdefault((a, b), set()).add(peer)
        for a, b in zip(up[first:], up[first + 1:]):
            if a not in TIER1 or b not in TIER1:
                break
            behind.setdefault((a, b), set()).add(peer)
    first_t1 = {b for _, b in reach if b in TIER1}
    edges = {k: len(v) for k, v in reach.items() if k[0] != k[1]}
    strongest = {}
    for (a, b), peers in behind.items():
        if b not in first_t1 and a != b and len(peers) > strongest.get(b, (None, 0))[1]:
            strongest[b] = (a, len(peers))
    for b, (a, n) in strongest.items():
        edges[(a, b)] = n
    nodes = sorted({a for e in edges for a in e})
    found = [a for a in nodes if a in origin_set] or [a for a in origins if a in nodes]
    return {'origins': found or list(origins), 'nodes': nodes, 'edges': [{'from': a, 'to': b, 'peers': n} for (a, b), n in sorted(edges.items())],
            'routes': len(routes), 'via': routes_via}


class Bgp:
    def __init__(self, service):
        self.service = service
        self.pending = {}

    async def _get(self, call, resource):
        async with self.service.http.get(STAT.format(call=call, resource=resource)) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)
        if not isinstance(data, dict) or data.get('status') not in (None, 'ok') or not isinstance(data.get('data'), dict):
            raise ValueError(f'RIPEstat {call}: {str(data.get("messages") if isinstance(data, dict) else data)[:120]}')
        return data['data']

    async def _cached(self, key, max_age):
        with contextlib.suppress(Exception):
            raw = await self.service.metrics.meta(key)
            if raw:
                value = json.loads(raw)
                if time.time() * 1000 - value.get('atMs', 0) < max_age * 1000:
                    return value
        return None

    async def _keep(self, key, value):
        await self.service.metrics.set_meta(key, json.dumps(value, ensure_ascii=False, separators=(',', ':')))

    async def _names(self, asns):
        out = {a: TIER1.get(a) or KNOWN.get(a) for a in asns}
        missing = [a for a, name in out.items() if not name]
        gate = asyncio.Semaphore(4)

        async def one_name(asn):
            hit = await self._cached(f'asname:{asn}', NAMES_S)
            if hit:
                return asn, hit['name']
            async with gate:
                with contextlib.suppress(Exception):
                    name = short_name((await self._get('as-overview', f'AS{asn}')).get('holder'))
                    await self._keep(f'asname:{asn}', {'atMs': int(time.time() * 1000), 'name': name})
                    return asn, name
            return asn, ''
        for asn, name in await asyncio.gather(*(one_name(a) for a in missing)):
            out[asn] = name
        return out

    async def _fetch(self, ip, fresh):
        if not fresh:
            known = await self._cached(f'bgp-ip:{ip}', KEEP_S)
            hit = known and await self._cached(f'bgp:{known.get("prefix")}', KEEP_S)
            if hit:
                return hit
        info = await self._get('network-info', ip)
        prefix, origins = info.get('prefix') or '', [int(a) for a in info.get('asns') or [] if str(a).isdigit()]
        if not prefix or not origins:
            return {'state': 'not_routed', 'prefix': prefix, 'origins': origins}
        await self._keep(f'bgp-ip:{ip}', {'atMs': int(time.time() * 1000), 'prefix': prefix})
        hit = not fresh and await self._cached(f'bgp:{prefix}', KEEP_S)
        if hit:
            return hit
        routes = (await self._get('bgp-state', prefix)).get('bgp_state') or []
        drawn = graph([r for r in routes if isinstance(r, dict)], origins)
        names = await self._names(drawn['nodes'] or drawn['origins'])
        value = {'state': 'ok' if drawn['edges'] else 'no_transit', 'prefix': prefix, 'atMs': int(time.time() * 1000), **drawn,
                 'nodes': [{'asn': a, 'name': names.get(a) or '', 'tier1': a in TIER1, 'origin': a in drawn['origins']} for a in drawn['nodes']],
                 'originNames': {str(a): names.get(a) or '' for a in drawn['origins']}}
        await self._keep(f'bgp:{prefix}', value)
        return value

    async def view(self, owner, server_id, family, fresh=False):
        """The server's network at one family: its address, the prefix, and the graph (state ok), or why not.
        RIPEstat out of reach: what was drawn last, however old, with the error."""
        if family not in (4, 6):
            raise InstanceError('invalid_family')
        await self.service.registry.get(owner, server_id)
        row = await one(self.service.db.conn, 'SELECT info_json FROM server_nodes WHERE owner=? AND server_id=?', (owner, server_id))
        info = (json.loads(row['info_json'] or '{}') if row else {}).get('info') or {}
        ips = {4: clean_ip(info.get('ipv4'), 4), 6: _global6(info.get('ipv6'))}
        ip = ips[family]
        base = {'ok': True, 'family': family, 'ip': ip, 'families': [f for f in (4, 6) if ips[f]]}
        if not ip:
            return {**base, 'state': 'no_address'}
        # One fetch for every request of the same ip, a refresh only of its own kind; a request
        # that goes away leaves it to the others.
        task = self.pending.get((ip, True)) or (None if fresh else self.pending.get((ip, False)))
        if task is None:
            key = (ip, fresh)
            task = self.pending[key] = asyncio.ensure_future(self._fetch(ip, fresh))
            task.add_done_callback(lambda t: self._done(key, t))
        try:
            return {**base, **await asyncio.shield(task), 'error': ''}
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'[:200]
            return {**base, **(await self._stale(ip) or {'state': 'unavailable'}), 'error': error}

    def _done(self, key, task):
        if self.pending.get(key) is task:
            del self.pending[key]
        if not task.cancelled():
            task.exception()

    async def _stale(self, ip):
        with contextlib.suppress(Exception):
            prefix = json.loads(await self.service.metrics.meta(f'bgp-ip:{ip}') or '{}').get('prefix')
            value = json.loads(await self.service.metrics.meta(f'bgp:{prefix}') or '{}') if prefix else {}
            return value if value.get('state') else None
        return None


def _global6(value):
    ip = clean_ip(value, 6)
    return ip if ip and ipaddress.ip_address(ip).is_global else ''
