"""DDNS B1-B5/R1 regressions: isolated controller DB, synthetic credentials and no DNS/API IO."""
import json
import xml.etree.ElementTree as ET

import dns.message
import dns.name
import dns.rcode
import dns.rdataclass
import dns.rdatatype
import dns.rrset
import httpx
import pytest

from app.ddns import model, providers
from app.ddns.providers.base import ProviderError
from app.ddns.service import Ddns, decode
from app.webhooks.repository import one
from tests.ddns import fakeprovider
from tests.ddns.harness import (  # noqa: F401
    OWNER, add_account, add_node, advance, base_env, env, probe_result, record, sync, url_record,
)
from tests.ddns.test_provider_route53 import CHANGE, NS, XMLNS, rrset

NAME, ZONE = 'home.example.com', 'example.com'
OLD, NEW, OTHER = '198.51.100.1', '198.51.100.7', '198.51.100.99'
RRSET_KINDS = ('huawei', 'route53', 'rfc2136')
CREDS = {
    'huawei': {'access_key_id': 'fixture-ak', 'secret_access_key': 'fixture-sk'},
    'route53': {'access_key_id': 'fixture-ak', 'secret_access_key': 'fixture-sk'},
    'rfc2136': {'server': '192.0.2.53', 'key_name': 'fixture.', 'secret': 'dGVzdA=='},
}


class RrsetWire:
    """Exercise real record decoding and CRUD; replace only provider transport with memory."""
    def __init__(self, kind, values):
        self.kind, self.values, self.ttl, self.writes = kind, list(values), 300, []
        self.before_write, self.attempts = None, []

    def race(self):
        self.attempts.append(list(self.values))
        if self.before_write:
            self.before_write(self)

    def make(self, creds, settings):
        p = providers.get(self.kind)(creds, settings, client=object())
        p._zone_ids = {ZONE: 'z'}

        async def check():
            return {'zones': [ZONE], 'writable': True}
        p.check = check

        async def call(method, path, query=None, payload=None, body=b''):
            if self.kind == 'huawei':
                item = {'id': 'rr', 'name': NAME + '.', 'type': 'A', 'records': list(self.values), 'ttl': self.ttl, 'line': 'default_view'}
                if method == 'GET':
                    return {'recordsets': [item] if self.values else []}
                self.writes.append((method, payload))
                self.values = list(payload['records']) if payload else []
                self.ttl = (payload or {}).get('ttl', self.ttl)
                return {**item, 'records': list(self.values), 'ttl': self.ttl}
            if method == 'GET':
                rows = rrset(NAME + '.', 'A', self.values, self.ttl) if self.values else ''
                return ET.fromstring(f'<ListResourceRecordSetsResponse xmlns="{XMLNS}"><ResourceRecordSets>{rows}</ResourceRecordSets></ListResourceRecordSetsResponse>')
            self.race()
            root = ET.fromstring(body)
            values, ttl, actions = list(self.values), self.ttl, []
            for change in root.findall(f'{NS}ChangeBatch/{NS}Changes/{NS}Change'):
                action = change.findtext(f'{NS}Action')
                incoming = [v.text for v in change.iter(f'{NS}Value')]
                incoming_ttl = int(change.findtext(f'.//{NS}TTL'))
                actions.append(action)
                if action == 'DELETE':
                    if set(values) != set(incoming) or ttl != incoming_ttl:
                        raise ProviderError('record_conflict', 'old RRset changed')
                    values = []
                else:
                    assert action == 'CREATE', 'never silently implement UPSERT'
                    if values:
                        raise ProviderError('record_conflict', 'RRset already exists')
                    values, ttl = incoming, incoming_ttl
            # Route 53 validates the entire batch before making any change visible.
            self.values, self.ttl = values, ttl
            self.writes.append((tuple(actions), body))
            return ET.fromstring(CHANGE)

        async def query(name, rtype, any_rcode=False):
            msg = dns.message.make_response(dns.message.make_query(name, rtype))
            if self.values:
                # Echo the question's absolute owner name; from_text_list(str)
                # otherwise treats a name without a trailing dot as relative.
                msg.answer.append(dns.rrset.from_text_list(msg.question[0].name, self.ttl, 'IN', rtype, self.values))
            # Match a received DNS response: parsing builds the RRset lookup index.
            # Appending to answer alone leaves make_response()'s index empty, so
            # the real adapter's get_rrset() would incorrectly see an absent set.
            return dns.message.from_wire(msg.to_wire())

        async def send(msg, any_rcode=False):
            self.race()
            response = dns.message.make_response(msg)
            expected = {r.to_text() for item in msg.prerequisite if item.deleting is None for r in item}
            # Constructed messages carry NONE in rdclass; wire-decoded messages in deleting.
            absent = any(item.rdclass == dns.rdataclass.NONE or item.deleting == dns.rdataclass.NONE for item in msg.prerequisite)
            assert absent or expected, 'every write needs an atomic prerequisite'
            if absent and self.values:
                response.set_rcode(dns.rcode.YXRRSET)
                return response
            if expected and expected != set(self.values):
                response.set_rcode(dns.rcode.NXRRSET)
                return response
            self.writes.append(('UPDATE', msg))
            for item in msg.update:
                if item.deleting == dns.rdataclass.ANY:
                    self.values = []
                else:
                    self.values += [r.to_text() for r in item]
                    self.ttl = item.ttl
            return dns.message.make_response(msg)

        p.call, p.query, p.send = call, query, send
        return p


