from __future__ import annotations

import asyncio

import pytest

from app.servers import mesh_apply


class Delivery:
    """deliver/uninstall for mesh_apply.run: each send waits until released, and the log keeps the order."""

    def __init__(self, fail=()):
        self.log, self.running, self.peak, self.fail = [], set(), 0, set(fail)
        self.release = asyncio.Event()

    async def send(self, sid, kind):
        self.log.append(('start', sid))
        self.running.add(sid)
        self.peak = max(self.peak, len(self.running))
        await self.release.wait()
        await asyncio.sleep(0)
        self.running.discard(sid)
        self.log.append(('end', sid))
        if sid in self.fail:
            return 'failed', f'{kind} failed'
        return 'done', ''

    async def deliver(self, sid, config):
        return await self.send(sid, 'apply')

    async def uninstall(self, sid):
        return await self.send(sid, 'uninstall')


def started(log):
    return [sid for what, sid in log if what == 'start']


async def drive(rollout, configs, d):
    task = asyncio.create_task(mesh_apply.run(rollout, configs, d.deliver, d.uninstall))
    for _ in range(50):
        await asyncio.sleep(0)
    return task


def test_order_puts_relays_in_the_first_wave_and_own_changes_first():
    previous = {'members': [{'serverId': 'm1', 'role': 'member', 'removing': False, 'x': 1},
                            {'serverId': 'm2', 'role': 'member', 'removing': False, 'x': 1}]}
    doc = {'members': [{'serverId': 'm1', 'role': 'member', 'removing': False, 'x': 1},
                       {'serverId': 'r1', 'role': 'relay', 'removing': False},
                       {'serverId': 'm2', 'role': 'member', 'removing': False, 'x': 2},
                       {'serverId': 'gone', 'role': 'member', 'removing': True}]}
    assert mesh_apply.order(previous, doc, {'m1', 'm2', 'r1', 'gone'}) == [['r1'], ['m2', 'm1']]
    rollout = mesh_apply.Rollout(3, [['r1'], ['m2', 'm1']], ['gone'])
    assert [p['serverId'] for p in rollout.progress] == ['r1', 'm2', 'm1', 'gone'] and rollout.targets == ['r1', 'm2', 'm1']


async def test_each_wave_goes_out_at_once_and_the_next_waits_for_it():
    d = Delivery()
    rollout = mesh_apply.Rollout(1, [['r1', 'r2'], ['m1', 'm2', 'm3']], ['gone'])
    task = await drive(rollout, {sid: {} for sid in ('r1', 'r2', 'm1', 'm2', 'm3')}, d)
    # Both relays are under way together; no member before they are done.
    assert started(d.log) == ['r1', 'r2']
    assert [p['state'] for p in rollout.progress] == ['applying', 'applying', 'pending', 'pending', 'pending', 'pending']
    d.release.set()
    assert await task == 'applied'
    assert started(d.log) == ['r1', 'r2', 'm1', 'm2', 'm3', 'gone']
    relays_done = max(i for i, (what, sid) in enumerate(d.log) if what == 'end' and sid in ('r1', 'r2'))
    assert min(i for i, (what, sid) in enumerate(d.log) if what == 'start' and sid.startswith('m')) > relays_done
    members_done = max(i for i, (what, sid) in enumerate(d.log) if what == 'end' and sid.startswith('m'))
    assert d.log.index(('start', 'gone')) > members_done
    assert d.peak == 3 and rollout.running is False


async def test_at_most_concurrency_nodes_are_under_way(monkeypatch):
    monkeypatch.setattr(mesh_apply, 'CONCURRENCY', 2)
    d = Delivery()
    rollout = mesh_apply.Rollout(1, [[], ['m1', 'm2', 'm3', 'm4', 'm5']], [])
    task = await drive(rollout, {f'm{i}': {} for i in range(1, 6)}, d)
    assert started(d.log) == ['m1', 'm2']
    d.release.set()
    assert await task == 'applied' and d.peak == 2


async def test_after_a_failure_nothing_new_starts_and_those_under_way_finish(monkeypatch):
    monkeypatch.setattr(mesh_apply, 'CONCURRENCY', 2)
    d = Delivery(fail={'m1'})
    rollout = mesh_apply.Rollout(1, [['r1'], ['m1', 'm2', 'm3', 'm4']], ['gone'])
    d.release.set()
    outcome = await mesh_apply.run(rollout, {sid: {} for sid in ('r1', 'm1', 'm2', 'm3', 'm4')}, d.deliver, d.uninstall)
    assert outcome == 'failed'
    states = {p['serverId']: (p['state'], p['error']) for p in rollout.progress}
    # m2 was sent together with m1; m3, m4 and the uninstall never start.
    assert states == {'r1': ('done', ''), 'm1': ('failed', 'apply failed'), 'm2': ('done', ''),
                      'm3': ('pending', 'stopped'), 'm4': ('pending', 'stopped'), 'gone': ('pending', 'stopped')}
    assert started(d.log) == ['r1', 'm1', 'm2']


async def test_a_failed_relay_keeps_every_member_on_its_config():
    d = Delivery(fail={'r2'})
    rollout = mesh_apply.Rollout(1, [['r1', 'r2'], ['m1', 'm2']], [])
    d.release.set()
    assert await mesh_apply.run(rollout, {sid: {} for sid in ('r1', 'r2', 'm1', 'm2')}, d.deliver, d.uninstall) == 'failed'
    assert started(d.log) == ['r1', 'r2']
    assert [(p['state'], p['error']) for p in rollout.progress[2:]] == [('pending', 'stopped'), ('pending', 'stopped')]


async def test_an_error_in_a_delivery_is_raised_once_the_others_have_finished():
    d = Delivery()
    finished = []

    async def deliver(sid, config):
        if sid == 'm1':
            raise RuntimeError('boom')
        result = await d.send(sid, 'apply')
        finished.append(sid)
        return result
    rollout = mesh_apply.Rollout(1, [[], ['m1', 'm2']], [])
    d.release.set()
    with pytest.raises(RuntimeError, match='boom'):
        await mesh_apply.run(rollout, {'m1': {}, 'm2': {}}, deliver, d.uninstall)
    assert finished == ['m2'] and rollout.running is False
