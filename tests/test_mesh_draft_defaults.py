"""Automatic member values are caches, not unapplied user edits."""
from __future__ import annotations

import copy

import pytest

from app.servers import mesh_auto, mesh_autolinks
from app.servers import mesh_model as model
from tests.test_mesh_service import OWNER, applied_base, settled
from tests.test_servers import api, env, shipped  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401


def test_member_defaults_keep_manual_fields_ports_and_link_edits():
    doc = model.empty_doc()
    relay = model.new_member('relay', doc['settings'])
    relay.update(role='relay', relayMode='site', auto=['relayMode', 'listen'])
    relay['listen']['udp'] = 51997
    peer = model.new_member('peer', doc['settings'])
    peer.update(role='relay', auto=[], listen={'udp': 53000, 'tcp': 53001, 'tls': 0})
    doc['members'] = [relay, peer]
    doc['relayLinks'] = {'mode': 'custom', 'pairs': [
        {'a': 'relay', 'b': 'peer', 'dialer': 'a', 'transport': 'tcp', 'cost': 1, 'bias': 23, 'auto': ['transport']}]}
    facts = {'relay': {'verdict': 'public'}, 'peer': {'verdict': 'public'}}
    before = copy.deepcopy(doc)
    current = mesh_auto.member_defaults(doc, facts)
    assert current['members'][0]['relayMode'] == 'public'
    assert current['members'][0]['listen'] == {'udp': 51997, 'tcp': 52000, 'tls': 0}
    assert current['members'][1] == peer
    assert current['relayLinks'] == before['relayLinks']
    assert doc == before

    # A draft address or role is its own input, not copied from the applied member.
    facts['relay']['verdict'] = 'symmetric'
    doc['members'][0]['publicEndpoint'] = '203.0.113.1'
    assert mesh_auto.member_defaults(doc, facts)['members'][0]['relayMode'] == 'public'
    doc['members'][0].update(role='member', publicEndpoint='')
    assert mesh_auto.member_defaults(doc, facts)['members'][0]['listen']['tcp'] == 0


async def public_facts(mesh, monkeypatch):
    original = mesh._facts

    async def facts(owner, rows=None):
        values = await original(owner, rows)
        for value in values.values():
            value['verdict'] = 'public'
        return values

    monkeypatch.setattr(mesh, '_facts', facts)


async def test_view_clears_stale_auto_differences_without_writing_or_hiding_edits(env, monkeypatch):  # noqa: F811
    e = env
    nodes, ids, _ = await applied_base(e)
    mesh = e.servers.mesh
    await public_facts(mesh, monkeypatch)
    applied = await mesh._load(OWNER, 'applied')
    draft = copy.deepcopy(applied)
    draft['members'][0].update(relayMode='site', listen={'udp': 51999, 'tcp': 0, 'tls': 0})
    await mesh._save_draft(OWNER, draft, auto_base=[{'version': 'old', 'applied': applied}])
    stored, version = await mesh._load(OWNER, 'draft', with_version=True)
    nodes.calls.clear()

    view = await api(e, 'GET', '/api/servers/mesh')
    assert view['draft'] == {'dirty': False, 'changes': []}
    assert view['members'][0]['relayMode'] == 'public'
    assert view['members'][0]['listen']['tcp'] == 52000
    assert view['draftVersion'] == version
    assert await mesh._load(OWNER, 'draft', with_version=True) == (stored, version)
    assert nodes.sent('mesh.apply') == []

    draft['members'][1]['uploadLimitMbps'] = 7
    await mesh._save_draft(OWNER, draft)
    view = await api(e, 'GET', '/api/servers/mesh')
    assert view['draft']['dirty'] and len(view['draft']['changes']) == 1
    assert view['draft']['changes'][0]['serverId'] == ids['m1']
    assert view['members'][1]['uploadLimitMbps'] == 7

    # A manual override remains an edit even when it equals the current automatic value.
    draft['members'][0].update(auto=[], relayMode='public', listen={'udp': 51999, 'tcp': 52000, 'tls': 0})
    await mesh._save_draft(OWNER, draft)
    view = await api(e, 'GET', '/api/servers/mesh')
    assert {c['serverId'] for c in view['draft']['changes']} == {ids['r1'], ids['m1']}
    draft['members'][0].update(relayMode='site', listen={'udp': 51997, 'tcp': 0, 'tls': 0})
    await mesh._save_draft(OWNER, draft)
    view = await api(e, 'GET', '/api/servers/mesh')
    assert view['members'][0]['relayMode'] == 'site' and view['members'][0]['listen']['udp'] == 51997
    assert '中转类型' in view['draft']['changes'][0]['text'] and '监听端口' in view['draft']['changes'][0]['text']
    assert await mesh._load(OWNER, 'applied') == applied
    assert nodes.sent('mesh.apply') == []


@pytest.mark.parametrize('manual', [False, True])
async def test_auto_revision_refreshes_member_caches_and_keeps_draft_edits(env, monkeypatch, manual):  # noqa: F811
    e = env
    nodes, ids, _ = await applied_base(e)
    mesh = e.servers.mesh
    await public_facts(mesh, monkeypatch)
    old = await mesh._load(OWNER, 'applied')
    old['members'][0].update(relayMode='site', listen={'udp': 51999, 'tcp': 0, 'tls': 0})
    async with e.db.write_transaction(label='test') as conn:
        await mesh._save(conn, OWNER, 'applied', old)
    draft = copy.deepcopy(old)
    if manual:
        draft['settings']['mtu'] = 1360
        draft['members'][1]['uploadLimitMbps'] = 7
    await mesh._save_draft(OWNER, draft, auto_base=[{'version': 'old', 'applied': old}])
    prior_version = (await mesh._load(OWNER, 'draft', with_version=True))[1]

    def optimize(doc, facts, statuses, *, state, now_ms):
        return {'changed': True, 'doc': copy.deepcopy(doc), 'state': state,
                'revisionReason': '自动：更新线路', 'dropped': []}

    monkeypatch.setattr(mesh_autolinks, 'optimize', optimize)
    result = await mesh.check_auto_links(OWNER, now=1_000_000)
    assert result['changed']
    view = await settled(e)
    saved = await mesh._load(OWNER, 'draft')
    applied = await mesh._load(OWNER, 'applied')
    assert saved['members'][0]['relayMode'] == applied['members'][0]['relayMode'] == 'public'
    assert saved['members'][0]['listen'] == applied['members'][0]['listen'] == {'udp': 51999, 'tcp': 52000, 'tls': 0}
    assert view['draft']['dirty'] is manual
    assert saved['members'][1]['uploadLimitMbps'] == (7 if manual else None)
    assert applied['members'][1]['uploadLimitMbps'] is None
    assert applied['settings']['mtu'] != 1360
    assert view['draftVersion'] != prior_version
    assert any(b['version'] == prior_version for b in saved['_autoBase'])

    # A tab opened before the automatic revision can save without reviving old auto values.
    draft['members'][1]['uploadLimitMbps'] = 9
    payload = {k: v for k, v in draft.items() if k in model.DOC_KEYS}
    merged = await api(e, 'PUT', '/api/servers/mesh/draft', {**payload, 'draftVersion': prior_version})
    assert merged['members'][0]['relayMode'] == 'public' and merged['members'][0]['listen']['tcp'] == 52000
    assert merged['members'][1]['uploadLimitMbps'] == 9
