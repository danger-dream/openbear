"""A benchmark through a real node on this machine: the binary downloaded and pinned, the
package pushed in pieces, the job run, its report collected and its directory removed.
Uses the network (GitHub and the probed services), so it runs only with OPENBEAR_BENCH_LIVE=1;
run it after changing benchmark-package/ or the code that pushes it."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from app.servers import bench_catalog as catalog
from tests.test_servers import api, env, node_binary, shipped, start_node, wait_for  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401

pytestmark = pytest.mark.skipif(os.environ.get('OPENBEAR_BENCH_LIVE') != '1', reason='set OPENBEAR_BENCH_LIVE=1 to run a real benchmark')


async def test_service_unlock_through_a_real_node(env, node_binary):  # noqa: F811
    e = env
    package = Path(catalog.package_dir('unlock'))
    keep = package.exists()
    server_id = (await api(e, 'POST', '/api/servers', {'name': '本机'}))['item']['id']
    await start_node(e, node_binary, server_id)

    async def online():
        item = (await api(e, 'GET', f'/api/servers/{server_id}'))['item']
        return item['node'] and item['node']['online'] and item['node']['info'].get('arch')
    await wait_for(online, 30)
    try:
        for attempt in range(2):
            run = (await api(e, 'POST', '/api/servers/bench/runs', {'serverId': server_id, 'test': 'unlock', 'options': {'region': 'global'}}))['runs'][0]

            async def finished(run_id=run['id']):
                found = (await api(e, 'GET', f'/api/servers/bench/runs/{run_id}'))['run']
                return found if found['state'] not in ('queued', 'preparing', 'running', 'collecting') else None
            done = await wait_for(finished, 300)
            assert done['state'] == 'done', done
            section = done['sections'][0]
            assert 'Netflix' in section['ansi'] and isinstance(section['json'], dict), section['ansi'][:300]
            assert not Path(f'{catalog.NODE_ROOT}/runs/{run["id"]}').exists(), 'the run directory is removed'
        assert (package / 'bin/service-unlock').stat().st_size > 1 << 20 and (package / catalog.LISTING).is_file()
        rows = await e.servers.bench.service.db.conn.execute("SELECT command FROM server_exec_log WHERE server_id=? AND command LIKE '推送评测包%'", (server_id,))
        assert len(await rows.fetchall()) == 1, 'the second run found the package in place'
    finally:
        if not keep:
            shutil.rmtree(package, ignore_errors=True)


async def test_ip_check_place_through_a_real_node(env, node_binary):  # noqa: F811
    """The report and JSON files a patched script writes; IP.Check.Place installs nothing when jq, curl, bc, nc and dig are there."""
    e = env
    package = Path(catalog.package_dir('ipcheck'))
    keep = package.exists()
    server_id = (await api(e, 'POST', '/api/servers', {'name': '本机'}))['item']['id']
    await start_node(e, node_binary, server_id)

    async def online():
        item = (await api(e, 'GET', f'/api/servers/{server_id}'))['item']
        return item['node'] and item['node']['online'] and item['node']['info'].get('arch')
    await wait_for(online, 30)
    try:
        run = (await api(e, 'POST', '/api/servers/bench/runs', {'serverId': server_id, 'test': 'ipcheck'}))['runs'][0]

        async def finished():
            found = (await api(e, 'GET', f'/api/servers/bench/runs/{run["id"]}'))['run']
            return found if found['state'] not in ('queued', 'preparing', 'running', 'collecting') else None
        done = await wait_for(finished, 400)
        assert done['state'] == 'done', done
        section = done['sections'][0]
        assert section['label'] == 'IPv4' and 'IP质量体检报告' in section['ansi'] and section['json']['Head']['Version'], section['ansi'][:300]
        assert 'link' not in section, 'nothing uploaded'
        assert '今日' not in section['ansi'] and 'sponsor' not in section['ansi'].lower()
    finally:
        if not keep:
            shutil.rmtree(package, ignore_errors=True)