async def rrset_account(e, monkeypatch, kind, values):
    wire = RrsetWire(kind, values)
    original = e.ddns._make
    monkeypatch.setattr(e.ddns, '_make', lambda k, c, s: wire.make(c, s) if k == kind else original(k, c, s))
    account = (await e.ddns.create_account(OWNER, {'type': kind, 'alias': kind, 'credentials': CREDS[kind]}))['account']
    sid = await add_node(e)
    e.nodes.connect(sid)
    e.nodes.probe[sid] = probe_result(url=NEW)
    return wire, account, sid


@pytest.mark.parametrize('values', [[], [OLD], [OLD, OTHER]])
async def test_rfc2136_memory_response_decodes_actual_rrset(values):
    wire = RrsetWire('rfc2136', values)
    p = wire.make(CREDS['rfc2136'], {})
    response = await p.query(NAME, 'A')
    answer = response.get_rrset(response.answer, dns.name.from_text(NAME), dns.rdataclass.IN, dns.rdatatype.A)
    assert response.question[0].name.is_absolute()
    if values:
        assert answer is not None and answer.name.is_absolute() and answer.name == response.question[0].name
    assert ([] if answer is None else sorted(r.to_text() for r in answer)) == sorted(values)
    records = await p.records(ZONE, NAME, 'A')
    assert [r.value for r in records] == ([','.join(sorted(values))] if values else [])
    assert [r.ttl for r in records] == ([wire.ttl] if values else [])
    assert wire.writes == []


def test_rrset_inventory_and_legacy_policy_are_explicit():
    assert {k for k, p in providers.REGISTRY.items() if p.caps.rrset} == set(RRSET_KINDS)
    for kind in ('desec', 'gcore', 'ns1', 'hetzner', 'godaddy', 'dynadot', 'spaceship'):
        assert not providers.get(kind).caps.rrset, kind  # existing per-address preservation remains intact
    assert model.clean_policy({'multi': 'all'})['rrsetTakeover'] is False
    with pytest.raises(model.DdnsError):
        model.clean_policy({'rrsetTakeover': 'true'})
    assert providers.get('rfc2136')({**CREDS['rfc2136'], 'zones': ZONE}, client=object()).caps.rrset


@pytest.mark.parametrize('kind', RRSET_KINDS)
async def test_rrset_preview_create_requires_distinct_consent_then_replaces(env, monkeypatch, kind):
    e = env
    wire, account, sid = await rrset_account(e, monkeypatch, kind, [OLD, OTHER])
    preview = await e.ddns.preview(OWNER, {'serverId': sid, 'source': {'method': 'url'}, 'names': [NAME]})
    row = preview['rows'][0]
    assert row['rrset'] and row['requiresRrsetTakeover'] and row['action'] == 'takeover'
    assert set(row['remoteValues']) == {OLD, OTHER} and '整体替换将移除其他地址' in row['warning']
    for policy in ({}, {'multi': 'all'}):
        result = await url_record(e, sid, account, row={'confirmTakeover': True}, policy=policy)
        assert result['records'] == [] and result['skipped'][0]['reason'] == 'rrset_takeover_required'
        assert wire.writes == [] and wire.values == [OLD, OTHER]
    result = await url_record(e, sid, account, row={'confirmRrsetTakeover': True})
    rid = result['records'][0]['id']
    assert wire.values == [NEW] and (await record(e, rid))['policy']['rrsetTakeover'] is True
    assert (await record(e, rid))['state'] == 'ok'
    wire.values = [NEW, OTHER]
    await e.ddns.delete_record(OWNER, rid, delete_remote=True)
    assert wire.values == []  # explicit whole-set consent also covers deletion


