"""A controller with the DDNS service on an isolated database: the real web app and
ServerService, fake node sessions (no node process) answering ddns.* calls, the fake
providers and a clock the test moves."""
from __future__ import annotations

import json
import secrets

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.ddns import service as ddns_module
from app.instances import InstanceService
from app.servers.hub import Session
from app.servers.service import ServerService
from tests.ddns import fakeprovider
from tests.test_webhooks_backend import env as base_env  # noqa: F401

OWNER = 123
T0 = 1_800_000_000_000


class Nodes:
    """Fake node sessions: what each answers to ddns.probe, and every call it got."""

    def __init__(self, e):
        self.e = e
        self.calls = []
        self.probe = {}   # server id -> ddns.probe result, or a function of the params giving it
        self.refuse = set()

    async def call(self, server_id, method, params, timeout):
        session = self.e.servers.hub.sessions.get(server_id)
        if session is None:
            raise LookupError('node_offline')
        self.calls.append((server_id, method, params))
        if server_id in self.refuse:
            return {'ok': False, 'error': 'unsupported_method'}
        if method == 'ddns.watch':
            return {'ok': True, 'result': {'accepted': len(params['probes'])}}
        if method == 'ddns.probe':
            answer = self.probe.get(server_id, {'results': [], 'interfaces': []})
            return {'ok': True, 'result': answer(params) if callable(answer) else answer}
        return {'ok': False, 'error': 'unknown_method'}

    def watched(self, server_id):
        return [c[2]['probes'] for c in self.calls if c[0] == server_id and c[1] == 'ddns.watch']

    def connect(self, server_id, *, features=('ddns',), remote='203.0.113.50', forwarded=False):
        session = Session(server_id, None, None, remote)
        session.features = frozenset(features)
        session.caps = {'exec': True}
        session.info = {'platform': 'linux'}
        session.forwarded = forwarded
        self.e.servers.hub.sessions[server_id] = session
        return session

    def drop(self, server_id):
        self.e.servers.hub.sessions.pop(server_id, None)
        self.e.ddns.node_seen(server_id, False)


@pytest.fixture
async def env(base_env, monkeypatch):  # noqa: F811
    e = base_env
    monkeypatch.setattr(ddns_module, 'TICK_S', 10 ** 6)
    fakeprovider.install(monkeypatch)
    e.instances = InstanceService(e.db, e.server)
    e.server.instances = e.instances
    await e.instances.start()
    e.servers = ServerService(e.db, e.server, e.instances)
    e.server.servers = e.servers
    await e.servers.start()
    e.ddns = d = e.servers.ddns
    e.clock = [T0]
    d.now = lambda: e.clock[0]
    d.jitter = lambda seconds: seconds

    e.mono, e.slept = [0.0], []
    d.monotonic = lambda: e.mono[0]

    async def sleep(seconds):
        e.slept.append(seconds)
        e.mono[0] += seconds
    d.sleep = sleep
    d.started_ms = T0
    e.nodes = Nodes(e)
    monkeypatch.setattr(e.servers.hub, 'call', e.nodes.call)
    e.app = TestClient(TestServer(e.server.make_app()))
    await e.app.start_server()
    key = await e.server.get_secret_key()
    start = await e.app.post('/api/auth/login/start', json={'secret': key})
    request_uuid = (await start.json())['requestUuid']
    await e.server.decide_login_request(request_uuid, approved=True, decided_by=OWNER)
    consumed = await e.app.post(f'/api/auth/login/consume/{request_uuid}')
    e.cookies = {'openbear_web_session': consumed.cookies['openbear_web_session'].value}
    e.origin = {'Origin': str(e.app.make_url('')).rstrip('/')}
    try:
        yield e
    finally:
        await d.idle()
        await e.app.close()
        await e.servers.close()
        await e.instances.close()


def advance(e, seconds):
    e.clock[0] += int(seconds * 1000)


async def add_node(e, name='家', *, region=''):
    created = await e.servers.create(OWNER, {'name': name, **({'profile': {'region': region}} if region else {})})
    sid = created['item']['id']
    async with e.db.write_transaction(label='test-node') as conn:
        await conn.execute('INSERT INTO server_nodes(server_id,owner,public_key,info_json,enrolled_ms) VALUES(?,?,?,?,?)',
                           (sid, OWNER, secrets.token_hex(16), json.dumps({'info': {'platform': 'linux', 'ipv4': '203.0.113.50'},
                                                                           'geo': {'country': 'JP'}}), T0))
    return sid


async def add_account(e, token='tok', kind='fake', alias='主账号'):
    return (await e.ddns.create_account(OWNER, {'type': kind, 'alias': alias, 'credentials': {'token': token}}))['account']


def probe_result(**values):
    return {'results': [{'method': m, 'value': v, 'error': '' if v else 'no_address', 'detail': ''} for m, v in values.items()],
            'interfaces': [{'name': 'eth0', 'default': True, 'v4': ['10.0.0.2'], 'v6': []}]}


async def report(e, server_id, key, value='', error='', fails=0):
    session = e.servers.hub.sessions[server_id]
    await e.servers.hub._message(session, {'t': 'ddns', 'obs': [{'key': key, 'family': 'v4', 'method': 'url', 'value': value, 'error': error,
                                                                  'detail': '', 'at': e.clock[0], 'fails': fails}]})
    await e.ddns.idle()


async def record(e, rid):
    return await e.ddns.record_view(OWNER, rid)


async def events(e, rid):
    return [ev['kind'] for ev in reversed((await e.ddns.events(OWNER, rid, 200))['events'])]


async def alerts(e):
    from app.webhooks.repository import many
    return [dict(r) for r in await many(e.db.conn, "SELECT id,kind,text FROM server_notifications WHERE kind LIKE 'ddns_%' ORDER BY created_ms,id")]


async def sync(e, rid, reason='check'):
    await e.ddns.kick(rid, reason)
    await e.ddns.idle()


def key_of(e, sid):
    probes = e.nodes.watched(sid)[-1]
    assert len(probes) == 1
    return probes[0]['key']


async def url_record(e, sid, account, name='home.example.com', value='198.51.100.7', **extra):
    e.nodes.probe[sid] = probe_result(url=value)
    result = await e.ddns.create_records(OWNER, {'serverId': sid, 'source': {'method': 'url'}, 'rows': [
        {'name': name, 'type': 'A', 'accountId': account['id'], **extra.pop('row', {})}], **extra})
    await e.ddns.idle()
    return result


__all__ = ['env', 'base_env']
