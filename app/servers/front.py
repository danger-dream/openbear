"""前置机 (fronts): servers that are to reach nothing beyond their exits, the provider's servers
they make one dedicated line with. Each exit's node serves its fronts' access to this controller
(node/front.go): TLS with a self-signed certificate the fronts pin, forwarding only the node API to
the exit's own address of the controller. The controller reaches a front's SSH through a TCP
tunnel the exit's node opens, never directly."""
from __future__ import annotations

import ipaddress
import json
import logging
import re

from app.instances.contracts import InstanceError
from app.instances.servers import LOCAL
from app.webhooks.repository import many, one

log = logging.getLogger(__name__)

FEATURE = 'front'
DEFAULT_PORT = 52002
MAX_EXITS = 2
_HOST = re.compile(r'(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?')
_PIN = re.compile(r'[0-9a-f]{64}')


def _host(value):
    if not isinstance(value, str):
        return ''
    value = value.strip().strip('[]')
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return value.lower() if _HOST.fullmatch(value) else ''


def clean(value):
    """A profile's front: {'exits': [{'serverId', 'address'}]} in order (primary first), or None
    when the server is not one. address is how the front reaches that exit, without a port."""
    if value in (None, {}) or isinstance(value, dict) and value.get('exits') == []:
        return None
    if not isinstance(value, dict) or set(value) - {'exits'} or not isinstance(value.get('exits'), list) \
            or not 1 <= len(value['exits']) <= MAX_EXITS:
        raise InstanceError('invalid_front')
    out, seen = [], set()
    for entry in value['exits']:
        if not isinstance(entry, dict) or set(entry) - {'serverId', 'address'} or not isinstance(entry.get('serverId'), str) \
                or not 1 <= len(entry['serverId']) <= 80:
            raise InstanceError('invalid_front')
        address = _host(entry.get('address'))
        if not address:
            raise InstanceError('invalid_front_address')
        if entry['serverId'] in seen:
            raise InstanceError('front_exit_duplicate')
        seen.add(entry['serverId'])
        out.append({'serverId': entry['serverId'], 'address': address})
    return {'exits': out}


def exits(profile):
    return list(((profile or {}).get('front') or {}).get('exits') or [])


def is_front(profile):
    return bool(exits(profile))


def exit_ids(profiles):
    """Every server that is some front's exit."""
    return {e['serverId'] for prof in profiles.values() for e in exits(prof)}


def access_url(address, port):
    return f'https://[{address}]:{port}' if ':' in address else f'https://{address}:{port}'


def clean_status(raw):
    """An exit's front access as its node reports it, or {}."""
    if not isinstance(raw, dict) or type(raw.get('port')) is not int or not 0 < raw['port'] <= 65535:
        return {}
    fingerprint = str(raw.get('fingerprint') or '').lower()
    return {'port': raw['port'], 'fingerprint': fingerprint if _PIN.fullmatch(fingerprint) else '',
            'listening': raw.get('listening') is True, 'error': str(raw.get('error') or '')[:200]}


def _local_ips(addresses_json):
    out = set()
    for item in json.loads(addresses_json or '[]'):
        host = str(item or '').partition('/')[0].strip()
        if host:
            out.add(host)
    return out


