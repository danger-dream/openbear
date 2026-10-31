"""A server's network addresses and network tests (ping, tcping, trace) run by its node.
Tests aimed at a mesh address or a gateway subnet behind the mesh also get the path
through the mesh; a trace to one runs as a ping, since relays do not answer traceroute."""
from __future__ import annotations

import ipaddress
import json
import re
import time

from app.instances.contracts import InstanceError
from app.servers import geo, mesh_path
from app.servers import mesh_model as model
from app.servers.mesh_config import gateway_routes
from app.webhooks.repository import one

MODES = ('ping', 'tcping', 'trace')
# field: (low, high, default); count's range and default depend on the mode.
LIMITS = {'intervalMs': (200, 10000, 1000), 'timeoutMs': (200, 10000, 2000), 'maxHops': (1, 64, 30)}
COUNTS = {'ping': (1, 100, 10), 'tcping': (1, 100, 10), 'trace': (1, 10, 3)}
MESH_TRACE_COUNT = 10
# What one status may carry from the node, whatever it sends: hops, addresses per hop, results
# (as many as a test can make) and the length of any text.
MAX_HOPS = LIMITS['maxHops'][1]
MAX_HOP_ADDRS = 16
MAX_RESULTS = COUNTS['ping'][1]
MAX_TEXT = 256
KEEP_S = 600  # a test's record is kept this long after it ended or was last asked about
CALL_TIMEOUT = 15

_LABEL = r'[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?'
_HOSTNAME = re.compile(rf'{_LABEL}(?:\.{_LABEL})*\.?')
_LINKLOCAL = ipaddress.ip_network('169.254.0.0/16')
_CGNAT = ipaddress.ip_network('100.64.0.0/10')
_PRIVATE = tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7'))


def _ip(value):
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    return ip.ipv4_mapped if ip.version == 6 and ip.ipv4_mapped else ip


def scope(value, mesh_nets=()):
    """mesh, linklocal, cgnat, private, public or other, in that order of precedence."""
    ip = _ip(value)
    if ip is None:
        return 'other'
    if any(ip in net for net in mesh_nets if net.version == ip.version):
        return 'mesh'
    if ip in _LINKLOCAL:
        return 'linklocal'
    if ip in _CGNAT:
        return 'cgnat'
    if any(ip in net for net in _PRIVATE if net.version == ip.version):
        return 'private'
    return 'public' if ip.is_global and not ip.is_multicast else 'other'


def _bounded(value, depth=0):
    """Node data cut to size: texts, lists, objects and nesting."""
    if isinstance(value, str):
        return value[:MAX_TEXT]
    if isinstance(value, list):
        return [_bounded(v, depth + 1) for v in value[:MAX_RESULTS]] if depth < 5 else []
    if isinstance(value, dict):
        return {str(k)[:64]: _bounded(v, depth + 1) for k, v in list(value.items())[:32]} if depth < 5 else {}
    return value


class ProbeError(InstanceError):
    def __init__(self, field):
        super().__init__('invalid_probe', 400)
        self.field = field


def _int(data, field, low, high, default):
    value = data.get(field)
    if value is None:
        return default
    if type(value) is not int or not low <= value <= high:
        raise ProbeError(field)
    return value


def clean_probe(data):
    """The request checked against the node's limits, defaults filled in."""
    if not isinstance(data, dict):
        raise ProbeError('mode')
    mode = data.get('mode')
    if mode not in MODES:
        raise ProbeError('mode')
    target = data.get('target')
    if (not isinstance(target, str) or not 0 < len(target) <= 253 or any(c.isspace() for c in target)
            or (_ip(target) is None and not _HOSTNAME.fullmatch(target))):
        raise ProbeError('target')
    family = data.get('family', 0)
    if family is None:
        family = 0
    literal = _ip(target)
    if type(family) is not int or family not in (0, 4, 6) or (family and literal is not None and literal.version != family):
        raise ProbeError('family')
    out = {'mode': mode, 'target': target, 'family': family, 'count': _int(data, 'count', *COUNTS[mode]),
           'intervalMs': _int(data, 'intervalMs', *LIMITS['intervalMs']), 'timeoutMs': _int(data, 'timeoutMs', *LIMITS['timeoutMs'])}
    if mode == 'tcping':
        if data.get('port') is None:
            raise ProbeError('port')
        out['port'] = _int(data, 'port', 1, 65535, None)
    if mode == 'trace':
        out['maxHops'] = _int(data, 'maxHops', *LIMITS['maxHops'])
    return out


