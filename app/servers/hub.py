"""Reverse connections from openbear-node. A session starts with a handshake
that both long-term keys sign (the node's enrolled key and this controller's
identity) over fresh X25519 shares; every later frame is AES-256-GCM, so a TLS
proxy or a plain private link only ever carries ciphertext."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import json
import re
import secrets
import time
from collections import deque

from aiohttp import WSMsgType, web
from aiohttp.http import WebSocketReader
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.servers import compat
from app.webhooks.repository import one

CLOSE_UNAUTHORIZED = 4401
CLOSE_REVOKED = 4403
CLOSE_REPLACED = 4409
REPORT_INTERVAL = 3
# Nodes drop a link that stays silent for 75 seconds.
PING_INTERVAL = 25
KIND_JSON, KIND_TERMINAL = 0, 1
# The node stops sending a terminal's output after this many unacknowledged bytes.
OUTPUT_WINDOW = 256 * 1024
ACK_EVERY = 64 * 1024
MAX_MESSAGE = 16 * 1024 * 1024
# The node's first frame, its signed auth, is a few hundred bytes.
HANDSHAKE_MESSAGE = 1024
HANDSHAKE_TIMEOUT = 3
HANDSHAKE_SLOTS = 32
# Handshakes at once from where a node last connected (the address its last handshake came
# from): a pool of their own, which handshakes from anywhere else cannot fill.
HANDSHAKE_KNOWN_SLOTS = 32
# Handshakes at once from one address; failed ones an address may have within
# HANDSHAKE_WINDOW seconds before it is refused. A node id with that many is
# refused only to the addresses that failed for it, so a node that has not
# keeps connecting. Successful handshakes are never counted: a crowd of nodes
# reconnecting after a restart is not limited.
HANDSHAKE_PER_SOURCE = 8
HANDSHAKE_FAILURES = 20
HANDSHAKE_WINDOW = 60
RECENT_SAMPLES = 200
# Why a node and this controller cannot work together: their protocol ranges do not overlap.
NODE_OUTDATED, CONTROLLER_OUTDATED = 'node_protocol_outdated', 'controller_protocol_outdated'
UPDATE_METHOD = 'update'
# A connection that came through a reverse proxy: its remote address is the proxy's.
FORWARD_HEADERS = ('Forwarded', 'X-Forwarded-For', 'X-Real-IP', 'CF-Connecting-IP', 'True-Client-IP')
INFO_FIELDS = {'hostname': 120, 'platform': 16, 'os': 120, 'kernel': 120, 'arch': 20, 'cpu': 160, 'virtualization': 40, 'ipv4': 64, 'ipv6': 64, 'ipv4Proxy': 64}
# The addresses a node looked up for itself go into every mesh member's config and serve as the
# instance address: only IP literals of their family, an IPv4 one public. ipv4Proxy takes ipv4's
# place for a server outside China (service.node_hello).
ADDRESS_FIELDS = {'ipv4': 4, 'ipv6': 6, 'ipv4Proxy': 4}
# Reported numbers stay within what a double holds exactly (and SQLite's integers with room to add).
SAMPLE_CAP = 2 ** 53
# What one session may send. Past the frame and byte rates its frames are read more slowly (only
# its own session waits); a hello waits for its own budget; reports past theirs are dropped,
# the next one is newer anyway. A node reports every REPORT_INTERVAL seconds and says hello on
# connecting and when its addresses change.
INBOUND_BYTES_PER_S = 16 * 1024 * 1024
INBOUND_BYTES_BURST = 2 * MAX_MESSAGE
MESSAGES_PER_S, MESSAGES_BURST = 50, 200
HELLO_EVERY_S, HELLO_BURST = 10, 5
REPORT_EVERY_S, REPORT_BURST = 1, 5


def now_ms():
    return int(time.time() * 1000)


def b64(raw):
    return base64.b64encode(raw).decode()


def handshake_base(node_id, nonce, controller_eph, node_eph):
    return f'{node_id}\n{nonce}\n{controller_eph}\n{node_eph}'.encode()


def signed(role, base):
    return f'openbear-node-v1/{role}\n'.encode() + base


def rejection(base, code, reason):
    """What the identity key signs to refuse a node before there is a channel. base carries
    the node's fresh key share (or, before the upgrade, its nonce), so it cannot be replayed."""
    return signed('reject', base + f'\n{code}\n{reason}'.encode())


