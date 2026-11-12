"""Server benchmarks through the real Web app; the node is a fake that keeps files and jobs
the way the node contract describes fs.*, exec and job.*."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import tarfile

import pytest

from app.servers import bench as bench_module
from app.servers import bench_catalog as catalog
from app.servers import bench_packages as packages
from app.servers.bench_terminal import after_last_clear, screen
from tests.test_mesh_service import server
from tests.test_servers import api, env, shipped  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401

REPORT = '\x1b[1mIP质量体检报告\x1b[0m\r\n风险：低\n'


class BenchNode:
    """Files, exec and jobs of fake nodes; a job ends when the test says so."""

    def __init__(self, e):
        self.e, self.files, self.jobs, self.calls, self.uploads = e, {}, {}, [], {}
        self.count = 0

    async def call(self, server_id, method, params, timeout):
        if server_id not in self.e.servers.hub.sessions:
            raise LookupError('node_offline')
        self.calls.append((server_id, method, params))
        files = self.files.setdefault(server_id, {})
        if method == 'fs.read':
            if params['path'] not in files:
                return {'ok': False, 'error': 'not_found'}
            data = files[params['path']][params['offset']:params['offset'] + params['limit']]
            return {'ok': True, 'result': {'data': base64.b64encode(data).decode(), 'size': len(files[params['path']]), 'offset': params['offset']}}
        if method == 'fs.write':
            part = self.uploads.setdefault(params['upload'], bytearray())
            assert params['offset'] == len(part)
            part += base64.b64decode(params['data'])
            if params['done']:
                files[params['path']] = bytes(self.uploads.pop(params['upload']))
            return {'ok': True, 'result': {'path': params['path'], 'done': params['done']}}
        if method == 'fs.list':
            prefix = params['path'].rstrip('/') + '/'
            names = [p[len(prefix):] for p in files if p.startswith(prefix) and '/' not in p[len(prefix):]]
            if not names:
                return {'ok': False, 'error': 'not_found'}
            return {'ok': True, 'result': {'path': params['path'], 'entries': [{'name': n, 'type': 'file'} for n in names]}}
        if method == 'exec':
            if params['command'].startswith('rm -rf '):
                for target in params['command'][len('rm -rf '):].split():
                    target = target.strip("'")
                    for path in [p for p in files if p == target or p.startswith(target + '/')]:
                        del files[path]
            return {'ok': True, 'result': {'exitCode': 0, 'stdout': '', 'stderr': '', 'durationMs': 1}}
        if method == 'job.start':
            self.count += 1
            job_id = f'job{self.count:04d}'
            self.jobs[job_id] = {'id': job_id, 'command': params['command'], 'state': 'running', 'exitCode': None,
                                 'startedAt': 1793115000000, 'endedAt': None, 'durationMs': 0, 'server': server_id, 'log': ''}
            return {'ok': True, 'result': self.jobs[job_id]}
        if method == 'job.list':
            return {'ok': True, 'result': {'jobs': [j for j in self.jobs.values() if j['server'] == server_id]}}
        if method in ('job.status', 'job.stop'):
            job = self.jobs.get(params['id'])
            if not job:
                return {'ok': False, 'error': 'job_not_found'}
            if method == 'job.stop':
                stopped = job['state'] == 'running'
                if stopped:
                    job.update(state='stopped', exitCode=-1, endedAt=1793115009000)
                return {'ok': True, 'result': {'stopped': stopped, 'job': job}}
            raw = job['log'].encode()
            offset = params.get('offset', max(0, len(raw) - params.get('limit', 65536)))
            data = raw[offset:offset + params.get('limit', 65536)]
            return {'ok': True, 'result': {'job': job, 'log': {'data': data.decode(), 'offset': offset, 'next': offset + len(data),
                                                               'size': len(raw), 'first': 0}}}
        raise AssertionError(method)

    def started(self):
        return [j for j in self.jobs.values()]

    def out_dir(self, job):
        return job['command'].split('OB_BENCH_OUT=')[1].split()[0]

    def finish(self, job, *, files=None, log='', code=0):
        for name, data in (files or {}).items():
            self.files[job['server']][f'{self.out_dir(job)}/{name}'] = data.encode()
        job.update(state='exited', exitCode=code, endedAt=1793115060000, log=log)


@pytest.fixture(autouse=True)
def own_package_locks(monkeypatch):
    """The download/unpack locks are module-wide asyncio locks; each test (and its event loop)
    gets its own, so a cache path seen again never meets a lock from an earlier loop."""
    monkeypatch.setattr(packages, '_LOCKS', {})


async def stand_in_fetch(binary, cache, session=None):
    """The pinned download, from the cache; on a miss a small stand-in of the declared form
    (plain or tar, with its member or strip) instead of the real file from the Internet.
    fetch() itself is tested against a local server below."""
    path = cache / binary['sha256']
    if not path.is_file():
        cache.mkdir(parents=True, exist_ok=True)
        data = f'#!/bin/sh\n# stand-in for {binary["url"]}\n'.encode()
        if binary.get('unpack') == 'tar':
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode='w:gz') as tar:
                info = tarfile.TarInfo(binary.get('member') or '/'.join(['top'] * int(binary.get('strip') or 0) + ['tool']))
                info.size, info.mode = len(data), 0o755
                tar.addfile(info, io.BytesIO(data))
            data = archive.getvalue()
        path.write_bytes(data)
    return path


async def bench_env(e, monkeypatch, *, platform='linux', arch='amd64'):
    monkeypatch.setattr(bench_module, 'POLL_S', 0.02)
    monkeypatch.setattr(bench_module, 'AWAY_POLL_S', 0.05)
    # Packages with binaries (service-unlock: 11 MB from GitHub) would otherwise be downloaded
    # for every test, then pushed to the fake node in full.
    monkeypatch.setattr(packages, 'fetch', stand_in_fetch)
    node = BenchNode(e)
    e.servers.hub.call = node.call
    sid = await server(e, 'OVH.SG', platform=platform, features=('mesh', 'jobs'))
    e.servers.hub.sessions[sid].info['arch'] = arch
    return node, sid


async def until(predicate, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError('condition not met in time')


async def run_state(e, run_id, *states):
    async def check():
        run = (await api(e, 'GET', f'/api/servers/bench/runs/{run_id}'))['run']
        return run if run['state'] in states else None
    return await until(check)


async def a_job(node, count=1):
    async def check():
        return node.started()[count - 1] if len(node.started()) >= count else None
    return await until(check)


# ----- catalogue -----
def test_catalogue_options_and_commands():
    view = catalog.view()
    assert [c['id'] for c in view['categories']] == ['ip', 'network', 'hardware', 'media']
    assert {t['id'] for t in view['tests']} == set(catalog.TESTS)
    assert view['suite']['tests'] == ['hwcheck', 'ipcheck', 'netcheck', 'netroute']
    for tool, version in catalog.PACKAGES.items():
        assert packages.manifest(tool, version)['version'] == version

    assert catalog.options('ipcheck', {}) == {'family': '4', 'report': False}
    assert catalog.options('netcheck', {'mode': 'full', 'family': '46'}) == {'family': '46', 'mode': 'full', 'report': False}
    for test, bad in [('ipcheck', {'family': '5'}), ('ipcheck', {'report': 'yes'}), ('ipcheck', {'other': 1}), ('unlock', {'region': 'cn'})]:
        with pytest.raises(catalog.InstanceError):
            catalog.options(test, bad)
    with pytest.raises(catalog.InstanceError):
        catalog.options('nope', {})

    assert catalog.arguments('ipcheck', catalog.options('ipcheck', {})) == ['-y', '-p', '-4']
    assert catalog.arguments('ipcheck', catalog.options('ipcheck', {'family': '46', 'report': True})) == ['-y']
    assert catalog.arguments('netcheck', catalog.options('netcheck', {})) == ['-y', '-p', '-4', '-L']
    assert catalog.arguments('netroute', catalog.options('netroute', {'province': '广东'})) == ['-y', '-p', '-4', '-R', '广东', '-S', '123']
    assert catalog.arguments('netroute', catalog.options('netroute', {})) == ['-y', '-p', '-4', '-R', '-S', '123']
    assert catalog.arguments('tcpquality', catalog.options('tcpquality', {'mode': 'all'}))[-1] == '--no-rank-upload'
    assert '--no-rank-upload' not in catalog.arguments('tcpquality', catalog.options('tcpquality', {'mode': 'v4', 'report': True}))
    # The route check never uploads, whatever the box says.
    assert catalog.arguments('tcpquality', catalog.options('tcpquality', {'mode': 'route-v4', 'report': True}))[-1] == '--no-rank-upload'
    assert catalog.arguments('unlock', catalog.options('unlock', {'region': 'jp'})) == ['--machine', '--region', 'jp']

    command = catalog.command('netroute', catalog.options('netroute', {'province': '内蒙古'}), '/var/tmp/openbear-bench/runs/abc')
    assert command.startswith("export OB_BENCH_DIR=/var/tmp/openbear-bench/net.check.place-v0.1.0 OB_BENCH_OUT=/var/tmp/openbear-bench/runs/abc")
    # BusyBox's sha256sum (OpenWrt, Alpine) has no --quiet; the output goes nowhere anyway.
    assert 'sha256sum -c .openbear-files.sha256 >/dev/null 2>&1' in command and f'exit {catalog.CHECKSUM_FAILED}' in command
    assert '--quiet' not in command and '--quiet' not in catalog.unlock_command()
    assert command.endswith('bash "$OB_BENCH_DIR/net.sh" -y -p -4 -R \'内蒙古\' -S 123')
    assert catalog.command('unlock', {'region': 'auto'}, '/x').endswith('"$OB_BENCH_DIR/bin/service-unlock" --machine > "$OB_BENCH_OUT/envelope.json"')

    assert catalog.label('netcheck', {'family': '4', 'mode': 'low', 'report': False}) == '低数据模式'
    assert catalog.label('ipcheck', {'family': '46', 'report': True}) == 'IPv4 + IPv6 · 生成在线报告'
    assert catalog.label('geekbench5', {}) == ''


def test_terminal_keeps_what_the_screen_finally_shows():
    assert screen('step 1%\rstep 50%\rstep 100%\n') == 'step 100%\n'
    assert screen('abcdef\r\x1b[Kxy\n') == 'xy\n'
    assert screen('\x1b[31mred\x1b[0m plain\n') == '\x1b[31mred\x1b[0m plain\n'
    assert screen('one\n\x1b[H\x1b[2Jtwo\n') == 'two\n'
    assert screen('one\n\x1b[2Jtwo\n', clears='mark').splitlines()[-1] == 'two'
    assert screen('a\nb\x1b[1Ac\n') == 'ac\nb\n'
    assert screen('\x1b]0;title\x07\x1b[?25lshown\n') == 'shown\n'
    assert after_last_clear('start\x1b[2Jmiddle\x1bcend') == 'end'


# ----- packages -----
async def test_a_binary_is_downloaded_once_and_kept_only_when_it_matches(tmp_path):
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    blob, asked = b'\x7fELF binary', []

    async def serve(request):
        asked.append(request.path)
        return web.Response(body=blob) if request.path == '/tool' else web.Response(status=404)
    app = web.Application()
    app.router.add_get('/{name}', serve)
    origin = TestServer(app)
    await origin.start_server()
    cache = tmp_path / 'cache'
    try:
        binary = {'url': str(origin.make_url('/tool')), 'sha256': hashlib.sha256(blob).hexdigest()}
        path = await packages.fetch(binary, cache)
        assert path == cache / binary['sha256'] and path.read_bytes() == blob
        assert await packages.fetch(binary, cache) == path and asked == ['/tool']  # from the cache
        path.write_bytes(b'corrupt')
        assert await packages.fetch(binary, cache) == path and path.read_bytes() == blob
        assert asked == ['/tool', '/tool']
        with pytest.raises(packages.PackageError) as raised:
            await packages.fetch({**binary, 'sha256': '0' * 64}, cache)
        assert raised.value.code == 'checksum_mismatch'
        with pytest.raises(packages.PackageError) as raised:
            await packages.fetch({'url': str(origin.make_url('/gone')), 'sha256': '1' * 64}, cache)
        assert raised.value.code == 'download_failed'
        assert sorted(p.name for p in cache.iterdir()) == [binary['sha256']]  # nothing else kept
    finally:
        await origin.close()


async def test_binaries_unpack_from_the_cache(tmp_path):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w:gz') as tar:
        for name, data, mode in [('Tool-1/tool', b'#!x', 0o755), ('Tool-1/data.bin', b'data', 0o644)]:
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            tar.addfile(info, io.BytesIO(data))
    blob = archive.getvalue()
    digest = hashlib.sha256(blob).hexdigest()
    cache = tmp_path / 'cache'
    cache.mkdir()
    (cache / digest).write_bytes(blob)
    root = tmp_path / 'pkg'
    (root / 'tool' / 'v0.1.0').mkdir(parents=True)
    (root / 'tool' / 'v0.1.0' / 'run.sh').write_text('echo hi\n')
    manifest = {'tool': 'tool', 'version': 'v0.1.0', 'platforms': ['linux/amd64'],
                'files': {'run.sh': hashlib.sha256(b'echo hi\n').hexdigest()},
                'binaries': [{'platform': 'linux/amd64', 'dest': 'bin/tool', 'url': 'https://example.invalid/t.tgz', 'sha256': digest,
                              'unpack': 'tar', 'strip': 1},
                             {'platform': 'linux/arm64', 'dest': 'bin/other', 'url': 'https://example.invalid/o', 'sha256': '0' * 64}]}
    (root / 'tool' / 'v0.1.0' / 'manifest.json').write_text(json.dumps(manifest))
    found = await packages.files('tool', 'v0.1.0', 'linux/amd64', cache, root=root)
    assert [(rel, mode) for rel, _, mode in found] == [('run.sh', 0o755), ('bin/tool/data.bin', 0o644), ('bin/tool/tool', 0o755)]
    assert found[2][1].read_bytes() == b'#!x'
    found[2][1].write_bytes(b'bad')
    found = await packages.files('tool', 'v0.1.0', 'linux/amd64', cache, root=root)
    assert found[2][1].read_bytes() == b'#!x'
    with pytest.raises(packages.PackageError) as raised:
        await packages.files('tool', 'v0.1.0', 'linux/arm64', cache, root=root)
    assert raised.value.code == 'platform_unsupported'
    (root / 'tool' / 'v0.1.0' / 'run.sh').write_text('changed\n')
    with pytest.raises(packages.PackageError) as raised:
        await packages.files('tool', 'v0.1.0', 'linux/amd64', cache, root=root)
    assert raised.value.code == 'package_invalid'


# ----- runs -----
async def test_a_run_pushes_runs_collects_and_cleans_up(env, monkeypatch):  # noqa: F811
    node, sid = await bench_env(env, monkeypatch)
    created = await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'ipcheck', 'options': {'family': '46'}})
    run = created['runs'][0]
    assert (run['test'], run['state'], run['package'], run['label']) == ('ipcheck', 'queued', 'ip.check.place v0.1.0', 'IPv4 + IPv6')

    job = await a_job(node)
    base = '/var/tmp/openbear-bench/ip.check.place-v0.1.0'
    files = node.files[sid]
    assert files[f'{base}/ip.sh'] == (packages.ROOT / 'ip.check.place/v0.1.0/ip.sh').read_bytes()
    listing = files[f'{base}/.openbear-files.sha256'].decode()
    assert '  ip.sh\n' in listing and '  main/ref/dnsbl.list\n' in listing
    modes = {p['path']: p['mode'] for _, m, p in node.calls if m == 'fs.write'}
    assert modes[f'{base}/ip.sh'] == '755' and modes[f'{base}/main/ref/iso3166.json'] == '644'
    assert job['command'].endswith('bash "$OB_BENCH_DIR/ip.sh" -y -p')
    # It measures the machine's own line, past a transparent proxy as the node's checks go.
    assert next(p for _, m, p in node.calls if m == 'job.start')['bypass'] is True
    # The node has the job before the run notes it: wait for the run's own state.
    await run_state(env, run['id'], 'running')
    # Running runs cannot be deleted.
    await api(env, 'DELETE', f'/api/servers/bench/runs/{run["id"]}', status=409)

    node.finish(job, files={'report-4.ansi': REPORT, 'result-4.json': '{"Head": {"IP": "1.2.*.*"}}',
                            'report-6.ansi': 'v6 report\n', 'result-6.json': '{"a": 1}\n{"b": 2}\n'}, log='installing…\r\x1b[Kdone\n', code=1)
    done = await run_state(env, run['id'], 'done', 'failed')
    assert done['state'] == 'done' and done['exitCode'] == 1  # the script's last test fails; the report decides
    assert [s['label'] for s in done['sections']] == ['IPv4', 'IPv6']
    assert done['sections'][0]['ansi'] == '\x1b[1mIP质量体检报告\x1b[0m\n风险：低\n'
    assert done['sections'][0]['json'] == {'Head': {'IP': '1.2.*.*'}} and done['sections'][1]['json'] == {'a': 1}
    assert done['log'] == 'done\n'
    out = node.out_dir(job)
    await until(lambda: asyncio.sleep(0, result=not any(p.startswith(out) for p in node.files[sid])))
    # The command record is written once the node has answered the clean-up.
    async def cleaned():
        commands = [r['command'] for r in await api_log(env, sid)]
        return commands if '评测：清理 IP.Check.Place（IPv4 + IPv6）' in commands else None
    logged = await until(cleaned)
    assert any(c.startswith('评测：推送 ip.check.place v0.1.0（') for c in logged)
    assert '评测：运行 IP.Check.Place（IPv4 + IPv6）（后台任务 job0001）' in logged and '评测：清理 IP.Check.Place（IPv4 + IPv6）' in logged
    assert '评测：删除 ip.check.place 的旧版本' in logged
    assert not any('OB_BENCH_DIR' in c for c in logged), 'the record says what ran, not the whole command'

    # A second run finds the package in place and pushes nothing.
    writes = sum(1 for _, m, _ in node.calls if m == 'fs.write')
    again = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'ipcheck'}))['runs'][0]
    second = await a_job(node, 2)
    assert sum(1 for _, m, _ in node.calls if m == 'fs.write') == writes
    node.finish(second, files={'report-4.ansi': REPORT})
    finished = await run_state(env, again['id'], 'done')
    assert [s['label'] for s in finished['sections']] == ['IPv4'] and 'json' not in finished['sections'][0]

    listed = (await api(env, 'GET', '/api/servers/bench/runs'))['runs']
    assert [r['id'] for r in listed] == [again['id'], run['id']] and 'sections' not in listed[0]
    await api(env, 'DELETE', f'/api/servers/bench/runs/{run["id"]}')
    await api(env, 'GET', f'/api/servers/bench/runs/{run["id"]}', status=404)


async def api_log(e, sid):
    rows = await bench_module.many(e.db.conn, 'SELECT command FROM server_exec_log WHERE server_id=? ORDER BY id', (sid,))
    return rows


async def test_one_run_at_a_time_and_the_suite(env, monkeypatch):  # noqa: F811
    node, sid = await bench_env(env, monkeypatch)
    suite = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'suite': True, 'options': {'family': '46'}}))['runs']
    assert [r['test'] for r in suite] == ['hwcheck', 'ipcheck', 'netcheck', 'netroute'] and len({r['suite'] for r in suite}) == 1
    assert [r['options'] for r in suite] == [{'report': False}, {'family': '46', 'report': False},
                                             {'family': '46', 'mode': 'low', 'report': False}, {'family': '46', 'province': ''}]
    first = await a_job(node)
    await asyncio.sleep(0.1)
    assert len(node.started()) == 1 and 'hardware.sh' in first['command']
    node.finish(first, files={'report-4.ansi': 'hw\n'})
    second = await a_job(node, 2)
    assert 'ip.sh' in second['command']
    hw = await run_state(env, suite[0]['id'], 'done')
    assert hw['sections'][0]['label'] == ''  # the hardware check has no protocol choice


async def test_several_tests_at_once_alone_or_beside_the_suite(env, monkeypatch):  # noqa: F811
    node, sid = await bench_env(env, monkeypatch)
    runs = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'tests': [
        {'test': 'unlock', 'options': {'region': 'jp'}}, {'test': 'ipcheck'}]}))['runs']
    assert [(r['test'], r['options'], r['suite'], r['state']) for r in runs] == [
        ('unlock', {'region': 'jp'}, '', 'queued'), ('ipcheck', {'family': '4', 'report': False}, '', 'queued')]
    both = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'suite': True, 'options': {'family': '46'},
                                                              'tests': [{'test': 'geekbench5'}]}))['runs']
    assert [r['test'] for r in both] == ['hwcheck', 'ipcheck', 'netcheck', 'netroute', 'geekbench5']
    # The suite's four share one suite id; a test beside it is its own run.
    assert len({r['suite'] for r in both[:4]}) == 1 and both[4]['suite'] == ''
    for body, status in [({'serverId': sid, 'tests': []}, 400), ({'serverId': sid, 'tests': 'ipcheck'}, 400),
                         ({'serverId': sid, 'test': 'ipcheck', 'tests': [{'test': 'unlock'}]}, 400),
                         ({'serverId': sid, 'tests': [{'test': 'unlock'}], 'options': {}}, 400),
                         ({'serverId': sid, 'tests': [{'test': 'unlock', 'extra': 1}]}, 400),
                         ({'serverId': sid, 'tests': [{'test': 'unlock'}, {'test': 'unlock'}]}, 400),
                         ({'serverId': sid, 'suite': True, 'tests': [{'test': 'ipcheck'}]}, 400),
                         ({'serverId': sid, 'tests': [{'test': 'nope'}]}, 404)]:
        await api(env, 'POST', '/api/servers/bench/runs', body, status=status)
    assert len((await api(env, 'GET', '/api/servers/bench/runs'))['runs']) == 7, 'a refused request queues nothing'


async def test_stopping_queued_and_running_runs(env, monkeypatch):  # noqa: F811
    node, sid = await bench_env(env, monkeypatch)
    first = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'unlock'}))['runs'][0]
    second = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'tcpquality'}))['runs'][0]
    job = await a_job(node)
    stopped = (await api(env, 'POST', f'/api/servers/bench/runs/{second["id"]}/stop'))['run']
    assert stopped['state'] == 'stopped'
    # The node has the job before the run records it; a run still preparing leaves job.stop to its waiter.
    await run_state(env, first['id'], 'running')
    await api(env, 'POST', f'/api/servers/bench/runs/{first["id"]}/stop')
    assert job['state'] == 'stopped'
    ended = await run_state(env, first['id'], 'stopped', 'failed', 'done')
    assert ended['state'] == 'stopped'
    await asyncio.sleep(0.1)
    assert len(node.started()) == 1


async def test_reports_from_the_envelope_and_the_log(env, monkeypatch):  # noqa: F811
    node, sid = await bench_env(env, monkeypatch)
    unlock = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'unlock'}))['runs'][0]
    job = await a_job(node)
    node.finish(job, files={'envelope.json': json.dumps({'format': 'guko-unified-v1', 'terminal': 'Netflix  Yes\n', 'report': {'n': 1}})}, code=1)
    done = await run_state(env, unlock['id'], 'done')
    assert done['sections'] == [{'label': '', 'ansi': 'Netflix  Yes\n', 'json': {'n': 1}}]

    tq = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'tcpquality', 'options': {'mode': 'route-v4'}}))['runs'][0]
    job = await a_job(node, 2)
    node.finish(job, log='[!] installing\n\x1b[H\x1b[2JTcpQuality\n  进度 1/2\r  进度 2/2\n报告：https://tcpquality.ibsgss.uk/r/AbC_12-x9\n')
    done = await run_state(env, tq['id'], 'done')
    assert done['sections'][0]['ansi'].startswith('TcpQuality\n  进度 2/2\n')
    assert done['sections'][0]['link'] == 'https://tcpquality.ibsgss.uk/r/AbC_12-x9' and done['links'] == [done['sections'][0]['link']]


async def test_failures_no_report_and_a_damaged_package(env, monkeypatch):  # noqa: F811
    node, sid = await bench_env(env, monkeypatch)
    run = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'hwcheck'}))['runs'][0]
    job = await a_job(node)
    node.finish(job, log='curl: (6) Could not resolve host\n', code=7)
    failed = await run_state(env, run['id'], 'failed')
    assert (failed['error'], failed['exitCode'], failed['log']) == ('bench_no_report', 7, 'curl: (6) Could not resolve host\n')

    listing = '/var/tmp/openbear-bench/hardware.check.place-v0.1.0/.openbear-files.sha256'
    assert listing in node.files[sid]
    writes = sum(1 for _, m, p in node.calls if m == 'fs.write' and p['path'] == listing)
    run = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'hwcheck'}))['runs'][0]
    job = await a_job(node, 2)
    node.finish(job, log='评测包文件校验失败\n', code=catalog.CHECKSUM_FAILED)
    # Damaged on the node: pushed again and run once more, in a directory of its own.
    again = await a_job(node, 3)
    assert sum(1 for _, m, p in node.calls if m == 'fs.write' and p['path'] == listing) == writes + 1
    assert node.out_dir(again) == node.out_dir(job) + '.1'
    node.finish(again, log='评测包文件校验失败\n', code=catalog.CHECKSUM_FAILED)
    failed = await run_state(env, run['id'], 'failed')
    assert failed['error'] == 'bench_package_corrupt'
    await until(lambda: asyncio.sleep(0, result=listing not in node.files[sid]))
    await asyncio.sleep(0.1)
    assert len(node.started()) == 3, 'once more only'


async def test_unsupported_platforms_and_bad_requests(env, monkeypatch):  # noqa: F811
    node, sid = await bench_env(env, monkeypatch, platform='windows')
    await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'ipcheck'}, status=409)
    linux = await server(env, 'arm', features=('mesh', 'jobs'))
    env.servers.hub.sessions[linux].info['arch'] = 'arm64'
    assert (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': linux, 'test': 'hwcheck'}))['runs'][0]['state'] == 'queued'
    for body, status in [({'serverId': linux, 'test': 'nope'}, 404), ({'serverId': linux, 'test': 'ipcheck', 'options': {'family': 'x'}}, 400),
                         ({'serverId': linux, 'suite': True, 'options': {'mode': 'full'}}, 400), ({'test': 'ipcheck'}, 400),
                         ({'serverId': linux, 'test': 'ipcheck', 'extra': 1}, 400)]:
        await api(env, 'POST', '/api/servers/bench/runs', body, status=status)
    await api(env, 'GET', '/api/servers/bench/runs/missing', status=404)
    catalogue = await api(env, 'GET', '/api/servers/bench/catalog')
    assert catalogue['suite']['name'] == '综合评测' and catalogue['tests'][0]['version'] == 'v0.1.0'
    assert catalogue['openwrt']['tests'] == ['unlock'] and 'Service Unlock' in catalogue['openwrt']['reason']


async def test_an_openwrt_router_runs_service_unlock_alone_and_keeps_no_package(env, monkeypatch):  # noqa: F811
    node, sid = await bench_env(env, monkeypatch, arch='arm64')
    env.servers.hub.sessions[sid].info['openwrt'] = True
    for body in [{'serverId': sid, 'test': 'ipcheck'}, {'serverId': sid, 'suite': True},
                 {'serverId': sid, 'tests': [{'test': 'unlock'}, {'test': 'geekbench5'}]}]:
        assert (await api(env, 'POST', '/api/servers/bench/runs', body, status=409))['error'] == 'bench_openwrt_unsupported'
    assert not node.started(), 'nothing of a refused request is queued'
    run = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'unlock'}))['runs'][0]
    job = await a_job(node)
    base = catalog.package_dir('unlock')
    assert f'{base}/{bench_module.LISTING}' in node.files[sid]
    node.finish(job, files={'envelope.json': json.dumps({'terminal': 'Netflix  Yes\n', 'report': {'n': 1}})})
    assert (await run_state(env, run['id'], 'done', 'failed'))['state'] == 'done'
    # /var/tmp is a RAM disk there: the package goes with the run's directory.
    await until(lambda: asyncio.sleep(0, result=not any(p.startswith(base + '/') for p in node.files[sid])))


async def test_geekbench_that_could_not_run_fails(env, monkeypatch):  # noqa: F811
    node, sid = await bench_env(env, monkeypatch)
    run = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'geekbench5'}))['runs'][0]
    job = await a_job(node)
    # run.sh writes its report whatever happened; 127: a glibc program on a musl system.
    node.finish(job, files={'report-4.ansi': 'Geekbench 5\n单核成绩：未取得\n', 'result-4.json': '{"url":"","single":"","multi":"","oom":false}'},
                log='run.sh: line 33: geekbench5: required file not found\n', code=127)
    failed = await run_state(env, run['id'], 'done', 'failed')
    assert (failed['state'], failed['error'], failed['exitCode']) == ('failed', 'bench_script_failed', 127)
    again = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'geekbench5'}))['runs'][0]
    job = await a_job(node, 2)
    node.finish(job, files={'report-4.ansi': 'Geekbench 5\n单核成绩：1000\n', 'result-4.json': '{"url":"https://browser.geekbench.com/v5/cpu/1","single":"1000"}'})
    assert (await run_state(env, again['id'], 'done', 'failed'))['state'] == 'done'


async def test_a_restart_follows_the_job_it_left_running(env, monkeypatch):  # noqa: F811
    node, sid = await bench_env(env, monkeypatch)
    run = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'ipcheck'}))['runs'][0]
    job = await a_job(node)
    await run_state(env, run['id'], 'running')
    bench = env.servers.bench
    for task in list(bench.workers.values()):
        task.cancel()
    await asyncio.sleep(0.05)
    assert not bench.workers
    node.finish(job, files={'report-4.ansi': REPORT})
    await bench.start()
    assert (await run_state(env, run['id'], 'done'))['sections'][0]['label'] == 'IPv4'


async def test_deleting_the_server_takes_its_runs(env, monkeypatch):  # noqa: F811
    node, sid = await bench_env(env, monkeypatch)
    run = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'ipcheck'}))['runs'][0]
    await a_job(node)
    await api(env, 'DELETE', f'/api/servers/{sid}')
    await api(env, 'GET', f'/api/servers/bench/runs/{run["id"]}', status=404)
    assert sid not in env.servers.bench.workers


# ----- the Servers tool -----
async def test_the_servers_tool_runs_reads_and_removes_benchmarks(env, monkeypatch):  # noqa: F811
    from app.tools import servers as servers_tool
    from tests.fake_clock import FastClock
    from tests.test_servers import _tool
    node, sid = await bench_env(env, monkeypatch)
    # bench_wait polls once a second: on a tenth-speed clock the timeout below is real but short,
    # and the waits that end do not sit out a whole poll. Their limits leave room for a loaded machine.
    FastClock().install(monkeypatch, servers_tool)
    call = await _tool(env, 790, 'conv-bench')
    described = await call('describe', topic='bench')
    assert described['actions'] == ['bench_catalog', 'bench_runs', 'bench_start', 'bench_wait', 'bench_get', 'bench_stop', 'bench_delete']
    assert 'bench' in (await call('describe'))['topics']
    assert {t['id'] for t in (await call('bench_catalog'))['tests']} == set(catalog.TESTS)

    assert (await call('bench_start', test='ipcheck'))['error'] == 'server_id_required'
    assert (await call('bench_start', serverId=sid, test='ipcheck', options={'family': 'x'}))['error'] == 'bench_invalid_option'
    started = await call('bench_start', serverId=sid, test='ipcheck', options={'family': '46'})
    run = started['runs'][0]
    assert (run['server'], run['label'], run['state']) == ('OVH.SG', 'IPv4 + IPv6', 'queued')
    job = await a_job(node)
    waited = await call('bench_wait', runId=run['id'], timeoutSeconds=1)
    assert waited['reason'] == 'timeout' and waited['run']['state'] == 'running'
    long_text = ('\x1b[32m' + 'x' * 100 + '\x1b[0m\n') * 150
    node.finish(job, files={'report-4.ansi': long_text, 'result-4.json': '{"Head": {"IP": "1.2.*.*"}}', 'report-6.ansi': REPORT}, log='\x1b[1mlog\x1b[0m\n')
    waited = await call('bench_wait', runId=run['id'], timeoutSeconds=60)
    assert waited['reason'] == 'ended' and waited['run']['state'] == 'done'

    got = await call('bench_get', runId=run['id'])
    assert got['untrustedData'].startswith('来源：节点 OVH.SG')
    first, second = got['sections']
    assert (first['label'], first['textChars'], first['truncated'], first['hasJson']) == ('IPv4', 15150, True, True)
    assert len(first['text']) == 10000 and '\x1b' not in first['text'] and second['text'] == 'IP质量体检报告\n风险：低\n'
    assert got['logChars'] == len('\x1b[1mlog\x1b[0m\n') and 'note' in got
    rest = await call('bench_get', runId=run['id'], part='text', section=0, offset=10000)
    assert (rest['offset'], rest['next'], rest['total'], len(rest['content'])) == (10000, 15150, 15150, 5150)
    assert json.loads((await call('bench_get', runId=run['id'], part='json'))['content']) == {'Head': {'IP': '1.2.*.*'}}
    assert (await call('bench_get', runId=run['id'], part='log'))['content'] == 'log\n'
    assert (await call('bench_get', runId=run['id'], part='json', section=5))['error'] == 'bench_section_not_found'
    assert (await call('bench_get', runId=run['id'], part='html'))['error'] == 'invalid_parameters'

    queued = (await call('bench_start', serverId=sid, test='unlock'))['runs'][0]
    second_job = await a_job(node, 2)
    assert [r['id'] for r in (await call('bench_runs', test='unlock'))['runs']] == [queued['id']]
    assert (await call('bench_runs', state='done'))['count'] == 1
    assert (await call('bench_delete', runId=queued['id']))['error'] == 'bench_run_active'
    stopped = await call('bench_stop', runId=queued['id'])
    assert stopped['run']['stopping'] and second_job['state'] == 'stopped'
    assert (await call('bench_wait', runId=queued['id'], timeoutSeconds=60))['run']['state'] == 'stopped'
    assert (await call('bench_delete', runId=run['id']))['ok']
    assert (await call('bench_get', runId=run['id']))['error'] == 'bench_run_not_found'
    assert (await call('bench_wait'))['error'] == 'run_id_required'


async def test_a_run_waits_for_its_node_and_finds_a_job_it_lost(env, monkeypatch):  # noqa: F811
    node, sid = await bench_env(env, monkeypatch)
    session = env.servers.hub.sessions.pop(sid)
    async with env.db.write_transaction(label='t') as conn:
        await conn.execute("UPDATE server_nodes SET info_json=? WHERE server_id=?", (json.dumps({'info': {'platform': 'linux', 'arch': 'amd64'}}), sid))
    run = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'unlock'}))['runs'][0]
    await asyncio.sleep(0.3)
    waiting = (await api(env, 'GET', f'/api/servers/bench/runs/{run["id"]}'))['run']
    assert waiting['state'] == 'preparing' and not node.started(), 'it waits for the node, as after a controller restart'
    env.servers.hub.sessions[sid] = session
    job = await a_job(node)
    node.finish(job, files={'envelope.json': json.dumps({'terminal': 'ok\n', 'report': {}})})
    assert (await run_state(env, run['id'], 'done'))['state'] == 'done'

    # The controller went away between starting a job and noting it: the job is found, not started twice.
    second = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'unlock'}))['runs'][0]
    job = await a_job(node, 2)
    await run_state(env, second['id'], 'running')
    for task in list(env.servers.bench.workers.values()):
        task.cancel()
    await asyncio.sleep(0.05)
    async with env.db.write_transaction(label='t') as conn:
        await conn.execute("UPDATE server_bench_runs SET state='preparing', job_id='' WHERE id=?", (second['id'],))
    await env.servers.bench.start()
    resumed = await run_state(env, second['id'], 'running')
    assert resumed['jobId'] == job['id'] and len(node.started()) == 2


async def test_a_stop_is_never_lost(env, monkeypatch):  # noqa: F811
    node, sid = await bench_env(env, monkeypatch)
    # Asked while the job is being started.
    original, ids = env.servers.job_start, []

    async def start_then_stop(*args, **kwargs):
        await api(env, 'POST', f'/api/servers/bench/runs/{ids[0]}/stop')
        return await original(*args, **kwargs)
    env.servers.job_start = start_then_stop
    run = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'ipcheck'}))['runs'][0]
    ids.append(run['id'])
    job = await a_job(node)
    await until(lambda: asyncio.sleep(0, result=job['state'] == 'stopped'))
    assert (await run_state(env, run['id'], 'stopped', 'done', 'failed'))['state'] == 'stopped'
    env.servers.job_start = original

    # Asked while the node is away: told once it is back.
    run = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'ipcheck'}))['runs'][0]
    job = await a_job(node, 2)
    await run_state(env, run['id'], 'running')
    session = env.servers.hub.sessions.pop(sid)
    stopping = (await api(env, 'POST', f'/api/servers/bench/runs/{run["id"]}/stop'))['run']
    assert stopping['stopping'] and job['state'] == 'running'
    env.servers.hub.sessions[sid] = session
    await until(lambda: asyncio.sleep(0, result=job['state'] == 'stopped'))
    assert (await run_state(env, run['id'], 'stopped'))['state'] == 'stopped'
    logged = [r['command'] for r in await api_log(env, sid)]
    assert logged.count('评测：停止 IP.Check.Place') == 2


async def test_a_node_without_command_execution_is_refused(env, monkeypatch):  # noqa: F811
    node, sid = await bench_env(env, monkeypatch)
    env.servers.hub.sessions[sid].caps = {'exec': False}
    refused = await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'ipcheck'}, status=409)
    assert refused['error'] == 'bench_exec_disabled'


async def test_one_unpack_serves_concurrent_pushes(tmp_path):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w:gz') as tar:
        for i in range(30):
            data = bytes([i]) * (1 << 20)
            info = tarfile.TarInfo(f'T/f{i}')
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    blob = archive.getvalue()
    digest = hashlib.sha256(blob).hexdigest()
    cache = tmp_path / 'cache'
    cache.mkdir()
    (cache / digest).write_bytes(blob)
    root = tmp_path / 'pkg'
    (root / 'tool/v0.1.0').mkdir(parents=True)
    (root / 'tool/v0.1.0/manifest.json').write_text(json.dumps({'tool': 'tool', 'version': 'v0.1.0', 'platforms': ['linux/amd64'], 'files': {},
        'binaries': [{'platform': 'linux/amd64', 'dest': 'bin/t', 'url': 'x', 'sha256': digest, 'unpack': 'tar', 'strip': 1}]}))

    async def push():
        found = await packages.files('tool', 'v0.1.0', 'linux/amd64', cache, root=root)
        return sum(len(path.read_bytes()) for _, path, _ in found)
    assert await asyncio.gather(*(push() for _ in range(4))) == [30 << 20] * 4


def test_colours_never_leave_pieces_of_a_sequence():
    import re
    text = ''.join(('\x1b[1m', '\x1b[38;5;196m', '\x1b[1;31m')[i % 3] for i in range(31)) + 'X\n'
    assert screen(text) == '\x1b[1;31mX\x1b[0m\n'
    out = screen('\x1b[36m\x1b[32mA\x1b[1mB\x1b[22mC\x1b[0mD\x1b[7;44mE\x1b[27;49mF\x1b[38;2;1;2;3mG\n')
    assert out == '\x1b[32mA\x1b[0m\x1b[1;32mB\x1b[0m\x1b[32mC\x1b[0mD\x1b[7;44mE\x1b[0mF\x1b[38;2;1;2;3mG\x1b[0m\n'
    assert re.sub(r'\x1b\[[0-9;]*m', '', out) == 'ABCDEFG\n'


async def test_stopping_while_the_report_is_collected_from_a_node_gone_away(env, monkeypatch):  # noqa: F811
    """The job ended and the node went away before its report was read: 停止 gives the report up (rather than
    answering ok and leaving the run waiting hours for the node) and the server's queue moves on."""
    node, sid = await bench_env(env, monkeypatch)
    first = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'unlock'}))['runs'][0]
    second = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'tcpquality'}))['runs'][0]
    job = await a_job(node)
    await run_state(env, first['id'], 'running')
    async with env.db.write_transaction(label='t') as conn:
        await conn.execute("UPDATE server_nodes SET info_json=? WHERE server_id=?", (json.dumps({'info': {'platform': 'linux', 'arch': 'amd64'}}), sid))
    held, original = [], node.call

    async def call(server_id, method, params, timeout):
        result = await original(server_id, method, params, timeout)
        if method == 'job.status' and node.jobs[params['id']]['state'] != 'running' and server_id in env.servers.hub.sessions:
            held.append(env.servers.hub.sessions.pop(server_id))
        return result
    env.servers.hub.call = call
    node.finish(job, files={'envelope.json': json.dumps({'terminal': 'ok\n', 'report': {}})})
    await run_state(env, first['id'], 'collecting')
    await asyncio.sleep(0.2)
    assert (await api(env, 'GET', f'/api/servers/bench/runs/{first["id"]}'))['run']['state'] == 'collecting', 'waiting for the node'
    stopping = (await api(env, 'POST', f'/api/servers/bench/runs/{first["id"]}/stop'))['run']
    assert stopping['stopping'] and stopping['state'] == 'collecting'
    stopped = await run_state(env, first['id'], 'stopped')
    assert stopped['endedMs'] and not stopped['sections']
    assert (await run_state(env, second['id'], 'preparing'))['state'] == 'preparing', 'the next run is no longer held up'
    env.servers.hub.call = original
    env.servers.hub.sessions[sid] = held[0]
    job = await a_job(node, 2)
    node.finish(job, log='TcpQuality\n')
    assert (await run_state(env, second['id'], 'done', 'failed'))['state'] in ('done', 'failed')


