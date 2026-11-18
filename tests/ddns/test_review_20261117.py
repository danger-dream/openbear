"""Read/write interleavings use real adapters with in-memory transports, never a live DNS API."""
import pytest

from app.ddns import model
from app.ddns.service import Ddns
from app.webhooks.repository import one
from tests.ddns.harness import OWNER, alerts, base_env, env, events, record, sync, url_record  # noqa: F401
from tests.ddns.test_fixes_20261112 import NEW, OLD, OTHER, rrset_account

CAS_KINDS = ('rfc2136', 'route53')


def once(wire, change):
    def race(w):
        w.before_write = None
        change(w)
    wire.before_write = race


@pytest.mark.parametrize('kind', CAS_KINDS)
@pytest.mark.parametrize('operation', ['create', 'update'])
async def test_new_sibling_between_read_and_write_requires_consent(env, monkeypatch, kind, operation):
    wire, account, sid = await rrset_account(env, monkeypatch, kind, [] if operation == 'create' else [OLD])
    once(wire, lambda w: setattr(w, 'values', [OLD, OTHER]))
    result = await url_record(env, sid, account, row={'confirmTakeover': True})
    rid = result['records'][0]['id']
    rec = await record(env, rid)
    assert rec['state'] == 'rrset_review' and rec['policy']['rrsetTakeover'] is False
    assert wire.values == [OLD, OTHER] and wire.writes == [] and len(wire.attempts) == 1
    assert rec['failCount'] == 0
    # Stopping management still must not send a delete after a conflict.
    await env.ddns.delete_record(OWNER, rid)
    assert wire.values == [OLD, OTHER] and wire.writes == []


@pytest.mark.parametrize('kind', CAS_KINDS)
@pytest.mark.parametrize('operation', ['create', 'update'])
async def test_safe_single_value_conflict_automatically_reconverges(env, monkeypatch, kind, operation):
    wire, account, sid = await rrset_account(env, monkeypatch, kind, [] if operation == 'create' else [OLD])

    def external(w):
        w.values, w.ttl = [OTHER], 900
    once(wire, external)
    rid = (await url_record(env, sid, account, row={'confirmTakeover': True}))['records'][0]['id']
    rec = await record(env, rid)
    assert rec['state'] == 'ok' and rec['failCount'] == 0
    assert not rec['policy']['rrsetTakeover']
    assert wire.values == [NEW] and wire.ttl == 900  # preserve the refreshed snapshot's default TTL
    assert len(wire.attempts) == 2 and len(wire.writes) == 1


@pytest.mark.parametrize('kind', CAS_KINDS)
async def test_create_conflict_already_at_desired_is_success_without_another_write(env, monkeypatch, kind):
    wire, account, sid = await rrset_account(env, monkeypatch, kind, [])
    once(wire, lambda w: setattr(w, 'values', [NEW]))
    rid = (await url_record(env, sid, account))['records'][0]['id']
    assert (await record(env, rid))['state'] == 'ok'
    assert len(wire.attempts) == 1 and wire.writes == [] and wire.values == [NEW]


@pytest.mark.parametrize('kind', CAS_KINDS)
async def test_continuing_conflicts_are_bounded_then_automatically_retried(env, monkeypatch, kind):
    wire, account, sid = await rrset_account(env, monkeypatch, kind, [OLD])
    wire.before_write = lambda w: setattr(w, 'values', [OTHER if w.values == [OLD] else OLD])
    rid = (await url_record(env, sid, account, row={'confirmTakeover': True}))['records'][0]['id']
    rec = await env.ddns._rec(OWNER, rid)
    assert len(wire.attempts) == 3 and not wire.writes
    assert rec['state'] == 'pending' and rec['error_code'] == 'record_conflict' and rec['next_ms'] > env.clock[0]
    wire.before_write = None
    await sync(env, rid)  # periodic retry, no user confirmation/manual sync required
    assert (await record(env, rid))['state'] == 'ok' and wire.values == [NEW]


