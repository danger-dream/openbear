"""Checks the controller runs on its nodes by itself and keeps the newest result of in each node
record (the unlock and the route check). Each has a switch in the servers' settings, on by default:
while it is off nothing runs and the pages are not shown its results. Neither is for a server in
mainland China: nothing runs there and nothing is shown for it. Nor for a front (前置机), which is
to reach nothing beyond its exits. IP 质量's (ipcheck) are 评测 runs, app/servers/ip_quality.py: its
switch stops only the runs started by themselves."""
from __future__ import annotations

import asyncio
import json
import logging
import os

from app.instances.contracts import InstanceError
from app.servers import front as fronts
from app.webhooks.repository import many, one

log = logging.getLogger(__name__)
SWITCHES = ('unlock', 'route', 'ipcheck')
DOMESTIC = 'CN'


def server_region(prof, stored):
    """The server's country: the one the user set, else where GeoIP puts its address."""
    return str((prof or {}).get('region') or ((stored or {}).get('geo') or {}).get('country') or '').upper()


def clean_switches(value, prior=None):
    """{unlock, route, ipcheck}: booleans, on unless turned off, unchanged where value says nothing."""
    prior = prior if isinstance(prior, dict) else {}
    if not isinstance(value, dict) or set(value) - set(SWITCHES) or any(not isinstance(v, bool) for v in value.values()):
        raise InstanceError('invalid_server_settings')
    return {k: value.get(k, prior.get(k) is not False) for k in SWITCHES}


class ServerChecks:
    """KEY names the result in the node record and the switch (SWITCH when another check's). A subclass
    says when a server is due (due), whether its node can run it (capable) and runs it (check), returning the result."""
    KEY = ''
    SWITCH = ''
    TICK_S = 300
    AT_ONCE = 4

    def __init__(self, service):
        self.service = service
        self.running, self.again = set(), set()
        self.gate = asyncio.Semaphore(self.AT_ONCE)
        # server id -> its check under way, joined by any other asking meanwhile (重新检测 on two pages, beside
        # the automatic one): one set of probes on the node, one result for all.
        self.inflight = {}
        # Tests' nodes would otherwise reach real services on every hello.
        self.automatic = not os.environ.get('PYTEST_CURRENT_TEST')

    @property
    def db(self):
        return self.service.db

    async def enabled(self):
        return (await self.service.settings())['checks'][self.SWITCH or self.KEY]

    def due(self, server_id, stored, info, zone):
        raise NotImplementedError

    def capable(self, server_id):
        raise NotImplementedError

    async def check(self, server_id):
        raise NotImplementedError

    async def run_now(self, owner, server_id):
        """The page's 重新检测."""
        await self.service.registry.get(owner, server_id)
        if not await one(self.db.conn, 'SELECT 1 FROM server_nodes WHERE server_id=? AND owner=?', (server_id, owner)):
            raise InstanceError('node_not_enrolled', 404)
        if not await self.enabled():
            raise InstanceError(f'{self.KEY}_check_disabled', 409)
        if await self.front(server_id):
            raise InstanceError('check_not_for_front', 409)
        if await self.domestic(server_id):
            raise InstanceError('check_not_for_china', 409)
        result = await self._shared(server_id)
        return {'ok': True, self.KEY: {k: v for k, v in result.items() if k != 'v'}}

    async def _shared(self, server_id):
        task = self.inflight.get(server_id)
        if task is None:
            task = self.inflight[server_id] = self.service.spawn(self.check(server_id))
            task.add_done_callback(lambda t: self._settled(server_id, t))
        # One asker leaving (a closed page) does not end the check for the others.
        return await asyncio.shield(task)

    def _settled(self, server_id, task):
        if self.inflight.get(server_id) is task:
            del self.inflight[server_id]
        if not task.cancelled():
            task.exception()  # retrieved here too: every asker may have left

    def soon(self, server_id):
        if not self.automatic:
            return
        if server_id in self.running:
            self.again.add(server_id)
            return
        self.running.add(server_id)
        self.service.spawn(self._run(server_id))

    async def _run(self, server_id):
        try:
            if await self.enabled() and not await self.domestic(server_id):
                if server_id in self.inflight:
                    await self._shared(server_id)
                else:
                    async with self.gate:
                        await self._shared(server_id)
        except InstanceError:
            pass
        except Exception:
            log.exception('%s check of %s failed', self.KEY, server_id)
        finally:
            self.running.discard(server_id)
        if server_id in self.again:
            self.again.discard(server_id)
            stored = await self._stored(server_id)
            if stored is not None and self.capable(server_id) and self.due(server_id, stored, stored.get('info') or {}, await self.service.zone()):
                self.soon(server_id)

    async def domestic(self, server_id):
        """Whether nothing runs there: a server in mainland China, or a front."""
        row = await one(self.db.conn, '''SELECT n.info_json, p.profile_json FROM server_nodes n
                                         LEFT JOIN server_profiles p ON p.server_id = n.server_id AND p.owner = n.owner WHERE n.server_id=?''', (server_id,))
        if not row:
            return False
        prof = json.loads(row['profile_json'] or '{}')
        return fronts.is_front(prof) or server_region(prof, json.loads(row['info_json'] or '{}')) == DOMESTIC

    async def front(self, server_id):
        row = await one(self.db.conn, '''SELECT p.profile_json FROM server_nodes n
                                         JOIN server_profiles p ON p.server_id = n.server_id AND p.owner = n.owner WHERE n.server_id=?''', (server_id,))
        return bool(row) and fronts.is_front(json.loads(row['profile_json'] or '{}'))

    async def _stored(self, server_id):
        row = await one(self.db.conn, 'SELECT info_json FROM server_nodes WHERE server_id=?', (server_id,))
        return json.loads(row['info_json'] or '{}') if row else None

    async def _save(self, server_id, result):
        await self.service._merge_node_info(server_id, {self.KEY: result})

    async def loop(self):
        while True:
            await asyncio.sleep(self.TICK_S)
            try:
                if not await self.enabled():
                    continue
                zone = await self.service.zone()
                for row in await many(self.db.conn, 'SELECT server_id, info_json FROM server_nodes'):
                    stored = json.loads(row['info_json'] or '{}')
                    if self.capable(row['server_id']) and self.due(row['server_id'], stored, stored.get('info') or {}, zone) \
                            and not await self.domestic(row['server_id']):
                        self.soon(row['server_id'])
            except Exception:
                log.exception('%s checks', self.KEY)