def params_base(base, params):
    """What a node that signs its handshake parameters (params, the JSON text it sent) signs,
    and this controller then too."""
    return base + b'\n' + params.encode()


def handshake_source(request):
    """The address handshake limits count against: behind a reverse proxy on this machine,
    the client address the proxy appended, not the proxy's own."""
    remote = request.remote or ''
    try:
        loopback = ipaddress.ip_address(remote).is_loopback
    except ValueError:
        loopback = False
    if loopback:
        forwarded = request.headers.get('X-Forwarded-For', '').rsplit(',', 1)[-1].strip() or request.headers.get('X-Real-IP', '').strip()
        if forwarded:
            return forwarded[:64]
    return remote


def session_keys(shared, base):
    """(node->controller, controller->node) AES-256 keys, as the node derives them."""
    salt = hashes.Hash(hashes.SHA256())
    salt.update(base)
    keys = HKDF(algorithm=hashes.SHA256(), length=64, salt=salt.finalize(), info=b'openbear-node-v1 keys').derive(shared)
    return keys[:32], keys[32:]


def nonce_for(counter):
    return bytes(4) + counter.to_bytes(8, 'big')


class Channel:
    """Sealed frames with per-direction counters; any gap, replay or edit fails."""

    def __init__(self, ws, tx_key, rx_key):
        self.ws, self.tx, self.rx = ws, AESGCM(tx_key), AESGCM(rx_key)
        self.sent = self.got = 0
        self.lock = asyncio.Lock()

    async def send(self, kind, payload):
        async with self.lock:
            sealed = self.tx.encrypt(nonce_for(self.sent), bytes([kind]) + payload, None)
            self.sent += 1
            await self.ws.send_bytes(sealed)

    async def send_json(self, value):
        await self.send(KIND_JSON, json.dumps(value, separators=(',', ':')).encode())

    def open(self, data):
        plain = self.rx.decrypt(nonce_for(self.got), data, None)
        if not plain:
            raise InvalidTag
        self.got += 1
        return plain[0], plain[1:]


def clean_ip(value, version):
    """value as an IP literal of the given version, '' when it is not one; IPv4 must be public."""
    if not isinstance(value, str) or not 0 < len(value) <= INFO_FIELDS['ipv4']:
        return ''
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return ''
    if ip.version != version or (not ip.is_global if version == 4 else bool(ip.scope_id)):
        return ''
    return str(ip)


def clean_info(raw):
    """An address that is not one comes out '' (as if the node had not looked it up yet), so the
    last good one is kept."""
    raw = raw if isinstance(raw, dict) else {}
    info = {k: str(raw.get(k) or '')[:limit] for k, limit in INFO_FIELDS.items()}
    info.update((k, clean_ip(raw.get(k), version)) for k, version in ADDRESS_FIELDS.items())
    for key in ('cores', 'memTotal', 'swapTotal', 'diskTotal'):
        value = raw.get(key)
        info[key] = int(value) if isinstance(value, (int, float)) and 0 <= value < 2**63 else 0
    return info


