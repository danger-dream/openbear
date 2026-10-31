"""Instance notices go to Telegram as HTML like server reminders: the instance name, from any notice
(logins, health, operations), sits whole in one <code>, so it can neither become a link nor end the
quote early, and the rest of the text is unchanged."""
from __future__ import annotations

import ast
import json
from pathlib import Path

from app.instances.monitor import once
from app.llm.events import StreamEvent
from app.notice_names import quoted
from app.web_task_telegram import WebTaskTelegramNotifier
from app.webhooks.repository import many
from tests.test_instance_hardening import edit, now
from tests.test_instances import create, env, model, run, tool  # noqa: F401
from tests.test_server_notice_names import HOSTILE, assert_html_safe
from tests.test_web_task_telegram import NotificationBot
from tests.test_webhooks_backend import env as base_env  # noqa: F401

APP = Path(__file__).resolve().parent.parent / 'app'


async def test_instance_notices_keep_the_name_in_code_for_telegram(env):  # noqa: F811
    e = env
    item, _ = await create(e)
    await edit(e, item['id'], name=HOSTILE)
    at = now()
    # A gateway address that is hex but no address (bad.cafe is a real TLD) or carries a scope is left out.
    e.gateway.events = [{'seq': 1, 'eventId': 'a', 'instanceId': item['id'], 'type': 'login_failure', 'atMs': at, 'data': {'ip': '203.0.113.9'}},
                        {'seq': 2, 'eventId': 'b', 'instanceId': item['id'], 'type': 'login_success', 'atMs': at + 1, 'data': {'ip': 'bad.cafe'}},
                        {'seq': 3, 'eventId': 'c', 'instanceId': item['id'], 'type': 'login_failure', 'atMs': at + 2, 'data': {'ip': 'fe80::1%ab.cafe'}},
                        {'seq': 4, 'eventId': 'd', 'instanceId': item['id'], 'type': 'login_success', 'atMs': at + 3, 'data': {'ip': '2001:db8::1'}}]
    await e.instances.gateway.pull_events()
    assert [x['data'] for x in (await e.instances.events(123, item['id']))['items']] == [{'ip': '2001:db8::1'}, {}, {}, {'ip': '203.0.113.9'}]
    await e.instances.verify(123, item['id'])
    await once(e.instances, force=True)
    e.child.health = 'degraded'
    await once(e.instances, force=True)
    model(e, [tool('report', {'revision': 1, 'outcome': 'completed'}), [StreamEvent(kind='content', text='done'), StreamEvent(kind='finish', finish_reason='stop')]])
    await run(e, item)

    name = quoted(HOSTILE)
    rows = await many(e.db.conn, 'SELECT id,text FROM instance_notifications ORDER BY created_ms,rowid')
    texts = {r['id'].split(':')[0] + ':' + r['text'].removeprefix('实例' + name) for r in rows}
    # Stored as plain text, the wording around the name as before.
    assert texts >= {'event:登录失败（203.0.113.9）', 'event:登录成功', 'event:登录失败', 'event:登录成功（2001:db8::1）', 'health:运行异常'}, texts
    assert any(t.startswith('operation:的部署') for t in texts), texts
    assert all(r['text'].startswith('实例' + name) for r in rows)

    bot = NotificationBot()
    e.server.web_task_telegram = notifier = WebTaskTelegramNotifier(e.server.config, e.db, bot)
    await e.db.conn.execute("UPDATE instance_notifications SET next_ms=0")
    await e.db.conn.commit()
    await e.instances.notify_pending()
    outbox = await many(e.db.conn, "SELECT payload_json FROM web_tg_notification_outbox WHERE root_turn_uuid LIKE 'instance-notification:%'")
    assert len(outbox) == len(rows)
    for row in outbox:
        payload = json.loads(row['payload_json'])
        assert payload['html'] is True
        assert payload['text'].startswith('实例「<code>x』 https://evil.example/login 『y &lt;b&gt;&amp;amp;</code>」'), payload['text']
        assert_html_safe(payload['text'], HOSTILE)
    while due := await notifier._claim_due():
        await notifier._deliver(due)
    assert len(bot.sent) == len(rows)
    for sent in bot.sent:
        head = '<b>🧩 实例管理</b><br><br>'
        assert sent['body'].startswith(head + '实例「<code>x』'), sent['body']
        assert_html_safe(sent['body'].removeprefix(head), HOSTILE)


def test_instance_notice_texts_quote_names_through_quoted():
    """No notice text in app/instances writes 「{name}」 by hand."""
    raw = []
    for path in (APP / 'instances').glob('*.py'):
        for function in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, 'id', '') for n in ast.walk(function) if isinstance(n, ast.Call)}
            if not calls & {'_enqueue', 'notice_text'}:
                continue
            for text in (n for n in ast.walk(function) if isinstance(n, ast.JoinedStr)):
                for before, value in zip(text.values, text.values[1:]):
                    if isinstance(value, ast.FormattedValue) and isinstance(before, ast.Constant) and str(before.value).endswith('「'):
                        raw.append(f'{path.name}:{text.lineno}')
    assert raw == []
