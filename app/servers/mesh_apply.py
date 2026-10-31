"""Rolling a revision out (contract §7). Relays first, then members whose own settings
changed, then every other node whose config changed; removed members are uninstalled
last, after the others no longer accept them. One node at a time, each confirmed."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Rollout:
    revision: int
    targets: list
    cleanup: list
    running: bool = True
    progress: list = field(default_factory=list)
    recovery: str = ''

    def __post_init__(self):
        self.progress = [{'serverId': sid, 'state': 'pending', 'error': ''} for sid in [*self.targets, *self.cleanup]]

    def view(self):
        return {'running': self.running, 'revision': self.revision, 'progress': [dict(p) for p in self.progress],
                **({'recovery': self.recovery} if self.recovery else {})}

    def mark(self, server_id, state, error=''):
        for item in self.progress:
            if item['serverId'] == server_id:
                item['state'], item['error'] = state, error


def order(previous, doc, changed):
    """Who gets the new configs, in order. previous and doc are applied documents."""
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
    return relays + own + others


async def run(rollout, configs, deliver, uninstall):
    """deliver(serverId, config) and uninstall(serverId) return (state, error).
    After a failure the remaining nodes keep their previous config: members
    reached later would otherwise dial relays that never accepted them."""
    stopped = False
    try:
        for item in rollout.progress:
            if stopped:
                item['error'] = 'stopped'
                continue
            sid = item['serverId']
            item['state'] = 'applying'
            state, error = await (deliver(sid, configs[sid]) if sid in configs else uninstall(sid))
            item['state'], item['error'] = state, error
            stopped = state == 'failed'
    finally:
        rollout.running = False
    states = {item['state'] for item in rollout.progress}
    return 'applied' if states <= {'done'} else 'failed' if 'failed' in states or stopped else 'partial'