def clean_sample(raw):
    """Node output is untrusted: keep known numeric fields within sane bounds."""
    def num(v, cap=SAMPLE_CAP):
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < cap else 0
    def usage(v):
        v = v if isinstance(v, dict) else {}
        return {'total': int(num(v.get('total'))), 'used': int(num(v.get('used')))}
    raw = raw if isinstance(raw, dict) else {}
    net = raw.get('net') if isinstance(raw.get('net'), dict) else {}
    io = raw.get('io') if isinstance(raw.get('io'), dict) else {}
    load = raw.get('load') if isinstance(raw.get('load'), list) else []
    return {'cpu': round(float(num(raw.get('cpu'), 1000)), 2), 'steal': round(float(num(raw.get('steal'), 1000)), 2),
            'iowait': round(float(num(raw.get('iowait'), 1000)), 2), 'mem': usage(raw.get('mem')), 'swap': usage(raw.get('swap')),
            'disk': usage(raw.get('disk')), 'load': [round(float(num(v, 1e6)), 2) for v in load[:3]] or [0, 0, 0],
            'io': {k: int(num(io.get(k))) for k in ('readRate', 'writeRate')},
            'net': {k: int(num(net.get(k))) for k in ('rx', 'tx', 'rxRate', 'txRate')},
            'tcp': int(num(raw.get('tcp'), 1e9)), 'udp': int(num(raw.get('udp'), 1e9)), 'processes': int(num(raw.get('processes'), 1e9)),
            'uptime': int(num(raw.get('uptime'))), 'boot': int(num(raw.get('boot'))), 'nodeRss': int(num(raw.get('nodeRss'), 2**40))}


def _protocol_number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_000_000 else None


def protocol_range(data, current=(1, 1)):
    """(min, max) a node declares in its handshake or hello: protocol is its newest, protocolMin
    its oldest. A node from before protocolMin speaks only protocol; one from before protocol, 1.
    A message without protocol (an older node's hello) keeps current."""
    high = _protocol_number(data.get('protocol'))
    if high is None:
        return current if 'protocol' not in data else (1, 1)
    low = _protocol_number(data.get('protocolMin'))
    return (high if low is None or low > high else low), high


class Rate:
    """A token bucket. delay(n) takes n and says how long to wait for the budget to recover
    (0 within it); allow(n) takes n only when they are there."""

    def __init__(self, per_second, burst):
        self.rate, self.burst = per_second, burst
        self.tokens, self.at = float(burst), time.monotonic()

    def _fill(self):
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.at) * self.rate)
        self.at = now

    def delay(self, n=1):
        self._fill()
        self.tokens -= n
        return -self.tokens / self.rate if self.tokens < 0 else 0

    def allow(self, n=1):
        self._fill()
        if self.tokens < n:
            return False
        self.tokens -= n
        return True


class Session:
    def __init__(self, server_id, ws, channel, remote):
        self.server_id, self.ws, self.channel, self.remote = server_id, ws, channel, remote
        self.connected_ms = now_ms()
        self.version = ''
        self.build = ''
        # The protocol range the node declared; protocol is its newest, protocol_min its oldest.
        self.protocol = 1
        self.protocol_min = 1
        self.caps = {}
        # Methods newer than protocol 1 that this node build has.
        self.features = frozenset()
        self.info = {}
        self.sample = None
        self.sample_ms = 0
        self.recent = deque(maxlen=RECENT_SAMPLES)
        self.calls = {}
        self.terminals = {}
        self.next_sid = 1
        self.endpoint = ''
        self.on_backup = False
        self.forwarded = False
        self.inbound = Rate(INBOUND_BYTES_PER_S, INBOUND_BYTES_BURST)
        self.messages = Rate(MESSAGES_PER_S, MESSAGES_BURST)
        self.hellos = Rate(1 / HELLO_EVERY_S, HELLO_BURST)
        self.reports = Rate(1 / REPORT_EVERY_S, REPORT_BURST)

    def set_protocol(self, low, high):
        self.protocol_min, self.protocol = low, high

    @property
    def protocol_used(self):
        """The protocol this session speaks: the highest both sides have, or None."""
        return compat.common(compat.PROTOCOL, (self.protocol_min, self.protocol))

    @property
    def incompatible(self):
        """'' when the ranges overlap; else which side has to be updated."""
        if self.protocol_used is not None:
            return ''
        return NODE_OUTDATED if self.protocol < compat.PROTOCOL[0] else CONTROLLER_OUTDATED


