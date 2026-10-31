"""Bounded instance events. The controller pulls login events from the gateway
and derives health events from its own probes; nothing pushes to the controller."""
from __future__ import annotations

import base64
import ipaddress
import json
import re
import time
import uuid

from app.instances.contracts import EVENT_TYPES, InstanceError
from app.notice_names import quoted
from app.webhooks.repository import many, one

AUDIT_TTL = 30*86400000
LOGIN_MAX = 1000
HEALTH_MAX = 200
LABELS = {'login_success': '登录成功', 'login_failure': '登录失败', 'health': '运行异常', 'recovery': '运行恢复'}
# Telegram notices per instance and window; a password-guessing run must not flood the chat.
NOTICE_WINDOW_MS = 600000
NOTICE_BURST = 5


def now():
    return int(time.time()*1000)


async def trim(conn, identity, timestamp):
    await conn.execute('DELETE FROM instance_events WHERE instance_id=? AND at_ms<?', (identity, timestamp-AUDIT_TTL))
    for kind in EVENT_TYPES:
        cap = LOGIN_MAX if kind.startswith('login_') else HEALTH_MAX
        await conn.execute('DELETE FROM instance_events WHERE instance_id=? AND type=? AND id IN (SELECT id FROM instance_events WHERE instance_id=? AND type=? ORDER BY at_ms DESC,id DESC LIMIT -1 OFFSET ?)', (identity, kind, identity, kind, cap))


async def cleanup(service):
    timestamp = now()
    async with service.db.write_transaction(label='instance-retention') as conn:
        for row in await many(conn, 'SELECT id FROM managed_instances'):
            await trim(conn, row['id'], timestamp)
            await conn.execute("UPDATE instance_notifications SET status='suppressed',updated_ms=? WHERE instance_id=? AND status='pending' AND id IN (SELECT id FROM instance_notifications WHERE instance_id=? AND status='pending' ORDER BY created_ms DESC,rowid DESC LIMIT -1 OFFSET 1000)", (timestamp, row['id'], row['id']))
        await conn.execute("DELETE FROM instance_notifications WHERE status!='pending' AND (updated_ms<? OR id IN (SELECT id FROM instance_notifications WHERE status!='pending' ORDER BY updated_ms DESC,rowid DESC LIMIT -1 OFFSET 5000))", (timestamp-AUDIT_TTL,))


def notice_text(item, text, timestamp):
    """text when this instance may notify now, else None (counted). The first notice of the
    next window says how many were left out."""
    window = item.get('noticeWindow') or {}
    if timestamp - window.get('startMs', 0) >= NOTICE_WINDOW_MS:
        skipped = window.get('skipped', 0)
        item['noticeWindow'] = {'startMs': timestamp, 'count': 1, 'skipped': 0}
        return text + (f'（期间另有 {skipped} 条通知未发送）' if skipped else '')
    if window.get('count', 0) < NOTICE_BURST:
        item['noticeWindow'] = {**window, 'count': window.get('count', 0) + 1}
        return text
    item['noticeWindow'] = {**window, 'skipped': window.get('skipped', 0) + 1}
    return None


async def record(service, conn, row, item, kind, data, *, event_id=None, at_ms=None):
    event_id = event_id or uuid.uuid4().hex
    at_ms = at_ms or now()
    cursor = await conn.execute('INSERT OR IGNORE INTO instance_events VALUES(?,?,?,?,?)', (row['id'], event_id, kind, at_ms, json.dumps(data)))
    if not cursor.rowcount:
        return False
    if kind.startswith('login_'):
        item['lastLogin' + ('SuccessAtMs' if kind == 'login_success' else 'FailureAtMs')] = at_ms
        toggle = 'loginSuccess' if kind == 'login_success' else 'loginFailure'
        if item.get('notifications', {}).get(toggle):
            ip = f'（{data["ip"]}）' if data.get('ip') else ''
            text = notice_text(item, f'实例{quoted(item["name"])}{LABELS[kind]}{ip}', now())
            if text:
                await service._enqueue(conn, 'event:'+row['id']+':'+event_id, row, text, item.get('conversationUuid', ''))
    await trim(conn, row['id'], now())
    return True


