"""Remote operations on a node in normal use: a file downloaded in chunks while it is replaced,
a terminal the browser leaves while it opens, the node install script failing half way."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import subprocess

import aiohttp
import pytest

from app.servers import install
from tests.test_servers import _online, api, env, shipped, start_fake, wait_for  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401


async def test_a_download_stops_when_the_file_is_replaced_under_it(env):
    e = env
    server_id = (await api(e, 'POST', '/api/servers', {'name': 'dl'}))['item']['id']
    await start_fake(e, server_id)
    await wait_for(lambda: _online(e, server_id))
    target = e.tmp / 'remote-log'
    old = os.urandom(3 * 1024 * 1024)  # three chunks
    target.write_bytes(old)
    real_read = e.servers.read_file
    url = f'/api/servers/{server_id}/files/download?path={target}'

    async def replaced_after_first(owner, sid, path, offset=0, limit=None):
        chunk = await real_read(owner, sid, path, offset)
        if offset == 0:
            # An editor's atomic save, or a log rotated to a new file of the same length.
            fresh = target.with_name('remote-log.new')
            fresh.write_bytes(os.urandom(len(old)))
            fresh.replace(target)
        return chunk
    try:
        e.servers.read_file = replaced_after_first
        mixed = await e.app.get(url, cookies=e.cookies)
        assert mixed.status == 200
        with pytest.raises(aiohttp.ClientPayloadError):
            await mixed.read()

        # A file that only grows is still the same file: the length first seen comes whole.
        target.write_bytes(old)

        async def growing(owner, sid, path, offset=0, limit=None):
            if offset:
                with target.open('ab') as f:
                    f.write(b'appended line\n')
            return await real_read(owner, sid, path, offset)
        e.servers.read_file = growing
        grown = await e.app.get(url, cookies=e.cookies)
        assert await grown.read() == old

        # A node from before file identities says nothing about it, and downloads as it did.
        async def older_node(owner, sid, path, offset=0, limit=None):
            chunk = await real_read(owner, sid, path, offset)
            chunk.pop('file', None)
            return chunk
        target.write_bytes(old)
        e.servers.read_file = older_node
        plain = await e.app.get(url, cookies=e.cookies)
        assert await plain.read() == old
    finally:
        e.servers.__dict__.pop('read_file', None)


class Browser:
    async def send_json(self, data):
        pass

    async def send_bytes(self, data):
        pass

    async def close(self):
        pass


async def test_a_terminal_left_while_it_opens_is_closed_on_the_node(env):
    e = env
    server_id = (await api(e, 'POST', '/api/servers', {'name': 'tty'}))['item']['id']
    node, _, _ = await start_fake(e, server_id)
    await wait_for(lambda: _online(e, server_id))
    session = e.servers.hub.sessions[server_id]
    sent = []
    real_send = session.channel.send_json

    async def held_open(message):
        sent.append(message)
        if message.get('t') != 'term.open':
            return await real_send(message)
        # The node is slow to answer; the browser gives up meanwhile.
    session.channel.send_json = held_open
    try:
        opening = asyncio.ensure_future(e.servers.hub.open_terminal(server_id, Browser(), 80, 24))
        await wait_for(lambda: asyncio.sleep(0, result=any(m.get('t') == 'term.open' for m in sent)))
        opening.cancel()
        with pytest.raises(asyncio.CancelledError):
            await opening
        sid = next(m['sid'] for m in sent if m.get('t') == 'term.open')
        assert sid not in session.terminals
        assert {'t': 'term.close', 'sid': sid} in sent
    finally:
        session.channel.send_json = real_send


async def test_a_terminal_whose_browser_left_as_it_opened_is_closed_on_the_node(env):
    e = env
    server_id = (await api(e, 'POST', '/api/servers', {'name': 'tty-gone'}))['item']['id']
    await start_fake(e, server_id)
    await wait_for(lambda: _online(e, server_id))
    session = e.servers.hub.sessions[server_id]
    sent, real_send, real_open = [], session.channel.send_json, e.servers.hub.open_terminal

    async def recording(message):
        sent.append(message)
        return await real_send(message)

    async def left_meanwhile(sid, ws, cols, rows):
        terminal = await real_open(sid, ws, cols, rows)
        # The page was closed while the node was opening it: nothing can be written to it any more.
        await ws.close()
        return terminal
    session.channel.send_json = recording
    e.servers.hub.open_terminal = left_meanwhile
    try:
        async with aiohttp.ClientSession(cookies=e.cookies) as http:
            async with http.ws_connect(f'{e.url}/api/servers/{server_id}/terminal', headers=e.origin) as ws:
                await ws.receive()
        await wait_for(lambda: asyncio.sleep(0, result=any(m.get('t') == 'term.close' for m in sent)))
        assert not session.terminals
    finally:
        session.channel.send_json = real_send
        e.servers.hub.__dict__.pop('open_terminal', None)


def _script(tmp_path, binary):
    """The install script with its program directory, config and checksum moved into tmp_path."""
    text = install.script().replace('/usr/local/bin', str(tmp_path / 'bin')).replace('/etc/openbear-node/config.json', str(tmp_path / 'config.json'))
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    return re.sub(r'(\w+-\w+\) SUM=)"[^"]*"', rf'\1"{digest}"', text)


def _tools(tmp_path, *, cp_fails=False, mv_fails=False):
    """systemctl that only logs, and cp/mv that fail on the node program as a full disk would."""
    tools = tmp_path / 'tools'
    tools.mkdir(exist_ok=True)
    log = tmp_path / 'service.log'
    (tools / 'systemctl').write_text(f'#!/bin/sh\necho "$1" >> {log}\n')
    (tools / 'cp').write_text('#!/bin/sh\n' + ('case "$2" in *openbear-node.new) echo "No space left on device" >&2; exit 1 ;; esac\n' if cp_fails else '')
                              + 'exec /bin/cp "$@"\n')
    (tools / 'mv').write_text('#!/bin/sh\n' + ('case "$*" in *openbear-node.new*) echo "mv failed" >&2; exit 1 ;; esac\n' if mv_fails else '')
                              + 'exec /bin/mv "$@"\n')
    for tool in tools.iterdir():
        tool.chmod(0o755)
    return {**os.environ, 'PATH': f'{tools}:{os.environ["PATH"]}'}, log


@pytest.mark.parametrize('failure', ['cp', 'mv', None])
def test_reinstalling_leaves_the_running_node_alone_until_its_program_is_written(tmp_path, failure):
    (tmp_path / 'config.json').write_text('{"nodeId": "srv-a"}\n')
    (tmp_path / 'bin').mkdir()
    (tmp_path / 'bin' / 'openbear-node').write_bytes(b'old program')
    sent = tmp_path / 'sent'
    sent.write_bytes(b'new program')
    environ, log = _tools(tmp_path, cp_fails=failure == 'cp', mv_fails=failure == 'mv')
    done = subprocess.run(['sh', '-s', '--', '--url', 'http://127.0.0.1:9', '--stage', 'download', '--binary', str(sent)],
                          input=_script(tmp_path, sent), capture_output=True, text=True, timeout=60, env=environ)
    calls = log.read_text().split() if log.exists() else []
    program = (tmp_path / 'bin' / 'openbear-node').read_bytes()
    if failure == 'cp':
        # Nothing was stopped for a program that could not even be written.
        assert done.returncode == 1 and 'cannot write' in done.stderr
        assert calls == [] and program == b'old program'
    elif failure == 'mv':
        # Stopped to replace it, and started again when that failed.
        assert done.returncode == 1 and calls == ['stop', 'start'] and program == b'old program'
    else:
        assert done.returncode == 0, done.stderr
        assert calls == ['stop'] and program == b'new program'
    assert not (tmp_path / 'bin' / '.openbear-node.new').exists()