class Terminal:
    """One shell on a node, relayed to one browser socket. Output goes through
    a pump so a slow browser only slows its own terminal, never the session."""

    def __init__(self, session, sid, browser):
        self.session, self.sid, self.browser = session, sid, browser
        self.opened = asyncio.get_running_loop().create_future()
        # Set once the browser was told the terminal is open, so no output reaches it before that.
        self.ready = asyncio.Event()
        self.reason = ''
        self.queue = asyncio.Queue()
        self.buffered = 0
        self.pump = None
        self.hung_up = False
        # When the shell last printed: a command running and printing is no idle terminal.
        self.output_at = time.monotonic()

    @property
    def closed(self):
        return bool(self.reason)

    def start(self, spawn):
        self.spawn = spawn
        self.pump = spawn(self._pump())

    def feed(self, data):
        """Called from the session loop; never waits."""
        if self.reason:
            return
        self.output_at = time.monotonic()
        self.buffered += len(data)
        if self.buffered > OUTPUT_WINDOW * 2:
            # The node ignored its window; drop this terminal, not the session.
            self.reason = 'protocol_error'
            self.queue.put_nowait(None)
            self.spawn(self.close())
            return
        self.queue.put_nowait(data)

    async def _pump(self):
        acked = 0
        await self.ready.wait()
        while True:
            data = await self.queue.get()
            if data is None:
                break
            self.buffered -= len(data)
            with contextlib.suppress(ConnectionError, RuntimeError):
                await self.browser.send_bytes(data)
            acked += len(data)
            if acked >= ACK_EVERY or self.queue.empty():
                with contextlib.suppress(ConnectionError, RuntimeError):
                    await self.session.channel.send_json({'t': 'term.ack', 'sid': self.sid, 'n': acked})
                acked = 0
        # Output that arrived before the node closed the shell is delivered first.
        with contextlib.suppress(ConnectionError, RuntimeError):
            await self.browser.send_json({'type': 'status', 'state': 'closed', 'reason': self.reason})
            await self.browser.close()

    async def input(self, data):
        await self.session.channel.send(KIND_TERMINAL, self.sid.to_bytes(4, 'big') + data)

    async def resize(self, cols, rows):
        await self.session.channel.send_json({'t': 'term.resize', 'sid': self.sid, 'cols': cols, 'rows': rows})

    def finish(self, reason):
        """The node side ended; the pump tells and closes the browser."""
        if self.reason:
            return
        self.reason = reason
        if not self.opened.done():
            self.opened.set_result(False)
        self.queue.put_nowait(None)
        self.ready.set()

    async def close(self, reason='closed'):
        """The browser side ended (or the relay gave up); stop the shell on the node."""
        self.session.terminals.pop(self.sid, None)
        if not self.reason:
            self.reason = reason
            self.queue.put_nowait(None)
        self.ready.set()
        if not self.hung_up:
            self.hung_up = True
            with contextlib.suppress(ConnectionError, RuntimeError):
                await self.session.channel.send_json({'t': 'term.close', 'sid': self.sid})


class Failures:
    """Failed handshakes per key within the last HANDSHAKE_WINDOW seconds."""

    def __init__(self):
        self.seen = {}

    def _fresh(self, key, cutoff):
        times = self.seen.get(key)
        while times and times[0] < cutoff:
            times.popleft()
        if not times:
            self.seen.pop(key, None)
        return times

    def count(self, key):
        return len(self._fresh(key, time.monotonic() - HANDSHAKE_WINDOW) or ())

    def add(self, key):
        now = time.monotonic()
        if len(self.seen) > 4096:
            for stale in list(self.seen):
                self._fresh(stale, now - HANDSHAKE_WINDOW)
        self.seen.setdefault(key, deque(maxlen=HANDSHAKE_FAILURES)).append(now)


class Attempt:
    """A handshake in progress; a newer one for the same node id from the same address preempts it."""

    def __init__(self, source):
        self.source = source
        self.ws = None
        self.preempted = False