def _follow_public_ip(address, info, local_ips=()):
    """A front exit's global IP literal (picked from 公网出口) follows the exit's current public IP
    of the same family when it does not sit on one of the exit's own interfaces; private/dedicated-line
    IPs and hostnames stay as configured."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if not ip.is_global or str(ip) in local_ips:
        return address
    current = _host((info or {}).get('ipv6' if ip.version == 6 else 'ipv4'))
    return current or address


class Fronts:
    def __init__(self, service):
        self.service = service
        self.syncing = None
        self.again = set()

    @property
    def db(self):
        return self.service.db

    @property
    def hub(self):
        return self.service.hub

    async def port(self):
        return (await self.service.settings())['frontPort']

    def capable(self, server_id):
        """Whether the exit's node can serve fronts and tunnel to them now."""
        session = self.hub.sessions.get(server_id)
        return bool(session and FEATURE in session.features and session.caps.get('exec') and not session.incompatible)

    async def _status(self, server_id):
        row = await one(self.db.conn, 'SELECT info_json FROM server_nodes WHERE server_id=?', (server_id,))
        return clean_status(json.loads(row['info_json']).get('frontAccess')) if row else {}

    async def check(self, owner, server_id, front):
        """Exits are other servers of this owner, not fronts themselves, not this controller."""
        if not front:
            return
        profiles = await self.service._profiles(owner)
        known = {s['id'] for s in await self.service.registry.all(owner)}
        for entry in front['exits']:
            sid = entry['serverId']
            if sid == server_id or sid == LOCAL or sid not in known:
                raise InstanceError('invalid_front_exit')
            if is_front(profiles.get(sid)):
                raise InstanceError('front_exit_is_front')
        if server_id and server_id in exit_ids({k: v for k, v in profiles.items() if k != server_id}):
            raise InstanceError('front_is_exit')

    async def access(self, owner, server_id, profile):
        """What the front's node is to use: its exits' accesses with their certificates."""
        port = await self.port()
        points = []
        for entry in exits(profile):
            status = await self._status(entry['serverId'])
            if status.get('port') == port and status.get('fingerprint'):
                points.append((access_url(entry['address'], port), [status['fingerprint']]))
        if not points:
            raise InstanceError('front_exit_not_ready', 409)
        (url, pins), (backup, backup_pins) = points[0], points[1] if len(points) > 1 else ('', [])
        return {'url': url, 'backup': backup, 'pins': pins, 'backupPins': backup_pins, 'front': True}

    async def point(self, entry, port):
        """One exit as an access point: id front:<exit>, its access url, and whether it serves now."""
        status = await self._status(entry['serverId'])
        row = await one(self.db.conn, 'SELECT public_json FROM instance_servers WHERE id=?', (entry['serverId'],))
        name = (json.loads(row['public_json']).get('name') if row else '') or entry['serverId']
        return {'id': 'front:' + entry['serverId'], 'serverId': entry['serverId'], 'name': name, 'url': access_url(entry['address'], port),
                'ready': status.get('port') == port and bool(status.get('fingerprint')) and status.get('listening') is True,
                'error': status.get('error', '')}

    async def via(self, owner, server_id):
        """The exits a front's SSH goes through, primary first; [] for any other server."""
        row = await one(self.db.conn, 'SELECT profile_json FROM server_profiles WHERE server_id=? AND owner=?', (server_id, owner))
        return [e['serverId'] for e in exits(json.loads(row['profile_json']) if row else {})]

    def opener(self, via):
        """open(address, sink) -> a terminal that is a TCP connection to address from the first exit
        that can make one; None for a server with no exits."""
        if not via:
            return None

        async def open_tunnel(address, sink):
            error = InstanceError('front_exit_offline', 409)
            for sid in via:
                if self.capable(sid):
                    try:
                        return await self.hub.open_tunnel(sid, sink, address)
                    except (InstanceError, LookupError, OSError) as exc:
                        error = exc
            raise error
        return open_tunnel

    async def serve(self, server_id, port):
        """Have one exit's node serve fronts on port (0: stop); its status as recorded."""
        try:
            reply = await self.hub.call(server_id, 'front.serve', {'port': port}, 30)
        except LookupError:
            raise InstanceError('front_exit_offline', 409) from None
        except (ConnectionError, TimeoutError):
            raise InstanceError('front_serve_failed', 409) from None
        if not reply.get('ok'):
            raise InstanceError(str(reply.get('error') or 'front_serve_failed')[:80], 409)
        status = clean_status(reply.get('result')) if port else {}
        await self.service._merge_node_info(server_id, {'frontAccess': status})
        return status

    async def ensure(self, owner, front):
        """Every exit of a front about to be installed serves it now: at least the primary must."""
        port = await self.port()
        for index, entry in enumerate(front['exits']):
            sid = entry['serverId']
            status = await self._status(sid)
            if status.get('port') == port and status.get('listening') and status.get('fingerprint'):
                continue
            if not self.capable(sid):
                if index == 0:
                    raise InstanceError('front_exit_offline', 409)
                continue
            status = await self.serve(sid, port)
            if index == 0 and not status.get('listening'):
                raise InstanceError('front_serve_failed', 409)

    async def check_port_change(self):
        # The port is global, including fronts owned by another configured owner.
        for row in await many(self.db.conn, 'SELECT server_id,profile_json FROM server_profiles'):
            if is_front(json.loads(row['profile_json'])) and row['server_id'] not in self.hub.sessions:
                raise InstanceError('front_port_fronts_offline', 409)

    async def sync(self, owner):
        """Exits serve on the configured port, servers no longer exits stop; a front whose exit's
        certificate, port or public IP changed is moved to its new access."""
        profiles = await self.service._profiles(owner)
        wanted, port = exit_ids(profiles), await self.port()
        changed, infos = set(), {}
        for row in await many(self.db.conn, 'SELECT server_id,info_json FROM server_nodes WHERE owner=?', (owner,)):
            sid, payload = row['server_id'], json.loads(row['info_json'] or '{}')
            infos[sid] = payload.get('info') or {}
            status = clean_status(payload.get('frontAccess'))
            want = port if sid in wanted else 0
            if (status.get('port') or 0) == want and (not want or status.get('listening')) or not self.capable(sid):
                continue
            try:
                after = await self.serve(sid, want)
            except InstanceError:
                continue
            if after.get('fingerprint') != status.get('fingerprint') or after.get('port') != status.get('port'):
                changed.add(sid)
        local_ips = {r['server_id']: _local_ips(r['addresses_json'])
                     for r in await many(self.db.conn, 'SELECT server_id,addresses_json FROM mesh_nodes WHERE owner=?', (owner,))}
        reconcile_mesh = False
        for sid, prof in profiles.items():
            current_exits = exits(prof)
            if not current_exits:
                continue
            updated = [{'serverId': e['serverId'],
                        'address': _follow_public_ip(e['address'], infos.get(e['serverId']), local_ips.get(e['serverId'], ()))}
                       for e in current_exits]
            moved = updated != current_exits
            if moved:
                prof = {**prof, 'front': {'exits': updated}}
                await self.service._write_profile(owner, sid, prof)
                if getattr(self.service, 'latency', None):
                    self.service.latency.soon(sid)
                reconcile_mesh = True
            if moved or any(e['serverId'] in changed for e in updated):
                await self.service.mark_access_pending(sid, True)
                if sid in self.hub.sessions:
                    self.service._apply_access_soon(sid)
        if reconcile_mesh and getattr(self.service, 'mesh', None):
            self.service.mesh._reconcile_soon(owner)

    def soon(self, owner):
        """sync() the owner once more after any running one; calls in between are one for each owner."""
        self.again.add(owner)
        if self.syncing and not self.syncing.done():
            return

        async def run():
            while self.again:
                current = self.again.pop()
                try:
                    await self.sync(current)
                except Exception:
                    log.exception('front sync of owner %s failed', current)
        self.syncing = self.service.spawn(run())

    async def node_hello(self, server_id):
        row = await one(self.db.conn, 'SELECT owner FROM server_nodes WHERE server_id=?', (server_id,))
        if not row:
            return
        profiles = await self.service._profiles(row['owner'])
        if server_id in exit_ids(profiles) or await self._status(server_id):
            self.soon(row['owner'])