# ----- time limits -----
def test_each_test_has_a_time_limit_by_what_it_runs():
    """Several times the longest run seen of each (production, 2026-11: unlock 14 s, ipcheck 171 s, hwcheck 178 s,
    geekbench5 149 s, netroute 411 s, netcheck low 471 s / full 664 s), never under a quarter of an hour."""
    seen_s = {'unlock': 14, 'ipcheck': 171, 'hwcheck': 178, 'geekbench5': 149, 'netroute': 411, 'netcheck': 664, 'tcpquality': 16}
    for test in catalog.TESTS:
        minutes = catalog.time_limit(test, catalog.options(test, {}))
        assert minutes >= 15 and minutes * 60 >= 5 * seen_s[test], test
    assert catalog.time_limit('unlock', {'region': 'auto'}) == 15
    assert catalog.time_limit('ipcheck', {'family': '4'}) == 30
    assert catalog.time_limit('ipcheck', {'family': '46'}) == 60
    assert catalog.time_limit('netcheck', {'family': '4', 'mode': 'low'}) == 60
    assert catalog.time_limit('netcheck', {'family': '46', 'mode': 'full'}) == 240
    assert catalog.time_limit('hwcheck', {'report': True}) == 120
    assert [catalog.time_limit('tcpquality', {'mode': m}) for m in catalog.TCPQ_MODES] == [60, 60, 30, 30, 60, 90, 180]
    assert (catalog.limit_words(15), catalog.limit_words(120)) == ('15 分钟', '2 小时')