@pytest.mark.parametrize('kind', RRSET_KINDS)
@pytest.mark.parametrize('multi', ['one', 'all'])
async def test_rrset_existing_single_becomes_multi_blocks_sync_edit_delete_until_review(env, monkeypatch, kind, multi):
    e = env
    wire, account, sid = await rrset_account(e, monkeypatch, kind, [OLD])
    rid = (await url_record(e, sid, account, row={'confirmTakeover': True}, policy={'multi': multi}))['records'][0]['id']
    assert wire.values == [NEW]
    # Simulate legacy stored JSON with no new authorization and an external second address.
    await e.ddns._set(rid, policy={'multi': multi, 'allowPrivate': False, 'onDrift': 'revert'})
    wire.values = [NEW, OTHER]
    wire.writes.clear()
    advance(e, 600)
    await sync(e, rid, 'manual')
    rec = await record(e, rid)
    assert rec['state'] == 'rrset_review' and '整体替换将移除其他地址' in rec['stateText']
    assert set(rec['remote'].split(',')) == {NEW, OTHER}
    await e.ddns.update_record(OWNER, rid, {'ttl': 600})
    await e.ddns.idle()
    with pytest.raises(model.DdnsError) as err:
        await e.ddns.delete_record(OWNER, rid, delete_remote=True)
    assert err.value.error == 'rrset_takeover_required' and '整体替换' in err.value.message
    assert wire.writes == [] and wire.values == [NEW, OTHER]
    if kind == 'huawei':
        with pytest.raises(model.DdnsError) as err:
            await e.ddns.update_record(OWNER, rid, {'options': {'line': 'Dianxin'}})
        assert err.value.error == 'rrset_takeover_required' and wire.writes == []
        assert (await record(e, rid))['options'] == {}
    await e.ddns.update_record(OWNER, rid, {'policy': {'rrsetTakeover': True}})
    await e.ddns.idle()
    assert wire.values == [NEW] and wire.ttl == 600
    assert (await record(e, rid))['state'] == 'ok'


@pytest.mark.parametrize('kind', RRSET_KINDS)
async def test_rrset_discovered_after_create_preview_is_not_written(env, monkeypatch, kind):
    e = env
    wire, account, sid = await rrset_account(e, monkeypatch, kind, [])
    preview = await e.ddns.preview(OWNER, {'serverId': sid, 'source': {'method': 'url'}, 'names': [NAME]})
    assert preview['rows'][0]['action'] == 'new'
    wire.values = [OLD, OTHER]
    result = await url_record(e, sid, account)
    assert result['skipped'][0]['reason'] == 'rrset_takeover_required' and wire.writes == []


@pytest.mark.parametrize('kind', RRSET_KINDS)
async def test_rrset_stop_management_does_not_delete_multivalue(env, monkeypatch, kind):
    e = env
    wire, account, sid = await rrset_account(e, monkeypatch, kind, [OLD])
    rid = (await url_record(e, sid, account, row={'confirmTakeover': True}))['records'][0]['id']
    wire.values = [NEW, OTHER]
    wire.writes.clear()
    await e.ddns.delete_record(OWNER, rid)
    assert wire.values == [NEW, OTHER] and wire.writes == []


