"""Tests for browser-based remote desktop (macOS VNC and Windows RDP over RDCleanPath)."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import ssl
import tempfile
from pathlib import Path

import aiohttp
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.servers.desktop import (
    VERSION_1,
    _der_ctx,
    _der_int,
    _der_octets,
    _der_read_children,
    _der_read_tlv,
    _der_utf8,
    _der_wrap,
    TAG_SEQUENCE,
    build_rdcleanpath_error,
    build_rdcleanpath_response,
    parse_rdcleanpath_request,
)
from tests.test_servers import api, env, node_binary, shipped, start_node, wait_for  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401

OWNER = 123


def _build_sample_rdcleanpath_request(destination: str, x224: bytes, auth: str = 'openbear') -> bytes:
    parts = b''.join([
        _der_ctx(0, _der_int(VERSION_1)),
        _der_ctx(2, _der_utf8(destination)),
        _der_ctx(3, _der_utf8(auth)),
        _der_ctx(6, _der_octets(x224)),
    ])
    return _der_wrap(TAG_SEQUENCE, parts)


def test_rdcleanpath_der_roundtrip():
    x224_req = b'\x03\x00\x00\x13\x0e\xe0\x00\x00\x00\x00\x00\x01\x00\x08\x00\x03\x00\x00\x00'
    raw_req = _build_sample_rdcleanpath_request('127.0.0.1:3389', x224_req, 'token123')
    parsed = parse_rdcleanpath_request(raw_req)
    assert parsed['destination'] == '127.0.0.1:3389'
    assert parsed['proxyAuth'] == 'token123'
    assert parsed['x224Request'] == x224_req

    x224_resp = b'\x03\x00\x00\x13\x0e\xd0\x00\x00\x12\x34\x00\x02\x0f\x08\x00\x02\x00\x00\x00'
    cert = b'0\x82\x01\x0a\x02\x82\x01\x01\x00'
    resp_pdu = build_rdcleanpath_response('127.0.0.1:3389', x224_resp, [cert])
    outer_tag, outer_body, _ = _der_read_tlv(resp_pdu, 0)
    assert outer_tag == TAG_SEQUENCE
    children = dict(_der_read_children(outer_body))
    assert 0xA6 in children and 0xA7 in children and 0xA9 in children

    err_pdu = build_rdcleanpath_error(1, http_status=502, wsa_error=10061)
    etag, ebody, _ = _der_read_tlv(err_pdu, 0)
    assert etag == TAG_SEQUENCE
    echildren = dict(_der_read_children(ebody))
    assert 0xA0 in echildren and 0xA1 in echildren


async def test_desktop_config_ssh_fallback_and_override(env):
    e = env
    created = await api(e, 'POST', '/api/servers', {'name': 'WinServer'})
    sid = created['item']['id']

    # Inject SSH credentials directly into instance_servers to test fallback
    pub = {'name': 'WinServer', 'host': '10.0.0.2', 'port': 22, 'username': 'zy', 'auth': 'password', 'platform': 'windows'}
    sealed = e.instances.vault.seal({'password': 'ssh-secret-123'})
    async with e.db.write_transaction() as conn:
        await conn.execute('UPDATE instance_servers SET public_json=?,protected=? WHERE id=?', (json.dumps(pub), sealed, sid))

    cfg = await api(e, 'GET', f'/api/servers/{sid}/desktop/config')
    assert cfg['ok'] is True
    assert cfg['platform'] == 'windows'
    assert cfg['protocol'] == 'rdp'
    assert cfg['port'] == 3389
    assert cfg['username'] == 'zy'
    assert cfg['password'] == 'ssh-secret-123'
    assert cfg['source'] == 'ssh'
    assert cfg['hasSaved'] is False

    # Override with custom desktop credentials and custom port
    saved = await api(e, 'PUT', f'/api/servers/{sid}/desktop/config', {
        'port': 25918,
        'username': 'Administrator',
        'password': 'rdp-password-456',
        'domain': 'CORP',
    })
    assert saved['ok'] is True
    assert saved['port'] == 25918
    assert saved['customPort'] == 25918
    assert saved['username'] == 'Administrator'
    assert saved['password'] == 'rdp-password-456'
    assert saved['domain'] == 'CORP'
    assert saved['source'] == 'desktop'
    assert saved['hasSaved'] is True

    # Updating port alone without re-typing password preserves the saved password
    port_only = await api(e, 'PUT', f'/api/servers/{sid}/desktop/config', {
        'port': 25919,
        'username': 'Administrator',
        'password': '',
        'domain': 'CORP',
    })
    assert port_only['port'] == 25919
    assert port_only['password'] == 'rdp-password-456'

    # Reset back to SSH fallback
    cleared = await api(e, 'PUT', f'/api/servers/{sid}/desktop/config', {'clear': True})
    assert cleared['port'] == 3389
    assert cleared['username'] == 'zy'
    assert cleared['password'] == 'ssh-secret-123'
    assert cleared['source'] == 'ssh'
    assert cleared['hasSaved'] is False


async def test_desktop_config_gives_no_ssh_password_where_there_is_no_desktop(env):
    e = env
    sid = (await api(e, 'POST', '/api/servers', {'name': 'LinuxBox'}))['item']['id']
    pub = {'name': 'LinuxBox', 'host': '10.0.0.3', 'port': 22, 'username': 'root', 'auth': 'password', 'platform': 'linux'}
    async with e.db.write_transaction() as conn:
        await conn.execute('UPDATE instance_servers SET public_json=?,protected=? WHERE id=?',
                           (json.dumps(pub), e.instances.vault.seal({'password': 'ssh-secret-123'}), sid))
    cfg = await api(e, 'GET', f'/api/servers/{sid}/desktop/config')
    assert (cfg['protocol'], cfg['username'], cfg['password'], cfg['source']) == ('', '', '', 'none')


async def test_desktop_vnc_and_rdp_relay_over_node_tunnel(env, node_binary):
    e = env
    created = await api(e, 'POST', '/api/servers', {'name': 'DesktopNode'})
    sid = created['item']['id']
    await start_node(e, node_binary, sid)
    async def ready():
        s = e.servers.hub.sessions.get(sid)
        return s if s and 'front' in s.features else None

    session = await wait_for(ready)
    ws_headers = {**e.origin, 'Cookie': f"openbear_web_session={e.cookies['openbear_web_session']}"}

    # 0. Not a desktop's platform: refused before any tunnel.
    async with e.app.ws_connect(f'/api/servers/{sid}/desktop/vnc', headers=ws_headers, protocols=('binary',)) as ws:
        msg = await ws.receive()
        assert (msg.type, ws.close_code, msg.extra) == (aiohttp.WSMsgType.CLOSE, 4400, 'unsupported_platform')

    # The tunnel is the same on every platform: this Linux node stands in for each.
    session.info = {**session.info, 'platform': 'darwin'}

    # 1. Start a mock local VNC server
    async def handle_vnc(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        writer.write(b'RFB 003.889\n')
        await writer.drain()
        client_ver = await reader.readexactly(12)
        writer.write(b'VNC_OK:' + client_ver)
        await writer.drain()
        writer.close()

    vnc_srv = await asyncio.start_server(handle_vnc, '127.0.0.1', 0)
    vnc_port = vnc_srv.sockets[0].getsockname()[1]

    try:
        async with e.app.ws_connect(
            f'/api/servers/{sid}/desktop/vnc?port={vnc_port}',
            headers=ws_headers,
            protocols=('binary',),
        ) as ws:
            banner = await ws.receive_bytes()
            assert banner == b'RFB 003.889\n'
            await ws.send_bytes(b'RFB 003.889\n')
            echo = await ws.receive_bytes()
            assert echo == b'VNC_OK:RFB 003.889\n'
    finally:
        vnc_srv.close()
        await vnc_srv.wait_closed()

    # 2. Start a mock local RDP server (X.224 + self-signed TLS + encrypted echo)
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'win-rdp-local')])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(priv.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(priv, hashes.SHA256())
    )
    expected_der = cert.public_bytes(serialization.Encoding.DER)

    with tempfile.TemporaryDirectory() as tmpdir:
        cpath = Path(tmpdir) / 'cert.pem'
        kpath = Path(tmpdir) / 'key.pem'
        cpath.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        kpath.write_bytes(priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))

        sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        sctx.load_cert_chain(str(cpath), str(kpath))

        x224_confirm = b'\x03\x00\x00\x13\x0e\xd0\x00\x00\x12\x34\x00\x02\x0f\x08\x00\x02\x00\x00\x00'

        async def handle_rdp(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            # Read X.224 request TPKT
            hdr = await reader.readexactly(4)
            total = (hdr[2] << 8) | hdr[3]
            await reader.readexactly(total - 4)
            writer.write(x224_confirm)
            await writer.drain()

            # Upgrade to TLS using MemoryBIO
            s_in, s_out = ssl.MemoryBIO(), ssl.MemoryBIO()
            s_ssl = sctx.wrap_bio(s_in, s_out, server_side=True)
            while True:
                try:
                    s_ssl.do_handshake()
                    if s_out.pending:
                        writer.write(s_out.read())
                        await writer.drain()
                    break
                except ssl.SSLWantReadError:
                    if s_out.pending:
                        writer.write(s_out.read())
                        await writer.drain()
                    data = await reader.read(16384)
                    if not data:
                        return
                    s_in.write(data)

            # Read one encrypted RDP PDU from client and echo back
            while True:
                try:
                    plain = s_ssl.read(4096)
                    if plain:
                        s_ssl.write(b'RDP_TLS_ECHO:' + plain)
                        if s_out.pending:
                            writer.write(s_out.read())
                            await writer.drain()
                        break
                except ssl.SSLWantReadError:
                    data = await reader.read(16384)
                    if not data:
                        break
                    s_in.write(data)
            writer.close()

        rdp_srv = await asyncio.start_server(handle_rdp, '127.0.0.1', 0)
        rdp_port = rdp_srv.sockets[0].getsockname()[1]
        session.info = {**session.info, 'platform': 'windows'}

        try:
            async with e.app.ws_connect(
                f'/api/servers/{sid}/desktop/rdp?port={rdp_port}',
                headers=ws_headers,
            ) as ws:
                x224_req = b'\x03\x00\x00\x13\x0e\xe0\x00\x00\x00\x00\x00\x01\x00\x08\x00\x03\x00\x00\x00'
                await ws.send_bytes(_build_sample_rdcleanpath_request(f'127.0.0.1:{rdp_port}', x224_req))
                rdcp_resp = await ws.receive_bytes()
                tag, body, _ = _der_read_tlv(rdcp_resp, 0)
                assert tag == TAG_SEQUENCE
                children = dict(_der_read_children(body))
                _, got_x224, _ = _der_read_tlv(children[0xA6], 0)
                assert got_x224 == x224_confirm
                _, certs_seq, _ = _der_read_tlv(children[0xA7], 0)
                _, first_cert, _ = _der_read_tlv(certs_seq, 0)
                assert first_cert == expected_der

                await ws.send_bytes(b'hello-ironrdp')
                echoed = await ws.receive_bytes()
                assert echoed == b'RDP_TLS_ECHO:hello-ironrdp'
        finally:
            rdp_srv.close()
            await rdp_srv.wait_closed()