async def test_a_run_past_its_limit_is_stopped_and_the_queue_moves_on(env, monkeypatch):  # noqa: F811
    """A job that hangs past its test's limit, by the node's own count of its running time, is stopped on the
    node and the run ends as interrupted with the limit as its reason; the next run of the server goes on
    and ends as it would, one under its limit is left running."""
    node, sid = await bench_env(env, monkeypatch)
    hung = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'unlock'}))['runs'][0]
    after = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'tcpquality', 'options': {'mode': 'route-v4'}}))['runs'][0]
    job = await a_job(node)
    await run_state(env, hung['id'], 'running')
    job['durationMs'] = 15 * 60000 - 1000
    polled = len(node.calls)
    await until(lambda: asyncio.sleep(0, len(node.calls) > polled + 5))
    assert job['state'] == 'running' and (await api(env, 'GET', f'/api/servers/bench/runs/{hung["id"]}'))['run']['state'] == 'running'
    job['durationMs'] = 15 * 60000
    ended = await run_state(env, hung['id'], 'interrupted')
    assert job['state'] == 'stopped'
    assert (ended['error'], ended['detail'], ended['stopping']) == ('bench_timeout', '超过 15 分钟未完成，已自动停止', False)
    # The next one runs as usual, its own limit (30 minutes) not reached.
    second = await a_job(node, 2)
    second['durationMs'] = 20 * 60000
    node.finish(second, log='\x1b[2J回程线路\n')
    done = await run_state(env, after['id'], 'done', 'failed')
    assert (done['state'], done['error'], done['detail']) == ('done', '', '')


