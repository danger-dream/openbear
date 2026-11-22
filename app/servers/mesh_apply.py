"""Rolling a revision out (contract §7). Relays first, then members whose own settings
changed, then every other node whose config changed; removed members are uninstalled
last, after the others no longer accept them. Within each of these waves the nodes are
sent to at once, CONCURRENCY at a time, each confirmed."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

CONCURRENCY = 12


@dataclass
class Rollout:
    revision: int
    waves: list  # lists of server ids: the relays, then the other nodes
    cleanup: list
    running: bool = True
    progress: list = field(default_factory=list)
    recovery: str = ''

    def __post_init__(self):
        self.progress = [{'serverId': sid, 'state': 'pending', 'error': ''} for sid in [*self.targets, *self.cleanup]]

    @property
    def targets(self):
        return [sid for wave in self.waves for sid in wave]

    def view(self):
        return {'running': self.running, 'revision': self.revision, 'progress': [dict(p) for p in self.progress],
                **({'recovery': self.recovery} if self.recovery else {})}

    def mark(self, server_id, state, error=''):
        for item in self.progress:
            if item['serverId'] == server_id:
                item['state'], item['error'] = state, error


def order(previous, doc, changed):
    """Who gets the new configs: [relays, other nodes], those whose own settings changed first."""
    before = {m['serverId']: m for m in previous['members'] if not m['removing']}
    relays, own, others = [], [], []
    for m in doc['members']:
        sid = m['serverId']
        if m['removing'] or sid not in changed:
            continue
        if m['role'] == 'relay':
            relays.append(sid)
        elif before.get(sid) != m:
            own.append(sid)
        else:
            others.append(sid)
    return [relays, own + others]


async def run(rollout, configs, deliver, uninstall):
    """deliver(serverId, config) and uninstall(serverId) return (state, error).
    After a failure nothing new starts and the nodes not reached keep their previous
    config: members would otherwise dial relays that never accepted them. Those already
    under way finish."""
    stopped = False
    items = {item['serverId']: item for item in rollout.progress}

    async def send(item, gate):
        nonlocal stopped
        async with gate:
            if stopped:
                item['error'] = 'stopped'
                return
            sid = item['serverId']
            item['state'] = 'applying'
            state, error = await (deliver(sid, configs[sid]) if sid in configs else uninstall(sid))
            item['state'], item['error'] = state, error
            stopped = stopped or state == 'failed'

    try:
        for wave in [*rollout.waves, rollout.cleanup]:
            gate = asyncio.Semaphore(CONCURRENCY)
            results = await asyncio.gather(*(send(items[sid], gate) for sid in wave), return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
    finally:
        rollout.running = False
    states = {item['state'] for item in rollout.progress}
    return 'applied' if states <= {'done'} else 'failed' if 'failed' in states or stopped else 'partial'
