import asyncio
import json

import pytest

from app.instances import network
from app.instances.contracts import InstanceError
from tests.test_instance_hardening import KEYS, edit, node_host
from tests.test_instances import create, env, form  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401


@pytest.mark.parametrize('status,proof', [('ready', {}), ('needs_verification', {'lastSeenAtMs': 123})])
async def test_unknown_delivered_image_preserves_keys_and_pending_rotation(env, monkeypatch, status, proof):
    item, old = await create(env)
    await edit(env, item['id'], image='', **proof)
    async with env.db.write_transaction() as conn:
        await conn.execute('UPDATE managed_instances SET status=? WHERE id=?', (status, item['id']))
    calls = []

    async def run(target, command, **kwargs):
        calls.append(command)
        return {'ok': True, 'stdout': '', 'stderr': '', 'exitCode': 0}

    monkeypatch.setattr(env.instances.servers, 'run', run)
    result = await env.instances.rotate_keys(123, item['id'])
    assert result['rotated'] is False and result['error'] == 'instance_image_unknown'
    row = await env.instances.row(123, item['id'])
    protected = env.instances.vault.open(row['protected'])
    assert all(protected[key] == old[key] for key in KEYS)
    assert protected['rotation'] and json.loads(row['public_json'])['rotation']['status'] == 'pending'
    assert all(command.startswith('cat ') for command in calls)


async def test_first_interrupted_deployment_without_delivery_can_rotate(env, monkeypatch):
    item, _ = await create(env)
    await edit(env, item['id'], image='', deploymentAccepted=False)
    async with env.db.write_transaction() as conn:
        await conn.execute("UPDATE managed_instances SET status='needs_verification' WHERE id=?", (item['id'],))

    async def run(*args, **kwargs):
        return {'ok': True, 'stdout': '', 'stderr': '', 'exitCode': 0}

    monkeypatch.setattr(env.instances.servers, 'run', run)
    assert (await env.instances.rotate_keys(123, item['id']))['rotated'] is True


@pytest.mark.parametrize('archived', [False, True])
async def test_workspace_rejects_ancestors_descendants_and_local_aliases(env, tmp_path, archived):
    home = tmp_path / 'volumes'
    home.mkdir()
    path = home / 'first'
    path.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(path, target_is_directory=True)
    item, _ = await create(env, workspacePath=str(path))
    if archived:
        await env.instances.archive(123, item['id'])
    for candidate in (home, path / 'nested', alias / 'nested'):
        with pytest.raises(InstanceError, match='instance_workspace_in_use'):
            await env.instances.save(123, form(env, workspacePath=str(candidate), account={'username': 'other', 'password': 'fixture-pass'}))
    other = await env.instances.save(123, form(env, workspacePath=str(home / 'second'), account={'username': 'second', 'password': 'fixture-pass'}))
    assert other['item']['workspacePath'] == str(home / 'second')


async def test_rotate_all_persists_busy_intent_then_upkeep_finishes(env, monkeypatch):
    item, old = await create(env)
    busy = True

    async def idle(row):
        return not busy

    async def control(owner):
        return {}

    monkeypatch.setattr(env.instances, '_idle', idle)
    monkeypatch.setattr(env.instances.gateway, 'rotate_control_key', control)
    with pytest.raises(InstanceError, match='operation_running'):
        await env.instances.rotate_keys(123, item['id'])
    result = await env.instances.gateway.rotate_all(123)
    assert result['pendingInstances'] == 1
    row = await env.instances.row(123, item['id'])
    stored = env.instances.vault.open(row['protected'])
    assert stored['rotation'] and all(stored[k] == old[k] for k in KEYS)
    public = json.loads(row['public_json'])
    public['rotation']['nextAtMs'] = 0
    await edit(env, item['id'], rotation=public['rotation'])
    busy = False
    await env.instances.rotation_upkeep()
    after = env.instances.vault.open((await env.instances.row(123, item['id']))['protected'])
    assert 'rotation' not in after and all(after[k] == stored['rotation'][k] for k in KEYS)


async def test_explicit_management_address_wins_over_online_node(env, monkeypatch):
    node_host(env, monkeypatch, calls=[])
    item = {'serverId': 'srv-n', 'port': 18970, 'managementUrl': 'https://explicit.example.test'}
    row = {'owner': 123, 'protected': env.instances.vault.seal({'form': {'managementUrl': item['managementUrl']}})}
    assert await network.route(env.instances, row, item) == {'url': item['managementUrl']}
    row['protected'] = env.instances.vault.seal({'form': {'managementUrl': ''}})
    assert (await network.route(env.instances, row, item))['serverId'] == 'srv-n'


@pytest.mark.parametrize('stage', ['probe', 'install'])
async def test_close_joins_gateway_update_before_closing_resources(env, monkeypatch, stage):
    gateway = env.instances.gateway
    entered, cancelled = asyncio.Event(), asyncio.Event()
    calls = []

    async def pending(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            assert env.instances.http is not None
            cancelled.set()

    async def reachable(*args):
        return True

    monkeypatch.setattr(gateway, '_server_reachable', pending if stage == 'probe' else reachable)
    monkeypatch.setattr(gateway, 'reinstall', pending)
    gateway.update_task = task = asyncio.create_task(gateway._auto_update())
    await asyncio.wait_for(entered.wait(), 5)
    await asyncio.wait_for(env.instances.close(), 5)
    assert task.done() and cancelled.is_set() and env.instances.http is None
    if stage == 'install':
        value, _ = await gateway.config()
        assert value['lastUpdate']['error'] == 'gateway_update_interrupted'
    assert calls == []
