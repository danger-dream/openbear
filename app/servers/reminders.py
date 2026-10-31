"""Billing and traffic reminders. Each reminder has a stable key, so a check
that runs again (every minute, or after a restart) never sends it twice."""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import re
import time

from app.instances.contracts import InstanceError
from app.notice_names import quoted, telegram_html
from app.servers import profile as profiles
from app.servers import releases
from app.webhooks.repository import many, one

DEFAULTS = {'enabled': True, 'time': '10:00', 'expiryDays': [7, 3, 1, 0], 'trafficPercents': [80, 90, 100], 'autoRenew': True}
_TIME = re.compile(r'([01]\d|2[0-3]):[0-5]\d')


def now_ms():
    return int(time.time() * 1000)


def clean(data, prior=None):
    value = {**DEFAULTS, **(prior or {})}
    if not isinstance(data, dict) or set(data) - set(DEFAULTS):
        raise InstanceError('invalid_reminders')
    for key in ('enabled', 'autoRenew'):
        if key in data:
            if not isinstance(data[key], bool):
                raise InstanceError('invalid_reminders')
            value[key] = data[key]
    if 'time' in data:
        if not isinstance(data['time'], str) or not _TIME.fullmatch(data['time']):
            raise InstanceError('invalid_reminder_time')
        value['time'] = data['time']
    for key, low, high in (('expiryDays', 0, 90), ('trafficPercents', 1, 100)):
        if key in data:
            items = data[key]
            if not isinstance(items, list) or len(items) > 10 or not all(type(v) is int and low <= v <= high for v in items):
                raise InstanceError(f'invalid_{key}')
            value[key] = sorted(set(items), reverse=key == 'expiryDays')
    return value


def money(billing):
    price = billing.get('price') or 0
    return f"{billing.get('currency') or ''}{price:g}" if price else ''


def gib(n):
    return f'{n / 1024 ** 3:.1f} GB' if n < 1024 ** 4 else f'{n / 1024 ** 4:.2f} TB'


