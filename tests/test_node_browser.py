"""A server node's browser through the Browser tool: a node (tests/fake_node.py, which runs the
engine test_browser_engine_parity holds to the Go one), a real headless Chrome, and the
controller's remote engine with its artifact and upload transfer. No local browser is
configured. The node starting its own browser as another account is the real node's."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.browser.service import BrowserService
from app.config import Config
from app.tools.base import ToolRuntimeContext
from tests.test_servers import (  # noqa: F401
    _online,
    api,
    env,
    node_binary,
    shipped,
    start_fake,
    start_node,
    wait_for,
)
from tests.test_webhooks_backend import env as base_env  # noqa: F401

CHROME = shutil.which('google-chrome') or shutil.which('chromium')
pytestmark = pytest.mark.skipif(not CHROME, reason='needs Chrome')

PAGE = '''<!doctype html><title>节点页</title>
<button id="go" onclick="document.title='clicked'">点我</button>
<input id="file" type="file" onchange="document.getElementById('out').textContent=this.files[0].name+':'+this.files[0].size">
<p id="out"></p>'''


@pytest.fixture
async def chrome(tmp_path):
    profile = tmp_path / 'chrome'
    process = await asyncio.create_subprocess_exec(CHROME, '--headless=new', '--no-sandbox', '--remote-debugging-port=0', f'--user-data-dir={profile}',
                                                   '--no-first-run', 'about:blank', stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port_file = profile / 'DevToolsActivePort'
    for _ in range(150):
        if port_file.exists() and len(port_file.read_text().split()) >= 2:
            break
        await asyncio.sleep(0.1)
    try:
        yield f"http://127.0.0.1:{port_file.read_text().split()[0]}"
    finally:
        process.kill()
        await process.wait()


async def test_browser_tool_drives_a_node_browser(env, chrome, tmp_path):  # noqa: F811
    e = env
    site = web.Application()

    async def page(request):
        return web.Response(text=PAGE, content_type='text/html')
    site.router.add_get('/', page)
    server = TestServer(site)
    await server.start_server()
    try:
        sid = (await api(e, 'POST', '/api/servers', {'name': '法兰克福'}))['item']['id']
        await start_fake(e, sid)
        await wait_for(lambda: _online(e, sid))
        assert (await api(e, 'GET', f'/api/servers/{sid}/browser'))['browser'] == {'enabled': False, 'online': True}

        # A node that runs commands can be switched on from the page.
        on = await api(e, 'PUT', f'/api/servers/{sid}/browser', {'enabled': True, 'endpoint': chrome})
        assert on['browser']['enabled'] is True and on['browser']['endpoint'] == chrome

        async def browser_cap():
            return e.servers.hub.sessions[sid].caps.get('browser') and e.servers.browsers.any_browser
        await wait_for(browser_cap)

        config = Config.model_validate({'telegram': {'botToken': 'test', 'whitelistIds': [123]}, 'models': {'providers': {}, 'primary': ''},
                                        'memory': {'provider': 'builtin'}, 'storage': {'dbPath': str(tmp_path / 'b.sqlite')}})
        workspace = tmp_path / 'workspace'
        browser = BrowserService(config, str(workspace), str(tmp_path / 'browser-state'))
        browser.nodes = e.servers.browsers
        async with e.db.write_transaction(label='test') as conn:
            await conn.execute("INSERT INTO web_conversations(conversation_uuid,internal_chat_id,owner_chat_id,title) VALUES('conv-b',779,123,'t')")
        ctx = ToolRuntimeContext(chat_id=779, conversation_uuid='conv-b')

        async def call(action, context=ctx, page=None, **params):
            return await browser.call({'action': action, **({'page': page} if page else {}), 'params': params}, context)

        status = await call('status')
        assert status['local'] is False and status['nodes'] == [{'node': '法兰克福', 'serverId': sid, 'online': True}], status
        # Without a node the local browser is required, and none is configured.
        assert (await call('page', op='new'))['error'] == 'browser_disabled'

        url = str(server.make_url('/'))
        opened = await call('page', op='new', node='法兰克福', url=url)
        assert opened['status'] == 'ok' and opened['pageCreated'], opened
        handle = opened['page']
        snapshot = await call('snapshot', page=handle)
        assert snapshot['status'] == 'ok' and '点我' in json.dumps(snapshot, ensure_ascii=False)
        assert (await call('act', page=handle, op='click', target='css=#go'))['status'] == 'ok'
        assert (await call('evaluate', page=handle, expression='document.title'))['status'] == 'ok'

        # A screenshot taken on the node lands in this workspace.
        shot = await call('capture', page=handle)
        assert shot['status'] == 'ok' and shot['artifact'].startswith('workspace/artifacts/browser/'), shot
        png = (workspace.parent / shot['artifact']).read_bytes()
        assert png[:8] == b'\x89PNG\r\n\x1a\n' and len(png) > 1000

        # A local file reaches the page through the node.
        upload = tmp_path / '报告.txt'
        upload.write_text('x' * 5000)
        result = await call('files', page=handle, op='upload', target='css=#file', paths=[str(upload)])
        assert result['status'] == 'ok', result
        await call('wait', page=handle, text='报告.txt:5000')

        listed = await call('page', op='list', node=sid)
        assert listed['node'] == '法兰克福' and handle in {p['page'] for p in listed['pages']}
        detail = (await api(e, 'GET', f'/api/servers/{sid}/browser'))['browser']
        assert detail['state'] == 'ready' and detail['source'] == 'endpoint' and detail['product'], detail

        # Agents never reach a node's browser.
        agent = ToolRuntimeContext(chat_id=779, conversation_uuid='conv-b', agent_session_uuid='agent-1')
        assert (await call('snapshot', context=agent, page=handle))['error'] == 'node_browser_main_controller_only'
        assert (await call('page', context=agent, op='new', node=sid))['error'] == 'node_browser_main_controller_only'

        # Restarting an attached browser reconnects; its pages stay with that browser.
        restarted = await api(e, 'POST', f'/api/servers/{sid}/browser/restart')
        assert restarted['browser']['state'] == 'ready' and restarted['browser']['epoch'] != detail['epoch']
        await call('page', op='close', page=handle)
        off = await api(e, 'PUT', f'/api/servers/{sid}/browser', {'enabled': False})
        assert off['browser']['enabled'] is False
        await wait_for(lambda: asyncio.sleep(0, not e.servers.hub.sessions[sid].caps.get('browser')))
        await browser.close()
    finally:
        await server.close()


def _chrome_processes(marker):
    out = subprocess.run(['ps', '-eo', 'user=,args='], capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if marker in line and 'chrom' in line.lower()]


@pytest.mark.live  # builds and starts the real Go node, as root; run with --run-live
async def test_node_starts_its_own_browser_as_an_unprivileged_account(env, node_binary, tmp_path, monkeypatch):  # noqa: F811
    """No endpoint: the node finds the installed Chrome, runs it headless as an
    unprivileged account over a pipe (no port), and ends it when turned off. An
    installed Chrome that account cannot reach is reported, not replaced; Chrome
    for Testing is used only once chosen."""
    import os
    import pwd
    import tempfile
    if os.geteuid() != 0:
        pytest.skip('dropping to another account needs root')
    e = env
    # The account must be able to pass every folder above the node's files.
    root = tempfile.mkdtemp(prefix='obnode-', dir='/tmp')
    os.chmod(root, 0o711)
    e.tmp = __import__('pathlib').Path(root)
    monkeypatch.setenv('OPENBEAR_BROWSER_USER', 'nobody')
    try:
        sid = (await api(e, 'POST', '/api/servers', {'name': '自启动'}))['item']['id']
        await start_node(e, node_binary, sid)
        await wait_for(lambda: _online(e, sid))
        await api(e, 'PUT', f'/api/servers/{sid}/browser', {'enabled': True})
        started = await e.servers._node_call(123, sid, 'browser.start', {}, timeout=60)
        source = 'system'
        if started['state'] == 'failed':
            assert started['error'].startswith('browser_not_runnable: '), started
            # The real file, whose folders are what the account cannot enter, not a launcher link.
            reported = started['error'].removeprefix('browser_not_runnable: ')
            assert os.path.realpath(reported) == reported, started
            if os.environ.get('OPENBEAR_TEST_DOWNLOAD') != '1':
                pytest.skip("the installed Chrome is not reachable by nobody here; OPENBEAR_TEST_DOWNLOAD=1 downloads Chrome for Testing instead")
            await api(e, 'PUT', f'/api/servers/{sid}/browser', {'enabled': True, 'download': True})
            started = await e.servers._node_call(123, sid, 'browser.start', {}, timeout=60)
            assert started['state'] == 'installing', started

            async def downloaded():
                state = await e.servers._node_call(123, sid, 'browser.status', {}, timeout=15)
                return state if state['state'] in {'stopped', 'failed'} else None
            settled = await wait_for(downloaded, 900)
            assert settled['state'] == 'stopped', settled
            started, source = await e.servers._node_call(123, sid, 'browser.start', {}, timeout=60), 'download'
        assert started['state'] == 'ready' and started['source'] == source and started['headless'] is True, started
        profile = os.path.join(root, 'browser', 'profile')
        running = _chrome_processes(profile)
        nobody = pwd.getpwnam('nobody').pw_name
        assert running and all(line.split()[0] == nobody for line in running), running
        assert '--remote-debugging-pipe' in running[0] and '--remote-debugging-port' not in ' '.join(running)
        # Root's supplementary groups (root, kvm, ...) are not carried over to the browser.
        for pid in subprocess.run(['pgrep', '-f', profile], capture_output=True, text=True).stdout.split():
            with open(f'/proc/{pid}/status') as status:
                groups = next(line for line in status if line.startswith('Groups:')).split()[1:]
            assert set(groups) <= {str(pwd.getpwnam('nobody').pw_gid)}, (pid, groups)

        config = Config.model_validate({'telegram': {'botToken': 'test', 'whitelistIds': [123]}, 'models': {'providers': {}, 'primary': ''},
                                        'memory': {'provider': 'builtin'}, 'storage': {'dbPath': str(tmp_path / 'b.sqlite')}})
        browser = BrowserService(config, str(tmp_path / 'workspace'), str(tmp_path / 'browser-state'))
        browser.nodes = e.servers.browsers
        async with e.db.write_transaction(label='test') as conn:
            await conn.execute("INSERT INTO web_conversations(conversation_uuid,internal_chat_id,owner_chat_id,title) VALUES('conv-l',780,123,'t')")
        ctx = ToolRuntimeContext(chat_id=780, conversation_uuid='conv-l')
        opened = await browser.call({'action': 'page', 'params': {'op': 'new', 'node': sid}}, ctx)
        assert opened['status'] == 'ok', opened
        shot = await browser.call({'action': 'capture', 'page': opened['page'], 'params': {}}, ctx)
        assert shot['status'] == 'ok', shot
        # Sites must not see a headless browser.
        seen = await browser.call({'action': 'evaluate', 'page': opened['page'], 'params': {
            'expression': '({ua: navigator.userAgent, webdriver: navigator.webdriver, screen: [screen.width, screen.height]})'}}, ctx)
        assert seen['status'] == 'ok', seen
        assert 'Headless' not in seen['value']['ua'] and ' Chrome/' in seen['value']['ua'], seen
        assert seen['value']['webdriver'] is False and seen['value']['screen'] == [1920, 1080], seen

        # A restart starts a fresh browser on the same profile; turning off ends it.
        restarted = await e.servers.restart_browser(123, sid)
        assert restarted['browser']['state'] == 'ready' and restarted['browser']['epoch'] != started['epoch']
        await api(e, 'PUT', f'/api/servers/{sid}/browser', {'enabled': False})

        async def ended():
            return not _chrome_processes(profile)
        await wait_for(ended, 20)
        await browser.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)