async def test_spaceship_cleanup_survives_reloaded_record_and_preserves_unrelated_values(env, monkeypatch):
    e = env
    values, calls, fail_delete = [OLD, OTHER], [], [True]

    def transport(req):
        calls.append((req.method, json.loads(req.content) if req.content else None))
        if req.url.path == '/api/v1/domains':
            return httpx.Response(200, json={'items': [{'name': ZONE}], 'total': 1})
        if req.method == 'GET':
            return httpx.Response(200, json={'items': [{'name': 'home', 'type': 'A', 'address': v, 'ttl': 300} for v in values], 'total': len(values)})
        body = json.loads(req.content)
        if req.method == 'PUT':
            v = body['items'][0]['address']
            if v not in values:
                values.append(v)
        else:
            assert req.method == 'DELETE' and body[0]['address'] != OTHER
            if fail_delete[0]:
                return httpx.Response(503, json={'detail': 'retry'})
            if body[0]['address'] in values:
                values.remove(body[0]['address'])
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        original = e.ddns._make
        monkeypatch.setattr(e.ddns, '_make', lambda k, c, s: providers.get(k)(c, s, client=client) if k == 'spaceship' else original(k, c, s))
        account = (await e.ddns.create_account(OWNER, {'type': 'spaceship', 'credentials': {'key': 'fixture', 'secret': 'fixture'}}))['account']
        sid = await add_node(e)
        e.nodes.connect(sid)
        rid = (await url_record(e, sid, account, row={'confirmTakeover': True}))['records'][0]['id']
        rec = decode(await one(e.db.conn, 'SELECT * FROM ddns_records WHERE id=?', (rid,)))
        assert rec['state'] == 'pending' and rec['synced_ms'] == 0 and rec['fail_count'] == 1
        assert rec['pending_cleanup']['old']['value'] == OLD and rec['pending_cleanup']['replacement'] == NEW
        assert set(values) == {OLD, NEW, OTHER}
        # A second failed cleanup must retain the journal; not GET-only apparent success.
        await sync(e, rid)
        assert (await record(e, rid))['failCount'] == 2
        fail_delete[0] = False
        calls.clear()
        # New service object, same isolated DB: the cleanup must not depend on a live adapter/task.
        fresh = Ddns(e.servers)
        fresh.now, fresh.jitter = e.ddns.now, e.ddns.jitter
        fresh._make = e.ddns._make
        monkeypatch.setattr(fresh, '_call', e.ddns._call)
        await fresh._sync(rid, 'manual')
        assert set(values) == {NEW, OTHER}
        assert [method for method, _ in calls] == ['GET', 'DELETE', 'GET']
        stored = await e.ddns._rec(OWNER, rid)
        assert stored['pending_cleanup'] is None and stored['state'] == 'ok' and stored['synced_ms']


@pytest.mark.parametrize('shared,tool', [(False, False), (False, True), (True, False), (True, True)])
async def test_mapped_credentials_rotation_is_effective_and_shared_isolated(env, shared, tool):
    e = env
    account = await add_account(e, token='old-fixture')
    secret_id = account['secret']['id']
    await e.ddns._memory().tool_call('secret', {'action': 'set', 'id': secret_id, 'kvJson': json.dumps([{'key': 'key', 'value': 'old-fixture'}])})
    await e.ddns._set_account(account['id'], field_map_json=json.dumps({'token': 'key'}))
    sibling = None
    if shared:
        sibling = (await e.ddns.create_account(OWNER, {'type': 'fake', 'alias': 'sibling', 'secretRef': {'secretId': secret_id, 'map': {'token': 'key'}}}))['account']
    await e.ddns.update_account(OWNER, account['id'], {'credentials': {'token': 'new-fixture'}}, tool=tool)
    actual = await e.ddns._account(OWNER, account['id'])
    assert await e.ddns._creds(actual) == {'token': 'new-fixture'}
    assert actual['field_map'] == {'token': 'token'}
    assert (actual['secret_id'] != secret_id) is shared
    if sibling:
        assert await e.ddns._creds(await e.ddns._account(OWNER, sibling['id'])) == {'token': 'old-fixture'}


@pytest.mark.parametrize('secret_slot', ['private', 'key'])
async def test_partial_mapped_rotation_preserves_other_field_mapping(env, monkeypatch, secret_slot):
    e = env

    async def no_network(*args, **kwargs):
        return [], None
    monkeypatch.setattr(e.ddns, '_refresh_zones', no_network)
    account = (await e.ddns.create_account(OWNER, {'type': 'spaceship', 'credentials': {'key': 'old-key', 'secret': 'old-secret'}}))['account']
    secret_id = account['secret']['id']
    await e.ddns._memory().tool_call('secret', {'action': 'set', 'id': secret_id, 'kvJson': json.dumps([
        {'key': 'access', 'value': 'old-key'}, {'key': secret_slot, 'value': 'old-secret'}])})
    await e.ddns._set_account(account['id'], field_map_json=json.dumps({'key': 'access', 'secret': secret_slot}))
    await e.ddns.update_account(OWNER, account['id'], {'credentials': {'key': 'new-key'}})
    actual = await e.ddns._account(OWNER, account['id'])
    assert actual['field_map'] == {'key': 'key_2' if secret_slot == 'key' else 'key', 'secret': secret_slot}
    assert await e.ddns._creds(actual) == {'key': 'new-key', 'secret': 'old-secret'}