async def test_a_run_past_its_limit_with_its_node_away_frees_the_queue(env, monkeypatch):  # noqa: F811
    """The limit passes while the node is away: the run ends at once rather than waiting hours for the node,
    and its job is stopped once the node is back, before the next run's job starts."""
    node, sid = await bench_env(env, monkeypatch)
    hung = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'unlock'}))['runs'][0]
    after = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'tcpquality', 'options': {'mode': 'route-v4'}}))['runs'][0]
    job = await a_job(node)
    await run_state(env, hung['id'], 'running')
    async with env.db.write_transaction(label='t') as conn:
        await conn.execute("UPDATE server_nodes SET info_json=? WHERE server_id=?", (json.dumps({'info': {'platform': 'linux', 'arch': 'amd64'}}), sid))
    job['durationMs'] = 15 * 60000 - 200
    polled = len(node.calls)
    await until(lambda: asyncio.sleep(0, len(node.calls) > polled + 2))
    session = env.servers.hub.sessions.pop(sid)
    ended = await run_state(env, hung['id'], 'interrupted')
    assert (ended['error'], ended['detail']) == ('bench_timeout', '超过 15 分钟未完成，已中断，等待节点上线后停止作业')
    assert job['state'] == 'running' and len(node.started()) == 1
    await api(env, 'DELETE', f'/api/servers/bench/runs/{hung["id"]}', status=409)
    env.servers.hub.sessions[sid] = session
    second = await a_job(node, 2)
    assert job['state'] == 'stopped'
    methods = [(m, p.get('id')) for _, m, p in node.calls if m in ('job.stop', 'job.start')]
    assert methods[0] == methods[-1] == ('job.start', None)
    assert methods[1:-1] and all(m == ('job.stop', job['id']) for m in methods[1:-1])
    assert (await api(env, 'GET', f'/api/servers/bench/runs/{hung["id"]}'))['run']['detail'] == '超过 15 分钟未完成，已自动停止'
    node.finish(second, log='回程线路\n')
    assert (await run_state(env, after['id'], 'done', 'failed'))['state'] == 'done'