@pytest.mark.parametrize('kind', CAS_KINDS)
@pytest.mark.parametrize('change', ['sibling', 'absent', 'unknown'])
async def test_delete_conflict_reselects_without_expanding_ownership(env, monkeypatch, kind, change):
    wire, account, sid = await rrset_account(env, monkeypatch, kind, [OLD])
    rid = (await url_record(env, sid, account, row={'confirmTakeover': True}))['records'][0]['id']
    wire.writes.clear()
    wire.attempts.clear()
    incoming = {'sibling': [NEW, OTHER], 'absent': [], 'unknown': [OTHER]}[change]
    once(wire, lambda w: setattr(w, 'values', list(incoming)))
    if change == 'absent':
        assert (await env.ddns.delete_record(OWNER, rid, delete_remote=True))['deletedRemote']
        assert await one(env.db.conn, 'SELECT 1 FROM ddns_records WHERE id=?', (rid,)) is None
    else:
        with pytest.raises(model.DdnsError) as exc:
            await env.ddns.delete_record(OWNER, rid, delete_remote=True)
        assert exc.value.error == ('rrset_takeover_required' if change == 'sibling' else 'record_ambiguous')
        assert await one(env.db.conn, 'SELECT 1 FROM ddns_records WHERE id=?', (rid,))
    assert wire.values == incoming and wire.writes == [] and len(wire.attempts) == 1


async def test_route53_delete_ttl_only_conflict_automatically_retries(env, monkeypatch):
    wire, account, sid = await rrset_account(env, monkeypatch, 'route53', [OLD])
    rid = (await url_record(env, sid, account, row={'confirmTakeover': True}))['records'][0]['id']
    wire.writes.clear()
    wire.attempts.clear()
    once(wire, lambda w: setattr(w, 'ttl', 900))
    assert (await env.ddns.delete_record(OWNER, rid, delete_remote=True))['deletedRemote']
    assert wire.values == [] and len(wire.attempts) == 2 and len(wire.writes) == 1


@pytest.mark.parametrize('kind', CAS_KINDS)
async def test_explicit_whole_set_permission_still_uses_atomic_snapshot(env, monkeypatch, kind):
    wire, account, sid = await rrset_account(env, monkeypatch, kind, [OLD, OTHER])
    once(wire, lambda w: setattr(w, 'values', [OLD, OTHER, '198.51.100.100']))
    rid = (await url_record(env, sid, account, row={'confirmRrsetTakeover': True}))['records'][0]['id']
    assert (await record(env, rid))['state'] == 'ok' and wire.values == [NEW]
    assert len(wire.attempts) == 2 and len(wire.writes) == 1


async def test_failure_identity_survives_review_and_service_reload_until_synced(env, monkeypatch):
    wire, account, sid = await rrset_account(env, monkeypatch, 'rfc2136', [OLD])
    rid = (await url_record(env, sid, account, row={'confirmTakeover': True}))['records'][0]['id']
    stored_account = await env.ddns._account(OWNER, account['id'])
    # A real failure notification tied to the first failure, not just a synthetic counter.
    for _ in range(3):
        await env.ddns._failed(await env.ddns._rec(OWNER, rid), stored_account, 'network', 'fixture timeout', False)
    failed = await env.ddns._rec(OWNER, rid)
    assert failed['first_fail_ms'] and failed['fail_count'] == 3
    failure_id = f'ddns-fail:{rid}:{failed["first_fail_ms"]}'
    assert any(a['id'] == failure_id for a in await alerts(env))
    wire.values = [NEW, OTHER]
    await sync(env, rid)
    reviewed = await env.ddns._rec(OWNER, rid)
    assert reviewed['state'] == 'rrset_review'
    assert (reviewed['first_fail_ms'], reviewed['fail_count']) == (failed['first_fail_ms'], 3)
    assert 'recover' not in await events(env, rid)
    assert not any(a['kind'] == 'ddns_recover' for a in await alerts(env))
    # Reload the record through a new service, as after a process restart.
    fresh = Ddns(env.servers)
    fresh.now, fresh.jitter, fresh._make = env.ddns.now, env.ddns.jitter, env.ddns._make
    monkeypatch.setattr(fresh, '_call', env.ddns._call)
    await fresh._set(rid, policy={**reviewed['policy'], 'rrsetTakeover': True})
    await fresh._sync(rid, 'manual')
    await fresh._sync(rid, 'check')
    recovered = await fresh._rec(OWNER, rid)
    assert recovered['state'] == 'ok' and wire.values == [NEW]
    assert recovered['first_fail_ms'] == recovered['fail_count'] == 0
    assert (await events(env, rid)).count('recover') == 1
    recoveries = [a for a in await alerts(env) if a['kind'] == 'ddns_recover']
    assert [a['id'] for a in recoveries] == [f'ddns-recover:{rid}:{failed["first_fail_ms"]}']
