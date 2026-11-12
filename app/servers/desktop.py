"""Browser-based remote desktop (macOS VNC via noVNC, Windows RDP via IronRDP
over RDCleanPath) relayed through the node's local TCP tunnel."""
from __future__ import annotations

import asyncio
import contextlib
import json
import ssl
import time

from aiohttp import WSMsgType, web

from app.instances.contracts import InstanceError
from app.servers.hub import TUNNEL_FEATURE
from app.webhooks.repository import one

VERSION_1 = 3390
TAG_SEQUENCE = 0x30
TAG_INTEGER = 0x02
TAG_OCTET_STRING = 0x04
TAG_UTF8STRING = 0x0C
HANDSHAKE_TIMEOUT_S = 20
DESKTOP_IDLE_S = 7200
DESKTOP_POLL_S = 60
DEFAULT_PORTS = {'darwin': 5900, 'windows': 3389}
PROTOCOLS = {'darwin': 'vnc', 'windows': 'rdp'}
RDP_PORT_QUERY = "(Get-ItemProperty -Path 'HKLM:\\System\\CurrentControlSet\\Control\\Terminal Server\\WinStations\\RDP-Tcp' -Name PortNumber -ErrorAction SilentlyContinue).PortNumber"


def now_ms():
    return int(time.time() * 1000)