async def test_import_preview_explains_rrset_and_old_confirm_does_not_authorize(env, monkeypatch):
    e = env
    wire, _, sid = await rrset_account(e, monkeypatch, 'huawei', [OLD, OTHER])
    source = '''dnsconf:
  - ipv4: {enable: true, gettype: url, domains: [home:example.com]}
    dns: {name: huaweicloud, id: fixture-ak, secret: fixture-sk}
'''
    preview = await e.ddns.import_preview(OWNER, {'yaml': source, 'serverId': sid})
    assert preview['records'][0]['requiresRrsetTakeover']
    assert set(preview['records'][0]['remoteValues']) == {OLD, OTHER}
    result = await e.ddns.import_apply(OWNER, {'yaml': source, 'serverId': sid, 'confirmTakeover': True})
    assert not result['records'] and result['skipped'][0]['reason'] == 'rrset_takeover_required'
    assert wire.writes == [] and wire.values == [OLD, OTHER]


async def test_preview_zone_survives_create_and_rejects_forged_zone(env):
    e = env
    account = await add_account(e, kind='fakepush')
    sid = await add_node(e)
    e.nodes.connect(sid)
    e.nodes.probe[sid] = probe_result(url=NEW)
    preview = await e.ddns.preview(OWNER, {'serverId': sid, 'source': {'method': 'url'}, 'names': ['home:example.co.uk']})
    row = preview['rows'][0]
    assert (row['name'], row['zone']) == ('home.example.co.uk', 'example.co.uk')
    result = await e.ddns.create_records(OWNER, {'serverId': sid, 'source': {'method': 'url'}, 'rows': [
        {k: row[k] for k in ('name', 'type', 'zone', 'accountId')},
        {'name': 'bad.example.co.uk', 'zone': 'other.org', 'type': 'A', 'accountId': account['id']},
    ]})
    await e.ddns.idle()
    assert result['records'][0]['zone'] == 'example.co.uk'
    assert result['skipped'][0]['error'] == 'zone_not_found'


async def test_account_ttl_inherited_on_takeover_manual_and_retry_not_bulk_rewrite(env):
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    await e.ddns.update_account(OWNER, account['id'], {'settings': {'ttl': 600}})
    api = fakeprovider.store('tok')
    pid = api.add(NAME, 'A', NEW, ttl=300)
    rid = (await url_record(e, sid, account))['records'][0]['id']
    assert api.records[pid].ttl == 600
    writes = len(api.writes())
    advance(e, 1)
    await e.ddns.update_account(OWNER, account['id'], {'settings': {'ttl': 900}})
    await e.ddns.idle()
    assert len(api.writes()) == writes  # no new account-wide immediate rewrite policy
    await sync(e, rid)  # the next regular check sees the account change; no immediate bulk kick
    assert api.records[pid].ttl == 900
    api.records[pid].ttl = 300
    await sync(e, rid, 'manual')
    assert api.records[pid].ttl == 900
    await e.ddns.update_record(OWNER, rid, {'ttl': 1200})
    await e.ddns.idle()
    assert api.records[pid].ttl == 1200
    api.fail('network')
    advance(e, 1)
    await e.ddns.update_record(OWNER, rid, {'ttl': None})
    await e.ddns.idle()
    assert (await record(e, rid))['state'] == 'pending'
    await sync(e, rid)
    assert api.records[pid].ttl == 900 and (await record(e, rid))['state'] == 'ok'


async def test_effective_ttl_special_normalizations_match_writes():
    cf = providers.get('cloudflare')({'token': 'fixture'}, {'ttl': 1}, client=object())
    assert await cf.effective_ttl(ZONE, None) == 1
    cloudns = providers.get('cloudns')({'auth_id': '1', 'password': 'fixture'}, {'ttl': 600}, client=object())
    assert await cloudns.effective_ttl(ZONE, None) == cloudns._ttl(None) == 900
    desec = providers.get('desec')({'token': 'fixture'}, {'ttl': 600}, client=object())
    desec._min_ttl[ZONE] = 3600
    assert await desec.effective_ttl(ZONE, None) == await desec._ttl(ZONE, None) == 3600