async def test_a_run_stopped_at_its_limit_whose_node_goes_away_while_collecting(env, monkeypatch):  # noqa: F811
    """Stopped at its limit, then the node went away before its report was read: the run is not held up for
    the node either, and still ends as interrupted at its limit rather than stopped by the user."""
    node, sid = await bench_env(env, monkeypatch)
    hung = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'unlock'}))['runs'][0]
    job = await a_job(node)
    await run_state(env, hung['id'], 'running')
    held, original = [], node.call

    async def call(server_id, method, params, timeout):
        result = await original(server_id, method, params, timeout)
        if method == 'job.status' and node.jobs[params['id']]['state'] == 'stopped' and server_id in env.servers.hub.sessions:
            held.append(env.servers.hub.sessions.pop(server_id))
        return result
    env.servers.hub.call = call
    job['durationMs'] = 15 * 60000
    ended = await run_state(env, hung['id'], 'interrupted', 'stopped')
    assert held and (ended['state'], ended['error'], ended['detail']) == ('interrupted', 'bench_timeout', '超过 15 分钟未完成，已自动停止')


async def test_timeout_stop_is_resumed_after_restart_even_after_a_long_disconnection(env, monkeypatch):
    node, sid = await bench_env(env, monkeypatch)
    bench = env.servers.bench
    run = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'unlock'}))['runs'][0]
    job = await a_job(node)
    await run_state(env, run['id'], 'running')
    tasks = list(bench.workers.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await bench._set(run['id'], state='interrupted', error=bench_module.TIMED_OUT, stop=bench_module.ASKED)
    session = env.servers.hub.sessions.pop(sid)
    # The interrupted run is no longer in a queue. Startup must recover its pending stop separately.
    monkeypatch.setattr(bench_module, 'AWAY_LIMIT_S', 0)
    await bench.start()
    await asyncio.sleep(0.2)
    assert job['state'] == 'running'
    env.servers.hub.sessions[sid] = session
    async def saved_stop():
        row = await bench._get(run['id'])
        return row['stop'] == bench_module.SENT
    await until(saved_stop)
    assert job['state'] == 'stopped'
    kept = (await api(env, 'GET', f'/api/servers/bench/runs/{run["id"]}'))['run']
    assert kept['state'] == 'interrupted' and kept['detail'] == '超过 15 分钟未完成，已自动停止'


async def test_queue_time_is_not_running_time_and_old_reports_are_kept(env, monkeypatch):
    node, sid = await bench_env(env, monkeypatch)
    bench = env.servers.bench
    first = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'tcpquality'}))['runs'][0]
    job = await a_job(node)
    second = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'unlock'}))['runs'][0]
    await bench._set(second['id'], created_ms=1)
    node.finish(job, log='retained report\n')
    done = await run_state(env, first['id'], 'done')
    await bench._set(first['id'], created_ms=1, started_ms=1, ended_ms=2)
    next_job = await a_job(node, 2)
    await run_state(env, second['id'], 'running')
    await asyncio.sleep(0.1)
    assert next_job['state'] == 'running'
    node.finish(next_job, files={'envelope.json': json.dumps({'terminal': 'ok\n', 'report': {}})})
    await run_state(env, second['id'], 'done')
    await bench.start()
    kept = (await api(env, 'GET', f'/api/servers/bench/runs/{first["id"]}'))['run']
    assert kept['sections'] == done['sections'] and kept['log'] == done['log']


