"""The node link outside the encrypted channel: what a handshake may cost the controller,
refusals a node can verify, and the controller fingerprint in install commands. Old nodes
(no notices, no nonce, no fingerprint) are spoken to as before."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import subprocess

import aiohttp
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

from app.servers import hub as hub_module
from app.servers import install
from tests import fake_node
from tests import node_protocol as samples
from tests.test_servers import api, env, shipped, wait_for  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401


def b64(raw):
    return base64.b64encode(raw).decode()


async def enrolled(e, name='node'):
    server_id = (await api(e, 'POST', '/api/servers', {'name': name}))['item']['id']
    code = (await api(e, 'POST', f'/api/servers/{server_id}/enrollment'))['code']
    key = Ed25519PrivateKey.generate()
    response = await e.app.post('/api/server-nodes/v1/enroll', json={'code': code, 'publicKey': b64(key.public_key().public_bytes_raw())})
    controller = Ed25519PublicKey.from_public_bytes(base64.b64decode((await response.json())['controllerKey']))
    return server_id, key, controller


def connect(http, e, server_id, **headers):
    return http.ws_connect(e.url + '/api/server-nodes/v1/connect', headers={'X-OpenBear-Node': server_id, **headers})


async def handshake(ws, server_id, key, *, params=None, **extra):
    """The node side: (channel or None, handshake base, the controller's answer to the auth).
    params (text) is signed as a node that signs its parameters does; extra are plain fields."""
    challenge = await ws.receive_json()
    eph = X25519PrivateKey.generate()
    mine = b64(eph.public_key().public_bytes_raw())
    base = hub_module.handshake_base(server_id, challenge['nonce'], challenge['eph'], mine)
    auth = {'t': 'auth', 'node': server_id, 'eph': mine, 'sig': b64(key.sign(hub_module.signed('client', base))), 'version': 'test', **extra}
    if params is not None:
        auth.update(params=params, paramsSig=b64(key.sign(hub_module.signed('client-params', hub_module.params_base(base, params)))))
    await ws.send_json(auth)
    answer = await ws.receive()
    if answer.type != aiohttp.WSMsgType.TEXT:
        return None, base, answer
    shared = eph.exchange(X25519PublicKey.from_public_bytes(base64.b64decode(challenge['eph'])))
    to_controller, to_node = hub_module.session_keys(shared, base)
    return hub_module.Channel(ws, to_controller, to_node), base, answer


async def first_message(channel):
    kind, payload = channel.open((await channel.ws.receive()).data)
    return json.loads(payload)


async def refused(http, e, server_id, **headers):
    with pytest.raises(aiohttp.WSServerHandshakeError) as caught:
        async with connect(http, e, server_id, **headers):
            pass
    return caught.value.status


async def test_the_first_frame_is_small_and_the_session_is_not(env):
    e = env
    server_id, key, _ = await enrolled(e)
    async with aiohttp.ClientSession() as http:
        async with connect(http, e, server_id) as ws:
            await ws.receive_json()
            await ws.send_str('{"t":"auth","pad":"' + 'x' * 2000 + '"}')
            closing = await ws.receive()
            assert closing.type == aiohttp.WSMsgType.CLOSE and closing.data == aiohttp.WSCloseCode.MESSAGE_TOO_BIG
        async with connect(http, e, server_id) as ws:
            channel, _, _ = await handshake(ws, server_id, key)
            assert (await first_message(channel))['t'] == 'config'
            await channel.send_json({'t': 'report', 'm': {'cpu': 5}, 'pad': 'x' * 200_000})
            await wait_for(lambda: _cpu(e, server_id, 5))


async def _cpu(e, server_id, value):
    return ((e.servers.hub.live(server_id) or {}).get('m') or {}).get('cpu') == value


async def test_a_silent_handshake_ends_within_the_timeout(env, monkeypatch):
    e = env
    monkeypatch.setattr(hub_module, 'HANDSHAKE_TIMEOUT', 0.3)
    server_id, _, _ = await enrolled(e)
    async with aiohttp.ClientSession() as http:
        async with connect(http, e, server_id) as ws:
            await ws.receive_json()
            closing = await asyncio.wait_for(ws.receive(), 3)
            assert closing.type == aiohttp.WSMsgType.CLOSE and closing.data == hub_module.CLOSE_UNAUTHORIZED

    async def counted():
        return e.servers.hub.failed_sources.count('127.0.0.1') == 1 and not e.servers.hub.pending
    await wait_for(counted)


async def test_one_handshake_per_node_and_address(env):
    e = env
    server_id, key, _ = await enrolled(e)
    hub = e.servers.hub
    async with aiohttp.ClientSession() as http:
        # A node that reconnects while its last attempt still hangs takes over.
        async with connect(http, e, server_id) as stale:
            await stale.receive_json()
            async with connect(http, e, server_id) as fresh:
                channel, _, _ = await handshake(fresh, server_id, key)
                assert channel and (await first_message(channel))['t'] == 'config'
                closing = await asyncio.wait_for(stale.receive(), 3)
                assert closing.type == aiohttp.WSMsgType.CLOSE and closing.data == hub_module.CLOSE_REPLACED
        assert hub.known[server_id] == '127.0.0.1'
        # Again from where it last connected: the node itself, not counted as a failure.
        before = hub.failed_nodes.count(server_id)
        async with connect(http, e, server_id) as stale:
            await stale.receive_json()
            async with connect(http, e, server_id) as fresh:
                assert (await handshake(fresh, server_id, key))[0] is not None
            assert (await asyncio.wait_for(stale.receive(), 3)).data == hub_module.CLOSE_REPLACED
        assert hub.failed_nodes.count(server_id) == before and not hub.pending


async def test_a_node_may_connect_on_both_addresses_at_once(env):
    """Its primary and its backup (or a probe of the other one) at the same time: neither is
    refused or cut off half way; the later session replaces the earlier, as ever."""
    e = env
    server_id, key, _ = await enrolled(e)
    backup = {'X-Forwarded-For': '10.0.9.1'}
    async with aiohttp.ClientSession() as http:
        async with connect(http, e, server_id) as primary, connect(http, e, server_id, **backup) as spare:
            assert set(e.servers.hub.pending[server_id]) == {'127.0.0.1', '10.0.9.1'}
            first, second = await asyncio.gather(handshake(primary, server_id, key), handshake(spare, server_id, key))
            assert first[0] and second[0]
            assert [(await first_message(c[0]))['t'] for c in (first, second)] == ['config', 'config']
            assert server_id in e.servers.hub.sessions and not e.servers.hub.pending
            assert e.servers.hub.failed_nodes.count(server_id) == 0


async def test_a_stranger_with_the_node_id_cannot_block_or_take_over(env):
    e = env
    server_id, key, _ = await enrolled(e)
    hub = e.servers.hub
    stranger = {'X-Forwarded-For': '198.51.100.7'}
    async with aiohttp.ClientSession() as http:
        # Before the node ever connected (just after a restart) and after: its handshake goes
        # through beside the stranger's, which it does not replace either.
        for _ in range(2):
            async with connect(http, e, server_id, **stranger) as waiting:
                await waiting.receive_json()
                async with connect(http, e, server_id) as node:
                    channel, _, _ = await handshake(node, server_id, key)
                    assert channel and (await first_message(channel))['t'] == 'config'
                    # The stranger opening another attempt does not cut the node's session.
                    async with connect(http, e, server_id, **stranger) as again:
                        await again.receive_json()
                        assert (await waiting.receive(timeout=3)).data == hub_module.CLOSE_REPLACED
                        assert hub.sessions[server_id].channel is not None and not node.closed
                    await channel.send_json({'t': 'report', 'm': {'cpu': 7}})
                    await wait_for(lambda: _cpu(e, server_id, 7))
        assert hub.known[server_id] == '127.0.0.1'
        # The stranger's own failures are its own: the node keeps connecting where it did.
        assert hub.failed_pairs.count((server_id, '198.51.100.7')) >= 2 and hub.failed_pairs.count((server_id, '127.0.0.1')) == 0


async def test_failures_are_limited_by_address_and_node_but_successes_never(env, monkeypatch):
    e = env
    monkeypatch.setattr(hub_module, 'HANDSHAKE_FAILURES', 3)
    first, key, _ = await enrolled(e, 'a')
    second, other_key, _ = await enrolled(e, 'b')
    wrong = Ed25519PrivateKey.generate()
    attacker = {'X-Forwarded-For': '198.51.100.1'}
    async with aiohttp.ClientSession() as http:
        for _ in range(3):
            async with connect(http, e, first, **attacker) as ws:
                assert (await handshake(ws, first, wrong))[0] is None
        # That address is refused for every node; the node it failed for only to it.
        assert await refused(http, e, second, **attacker) == 429
        for _ in range(3):
            async with connect(http, e, first, **{'X-Forwarded-For': f'198.51.100.{10 + _}'}) as ws:
                assert (await handshake(ws, first, wrong))[0] is None
        assert await refused(http, e, first, **{'X-Forwarded-For': '198.51.100.10'}) == 429
        async with connect(http, e, first) as ws:
            channel, _, _ = await handshake(ws, first, key)
            assert channel is not None
        # A crowd of nodes reconnecting at once, again and again, from one address.
        for _ in range(3):
            async def join(server_id, signer):
                async with connect(http, e, server_id) as ws:
                    channel, _, _ = await handshake(ws, server_id, signer)
                    return (await first_message(channel))['t']
            assert await asyncio.gather(join(first, key), join(second, other_key)) == ['config', 'config']


async def test_handshakes_at_once_are_limited_per_address(env, monkeypatch):
    e = env
    monkeypatch.setattr(hub_module, 'HANDSHAKE_PER_SOURCE', 2)
    nodes = [await enrolled(e, f'n{i}') for i in range(3)]
    async with aiohttp.ClientSession() as http:
        async with connect(http, e, nodes[0][0]) as a, connect(http, e, nodes[1][0]) as b:
            await a.receive_json()
            await b.receive_json()
            assert await refused(http, e, nodes[2][0]) == 503
            async with connect(http, e, nodes[2][0], **{'X-Forwarded-For': '198.51.100.20'}) as ws:
                channel, _, _ = await handshake(ws, nodes[2][0], nodes[2][1])
                assert channel is not None


async def test_strangers_filling_the_handshakes_do_not_keep_a_known_node_out(env, monkeypatch):
    """From the 10-30 check: 32 sources holding handshakes open got every node 503, its own ones too.
    A node reconnecting from where it last connected has its own pool."""
    e = env
    monkeypatch.setattr(hub_module, 'HANDSHAKE_SLOTS', 2)
    (mine, key, _), (other, _, _) = await enrolled(e, 'mine'), await enrolled(e, 'other')
    home = {'X-Forwarded-For': '198.51.100.30'}
    async with aiohttp.ClientSession() as http:
        async with connect(http, e, mine, **home) as ws:
            assert (await handshake(ws, mine, key))[0] is not None
        async def at_home():
            return e.servers.hub.known.get(mine) == '198.51.100.30'
        await wait_for(at_home)
        async with connect(http, e, other, **{'X-Forwarded-For': '203.0.113.1'}) as a, connect(http, e, other, **{'X-Forwarded-For': '203.0.113.2'}) as b:
            await a.receive_json()
            await b.receive_json()
            assert await refused(http, e, other, **{'X-Forwarded-For': '203.0.113.3'}) == 503
            # Its node id from elsewhere waits like any stranger; from home it gets in.
            assert await refused(http, e, mine, **{'X-Forwarded-For': '203.0.113.4'}) == 503
            async with connect(http, e, mine, **home) as ws:
                assert (await handshake(ws, mine, key))[0] is not None


async def test_refusals_before_the_channel_are_signed_for_new_nodes(env):
    e = env
    server_id, key, controller = await enrolled(e)
    async with aiohttp.ClientSession() as http:
        # An unknown node that sent a nonce gets a 401 signed for it.
        nonce = b64(os.urandom(16))
        async with http.get(e.url + '/api/server-nodes/v1/connect', headers={'X-OpenBear-Node': 'gone', 'X-OpenBear-Nonce': nonce}) as response:
            body = await response.json()
        assert response.status == 401 and body['error'] == 'unknown_node'
        controller.verify(base64.b64decode(body['sig']), hub_module.rejection(hub_module.handshake_base('gone', nonce, '', ''), 4401, 'unknown_node'))
        async with http.get(e.url + '/api/server-nodes/v1/connect', headers={'X-OpenBear-Node': 'gone'}) as response:
            assert response.status == 401 and await response.json() == {'ok': False, 'error': 'unknown_node'}
        # A wrong key: the close reason carries the signature over this handshake.
        async with connect(http, e, server_id) as ws:
            _, base, closing = await handshake(ws, server_id, Ed25519PrivateKey.generate(), notices=True)
        reason, sig = closing.extra.rsplit(' ', 1)
        assert closing.data == hub_module.CLOSE_UNAUTHORIZED and reason == 'unauthorized'
        controller.verify(base64.b64decode(sig), hub_module.rejection(base, hub_module.CLOSE_UNAUTHORIZED, reason))
        with pytest.raises(InvalidSignature):
            controller.verify(base64.b64decode(sig), hub_module.rejection(base, hub_module.CLOSE_REVOKED, reason))
        # A node from before notices gets the close it always got.
        async with connect(http, e, server_id) as ws:
            _, _, closing = await handshake(ws, server_id, Ed25519PrivateKey.generate())
        assert (closing.data, closing.extra) == (hub_module.CLOSE_UNAUTHORIZED, 'unauthorized')


async def test_revocation_is_noticed_inside_the_channel(env, monkeypatch):
    e = env
    server_id, key, _ = await enrolled(e)
    async with aiohttp.ClientSession() as http:
        for notices in (True, False):
            async with connect(http, e, server_id) as ws:
                channel, _, _ = await handshake(ws, server_id, key, **({'notices': True} if notices else {}))
                assert (await first_message(channel))['t'] == 'config'
                await e.servers.hub.disconnect(server_id)
                # Every node gets the notice (an older one skips the unknown message), then the close code.
                assert await first_message(channel) == {'t': 'close', 'code': hub_module.CLOSE_REVOKED, 'reason': 'revoked'}
                closing = await ws.receive()
                assert closing.type == aiohttp.WSMsgType.CLOSE and closing.data == hub_module.CLOSE_REVOKED

    # Revoked while the node signed: the notice takes the place of the session config, for new nodes only.
    original = hub_module.one

    async def revoked_meanwhile(conn, sql, params=()):
        row = await original(conn, sql, params)
        return None if sql.startswith('SELECT public_key FROM server_nodes') else row
    monkeypatch.setattr(hub_module, 'one', revoked_meanwhile)
    async with aiohttp.ClientSession() as http:
        async with connect(http, e, server_id) as ws:
            channel, _, _ = await handshake(ws, server_id, key, notices=True)
            assert await first_message(channel) == {'t': 'close', 'code': hub_module.CLOSE_REVOKED, 'reason': 'revoked'}
            assert (await ws.receive()).data == hub_module.CLOSE_REVOKED
        async with connect(http, e, server_id) as ws:
            channel, _, _ = await handshake(ws, server_id, key)
            closing = await ws.receive()
            assert closing.type == aiohttp.WSMsgType.CLOSE and closing.data == hub_module.CLOSE_REVOKED


def test_install_commands_carry_the_controller_fingerprint():
    key = b64(Ed25519PrivateKey.generate().public_key().public_bytes_raw())
    fingerprint = install.fingerprint(key)
    assert fingerprint == hashlib.sha256(base64.b64decode(key)).hexdigest() and len(fingerprint) == 64
    assert install.command('https://c.example.com', 'code', fingerprint=fingerprint, node='srv-a').endswith(
        f'--url https://c.example.com --code code --fingerprint {fingerprint} --node srv-a')
    assert f"-Code 'code' -Fingerprint '{fingerprint}'" in install.windows_command('https://c.example.com', 'code', fingerprint=fingerprint)
    body = base64.b64decode(install.windows_ssh_command('https://c.example.com', 'code', fingerprint=fingerprint).split()[-1]).decode('utf-16-le')
    assert f"-Fingerprint '{fingerprint}'" in body
    # Commands saved before fingerprints stay as they were.
    assert '--fingerprint' not in install.command('https://c.example.com', 'code') and 'Fingerprint' not in install.windows_command('https://c.example.com', 'code')
    script = install.windows_script()
    assert "[string]$Fingerprint = ''" in script and '$env:OPENBEAR_CONTROLLER_FINGERPRINT = $Fingerprint' in script


def test_the_script_hands_the_fingerprint_to_the_node(tmp_path, shipped):  # noqa: F811
    """The enroll stage of install.sh, with a stand-in for the node binary."""
    fake = tmp_path / 'openbear-node'
    fake.write_text('#!/bin/sh\necho "fingerprint=[$OPENBEAR_CONTROLLER_FINGERPRINT] $*"\n')
    fake.chmod(0o755)
    script = install.script().replace('/usr/local/bin/openbear-node', str(fake))

    def enroll(*args):
        done = subprocess.run(['sh', '-s', '--', '--url', 'http://127.0.0.1:9', '--code', 'c0de', '--stage', 'enroll', *args],
                              input=script, capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr
        return done.stdout
    assert f'fingerprint=[{"a" * 64}] enroll --url http://127.0.0.1:9 --code c0de' in enroll('--fingerprint', 'a' * 64)
    assert 'fingerprint=[] enroll' in enroll()


async def test_the_node_enrolls_only_with_the_fingerprinted_controller(env):  # noqa: F811
    """The fingerprint is the controller's (the shared sample holds what the node compares); the
    real node's check, OPENBEAR_CONTROLLER_FINGERPRINT included, is in the end-to-end test in
    test_servers.py and in node/protocol_samples_test.go."""
    e = env
    server_id = (await api(e, 'POST', '/api/servers', {'name': 'fp'}))['item']['id']
    fingerprint = install.fingerprint(e.servers.identity_public)
    shared = samples.SAMPLES['enroll:fingerprint']
    assert install.fingerprint(shared['controllerKey']) == shared['fingerprint']

    async def enroll(given=''):
        code = (await api(e, 'POST', f'/api/servers/{server_id}/enrollment'))['code']
        try:
            await fake_node.enroll(e.url, code, e.tmp / 'fp-node.json', fingerprint=given)
        except fake_node.EnrollError as exc:
            return 1, str(exc)
        return 0, ''
    code, errors = await enroll('0' * 64)
    assert code != 0 and 'enrollment stopped' in errors and fingerprint in errors
    assert (await enroll(fingerprint))[0] == 0
    assert (await enroll(fingerprint.upper()))[0] == 0
    assert (await enroll())[0] == 0


async def test_signed_parameters_are_what_the_controller_goes_by(env):
    e = env
    server_id, key, controller = await enrolled(e)
    params = json.dumps({'version': '1.2.0', 'protocol': 1, 'protocolMin': 1, 'notices': True})
    async with aiohttp.ClientSession() as http:
        # The plain copies say otherwise, as something on the way could have made them.
        async with connect(http, e, server_id) as ws:
            channel, base, welcome = await handshake(ws, server_id, key, params=params, version='9.9.9', protocol=7, protocolMin=7, probe=True)
            sig = base64.b64decode(json.loads(welcome.data)['paramsSig'])
            controller.verify(sig, hub_module.signed('server-params', hub_module.params_base(base, params)))
            config = await first_message(channel)
            assert config == {'t': 'config', 'interval': hub_module.REPORT_INTERVAL, 'protocol': 1, 'protocolMin': 1, 'signedParams': True}
            session = e.servers.hub.sessions[server_id]
            assert session.version == '1.2.0' and session.incompatible == ''
        # Parameters changed after signing: refused, with a refusal the node can verify.
        async with connect(http, e, server_id) as ws:
            challenge = await ws.receive_json()
            eph = b64(X25519PrivateKey.generate().public_key().public_bytes_raw())
            base = hub_module.handshake_base(server_id, challenge['nonce'], challenge['eph'], eph)
            sig = b64(key.sign(hub_module.signed('client-params', hub_module.params_base(base, params))))
            await ws.send_json({'t': 'auth', 'node': server_id, 'eph': eph, 'sig': b64(key.sign(hub_module.signed('client', base))), 'notices': True,
                                'params': params.replace('1.2.0', '1.2.1'), 'paramsSig': sig})
            closing = await ws.receive()
            assert closing.data == hub_module.CLOSE_UNAUTHORIZED and closing.extra.startswith('unauthorized ')
        # A probe says so in its signed parameters.
        async with connect(http, e, server_id) as ws:
            channel, _, _ = await handshake(ws, server_id, key, params=json.dumps({'version': '1.2.0', 'probe': True}))
            assert (await first_message(channel))['probe'] is True and (await ws.receive()).type == aiohttp.WSMsgType.CLOSE
        # A node from before signs no parameters: taken as it says them, as before.
        async with connect(http, e, server_id) as ws:
            channel, _, welcome = await handshake(ws, server_id, key, version='1.1.0')
            assert 'paramsSig' not in json.loads(welcome.data)
            assert (await first_message(channel))['signedParams'] is False
            assert e.servers.hub.sessions[server_id].version == '1.1.0'


async def test_enrollment_checks_the_protocol_range(env, monkeypatch):
    from app.servers import compat
    e = env
    server_id = (await api(e, 'POST', '/api/servers', {'name': 'p'}))['item']['id']
    code = (await api(e, 'POST', f'/api/servers/{server_id}/enrollment'))['code']

    async def enroll(**extra):
        key = b64(Ed25519PrivateKey.generate().public_key().public_bytes_raw())
        response = await e.app.post('/api/server-nodes/v1/enroll', json={'code': code, 'publicKey': key, **extra})
        return response.status, await response.json()
    assert await enroll(protocol=3, protocolMin=2) == (409, {'ok': False, 'error': 'controller_protocol_outdated'})
    monkeypatch.setattr(compat, 'PROTOCOL', (2, 3))
    # A node from before protocol speaks 1.
    assert await enroll() == (409, {'ok': False, 'error': 'node_protocol_outdated'})
    monkeypatch.setattr(compat, 'PROTOCOL', (1, 1))
    # The refusals left the code unspent.
    status, body = await enroll(protocol=2, protocolMin=1)
    assert status == 200 and body['ok'] and body['nodeId'] == server_id


async def test_a_node_that_is_behind_stays_updatable(env, monkeypatch):
    from app.servers import compat
    e = env
    server_id, key, _ = await enrolled(e)
    monkeypatch.setattr(compat, 'PROTOCOL', (2, 3))
    async with aiohttp.ClientSession() as http:
        async with connect(http, e, server_id) as ws:
            channel, _, _ = await handshake(ws, server_id, key, params=json.dumps({'version': '1.0.0', 'protocol': 1, 'protocolMin': 1}))
            # Kept connected, and told this controller's range so it can say why.
            assert (await first_message(channel))['protocol'] == 3
            session = e.servers.hub.sessions[server_id]
            assert session.incompatible == 'node_protocol_outdated'
            live = (await api(e, 'GET', f'/api/servers/{server_id}'))['item']['live']
            assert live['incompatible'] == 'node_protocol_outdated'
            assert (await e.servers.hub.call(server_id, 'exec', {}, 5))['error'] == 'node_protocol_outdated'
            # The update still goes to it: that is what brings it back.
            pending = asyncio.create_task(e.servers.hub.call(server_id, 'update', {'version': '1.3.0'}, 5))
            _, payload = channel.open((await ws.receive()).data)
            sent = json.loads(payload)
            assert (sent['t'], sent['method']) == ('call', 'update')
            await channel.send_json({'t': 'reply', 'id': sent['id'], 'ok': True, 'result': {'changed': True}})
            assert (await pending)['ok'] is True
        # One ahead of this controller is not: an update would take it back.
        monkeypatch.setattr(compat, 'PROTOCOL', (1, 1))
        async with connect(http, e, server_id) as ws:
            channel, _, _ = await handshake(ws, server_id, key, params=json.dumps({'version': '9.0.0', 'protocol': 3, 'protocolMin': 2}))
            await first_message(channel)
            assert await e.servers.hub.call(server_id, 'update', {}, 5) == {'ok': False, 'error': 'controller_protocol_outdated'}


def test_a_refused_enroll_starts_the_node_that_was_installed_again(tmp_path, shipped):  # noqa: F811
    """From the 10-30 check: reinstalling with an expired code left the node already there
    stopped (the download stage stopped it) though its config was still in place."""
    fake = tmp_path / 'openbear-node'
    fake.write_text('#!/bin/sh\necho "invalid_enrollment_code" >&2\nexit 1\n')
    fake.chmod(0o755)
    calls = tmp_path / 'calls'
    systemctl = tmp_path / 'systemctl'
    systemctl.write_text(f'#!/bin/sh\necho "$*" >> {calls}\n')
    systemctl.chmod(0o755)
    conf = tmp_path / 'config.json'
    script = (install.script().replace('/usr/local/bin/openbear-node', str(fake)).replace('CONF=/etc/openbear-node/config.json', f'CONF={conf}')
              .replace('systemctl start openbear-node', f'{systemctl} start openbear-node'))

    def enroll():
        return subprocess.run(['sh', '-s', '--', '--url', 'http://127.0.0.1:9', '--code', 'old', '--stage', 'enroll'],
                              input=script, capture_output=True, text=True, timeout=60)
    done = enroll()
    assert done.returncode == 1 and not calls.exists()  # nothing was installed here: nothing to start
    conf.write_text('{}')
    done = enroll()
    assert done.returncode == 1 and calls.read_text().split() == ['start', 'openbear-node'] and 'started again' in done.stderr
