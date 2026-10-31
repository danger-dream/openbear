"""Controller side of gateway proxy trust: upgrade migration and one-click confirmation."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest

from app.instances.contracts import InstanceError
from app.instances.gateway import Gateway


def gateway(status, *, fail_put=False):
    calls = []
    stored = {}
    service = SimpleNamespace(lock=lambda name: stored.setdefault('lock:' + name, asyncio.Lock()), require_controller=lambda: None)
    g = Gateway(service)

    async def call(value, private, method, path, body=None):
        calls.append((method, path, copy.deepcopy(body)))
        if method == 'GET':
            return copy.deepcopy(status)
        if fail_put:
            raise InstanceError('gateway_unreachable', 502)
        normalized = sorted(p if '/' in p else p + ('/128' if ':' in p else '/32') for p in body['trustedProxies'])
        return {'ok': True, 'clientIp': {'source': body['source'], 'trustedProxies': normalized},
                'clientIpObserved': {**(status.get('clientIpObserved') or {}), 'trusted': True}}

    async def no_accounts(result):
        return None

    g.call, g._account_names = call, no_accounts
    return g, calls


def config(observed, client_ip=None):
    return {'enabled': True, 'status': 'connected', 'clientIp': client_ip or {'source': 'auto', 'trustedProxies': []}, 'observed': observed}


LEGACY = {'peer': '10.0.0.5', 'header': 'X-Forwarded-For', 'atMs': 1}
OLD_STATUS = {'ok': True, 'clientIp': {'source': 'auto', 'trustedProxies': []}, 'clientIpObserved': LEGACY}


async def test_upgrade_trusts_the_private_proxy_an_earlier_gateway_used_once():
    g, calls = gateway(OLD_STATUS)
    value = config(None)
    assert await g._refresh(value, {})
    assert calls[-1] == ('PUT', '/control/v1/client-ip', {'source': 'header', 'trustedProxies': ['10.0.0.5']})
    assert value['clientIp'] == {'source': 'header', 'trustedProxies': ['10.0.0.5/32']} and value['status'] == 'connected'
    # The owner may remove it again: later refreshes do not re-add it.
    value['clientIp'] = {'source': 'auto', 'trustedProxies': []}
    status = {**OLD_STATUS}
    g2, calls2 = gateway(status)
    await g2._refresh(value, {})
    assert [c for c in calls2 if c[0] == 'PUT'] == []
    assert 'migratedProxies' not in Gateway.public_config(value, {}) and '_migratedProxies' not in Gateway.public_config(value, {})


async def test_upgrade_uses_the_stored_observation_when_the_new_gateway_has_none_yet():
    # The controller recorded the old gateway's proxy; the reinstalled gateway restarted.
    g, calls = gateway({'ok': True, 'clientIp': {'source': 'header', 'trustedProxies': ['198.51.100.0/24']}, 'clientIpObserved': None})
    value = config({'peer': 'fd00::7', 'header': 'Forwarded', 'atMs': 1})
    await g._refresh(value, {})
    assert calls[-1][2] == {'source': 'header', 'trustedProxies': ['198.51.100.0/24', 'fd00::7']}
    assert value['observed'] is None


@pytest.mark.parametrize('observed, client_ip', [
    ({**LEGACY, 'trusted': False}, None),                     # current gateway: owner must confirm
    ({**LEGACY, 'trusted': True}, None),
    ({'peer': '127.0.0.1', 'header': 'X-Real-IP', 'atMs': 1}, None),  # loopback stays trusted
    ({'peer': '162.158.1.1', 'header': 'CF-Connecting-IP', 'atMs': 1}, None),  # Cloudflare stays trusted
    ({'peer': '203.0.113.5', 'header': 'X-Real-IP', 'atMs': 1}, None),  # never trusted implicitly
    (LEGACY, {'source': 'header', 'trustedProxies': ['10.0.0.0/24']}),  # already covered
    (LEGACY, {'source': 'socket', 'trustedProxies': []}),
    ({'peer': 'not-an-ip', 'header': 'X-Real-IP', 'atMs': 1}, None),
    ({'peer': 7, 'header': 'X-Real-IP', 'atMs': 1}, None),
])
async def test_no_migration_for_confirmed_default_or_untrusted_observations(observed, client_ip):
    g, calls = gateway({'ok': True, 'clientIp': client_ip or {'source': 'auto', 'trustedProxies': []}, 'clientIpObserved': observed})
    value = config(None, client_ip)
    await g._refresh(value, {})
    assert [c for c in calls if c[0] == 'PUT'] == []


async def test_failed_migration_keeps_gateway_connected_and_retries():
    g, calls = gateway(OLD_STATUS, fail_put=True)
    value = config(None)
    assert await g._refresh(value, {})
    assert value['status'] == 'connected' and value['clientIp'] == {'source': 'auto', 'trustedProxies': []}
    g2, calls2 = gateway(OLD_STATUS)
    await g2._refresh(value, {})
    assert calls2[-1][2] == {'source': 'header', 'trustedProxies': ['10.0.0.5']}


async def test_confirming_a_proxy_stores_the_refreshed_observation():
    status = {'ok': True, 'clientIp': {'source': 'auto', 'trustedProxies': []}, 'clientIpObserved': {**LEGACY, 'trusted': False}}
    g, calls = gateway(status)
    saved = {}

    async def load():
        return config({**LEGACY, 'trusted': False}), {}

    async def store(value, private):
        saved.update(value)

    g.config, g.store = load, store
    result = await g.set_client_ip({'trustedProxies': ['10.0.0.5']})
    assert calls[-1] == ('PUT', '/control/v1/client-ip', {'source': 'header', 'trustedProxies': ['10.0.0.5']})
    assert result['observed']['trusted'] is True and saved['observed']['trusted'] is True
    assert result['clientIp'] == {'source': 'header', 'trustedProxies': ['10.0.0.5/32']}