class Reminders:
    def __init__(self, service):
        self.service = service
        self.task = None

    @property
    def db(self):
        return self.service.db

    def start(self):
        self.task = self.service.spawn(self._loop())

    async def _loop(self):
        await asyncio.sleep(20)
        while True:
            with contextlib.suppress(Exception):
                await self.check()
            with contextlib.suppress(Exception):
                await self.deliver()
            await asyncio.sleep(60)

    async def _enqueue(self, owner, key, server_id, kind, text, *, send=True):
        """send=False records it as done without sending (status 'off'): what reminders being off
        does not stop, such as an automatic renewal, still shows in the recent list."""
        async with self.db.write_transaction(label='server-reminder') as conn:
            cursor = await conn.execute('INSERT OR IGNORE INTO server_notifications(id,owner,server_id,kind,text,status,created_ms,updated_ms) '
                                        'VALUES(?,?,?,?,?,?,?,?)', (key, owner, server_id, kind, text, 'pending' if send else 'off', now_ms(), now_ms()))
            return bool(cursor.rowcount)

    async def check(self, now=None):
        settings = await self.service.settings()
        rules = settings['reminders']
        # Off stops the reminders, not automatic renewals.
        if not rules['enabled'] and not rules['autoRenew']:
            return
        zone = await self.service.zone()
        moment = (now or dt.datetime.now(zone)).astimezone(zone)
        today = moment.date()
        daily = moment.strftime('%H:%M') >= rules['time']
        owners = 'SELECT owner FROM server_profiles UNION SELECT owner FROM server_nodes'
        for owner in [r['owner'] for r in await many(self.db.conn, owners)]:
            # One listing per owner: this runs every minute, and a lookup per server
            # would read every profile again for each of them.
            listing = await self.service.list(owner)
            for item in listing['items']:
                if not item['profile']:
                    continue
                if daily:
                    await self._expiry(owner, item, rules, today)
                if rules['enabled'] and item['traffic'] and item['traffic']['limit']:
                    await self._traffic(owner, item, rules)
            # Node versions are part of the 主机 feature: nothing about them while it is off.
            if rules['enabled'] and self.service.servers_enabled():
                await self._node_updates(owner, listing)

    async def _expiry(self, owner, item, rules, today):
        billing = item['profile'].get('billing') or {}
        if billing.get('cycle') == -1 or not billing.get('expiresAt'):
            return
        name, expires = item['name'], dt.date.fromisoformat(billing['expiresAt'])
        days = (expires - today).days
        cost = money(billing)
        key = f'renew:{item["id"]}:{expires.isoformat()}'
        # Renewed from this date once already: set back to it, it was not renewed and is expired.
        if (days < 0 and billing.get('autoRenew') and rules['autoRenew'] and billing.get('cycle', 0) > 0
                and not await one(self.db.conn, 'SELECT 1 FROM server_notifications WHERE id=?', (key,))):
            renewed = profiles.renew(billing, expires, today)
            await self.service.set_expiry(owner, item['id'], renewed.isoformat(), billing.get('billingDay') or expires.day)
            await self._enqueue(owner, key, item['id'], 'renewed', f'{quoted(name)}已于 {expires} 到期，按自动续费顺延到 {renewed}'
                                + (f'（{cost}）' if cost else '') + '。如果实际没有续费，请在服务器页修改到期日。', send=rules['enabled'])
            return
        if not rules['enabled']:
            return
        if days < 0:
            await self._enqueue(owner, f'expired:{item["id"]}:{expires.isoformat()}', item['id'], 'expired',
                                f'{quoted(name)}已于 {expires} 到期（{-days} 天前），请确认是否续费；续费后在服务器页点「续费」更新到期日。')
            return
        crossed = [d for d in rules['expiryDays'] if days <= d]
        if not crossed:
            return
        # Only the closest threshold: turning reminders on never sends a backlog.
        threshold = min(crossed)
        when = '今天到期' if days == 0 else f'将在 {days} 天后（{expires}）到期'
        extra = '，已设置自动续费' if billing.get('autoRenew') else ''
        await self._enqueue(owner, f'expiry:{item["id"]}:{expires.isoformat()}:{threshold}', item['id'], 'expiry',
                            f'{quoted(name)}{when}' + (f'，续费 {cost}' if cost else '') + extra + '。')

    async def _traffic(self, owner, item, rules):
        t = item['traffic']
        percent = t['used'] * 100 / t['limit']
        crossed = [p for p in rules['trafficPercents'] if percent >= p]
        if not crossed:
            return
        threshold = max(crossed)
        await self._enqueue(owner, f'traffic:{item["id"]}:{t["periodStart"]}:{threshold}', item['id'], 'traffic',
                            f'{quoted(item["name"])}本期流量已用 {percent:.0f}%（{gib(t["used"])} / {gib(t["limit"])}），'
                            f'本期从 {t["periodStart"]} 开始，每月 {t["resetDay"]} 日重置。')

    async def _node_updates(self, owner, listing):
        """One reminder per node version that nodes can update to: "N 台节点可更新，其中 M 台需要更新
        才能用…". Keyed on the version (and on whether any node must update), so the same news is
        never sent again: new nodes falling behind the same version add nothing, a new version or
        nodes that now must update send one more. Local development builds remind no one, and with
        自动更新节点 on only the nodes it cannot reach (command execution off) are counted."""
        build = listing.get('node') or {}
        if not build.get('available') or build.get('source') != 'github':
            return
        auto = self.service.auto_update_enabled()
        due = [i for i in listing['items'] if i.get('node') and (i['node'].get('update') or {}).get('updatable')
               and not (auto and (i['node'].get('caps') or {}).get('exec') is not False)]
        if not due:
            return
        required = [i for i in due if i['node']['update']['state'] == 'required']
        version = build['version']
        missing = []
        for item in required:
            missing += [f for f in item['node']['update']['missing'] if f not in missing]
        text = f'有 {len(due)} 台服务器的节点可以更新到 {version}'
        if required:
            uses = '、'.join(releases.FEATURE_LABELS.get(f, f) for f in missing)
            text += f'，其中 {len(required)} 台' + (f'需要更新才能使用{uses}' if uses else '必须更新（所跑的版本已撤回或与主控不兼容）')
        blocked = [i for i in due if (i['node'].get('caps') or {}).get('exec') is False]
        text += '。在服务器页点「全部更新」即可' + (f'；其中 {len(blocked)} 台关闭了命令执行，需要在服务器上重新运行安装命令' if blocked else '') + '。'
        stem = f'node-update:{owner}:{version}:'
        # Once nodes were told they must update to this version, the rest needs no second word.
        if not required and await one(self.db.conn, 'SELECT 1 FROM server_notifications WHERE id=?', (stem + 'required',)):
            return
        await self._enqueue(owner, stem + ('required' if required else 'optional'), '', 'node_update', text)

    async def test(self, owner):
        key = f'test:{now_ms()}'
        await self._enqueue(owner, key, '', 'test', '这是一条测试提醒：服务器到期和流量提醒会通过这里发给你。')
        await self.deliver()
        row = await one(self.db.conn, 'SELECT status FROM server_notifications WHERE id=?', (key,))
        return {'ok': True, 'status': row['status'] if row else 'pending'}

    async def recent(self, owner, limit=20):
        rows = await many(self.db.conn, 'SELECT id,server_id,kind,text,status,created_ms FROM server_notifications WHERE owner=? ORDER BY created_ms DESC LIMIT ?', (owner, limit))
        return [{'serverId': r['server_id'], 'kind': r['kind'], 'text': r['text'], 'status': r['status'], 'createdAtMs': r['created_ms']} for r in rows]

    async def deliver(self):
        for row in await many(self.db.conn, "SELECT * FROM server_notifications WHERE status='pending' AND next_ms<=? LIMIT 50", (now_ms(),)):
            try:
                state = await self._send(row)
            except Exception:
                state = 'failed' if row['attempts'] >= 7 else 'pending'
            async with self.db.write_transaction(label='server-reminder-state') as conn:
                await conn.execute('UPDATE server_notifications SET status=?,attempts=attempts+1,next_ms=?,updated_ms=? WHERE id=?',
                                   (state, now_ms() + min(3600000, 5000 * 2 ** row['attempts']), now_ms(), row['id']))
        cutoff = now_ms() - 400 * 86400000
        async with self.db.write_transaction(label='server-reminder-prune') as conn:
            await conn.execute("DELETE FROM server_notifications WHERE status!='pending' AND created_ms<?", (cutoff,))

    async def _send(self, row):
        tg = getattr(self.service.host, 'web_task_telegram', None)
        if not tg:
            return 'suppressed'
        ddns = row['kind'].startswith('ddns_')
        # The outbox keys on this stable root; a crash here cannot send twice.
        queued = await tg.send_notice('server-notification:' + row['id'], owner_chat_id=row['owner'],
                                      title='🌐 动态域名' if ddns else '🔔 服务器提醒', text=telegram_html(row['text']), text_html=True,
                                      link=('/ddns', '打开动态域名') if ddns else ('/servers', '打开服务器'))
        return 'enqueued' if queued else 'suppressed'