@pytest.mark.parametrize('stage', ['preparing', 'starting_before', 'starting_after', 'running', 'collecting'])
async def test_stop_server_quiesces_every_phase_before_returning(env, monkeypatch, stage):
    node, sid = await bench_env(env, monkeypatch)
    bench = env.servers.bench
    reached, release = asyncio.Event(), asyncio.Event()
    original_start = env.servers.job_start

    if stage.startswith('starting'):
        async def start(*args, **kwargs):
            result = await original_start(*args, **kwargs) if stage == 'starting_after' else None
            reached.set()
            await release.wait()
            return result or await original_start(*args, **kwargs)
        monkeypatch.setattr(env.servers, 'job_start', start)
    elif stage == 'preparing':
        async def push(*args):
            reached.set()
            await release.wait()
        monkeypatch.setattr(bench, '_push', push)
    elif stage == 'collecting':
        async def gather(*args):
            reached.set()
            await release.wait()
        monkeypatch.setattr(bench, '_gather', gather)

    first = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'unlock'}))['runs'][0]
    second = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'tcpquality'}))['runs'][0]
    if stage in ('running', 'collecting'):
        job = await a_job(node)
        await run_state(env, first['id'], 'running')
        if stage == 'collecting':
            node.finish(job, files={'envelope.json': json.dumps({'terminal': 'ok\n', 'report': {}})})
        else:
            reached.set()
    await asyncio.wait_for(reached.wait(), 5)
    owner = (await bench._get(first['id']))['owner']
    stopping = asyncio.create_task(bench.stop_server(owner, sid))
    await until(lambda: asyncio.sleep(0, sid in bench.paused))
    if stage.startswith('starting'):
        await asyncio.sleep(0.05)
        assert not stopping.done(), 'must await the outstanding start receipt, not just cancel its waiter'
    await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'unlock'}, status=409)
    release.set()
    result = await asyncio.wait_for(stopping, 5)
    assert result['ok'] and set(result['runs']) == {first['id'], second['id']}
    assert sid not in bench.workers
    assert all(j['state'] != 'running' for j in node.started())
    assert len(node.started()) == (0 if stage == 'preparing' else 1)
    for run in (first, second):
        assert (await bench._get(run['id']))['state'] == 'stopped'
    bench._kick(sid)
    await asyncio.sleep(0.05)
    assert sid not in bench.workers and len(node.started()) <= 1
    bench.resume_server(sid)
    await asyncio.sleep(0.05)
    assert len(node.started()) <= 1, 'cancelled queue entries never start after the fence is released'


async def test_stop_server_does_not_confirm_a_job_that_is_still_running(env, monkeypatch):
    node, sid = await bench_env(env, monkeypatch)
    bench = env.servers.bench
    run = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'unlock'}))['runs'][0]
    job = await a_job(node)
    await run_state(env, run['id'], 'running')
    row = await bench._get(run['id'])
    original = env.servers.job_stop

    async def not_stopped(*args, **kwargs):
        return {'ok': True, 'stopped': False, 'job': {'state': 'running'}}
    monkeypatch.setattr(env.servers, 'job_stop', not_stopped)
    with pytest.raises(catalog.InstanceError, match='bench_run_active'):
        await bench.stop_server(row['owner'], sid)
    assert sid in bench.paused and sid not in bench.workers and job['state'] == 'running'
    monkeypatch.setattr(env.servers, 'job_stop', original)
    await bench.stop_server(row['owner'], sid)
    assert job['state'] == 'stopped'
    bench.resume_server(sid)
