import asyncio

import pytest

from app.ddns.model import DdnsError
from app.webhooks.repository import many, one
from tests.ddns import fakeprovider
from tests.ddns.harness import OWNER, add_account, add_node, base_env, env, url_record  # noqa: F401


@pytest.mark.parametrize('kind', ['fake', 'fakepush', 'fakezone'])
async def test_account_delete_drains_inflight_writes_without_deleting_remote(env, monkeypatch, kind):
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e, kind=kind)
    rids = [(await url_record(e, sid, account, name=f'{name}.example.com'))['records'][0]['id'] for name in ('one', 'two')]
    for rid in rids:
        await e.ddns._set(rid, desired='198.51.100.8', changed_ms=e.clock[0] + 1)
    entered, release = asyncio.Event(), asyncio.Event()
    original = e.ddns._call

    async def call(aid, fn, *args):
        if aid == account['id'] and fn.__name__ in ('records', 'update') and not release.is_set():
            entered.set()
            await asyncio.wait_for(release.wait(), 5)
        return await original(aid, fn, *args)

    monkeypatch.setattr(e.ddns, '_call', call)
    syncs = [e.ddns.kick(rid, 'manual') for rid in rids]
    await asyncio.wait_for(entered.wait(), 5)
    deletion = asyncio.create_task(e.ddns.delete_account(OWNER, account['id'], force=True))
    await asyncio.sleep(0)
    assert not deletion.done()
    for rid in rids:
        e.ddns.kick(rid, 'config')
    release.set()
    results = await asyncio.wait_for(asyncio.gather(*syncs, deletion), 5)
    assert results[-1] == {'ok': True, 'deleted': 2}
    provider = fakeprovider.store('tok')
    after = list(provider.writes())
    await asyncio.gather(*(e.ddns.kick(rid, 'manual') for rid in rids))
    await e.ddns.idle()
    assert provider.writes() == after
    assert all(op != 'delete' for op, _, _ in after), 'removing an account only stops management'
    assert not await many(e.db.conn, 'SELECT * FROM ddns_records')
    assert not await many(e.db.conn, 'SELECT * FROM ddns_events')
    assert not await one(e.db.conn, 'SELECT * FROM ddns_accounts WHERE id=?', (account['id'],))
    assert all(rid not in cache for rid in rids for cache in (e.ddns.held, e.ddns.writes, e.ddns.problems, e.ddns.again))


async def test_create_cannot_commit_using_an_account_deleted_during_preview(env, monkeypatch):
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    entered, release = asyncio.Event(), asyncio.Event()
    original = e.ddns._remote

    async def remote(*args, **kwargs):
        result = await original(*args, **kwargs)
        entered.set()
        await asyncio.wait_for(release.wait(), 5)
        return result

    monkeypatch.setattr(e.ddns, '_remote', remote)
    creation = asyncio.create_task(url_record(e, sid, account))
    await asyncio.wait_for(entered.wait(), 5)
    assert await e.ddns.delete_account(OWNER, account['id'], force=True) == {'ok': True, 'deleted': 0}
    release.set()
    result = await asyncio.wait_for(creation, 5)
    assert result['records'] == [] and result['skipped'][0]['error'] == 'account_not_found'
    assert not await many(e.db.conn, 'SELECT * FROM ddns_records')
    assert fakeprovider.store('tok').writes() == []


async def test_cancelled_account_delete_releases_locks_and_preserves_management(env):
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    rid = (await url_record(e, sid, account))['records'][0]['id']
    with pytest.raises(DdnsError, match='account_in_use'):
        await e.ddns.delete_account(OWNER, account['id'])
    with pytest.raises(DdnsError, match='account_not_found'):
        await e.ddns.delete_account(OWNER + 1, account['id'], force=True)
    lock = e.ddns._record_lock(rid)
    async with lock:
        deletion = asyncio.create_task(e.ddns.delete_account(OWNER, account['id'], force=True))
        for _ in range(100):
            if e.ddns._account_lock(account['id']).locked():
                break
            await asyncio.sleep(.001)
        deletion.cancel()
        with pytest.raises(asyncio.CancelledError):
            await deletion
    await e.ddns._set(rid, desired='198.51.100.9', changed_ms=e.clock[0] + 1)
    await asyncio.wait_for(e.ddns.kick(rid, 'manual'), 5)
    assert fakeprovider.store('tok').values('home.example.com') == ['198.51.100.9']
    assert await e.ddns.delete_account(OWNER, account['id'], force=True) == {'ok': True, 'deleted': 1}


async def test_account_lock_does_not_serialize_record_sync(env, monkeypatch):
    e = env
    sid = await add_node(e)
    e.nodes.connect(sid)
    account = await add_account(e)
    rids = [(await url_record(e, sid, account, name=f'{name}.example.com'))['records'][0]['id'] for name in ('one', 'two')]
    entered, release, seen = asyncio.Event(), asyncio.Event(), set()
    original = e.ddns._sync_locked

    async def sync(rid, reason):
        seen.add(rid)
        if len(seen) == 2:
            entered.set()
        await asyncio.wait_for(release.wait(), 5)
        return await original(rid, reason)

    monkeypatch.setattr(e.ddns, '_sync_locked', sync)
    jobs = [e.ddns.kick(rid, 'manual') for rid in rids]
    try:
        await asyncio.wait_for(entered.wait(), 5)
    finally:
        release.set()
    await asyncio.gather(*jobs)
