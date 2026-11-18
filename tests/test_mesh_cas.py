"""Draft CAS through HTTP and the tool, using only the isolated test database/fake nodes."""
from __future__ import annotations

import asyncio
import copy
import json

from app.instances.contracts import InstanceError
from app.servers.mesh import Mesh
from app.tools import servers as tool_module
from app.tools.base import ToolRegistry, ToolRuntimeContext
from tests.test_mesh_service import OWNER, mesh_setup
from tests.test_servers import api, env, shipped  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401


async def test_draft_requires_baseline_and_rejects_stale_full_document(env):  # noqa: F811
    e = env
    _, ids = await mesh_setup(e)
    joined = await api(e, 'POST', '/api/servers/mesh/members', {'serverIds': [ids['r1'], ids['m1']]})
    rule = {'src': {'members': [ids['m1']]}, 'dst': {'members': [ids['r1']]}, 'proto': 'icmp'}
    seeded = await api(e, 'PUT', '/api/servers/mesh/draft', {'draftVersion': joined['draftVersion'], 'rules': [rule]})
    stale = {k: copy.deepcopy(seeded[k]) for k in ('settings', 'members', 'relayLinks', 'directLinks', 'rules', 'draftVersion')}
    missing = await api(e, 'PUT', '/api/servers/mesh/draft', {'rules': []}, status=428)
    assert missing['error'] == 'mesh_draft_version_required'
    revoked = await api(e, 'PUT', '/api/servers/mesh/draft', {'draftVersion': seeded['draftVersion'], 'rules': []})
    stale['settings']['mtu'] = 1300
    conflict = await api(e, 'PUT', '/api/servers/mesh/draft', stale, status=409)
    assert conflict['error'] == 'mesh_draft_conflict'
    latest = await api(e, 'GET', '/api/servers/mesh')
    assert latest['rules'] == [] and latest['settings']['mtu'] == seeded['settings']['mtu']
    assert latest['draftVersion'] == revoked['draftVersion'] != seeded['draftVersion']
    # Token persists independently of a service object's lifecycle, and is not an applied revision.
    doc, version = await Mesh(e.servers)._load(OWNER, 'draft', with_version=True)
    assert version == latest['draftVersion'] and '_draftVersion' not in doc['settings']
    discarded = await api(e, 'POST', '/api/servers/mesh/discard')
    assert discarded['draftVersion'] != latest['draftVersion']
    assert (await api(e, 'PUT', '/api/servers/mesh/draft', {'draftVersion': latest['draftVersion'], 'rules': []}, status=409))['error'] == 'mesh_draft_conflict'


async def test_simultaneous_saves_have_one_winner(env):  # noqa: F811
    e = env
    version = (await api(e, 'GET', '/api/servers/mesh'))['draftVersion']
    async def save(mtu):
        try:
            return await e.servers.mesh.save_draft(OWNER, {'draftVersion': version, 'settings': {'mtu': mtu}})
        except InstanceError as exc:
            return exc.error
    results = await asyncio.gather(save(1300), save(1320))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert results.count('mesh_draft_conflict') == 1
    winner = next(r for r in results if isinstance(r, dict))
    current = await api(e, 'GET', '/api/servers/mesh')
    assert current['draftVersion'] == winner['draftVersion'] and current['settings']['mtu'] == winner['settings']['mtu']


async def test_web_and_tool_share_the_same_cas_baseline(env):  # noqa: F811
    e = env
    registry = ToolRegistry()
    tool_module.register_server_tools(registry, e.servers)
    async with e.db.write_transaction(label='test-mesh-cas') as conn:
        await conn.execute("INSERT INTO web_conversations(conversation_uuid,internal_chat_id,owner_chat_id,title) VALUES('conv-cas',779,?,'t')", (OWNER,))
    ctx = ToolRuntimeContext(chat_id=779, conversation_uuid='conv-cas')
    async def call(action, **params):
        return json.loads(await registry.dispatch('Servers', json.dumps({'action': action, 'params': params}), context=ctx))
    first = await call('mesh_draft')
    assert isinstance(first['draftVersion'], str)
    changed = await api(e, 'PUT', '/api/servers/mesh/draft', {'draftVersion': first['draftVersion'], 'settings': {'mtu': 1300}})
    for payload in ({'settings': {'mtu': 1320}}, {'patch': {'settings': {'mtu': 1320}}}):
        rejected = await call('mesh_save_draft', draftVersion=first['draftVersion'], **payload)
        assert rejected['error'] == 'mesh_draft_conflict'
    assert (await call('mesh_save_draft', settings={'mtu': 1320}))['error'] == 'mesh_draft_version_required'
    latest = await call('mesh_draft', parts=['settings'])
    assert latest['draftVersion'] == changed['draftVersion']
    saved = await call('mesh_save_draft', draftVersion=latest['draftVersion'], patch={'settings': {'mtu': 1320}})
    assert saved['ok'] and saved['draftVersion'] != latest['draftVersion']
    old_web = await api(e, 'PUT', '/api/servers/mesh/draft', {'draftVersion': changed['draftVersion'], 'settings': {'mtu': 1340}}, status=409)
    assert old_web['error'] == 'mesh_draft_conflict'
    assert (await api(e, 'GET', '/api/servers/mesh'))['settings']['mtu'] == 1320
