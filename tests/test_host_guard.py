"""Tests never change the machine they run on: the local target refuses all but read-only probes."""
from __future__ import annotations

import pytest

from app.instances.servers import PORT_CHECK, Servers


async def test_the_local_target_refuses_what_would_change_this_machine():
    servers = Servers.__new__(Servers)
    # What the gateway install sends: it once replaced the live access gateway from a test run.
    with pytest.raises(RuntimeError, match='must not run commands on this machine'):
        await servers.run({'local': True}, 'sh -s', stdin=b'systemctl restart openbear-gateway', root=True)
    with pytest.raises(RuntimeError, match='must not run commands on this machine'):
        await servers.run({'local': True}, 'docker compose up -d')
    assert (await servers.run({'local': True}, PORT_CHECK % 9))['ok']


async def test_a_shell_started_past_the_executors_cannot_change_this_machine():
    # A fixture that replaces Servers.run and starts its own shell is caught one layer lower.
    import asyncio
    with pytest.raises(RuntimeError, match='must not change this machine'):
        await asyncio.create_subprocess_exec('/bin/sh', '-c', 'systemctl restart openbear-gateway')
    process = await asyncio.create_subprocess_exec('/bin/sh', '-s', stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    with pytest.raises(RuntimeError, match='must not change this machine'):
        await process.communicate(b'install -m 0600 x /etc/openbear-gateway/gateway.json')
    process = await asyncio.create_subprocess_exec('/bin/sh', '-c', 'printf ok', stdout=asyncio.subprocess.PIPE)
    assert (await process.communicate())[0] == b'ok'