class NodeHub:
    def __init__(self, service):
        self.service = service
        self.sessions = {}
        self.handshakes = self.known_handshakes = 0
        self.sources = {}
        self.pending = {}
        self.failed_sources = Failures()
        self.failed_nodes = Failures()
        self.failed_pairs = Failures()
        # The address each node last completed a handshake from: strangers cannot
        # use up its node id's failures there.
        self.known = {}

    @property
    def db(self):
        return self.service.db

    def live(self, server_id):
        s = self.sessions.get(server_id)
        if not s:
            return None
        return {'online': True, 'connectedAtMs': s.connected_ms, 'version': s.version, 'caps': s.caps, 'features': sorted(s.features),
                'protocol': {'min': s.protocol_min, 'max': s.protocol, 'used': s.protocol_used}, 'incompatible': s.incompatible,
                'at': s.sample_ms, 'm': s.sample}

    async def _handshake(self, ws, node_id, public_key):
        nonce = secrets.token_urlsafe(32)
        eph = X25519PrivateKey.generate()
        mine = b64(eph.public_key().public_bytes_raw())
        data, theirs = {}, ''
        try:
            await ws.send_json({'t': 'challenge', 'nonce': nonce, 'eph': mine})
            message = await ws.receive(timeout=HANDSHAKE_TIMEOUT)
            data = json.loads(message.data) if message.type == WSMsgType.TEXT else {}
            theirs = str(data.get('eph', ''))
            base = handshake_base(node_id, nonce, mine, theirs)
            key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key))
            welcome = {'t': 'welcome', 'sig': b64(self.service.identity.sign(signed('server', base)))}
            if 'params' in data:
                # A node that signs its handshake parameters: what it said is the signed text, not
                # the plain copies beside it, which are there for controllers from before.
                bound = params_base(base, str(data['params']))
                key.verify(base64.b64decode(str(data.get('paramsSig', ''))), signed('client-params', bound))
                hello = json.loads(data['params'])
                if not isinstance(hello, dict):
                    raise ValueError('params')
                hello['signed'] = True
                welcome['paramsSig'] = b64(self.service.identity.sign(signed('server-params', bound)))
            else:
                key.verify(base64.b64decode(str(data.get('sig', ''))), signed('client', base))
                hello = {**data, 'signed': False}
            shared = eph.exchange(X25519PublicKey.from_public_bytes(base64.b64decode(theirs)))
            to_controller, to_node = session_keys(shared, base)
            await ws.send_json(welcome)
        except InvalidSignature:
            reason = 'unauthorized'
            # A node that declares notices backs off long only when it can verify the refusal.
            if theirs and data.get('notices') is True:
                reason += ' ' + b64(self.service.identity.sign(rejection(base, CLOSE_UNAUTHORIZED, reason)))
            await ws.close(code=CLOSE_UNAUTHORIZED, message=reason.encode())
            return None, {}
        except (ValueError, TypeError, AttributeError, TimeoutError, json.JSONDecodeError):
            await ws.close(code=CLOSE_UNAUTHORIZED, message=b'unauthorized')
            return None, {}
        return Channel(ws, to_node, to_controller), hello

    def _unknown(self, node_id, nonce):
        body = {'ok': False, 'error': 'unknown_node'}
        # Signed for a node that sent a nonce, so it can tell this from a proxy's 401.
        if 0 < len(node_id) <= 128 and 16 <= len(nonce) <= 64:
            body['sig'] = b64(self.service.identity.sign(rejection(handshake_base(node_id, nonce, '', ''), CLOSE_UNAUTHORIZED, body['error'])))
        return web.json_response(body, status=401)

    @staticmethod
    def _terms(hello):
        """What the session config tells a node about the handshake, inside the channel: this
        controller's protocol range, and whether it went by the node's signed parameters (a
        node that signed them and reads false had them stripped on the way)."""
        return {'protocol': compat.PROTOCOL[1], 'protocolMin': compat.PROTOCOL[0], 'signedParams': hello.get('signed') is True}

    def _failed(self, node_id, source):
        self.failed_sources.add(source)
        self.failed_nodes.add(node_id)
        self.failed_pairs.add((node_id, source))

    def _preempt(self, attempt):
        attempt.preempted = True
        if attempt.ws is not None and attempt.ws.prepared:
            self.service.spawn(attempt.ws.close(code=CLOSE_REPLACED, message=b'replaced'))

    @staticmethod
    def _full_messages(request, ws):
        """After the handshake the node may send MAX_MESSAGE. aiohttp fixes a socket's limit when
        it starts, so its frame parser is replaced; the node sends nothing until the session config."""
        request.protocol._payload_parser = WebSocketReader(ws._reader, MAX_MESSAGE, compress=bool(ws.compress))

    async def handle(self, request):
        node_id = request.headers.get('X-OpenBear-Node', '')
        source = handshake_source(request)
        if self.failed_sources.count(source) >= HANDSHAKE_FAILURES:
            return web.json_response({'ok': False, 'error': 'too_many_attempts'}, status=429)
        # A node a server took over after it registered (onboarding.settle_join) keeps the id it got
        # then; that server's record names it.
        row = await one(self.db.conn, "SELECT * FROM server_nodes WHERE server_id=? OR json_extract(info_json,'$.nodeId')=? ORDER BY server_id=? DESC LIMIT 1",
                        (node_id, node_id, node_id)) if 0 < len(node_id) <= 128 else None
        if not row:
            return self._unknown(node_id, request.headers.get('X-OpenBear-Nonce', ''))
        known = self.known.get(node_id)
        if (source != known and self.failed_nodes.count(node_id) >= HANDSHAKE_FAILURES
                and self.failed_pairs.count((node_id, source))):
            return web.json_response({'ok': False, 'error': 'too_many_attempts'}, status=429)
        reserved = source == known
        if ((self.known_handshakes >= HANDSHAKE_KNOWN_SLOTS if reserved else self.handshakes >= HANDSHAKE_SLOTS)
                or self.sources.get(source, 0) >= HANDSHAKE_PER_SOURCE):
            return web.json_response({'ok': False, 'error': 'busy'}, status=503)
        # One handshake per node id and address: the newer one, as a node that lost its link
        # reconnects. From another address (its backup, a probe) it runs alongside, and a
        # stranger elsewhere can neither take its turn nor keep it from its own.
        attempts = self.pending.setdefault(node_id, {})
        previous = attempts.get(source)
        if previous:
            self._preempt(previous)
            # Not a failure where the node last connected from: that is the node itself.
            if source != known:
                self._failed(node_id, source)
        attempt = attempts[source] = Attempt(source)
        if reserved:
            self.known_handshakes += 1
        else:
            self.handshakes += 1
        self.sources[source] = self.sources.get(source, 0) + 1
        channel = None
        try:
            ws = attempt.ws = web.WebSocketResponse(heartbeat=25, max_msg_size=HANDSHAKE_MESSAGE, autoping=True)
            await ws.prepare(request)
            if not attempt.preempted:
                channel, hello = await self._handshake(ws, node_id, row['public_key'])
        finally:
            if reserved:
                self.known_handshakes -= 1
            else:
                self.handshakes -= 1
            self.sources[source] -= 1
            if not self.sources[source]:
                del self.sources[source]
            if attempts.get(source) is attempt:
                del attempts[source]
            if not attempts and self.pending.get(node_id) is attempts:
                del self.pending[node_id]
        if channel is None or attempt.preempted:
            if not attempt.preempted:
                self._failed(node_id, source)
            with contextlib.suppress(ConnectionError, RuntimeError):
                await ws.close(code=CLOSE_REPLACED, message=b'replaced')
            return ws
        self.known[node_id] = source
        # From here on the server it connects as, its own id unless a server took it over.
        node_id = row['server_id']
        self._full_messages(request, ws)
        # Revocation or re-enrollment may have happened while the node signed.
        current = await one(self.db.conn, 'SELECT public_key FROM server_nodes WHERE server_id=?', (node_id,))
        if not current or current['public_key'] != row['public_key']:
            # A node from before notices would take a first message other than the config for a broken session.
            if hello.get('notices') is True:
                with contextlib.suppress(ConnectionError, RuntimeError):
                    await channel.send_json({'t': 'close', 'code': CLOSE_REVOKED, 'reason': 'revoked'})
            await ws.close(code=CLOSE_REVOKED, message=b'revoked')
            return ws
        if hello.get('probe') is True:
            # A node proving that a new address reaches this controller.
            with contextlib.suppress(ConnectionError, RuntimeError):
                await channel.send_json({'t': 'config', 'interval': REPORT_INTERVAL, 'probe': True, **self._terms(hello)})
            await ws.close()
            return ws
        session = Session(node_id, ws, channel, request.remote or '')
        session.forwarded = any(h in request.headers for h in FORWARD_HEADERS)
        session.version = str(hello.get('version', ''))[:40]
        session.set_protocol(*protocol_range(hello))
        previous = self.sessions.get(node_id)
        self.sessions[node_id] = session
        if previous:
            await previous.ws.close(code=CLOSE_REPLACED, message=b'replaced')
        await channel.send_json({'t': 'config', 'interval': REPORT_INTERVAL, **self._terms(hello)})
        await self.service.node_seen(node_id, connected=True)
        keepalive = asyncio.create_task(self._keepalive(ws))
        try:
            async for message in ws:
                if message.type != WSMsgType.BINARY:
                    continue
                wait = session.inbound.delay(len(message.data))
                if wait:
                    await asyncio.sleep(wait)
                try:
                    kind, payload = channel.open(message.data)
                except InvalidTag:
                    await ws.close(code=1008, message=b'bad frame')
                    break
                if kind == KIND_TERMINAL:
                    terminal = session.terminals.get(int.from_bytes(payload[:4], 'big')) if len(payload) > 4 else None
                    if terminal:
                        terminal.feed(payload[4:])
                    continue
                wait = session.messages.delay()
                if wait:
                    await asyncio.sleep(wait)
                try:
                    data = json.loads(payload)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(data, dict):
                    continue
                if data.get('t') == 'report' and not session.reports.allow():
                    continue
                if data.get('t') == 'hello' and (wait := session.hellos.delay()):
                    await asyncio.sleep(wait)
                await self._message(session, data)
        finally:
            keepalive.cancel()
            for future in session.calls.values():
                if not future.done():
                    future.set_exception(ConnectionError('node_disconnected'))
            current = self.sessions.get(node_id) is session
            if current:
                del self.sessions[node_id]

            # Runs to completion even though aiohttp cancels this handler
            # when the node's connection drops.
            async def end():
                for terminal in list(session.terminals.values()):
                    terminal.finish('node_disconnected')
                session.terminals.clear()
                if current:
                    with contextlib.suppress(Exception):
                        await self.service.node_seen(node_id, connected=False)
            await asyncio.shield(self.service.spawn(end()))
        return ws

    @staticmethod
    async def _keepalive(ws):
        # aiohttp's heartbeat pings only a quiet peer, and a reporting node is
        # never quiet, so without this the node would hear nothing at all.
        with contextlib.suppress(ConnectionError, RuntimeError):
            while not ws.closed:
                await asyncio.sleep(PING_INTERVAL)
                await ws.ping()

    async def _message(self, session, data):
        kind = data.get('t')
        if kind == 'report':
            session.sample, session.sample_ms = clean_sample(data.get('m')), now_ms()
            session.recent.append((session.sample_ms // 1000, session.sample))
            self.service.metrics.observe(session.server_id, session.sample)
            # A node this controller cannot work with may report the mesh in a form it does not know.
            if 'mesh' in data and not session.incompatible:
                await self.service.mesh.report(session.server_id, data['mesh'])
        elif kind == 'hello':
            caps = data.get('caps') if isinstance(data.get('caps'), dict) else {}
            session.caps = {k: caps.get(k) is True for k in ('exec', 'terminal', 'browser')}
            features = data.get('features') if isinstance(data.get('features'), list) else []
            session.features = frozenset(str(f)[:40] for f in features[:50])
            session.version = str(data.get('version', session.version))[:40]
            session.set_protocol(*protocol_range(data, (session.protocol_min, session.protocol)))
            build = str(data.get('build') or '').lower()
            session.build = build if re.fullmatch(r'[0-9a-f]{64}', build) else ''
            session.info = clean_info(data.get('info'))
            session.endpoint = str(data.get('endpoint') or '')[:200]
            session.on_backup = data.get('backup') is True
            await self.service.node_hello(session.server_id, session.info, session.caps, session.version)
        elif kind == 'ddns':
            if not session.incompatible:
                await self.service.ddns.observe(session.server_id, data.get('obs'))
        elif kind == 'reply':
            future = session.calls.pop(str(data.get('id', '')), None)
            if future and not future.done():
                future.set_result(data)
        elif kind in {'term.opened', 'term.closed'}:
            terminal = session.terminals.get(data.get('sid')) if isinstance(data.get('sid'), int) else None
            if terminal and kind == 'term.opened':
                if not terminal.opened.done():
                    terminal.opened.set_result(True)
            elif terminal:
                session.terminals.pop(terminal.sid, None)
                terminal.finish(str(data.get('reason') or 'exited')[:40])

    async def call(self, server_id, method, params, timeout):
        """One request/response. A lost connection means the effect is unknown."""
        session = self.sessions.get(server_id)
        if not session:
            raise LookupError('node_offline')
        # Refused as the node would refuse a call it does not know, without sending it; but a
        # node that is behind can still be updated, the way out (its call stays the same in every protocol).
        if session.incompatible and not (method == UPDATE_METHOD and session.incompatible == NODE_OUTDATED):
            return {'ok': False, 'error': session.incompatible}
        call_id = secrets.token_hex(8)
        future = asyncio.get_running_loop().create_future()
        session.calls[call_id] = future
        try:
            await session.channel.send_json({'t': 'call', 'id': call_id, 'method': method, 'params': params})
            return await asyncio.wait_for(future, timeout)
        finally:
            session.calls.pop(call_id, None)

    async def open_terminal(self, server_id, browser, cols, rows):
        session = self.sessions.get(server_id)
        if not session:
            raise LookupError('node_offline')
        if session.incompatible:
            raise PermissionError(session.incompatible)
        if not session.caps.get('terminal'):
            raise PermissionError('capability_disabled')
        terminal = Terminal(session, session.next_sid, browser)
        session.next_sid += 1
        session.terminals[terminal.sid] = terminal
        terminal.start(self.service.spawn)
        await session.channel.send_json({'t': 'term.open', 'sid': terminal.sid, 'cols': cols, 'rows': rows})
        try:
            opened = await asyncio.wait_for(asyncio.shield(terminal.opened), 15)
        except TimeoutError:
            await terminal.close('open_timeout')
            raise LookupError('open_timeout') from None
        if not opened:
            raise PermissionError(terminal.reason or 'open_failed')
        return terminal

    async def disconnect(self, server_id, code=CLOSE_REVOKED):
        session = self.sessions.pop(server_id, None)
        if session:
            # The close frame is plaintext: a node backs off for a revocation only on the notice
            # inside the channel. Older nodes ignore the notice and still read the close code.
            if code == CLOSE_REVOKED and session.channel:
                with contextlib.suppress(ConnectionError, RuntimeError):
                    await session.channel.send_json({'t': 'close', 'code': code, 'reason': 'revoked'})
            await session.ws.close(code=code, message=b'revoked')

    async def close(self):
        for server_id in list(self.sessions):
            session = self.sessions.pop(server_id)
            with contextlib.suppress(Exception):
                await session.ws.close(code=1001, message=b'controller stopping')
