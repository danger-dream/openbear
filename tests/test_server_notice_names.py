"""Names in server reminders (servers, hosts, registration codes, mesh members and relays, DNS
records), whichever module writes them: each goes in through reminders.quoted, so in the Telegram
HTML the whole name sits in one <code> and nothing of it can become a link or end the quote early."""
from __future__ import annotations

import ast
import base64
import html.parser
import os
import re
from pathlib import Path

from app.servers import mesh_status
from app.servers.reminders import quoted, telegram_html
from app.webhooks.repository import many
from tests.test_mesh_missing import meshed
from tests.test_mesh_service import connect, settled
from tests.test_servers import _arg, api, env, shipped  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401

APP = Path(__file__).resolve().parent.parent / 'app'
# Ends the quote, carries a bare URL, breaks the line and brings markup along.
HOSTILE = 'x」 https://evil.example/login 「y\n<b>&amp;'


class Html(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth, self.tags, self.inside, self.outside = 0, [], [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        assert tag == 'code' and self.depth == 0, self.tags
        self.depth += 1
        self.inside.append('')

    def handle_endtag(self, tag):
        assert tag == 'code' and self.depth == 1, self.tags
        self.depth -= 1

    def handle_data(self, data):
        if self.depth:
            self.inside[-1] += data
        else:
            self.outside.append(data)


def assert_names_safe(text, *names):
    """text as Telegram gets it: only balanced, unnested <code>, each name whole inside one of them,
    and nothing of a hostile name outside."""
    assert_html_safe(telegram_html(text), *names)


def assert_html_safe(markup, *names):
    parsed = Html()
    parsed.feed(markup)
    parsed.close()
    assert parsed.depth == 0, markup
    for name in names:
        # A name with nothing ASCII in it cannot be a link and stays plain, still whole in its quote.
        if re.search(r'[!-~]', quoted(name)):
            assert quoted(name)[1:-1] in parsed.inside, (name, parsed.inside)
        else:
            assert quoted(name) in ''.join(parsed.outside)
    outside = ''.join(parsed.outside)
    assert 'evil' not in outside and 'http' not in outside, outside


def test_a_quoted_name_stays_whole_in_one_code_element():
    for name in (HOSTILE, 'Edge-SG.Game', 'a「b」c', '「」', 'only\r\nlines', 'paypal.com'):
        assert_names_safe(f'服务器{quoted(name)}已到期，在服务器页点「续费」。', name)
    # Chinese page labels around it are left as they are.
    assert telegram_html(f'{quoted("Edge")}到期，点「续费」') == '「<code>Edge</code>」到期，点「续费」'


def _writer_functions():
    """Every function that writes a server reminder, in every module that does: the ones calling
    _enqueue or _alert, and the mesh Watch whose alerts mesh.py writes."""
    modules = {path for path in APP.rglob('*.py') if 'reminders._enqueue(' in path.read_text(encoding='utf-8')}
    modules.add(APP / 'servers' / 'reminders.py')
    modules.add(APP / 'servers' / 'mesh_status.py')
    found = {}
    for path in modules:
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = {n.func.attr for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
            if calls & {'_enqueue', '_alert'} or path.name == 'mesh_status.py' and node.name == 'observe':
                found[f'{path.relative_to(APP)}:{node.name}'] = node
    return found


def test_every_module_writing_reminders_quotes_names_through_quoted():
    functions = _writer_functions()
    modules = {key.split(':')[0] for key in functions}
    assert modules >= {'servers/reminders.py', 'servers/onboarding.py', 'servers/mesh.py', 'servers/mesh_status.py', 'ddns/service.py'}, modules
    raw = []
    for key, function in functions.items():
        for text in (n for n in ast.walk(function) if isinstance(n, ast.JoinedStr)):
            for before, value in zip(text.values, text.values[1:]):
                # 「{name}」 written by hand: the name could close the quote itself.
                if isinstance(value, ast.FormattedValue) and isinstance(before, ast.Constant) and str(before.value).endswith('「'):
                    raw.append(f'{key}:{text.lineno}')
    assert raw == []


def test_mesh_alerts_quote_the_member_and_its_relays():
    watch = mesh_status.Watch()
    status = {'entry': {'relay': '', 'candidates': [{'relay': 'r1', 'state': 'failing', 'lastError': 'timeout'},
                                                    {'relay': 'r2', 'state': 'failing', 'lastError': ''}]},
              'peers': [], 'reachability': {'consistent': False}, 'resources': {'rssBytes': 10 ** 10}}
    relay = {'r1': 'LAX https://evil.example/a', 'r2': 'SG」http://evil.example/「'}.get
    alerts = []
    for minute in range(0, 20):
        alerts += watch.observe(minute * 60000, 'sid', HOSTILE, 'member', True, status, [], relay)
    assert {key.split(':')[0] for key, _, _ in alerts} >= {'mesh_standby_unhealthy', 'mesh_exit_inconsistent', 'mesh-entry', 'mesh-memory'}
    for _, _, text in alerts:
        assert_names_safe(text, HOSTILE)
    standby = next(text for key, _, text in alerts if key.startswith('mesh_standby_unhealthy'))
    assert_names_safe(standby, HOSTILE, 'LAX https://evil.example/a', 'SG」http://evil.example/「')


async def test_joined_reminders_quote_the_server_host_and_code(env):  # noqa: F811
    e = env
    await api(e, 'PUT', '/api/servers/settings', {'accessPoints': [{'name': '直连', 'url': e.url}]})
    code_name = 'c」https://evil.example/「d'
    created = (await api(e, 'POST', '/api/servers/registration-codes', {'name': code_name, 'maxUses': 2}))['item']
    code = _arg(created['command'], '--code')
    host = 'evil.example」 https://evil.example/「'
    for _ in range(2):
        await e.servers.onboarding.enroll({'code': code, 'publicKey': base64.b64encode(os.urandom(32)).decode(),
                                           'info': {'hostname': host, 'machineId': 'same-machine'}})
    texts = [r['text'] for r in await many(e.db.conn, "SELECT text FROM server_notifications WHERE kind='joined' ORDER BY created_ms")]
    assert len(texts) == 2 and '已加入' in texts[0] and '像是同一台' in texts[1]
    names = [i['name'] for i in (await api(e, 'GET', '/api/servers'))['items'] if not i['local']]
    assert_names_safe(texts[0], code_name, host, names[0])
    assert_names_safe(texts[1], code_name, host, *names)


async def test_mesh_reminders_quote_the_member(env):  # noqa: F811
    e = env
    nodes, ids, gone = await meshed(e)
    m1 = ids['m1']
    await e.servers.registry.save(123, {'name': HOSTILE.replace('\n', ' ')}, m1)
    await e.servers.mesh._facts(123)
    gone.add(m1)
    await connect(e, m1)
    await settled(e)
    texts = [r['text'] for r in await many(e.db.conn, "SELECT text FROM server_notifications WHERE kind='mesh'")]
    assert len(texts) == 1 and '组网服务不见了' in texts[0]
    assert_names_safe(texts[0], HOSTILE.replace('\n', ' '))