# ----- ASN.1 DER helpers for RDCleanPath -----
def _der_len(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    out = bytearray()
    temp = length
    while temp > 0:
        out.insert(0, temp & 0xFF)
        temp >>= 8
    return bytes([0x80 | len(out), *out])


def _der_wrap(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + _der_len(len(content)) + content


def _der_int(value: int) -> bytes:
    if value == 0:
        return _der_wrap(TAG_INTEGER, b'\x00')
    out = bytearray()
    temp = value
    while temp > 0:
        out.insert(0, temp & 0xFF)
        temp >>= 8
    if out[0] & 0x80:
        out.insert(0, 0)
    return _der_wrap(TAG_INTEGER, bytes(out))


def _der_octets(data: bytes) -> bytes:
    return _der_wrap(TAG_OCTET_STRING, data)


def _der_utf8(text: str) -> bytes:
    return _der_wrap(TAG_UTF8STRING, text.encode('utf-8'))


def _der_ctx(num: int, content: bytes) -> bytes:
    return _der_wrap(0xA0 + num, content)


def _der_read_len(buf: bytes, offset: int) -> tuple[int, int]:
    if offset >= len(buf):
        raise ValueError('truncated DER length')
    first = buf[offset]
    if first < 0x80:
        return first, 1
    count = first & 0x7F
    if count == 0 or offset + 1 + count > len(buf):
        raise ValueError('invalid DER length')
    length = 0
    for i in range(count):
        length = (length << 8) | buf[offset + 1 + i]
    return length, 1 + count


def _der_read_tlv(buf: bytes, offset: int = 0) -> tuple[int, bytes, int]:
    if offset >= len(buf):
        raise ValueError('truncated DER tag')
    tag = buf[offset]
    length, len_bytes = _der_read_len(buf, offset + 1)
    start = offset + 1 + len_bytes
    end = start + length
    if end > len(buf):
        raise ValueError('truncated DER value')
    return tag, buf[start:end], 1 + len_bytes + length


def _der_read_children(buf: bytes) -> list[tuple[int, bytes]]:
    items = []
    offset = 0
    while offset < len(buf):
        tag, val, total = _der_read_tlv(buf, offset)
        items.append((tag, val))
        offset += total
    return items


def _der_decode_int(buf: bytes) -> int:
    val = 0
    for b in buf:
        val = (val << 8) | b
    return val


def parse_rdcleanpath_request(data: bytes) -> dict:
    tag, body, _ = _der_read_tlv(data, 0)
    if tag != TAG_SEQUENCE:
        raise ValueError('invalid RDCleanPath sequence')
    version = None
    destination = ''
    proxy_auth = ''
    x224_req = b''
    for child_tag, child_val in _der_read_children(body):
        if (child_tag & 0xE0) != 0xA0:
            continue
        ctx = child_tag & 0x1F
        inner_tag, inner_val, _ = _der_read_tlv(child_val, 0)
        if ctx == 0 and inner_tag == TAG_INTEGER:
            version = _der_decode_int(inner_val)
        elif ctx == 2 and inner_tag == TAG_UTF8STRING:
            destination = inner_val.decode('utf-8', errors='replace')
        elif ctx == 3 and inner_tag == TAG_UTF8STRING:
            proxy_auth = inner_val.decode('utf-8', errors='replace')
        elif ctx == 6 and inner_tag == TAG_OCTET_STRING:
            x224_req = inner_val
    if version != VERSION_1:
        raise ValueError(f'unsupported RDCleanPath version: {version}')
    if not x224_req:
        raise ValueError('missing X.224 request in RDCleanPath')
    return {'destination': destination, 'proxyAuth': proxy_auth, 'x224Request': x224_req}


def build_rdcleanpath_response(server_addr: str, x224_response: bytes, cert_chain: list[bytes]) -> bytes:
    certs_seq = _der_wrap(TAG_SEQUENCE, b''.join(_der_octets(c) for c in cert_chain))
    parts = b''.join([
        _der_ctx(0, _der_int(VERSION_1)),
        _der_ctx(6, _der_octets(x224_response)),
        _der_ctx(7, certs_seq),
        _der_ctx(9, _der_utf8(server_addr)),
    ])
    return _der_wrap(TAG_SEQUENCE, parts)


def build_rdcleanpath_error(error_code: int = 1, http_status: int | None = None, wsa_error: int | None = None, tls_alert: int | None = None) -> bytes:
    err_parts = [_der_ctx(0, _der_int(error_code))]
    if http_status is not None:
        err_parts.append(_der_ctx(1, _der_int(http_status)))
    if wsa_error is not None:
        err_parts.append(_der_ctx(2, _der_int(wsa_error)))
    if tls_alert is not None:
        err_parts.append(_der_ctx(3, _der_int(tls_alert)))
    err_seq = _der_wrap(TAG_SEQUENCE, b''.join(err_parts))
    return _der_wrap(TAG_SEQUENCE, _der_ctx(0, _der_int(VERSION_1)) + _der_ctx(1, err_seq))


class _StreamSink:
    """Collects bytes from a node tunnel into an asyncio queue for protocol handshakes and relay."""

    def __init__(self):
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.reason = ''
        self.closed = False
        self._buf = bytearray()

    async def send_bytes(self, data: bytes):
        if not self.closed and data:
            self.queue.put_nowait(bytes(data))

    async def send_json(self, value: dict):
        if isinstance(value, dict) and value.get('state') == 'closed':
            self.reason = str(value.get('reason') or 'closed')

    async def close(self):
        if not self.closed:
            self.closed = True
            self.queue.put_nowait(None)

    async def read_chunk(self, timeout: float | None = None) -> bytes:
        if self._buf:
            out = bytes(self._buf)
            self._buf.clear()
            return out
        chunk = await (asyncio.wait_for(self.queue.get(), timeout) if timeout else self.queue.get())
        if chunk is None:
            raise ConnectionError(self.reason or 'tunnel_closed')
        if self.queue.empty():
            return chunk
        # Coalesce multiple queued chunks that arrived together (e.g. a large RDP/VNC frame)
        # to minimize lock acquisitions, TLS read calls, and WebSocket frames.
        buf = bytearray(chunk)
        while not self.queue.empty() and len(buf) < 262144:
            nxt = self.queue.get_nowait()
            if nxt is None:
                self.queue.put_nowait(None)
                break
            buf.extend(nxt)
        return bytes(buf)

    async def read_exact(self, n: int, timeout: float = HANDSHAKE_TIMEOUT_S) -> bytes:
        deadline = time.monotonic() + timeout
        while len(self._buf) < n:
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise TimeoutError('tunnel_read_timeout')
            chunk = await asyncio.wait_for(self.queue.get(), rem)
            if chunk is None:
                raise ConnectionError(self.reason or 'tunnel_closed')
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    async def read_tpkt(self, timeout: float = HANDSHAKE_TIMEOUT_S) -> bytes:
        header = await self.read_exact(4, timeout)
        if header[0] == 0x03 and header[1] == 0x00:
            total = (header[2] << 8) | header[3]
            if total < 4 or total > 65535:
                raise ValueError(f'invalid TPKT length: {total}')
            rest = await self.read_exact(total - 4, timeout) if total > 4 else b''
            return header + rest
        # Fallback if a server sent a non-TPKT response.
        return header


def _make_rdp_ssl_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with contextlib.suppress(ssl.SSLError):
        ctx.set_ciphers('DEFAULT:@SECLEVEL=0')
    return ctx


async def perform_rdp_tls_handshake(terminal, sink: _StreamSink, x224_request: bytes, host: str = 'localhost') -> tuple[bytes, list[bytes], ssl.SSLObject, ssl.MemoryBIO, ssl.MemoryBIO]:
    await terminal.input(x224_request)
    x224_response = await sink.read_tpkt(HANDSHAKE_TIMEOUT_S)
    # Check X.224 RDP_NEG_FAILURE (0x03 at byte offset 11 when length >= 19)
    if len(x224_response) >= 19 and x224_response[5] == 0xD0 and x224_response[11] == 0x03:
        fail_code = int.from_bytes(x224_response[15:19], 'little')
        raise PermissionError(f'rdp_neg_failure:{fail_code}')

    ctx = _make_rdp_ssl_context()
    incoming = ssl.MemoryBIO()
    outgoing = ssl.MemoryBIO()
    ssl_obj = ctx.wrap_bio(incoming, outgoing, server_side=False, server_hostname=host)
    if sink._buf:
        incoming.write(bytes(sink._buf))
        sink._buf.clear()

    deadline = time.monotonic() + HANDSHAKE_TIMEOUT_S
    while True:
        try:
            ssl_obj.do_handshake()
            if outgoing.pending:
                await terminal.input(outgoing.read())
            break
        except ssl.SSLWantReadError:
            if outgoing.pending:
                await terminal.input(outgoing.read())
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise TimeoutError('tls_handshake_timeout') from None
            chunk = await sink.read_chunk(rem)
            incoming.write(chunk)

    certs: list[bytes] = []
    if hasattr(ssl_obj, 'get_unverified_chain'):
        with contextlib.suppress(Exception):
            chain = ssl_obj.get_unverified_chain() or []
            certs = [c.public_bytes(ssl.ENCODING_DER) for c in chain if hasattr(c, 'public_bytes')]
    if not certs:
        leaf = ssl_obj.getpeercert(binary_form=True)
        if leaf:
            certs = [leaf]
    if not certs:
        raise ssl.SSLError('no peer certificate from RDP server')
    return x224_response, certs, ssl_obj, incoming, outgoing


class DesktopManager:
    """Manages saved desktop credentials/ports and relays VNC / RDP WebSocket sessions."""

    def __init__(self, service):
        self.service = service

    @property
    def db(self):
        return self.service.db

    @property
    def vault(self):
        return self.service.registry.service.vault

    async def _platform_of(self, owner: int, server_id: str) -> str:
        session = self.service.hub.sessions.get(server_id)
        if session and session.info.get('platform'):
            return session.info['platform']
        node = await one(self.db.conn, 'SELECT info_json FROM server_nodes WHERE server_id=? AND owner=?', (server_id, owner))
        if node:
            plat = ((json.loads(node['info_json']).get('info') or {}).get('platform') or '').lower()
            if plat:
                return plat
        row = await one(self.db.conn, 'SELECT public_json FROM instance_servers WHERE id=? AND owner=?', (server_id, owner))
        if row:
            return (json.loads(row['public_json']).get('platform') or '').lower()
        return ''

    async def _read_saved(self, owner: int, server_id: str) -> dict:
        row = await one(self.db.conn, 'SELECT sealed FROM server_desktop_configs WHERE server_id=? AND owner=?', (server_id, owner))
        sealed = (row['sealed'] if row else '') or ''
        if not sealed:
            return {}
        with contextlib.suppress(Exception):
            data = self.vault.open(sealed)
            return data if isinstance(data, dict) else {}
        return {}

    async def _write_saved(self, owner: int, server_id: str, value: dict):
        now = now_ms()
        async with self.db.write_transaction(label='server-desktop-config') as conn:
            if not value:
                await conn.execute('DELETE FROM server_desktop_configs WHERE server_id=? AND owner=?', (server_id, owner))
            else:
                sealed = self.vault.seal(value)
                await conn.execute('INSERT OR REPLACE INTO server_desktop_configs VALUES(?,?,?,?)',
                                   (server_id, owner, sealed, now))

    async def _ssh_fallback(self, owner: int, server_id: str) -> dict:
        row = await one(self.db.conn, 'SELECT public_json,protected FROM instance_servers WHERE id=? AND owner=?', (server_id, owner))
        if not row:
            return {}
        pub = json.loads(row['public_json'])
        prot = self.vault.open(row['protected']) if row['protected'] else {}
        username = pub.get('username') or ''
        password = prot.get('password') or '' if pub.get('auth') == 'password' else ''
        return {'username': username, 'password': password}

    async def get_config(self, owner: int, server_id: str) -> dict:
        await self.service.registry.get(owner, server_id)
        platform = await self._platform_of(owner, server_id)
        protocol = PROTOCOLS.get(platform, '')
        default_port = DEFAULT_PORTS.get(platform, 0)
        saved = await self._read_saved(owner, server_id)
        # The SSH password is never shown otherwise: only a desktop that logs in with it gets it.
        ssh = await self._ssh_fallback(owner, server_id) if protocol else {}

        custom_port = saved.get('port') if isinstance(saved.get('port'), int) and 1 <= saved['port'] <= 65535 else 0
        auto_port = saved.get('autoPort') if isinstance(saved.get('autoPort'), int) and 1 <= saved['autoPort'] <= 65535 else 0
        port = custom_port or auto_port or default_port

        has_saved_creds = bool(saved.get('username') or saved.get('password'))
        username = saved.get('username') if has_saved_creds else (ssh.get('username') or '')
        password = saved.get('password') if has_saved_creds else (ssh.get('password') or '')
        domain = saved.get('domain') or ''
        source = 'desktop' if has_saved_creds else ('ssh' if (username or password) else 'none')

        return {
            'ok': True,
            'platform': platform,
            'protocol': protocol,
            'port': port,
            'customPort': custom_port,
            'defaultPort': default_port,
            'username': username,
            'password': password,
            'domain': domain,
            'hasSaved': bool(has_saved_creds or custom_port),
            'source': source,
        }

    async def save_config(self, owner: int, server_id: str, data: dict) -> dict:
        if not isinstance(data, dict):
            raise InstanceError('invalid_desktop_config')
        await self.service.registry.get(owner, server_id)
        if data.get('clear') is True:
            await self._write_saved(owner, server_id, {})
            return await self.get_config(owner, server_id)

        prior = await self._read_saved(owner, server_id)
        port_raw = data.get('port')
        if port_raw in (None, '', 0):
            port = 0
        elif isinstance(port_raw, int) and not isinstance(port_raw, bool) and 1 <= port_raw <= 65535:
            port = port_raw
        else:
            raise InstanceError('invalid_port')

        username = str(data.get('username') or '').strip()
        domain = str(data.get('domain') or '').strip()
        if len(username) > 128 or len(domain) > 128:
            raise InstanceError('invalid_desktop_config')

        if isinstance(data.get('password'), str) and data['password']:
            password = data['password']
        elif data.get('clearPassword') is True:
            password = ''
        else:
            password = prior.get('password', '')
        if len(password) > 8192:
            raise InstanceError('invalid_desktop_config')

        saved = {'port': port, 'username': username, 'password': password, 'domain': domain}
        if prior.get('autoPort') and not port:
            saved['autoPort'] = prior['autoPort']
        await self._write_saved(owner, server_id, saved)
        return await self.get_config(owner, server_id)

    async def _detect_rdp_port(self, owner: int, server_id: str) -> int:
        with contextlib.suppress(Exception):
            reply = await self.service.hub.call(server_id, 'exec', {'command': RDP_PORT_QUERY, 'timeout': 8}, 10)
            if reply.get('ok'):
                res = reply.get('result') or {}
                text = str(res.get('stdout') or '').strip().splitlines()
                if text:
                    port = int(text[-1].strip())
                    if 1 <= port <= 65535:
                        saved = await self._read_saved(owner, server_id)
                        if saved.get('autoPort') != port:
                            saved['autoPort'] = port
                            await self._write_saved(owner, server_id, saved)
                        return port
        return 0

    async def _resolve_port(self, owner: int, server_id: str, request, platform: str) -> tuple[int, bool]:
        query_port = request.query.get('port', '').strip()
        if query_port:
            try:
                p = int(query_port)
                if 1 <= p <= 65535:
                    return p, True
            except ValueError:
                pass
        saved = await self._read_saved(owner, server_id)
        custom = saved.get('port')
        if isinstance(custom, int) and 1 <= custom <= 65535:
            return custom, True
        auto = saved.get('autoPort')
        if isinstance(auto, int) and 1 <= auto <= 65535:
            return auto, False
        return DEFAULT_PORTS.get(platform, 5900), False

    async def _open_local_tunnel(self, owner: int, server_id: str, sink: _StreamSink, port: int, custom: bool, platform: str):
        try:
            return await self.service.hub.open_tunnel(server_id, sink, f'127.0.0.1:{port}')
        except PermissionError as exc:
            if str(exc) == 'connect_failed' and platform == 'windows' and not custom:
                detected = await self._detect_rdp_port(owner, server_id)
                if detected and detected != port:
                    sink.closed = False
                    sink.reason = ''
                    return await self.service.hub.open_tunnel(server_id, sink, f'127.0.0.1:{detected}')
            raise

    def _check_node(self, server_id: str, platform: str) -> str:
        session = self.service.hub.sessions.get(server_id)
        if not session:
            return 'node_offline'
        if session.incompatible:
            return session.incompatible
        if (session.info.get('platform') or '').lower() != platform:
            return 'unsupported_platform'
        if TUNNEL_FEATURE not in session.features:
            return 'node_upgrade_required'
        if not session.caps.get('exec'):
            return 'capability_disabled'
        return ''

    async def vnc_ws(self, request: web.Request, owner: int, server_id: str) -> web.WebSocketResponse:
        """Relay a noVNC WebSocket directly to the node's local VNC port (127.0.0.1:5900)."""
        protocols = ('binary',) if 'binary' in request.headers.get('Sec-WebSocket-Protocol', '') else ()
        ws = web.WebSocketResponse(heartbeat=25, max_msg_size=4 * 1024 * 1024, protocols=protocols)
        await ws.prepare(request)

        err = self._check_node(server_id, 'darwin')
        if err:
            await ws.close(code=4400, message=err.encode())
            return ws

        port, custom = await self._resolve_port(owner, server_id, request, 'darwin')
        sink = _StreamSink()
        try:
            terminal = await self._open_local_tunnel(owner, server_id, sink, port, custom, 'darwin')
        except (LookupError, PermissionError) as exc:
            await ws.close(code=4400, message=str(exc).encode()[:120])
            return ws

        log_id = None
        reason = ''
        pump_task = None
        try:
            async with self.db.write_transaction(label='server-desktop-vnc-open') as conn:
                cur = await conn.execute(
                    'INSERT INTO server_terminal_log(owner,server_id,started_ms,ip) VALUES(?,?,?,?)',
                    (owner, server_id, now_ms(), (request.remote or '')[:64]),
                )
                log_id = cur.lastrowid

            async def tunnel_to_ws():
                nonlocal reason
                try:
                    while True:
                        chunk = await sink.read_chunk()
                        await ws.send_bytes(chunk)
                except ConnectionError as exc:
                    reason = str(exc) or 'exited'
                except Exception:
                    reason = 'closed'
                finally:
                    if not ws.closed:
                        with contextlib.suppress(Exception):
                            await ws.close(code=1000, message=(reason or 'closed').encode()[:120])

            pump_task = asyncio.create_task(tunnel_to_ws())
            last_active = time.monotonic()
            while not sink.closed and not ws.closed:
                try:
                    msg = await ws.receive(timeout=DESKTOP_POLL_S)
                except TimeoutError:
                    if time.monotonic() - max(last_active, getattr(terminal, 'output_at', 0)) > DESKTOP_IDLE_S:
                        reason = 'idle'
                        break
                    continue
                if msg.type == WSMsgType.BINARY:
                    last_active = time.monotonic()
                    await terminal.input(msg.data)
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
        except (ConnectionError, RuntimeError):
            pass
        finally:
            if pump_task:
                pump_task.cancel()

            async def end():
                await terminal.close(reason or 'closed')
                if log_id is not None:
                    async with self.db.write_transaction(label='server-desktop-vnc-close') as conn:
                        await conn.execute(
                            'UPDATE server_terminal_log SET ended_ms=?,reason=? WHERE id=?',
                            (now_ms(), terminal.reason or reason or 'closed', log_id),
                        )
                if not ws.closed:
                    with contextlib.suppress(Exception):
                        await ws.close()

            await asyncio.shield(self.service.spawn(end()))
        return ws

    async def rdp_ws(self, request: web.Request, owner: int, server_id: str) -> web.WebSocketResponse:
        """RDCleanPath WebSocket proxy for IronRDP WASM to the node's local RDP port (127.0.0.1:3389)."""
        ws = web.WebSocketResponse(heartbeat=25, max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)

        async def send_rdp_error(code: int = 1, http_status: int = 502, wsa: int | None = None, tls_alert: int | None = None, close_reason: str = 'rdp_error'):
            if not ws.closed:
                with contextlib.suppress(Exception):
                    await ws.send_bytes(build_rdcleanpath_error(code, http_status, wsa, tls_alert))
                    await ws.close(code=4400, message=close_reason.encode()[:120])

        err = self._check_node(server_id, 'windows')
        if err:
            await send_rdp_error(1, 503, close_reason=err)
            return ws

        # 1. Wait for RDCleanPath Request PDU from browser
        try:
            first_msg = await ws.receive(timeout=HANDSHAKE_TIMEOUT_S)
        except TimeoutError:
            await send_rdp_error(1, 408, close_reason='handshake_timeout')
            return ws
        if first_msg.type != WSMsgType.BINARY:
            await send_rdp_error(1, 400, close_reason='invalid_frame')
            return ws
        try:
            rdcp = parse_rdcleanpath_request(bytes(first_msg.data))
        except ValueError as exc:
            await send_rdp_error(1, 400, close_reason=str(exc))
            return ws

        port, custom = await self._resolve_port(owner, server_id, request, 'windows')
        sink = _StreamSink()
        terminal = None
        log_id = None
        reason = ''
        pump_task = None
        try:
            try:
                terminal = await self._open_local_tunnel(owner, server_id, sink, port, custom, 'windows')
            except LookupError as exc:
                await send_rdp_error(1, 504, wsa=10060, close_reason=str(exc))
                return ws
            except PermissionError as exc:
                wsa = 10061 if str(exc) == 'connect_failed' else None
                await send_rdp_error(1, 502, wsa=wsa, close_reason=str(exc))
                return ws

            # 2. X.224 + TLS handshake over the node tunnel
            try:
                x224_resp, certs, ssl_obj, incoming, outgoing = await perform_rdp_tls_handshake(
                    terminal, sink, rdcp['x224Request'], host='127.0.0.1'
                )
            except PermissionError as exc:
                await send_rdp_error(2, 502, close_reason=str(exc))
                return ws
            except TimeoutError:
                await send_rdp_error(1, 504, wsa=10060, close_reason='tls_timeout')
                return ws
            except ssl.SSLError as exc:
                await send_rdp_error(1, 502, tls_alert=40, close_reason=f'tls_error:{exc}')
                return ws
            except Exception as exc:
                await send_rdp_error(1, 502, close_reason=str(exc))
                return ws

            # 3. Send RDCleanPath Response PDU
            resp_pdu = build_rdcleanpath_response(f'127.0.0.1:{port}', x224_resp, certs)
            await ws.send_bytes(resp_pdu)

            async with self.db.write_transaction(label='server-desktop-rdp-open') as conn:
                cur = await conn.execute(
                    'INSERT INTO server_terminal_log(owner,server_id,started_ms,ip) VALUES(?,?,?,?)',
                    (owner, server_id, now_ms(), (request.remote or '')[:64]),
                )
                log_id = cur.lastrowid

            ssl_lock = asyncio.Lock()

            async def flush_outgoing():
                if outgoing.pending:
                    await terminal.input(outgoing.read())

            async def drain_tls_plaintext() -> bytes:
                parts: list[bytes] = []
                async with ssl_lock:
                    while True:
                        try:
                            data = ssl_obj.read(131072)
                        except ssl.SSLWantReadError:
                            break
                        if not data:
                            break
                        parts.append(data)
                    await flush_outgoing()
                if not parts:
                    return b''
                return parts[0] if len(parts) == 1 else b''.join(parts)

            async def tunnel_to_ws():
                nonlocal reason
                try:
                    initial = await drain_tls_plaintext()
                    if initial:
                        await ws.send_bytes(initial)
                    while True:
                        chunk = await sink.read_chunk()
                        async with ssl_lock:
                            incoming.write(chunk)
                        plain = await drain_tls_plaintext()
                        if plain:
                            await ws.send_bytes(plain)
                except ConnectionError as exc:
                    reason = str(exc) or 'exited'
                except Exception:
                    reason = 'closed'
                finally:
                    if not ws.closed:
                        with contextlib.suppress(Exception):
                            await ws.close(code=1000, message=(reason or 'closed').encode()[:120])

            pump_task = asyncio.create_task(tunnel_to_ws())
            last_active = time.monotonic()
            while not sink.closed and not ws.closed:
                try:
                    msg = await ws.receive(timeout=DESKTOP_POLL_S)
                except TimeoutError:
                    if time.monotonic() - max(last_active, getattr(terminal, 'output_at', 0)) > DESKTOP_IDLE_S:
                        reason = 'idle'
                        break
                    continue
                if msg.type == WSMsgType.BINARY:
                    last_active = time.monotonic()
                    view = memoryview(msg.data)
                    offset = 0
                    async with ssl_lock:
                        while offset < len(view):
                            try:
                                written = ssl_obj.write(view[offset:])
                                offset += written
                                await flush_outgoing()
                            except ssl.SSLWantReadError:
                                await flush_outgoing()
                                break
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
        except (ConnectionError, RuntimeError):
            pass
        finally:
            if pump_task:
                pump_task.cancel()

            async def end():
                if terminal is not None:
                    await terminal.close(reason or 'closed')
                if log_id is not None:
                    async with self.db.write_transaction(label='server-desktop-rdp-close') as conn:
                        await conn.execute(
                            'UPDATE server_terminal_log SET ended_ms=?,reason=? WHERE id=?',
                            (now_ms(), (terminal.reason if terminal else '') or reason or 'closed', log_id),
                        )
                if not ws.closed:
                    with contextlib.suppress(Exception):
                        await ws.close()

            await asyncio.shield(self.service.spawn(end()))
        return ws