async def health_notice(service, conn, row, item, state):
    """Record and notify only real transitions; an unknown first state is not news."""
    previous = item.get('healthNoticeState')
    item['healthNoticeState'] = state
    if previous == state or previous in {None, 'unknown'} and state in {'healthy', 'unknown'}:
        return
    kind = 'recovery' if state == 'healthy' else 'health'
    await record(service, conn, row, item, kind, {'status': state})
    if item.get('notifications', {}).get('health'):
        text = '已恢复正常' if kind == 'recovery' else {'unreachable': '无法连接', 'degraded': '运行异常'}.get(state, state)
        text = notice_text(item, f'实例{quoted(item["name"])}{text}', now())
        if text:
            await service._enqueue(conn, 'health:'+row['id']+':'+uuid.uuid4().hex, row, text, item.get('conversationUuid', ''))


_SAFE_IP = re.compile(r'[0-9a-fA-F.:]{2,45}')


def _safe_ip(ip):
    """Only a real address: hex-only text such as bad.cafe passes the character check and would
    become a link in the Telegram notice."""
    if not isinstance(ip, str) or not _SAFE_IP.fullmatch(ip):
        return False
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return False
    return True


async def ingest_gateway(service, items):
    """Store login events pulled from the gateway; returns the highest sequence handled."""
    handled = 0
    for event in items:
        if not isinstance(event, dict) or type(event.get('seq')) is not int:
            continue
        handled = max(handled, event['seq'])
        kind, identity = event.get('type'), event.get('instanceId')
        if kind not in {'login_success', 'login_failure'} or not isinstance(identity, str) or not isinstance(event.get('eventId'), str) or type(event.get('atMs')) is not int:
            continue
        ip = (event.get('data') or {}).get('ip', '')
        data = {'ip': ip} if _safe_ip(ip) else {}
        row = await one(service.db.conn, "SELECT * FROM managed_instances WHERE id=? AND status!='archived'", (identity,))
        if not row:
            continue
        # The gateway's clock may run ahead; an event is never later than its arrival.
        at_ms = min(event['atMs'], now())
        async with service.lock(row['id']):
            row = await one(service.db.conn, 'SELECT * FROM managed_instances WHERE id=?', (identity,))
            item = json.loads(row['public_json'])
            async with service.db.write_transaction(label='instance-login-event') as conn:
                if await record(service, conn, row, item, kind, data, event_id='gw:'+event['eventId'][:120], at_ms=at_ms):
                    await conn.execute('UPDATE managed_instances SET public_json=? WHERE id=?', (json.dumps(item), row['id']))
    return handled


async def listing(service, identity, *, type=None, limit=200, cursor=None):
    try:
        limit = int(limit)
        if not 1 <= limit <= 200 or type is not None and type not in EVENT_TYPES:
            raise ValueError()
        filters, args = ['instance_id=?'], [identity]
        if type:
            filters.append('type=?')
            args.append(type)
        if cursor:
            at, event = json.loads(base64.urlsafe_b64decode(cursor))
            if not isinstance(at, int) or not isinstance(event, str) or len(event) > 160:
                raise ValueError()
            filters.append('(at_ms<? OR (at_ms=? AND id<?))')
            args += [at, at, event]
    except (ValueError, TypeError):
        raise InstanceError('invalid_event_page') from None
    rows = await many(service.db.conn, 'SELECT * FROM instance_events WHERE '+' AND '.join(filters)+' ORDER BY at_ms DESC,id DESC LIMIT ?', (*args, limit+1))
    items = rows[:limit]
    next_cursor = base64.urlsafe_b64encode(json.dumps([items[-1]['at_ms'], items[-1]['id']]).encode()).decode() if len(rows) > limit else None
    return {'ok': True, 'items': [{'id': r['id'], 'type': r['type'], 'atMs': r['at_ms'], 'data': json.loads(r['data_json'])} for r in items], 'nextCursor': next_cursor}