class NetTools:
    def __init__(self, service):
        self.service = service
        self.probes = {}  # (server_id, probe id) -> {owner, requestedMode, mesh, ended, touched}
        self.networks = geo.NetworkLookups(lambda ips: geo.fetch_networks(self.service.http, ips), service.spawn)

    async def _call(self, owner, server_id, method, params):
        try:
            return await self.service._node_call(owner, server_id, method, params, timeout=CALL_TIMEOUT)
        except InstanceError as exc:
            if exc.error == 'unsupported_method':
                raise InstanceError('node_upgrade_required', 409) from None
            raise

    async def _mesh(self, owner):
        """The applied mesh document, its active members by id, and the address ranges of
        mesh addresses (none when nobody is in the mesh)."""
        doc = await self.service.mesh._load(owner, 'applied')
        live = {m['serverId']: m for m in model.active(doc)}
        nets = [ipaddress.ip_network(doc['settings'][k]) for k in ('prefix', 'controlPrefix')] if live else []
        return doc, live, nets

    async def _public(self, server_id):
        session = self.service.hub.sessions.get(server_id)
        info = session.info if session else None
        if info is None:
            row = await one(self.service.db.conn, 'SELECT info_json FROM server_nodes WHERE server_id=?', (server_id,))
            info = (json.loads(row['info_json']).get('info') or {}) if row else {}
        return {'publicIPv4': str(info.get('ipv4') or ''), 'publicIPv6': str(info.get('ipv6') or ''), 'publicIPv4Proxy': str(info.get('ipv4Proxy') or '')}

    # ----- addresses -----
    async def addresses(self, owner, server_id):
        result = await self._call(owner, server_id, 'net.addrs', {})
        _, live, nets = await self._mesh(owner)
        public = await self._public(server_id)
        egress = {ip for ip in (_ip(public['publicIPv4']), _ip(public['publicIPv6'])) if ip}
        interfaces = []
        for iface in result.get('interfaces') if isinstance(result.get('interfaces'), list) else []:
            if not isinstance(iface, dict):
                continue
            addrs = [{**a, 'scope': scope(a['ip'], nets), 'egress': _ip(a['ip']) in egress}
                     for a in iface.get('addrs') or [] if isinstance(a, dict) and isinstance(a.get('ip'), str)]
            interfaces.append({**iface, 'addrs': addrs})
        member = live.get(server_id)
        return {'ok': True, 'interfaces': interfaces, **public, 'meshAddress': member['address'] if member else ''}

    # ----- tests -----
    def _target(self, doc, live, src, target, facts=None):
        """{kind, dst, subnet} when src is an active member and target an address in the
        mesh: another member's mesh or control address, or a gateway subnet src has a route to."""
        ip = _ip(target)
        if src not in live or ip is None:
            return None
        for m in live.values():
            if m['serverId'] != src and str(ip) in (m['address'], m['controlAddress']):
                return {'kind': 'member', 'dst': m['serverId'], 'subnet': None}
        for route in gateway_routes(doc, facts).get(src, []):
            net = ipaddress.ip_network(route)
            if ip.version != net.version or ip not in net:
                continue
            others = [m for m in live.values() if m['serverId'] != src and (m['gateway'] or {}).get('subnets')]
            gateway = next((m for m in others if any(ip in ipaddress.ip_network(s) for s in m['gateway']['subnets'])), None) \
                or next((m for m in others if any(net.overlaps(ipaddress.ip_network(s)) for s in m['gateway']['subnets'])), None)
            if gateway:
                return {'kind': 'gateway', 'dst': gateway['serverId'], 'subnet': route}
        return None

    def _prune(self):
        now = time.monotonic()
        for key, rec in list(self.probes.items()):
            if now - (rec['ended'] or rec['touched']) > KEEP_S:
                del self.probes[key]

    async def start(self, owner, server_id, data):
        params = clean_probe(data)
        doc, live, _ = await self._mesh(owner)
        target = self._target(doc, live, server_id, params['target'], await self.service.mesh._facts(owner) if server_id in live else None)
        requested = params['mode']
        if target and requested == 'trace':
            params = {k: v for k, v in params.items() if k != 'maxHops'}
            params['mode'] = 'ping'
            params['count'] = data.get('count') if data.get('count') is not None else MESH_TRACE_COUNT
        result = await self._call(owner, server_id, 'net.probe.start', params)
        pid = result.get('id')
        if not isinstance(pid, str) or not pid:
            raise InstanceError('node_error', 502)
        self._prune()
        self.probes[(server_id, pid)] = {'owner': owner, 'requestedMode': requested, 'mesh': target, 'ended': None, 'touched': time.monotonic()}
        return {'ok': True, 'id': pid}

    async def status(self, owner, server_id, pid):
        result = _bounded(await self._call(owner, server_id, 'net.probe.status', {'id': pid}))
        self._prune()
        rec = self.probes.get((server_id, pid))
        if rec and rec['owner'] != owner:
            rec = None
        if rec:
            rec['touched'] = time.monotonic()
            if result.get('state') != 'running' and rec['ended'] is None:
                rec['ended'] = rec['touched']
        _, live, nets = await self._mesh(owner)
        hops = result['hops'][:MAX_HOPS] if isinstance(result.get('hops'), list) else []
        for hop in hops:
            if isinstance(hop, dict):
                hop['addrs'] = [{**a, 'scope': scope(a['ip'], nets)} for a in (hop.get('addrs') if isinstance(hop.get('addrs'), list) else [])[:MAX_HOP_ADDRS]
                                if isinstance(a, dict) and isinstance(a.get('ip'), str)]
        # Past the server's lookup quota an address is shown as not looked up.
        skipped = self.networks.want([a['ip'] for h in hops if isinstance(h, dict) for a in h['addrs'] if a['scope'] == 'public'], server_id)
        for hop in hops:
            for a in hop['addrs'] if isinstance(hop, dict) else []:
                a['geo'] = self.networks.get(a['ip']) if a['scope'] == 'public' else None
                if a['ip'] in skipped:
                    a['geoSkipped'] = True
        path = await self._path(owner, live, server_id, rec['mesh']) if rec and rec['mesh'] else None
        return {'ok': True, **result, 'hops': hops, 'requestedMode': rec['requestedMode'] if rec else result.get('mode'), 'meshPath': path}

    async def _path(self, owner, live, src, target):
        """The path to the target's machine now, worked out as /mesh/path does. A member that
        has since left the mesh leaves an empty, stale path."""
        roles = {sid: m['role'] for sid, m in live.items()}
        if src not in roles or target['dst'] not in roles:
            return {**target, 'hops': [], 'rttMs': None, 'stale': True}
        mesh = self.service.mesh
        latest = await mesh._latest(owner, await mesh._network(owner))
        return {**target, **mesh_path.find(src, target['dst'], roles, mesh.status, latest, int(time.time() * 1000))}

    async def stop(self, owner, server_id, pid):
        await self._call(owner, server_id, 'net.probe.stop', {'id': pid})
        rec = self.probes.get((server_id, pid))
        if rec and rec['ended'] is None:
            rec['ended'] = time.monotonic()
        return {'ok': True}
