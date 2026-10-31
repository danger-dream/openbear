import asyncio
import json
import os
import re
import subprocess

import pytest

from app.instances.contracts import InstanceError
from app.servers import bench_packages as packages
from tests.test_servers import api, env, shipped  # noqa: F401
from tests.test_servers_bench import a_job, bench_env, own_package_locks, run_state  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401


@pytest.mark.parametrize('restart', [False, True])
async def test_stop_recovers_lost_start_receipt_before_releasing_queue(env, monkeypatch, restart):
    node, sid = await bench_env(env, monkeypatch)
    bench = env.servers.bench
    original = env.servers.job_start
    sent, retry = asyncio.Event(), asyncio.Event()

    async def start(*args, **kwargs):
        result = await original(*args, **kwargs)
        sent.set()
        if restart:
            await asyncio.Event().wait()
        raise InstanceError('node_connection_lost', 503)

    async def away(since):
        await retry.wait()
        return True

    monkeypatch.setattr(env.servers, 'job_start', start)
    monkeypatch.setattr(bench, '_away', away)
    run = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'tcpquality'}))['runs'][0]
    await asyncio.wait_for(sent.wait(), 5)
    assert node.started()[0]['state'] == 'running'
    if restart:
        tasks = list(bench.workers.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # An N-1 preparing row has no marker yet.
        await bench._set(run['id'], job_state='')
        kick = bench._kick
        monkeypatch.setattr(bench, '_kick', lambda _: None)
        await bench.start()
        monkeypatch.setattr(bench, '_kick', kick)
    stopped = (await api(env, 'POST', f'/api/servers/bench/runs/{run["id"]}/stop'))['run']
    assert stopped['stopping'] and stopped['state'] == 'preparing'
    retry.set()
    if restart:
        bench._kick(sid)
    done = await run_state(env, run['id'], 'stopped')
    assert done['jobId'] == node.started()[0]['id']
    assert len(node.started()) == 1 and node.started()[0]['state'] == 'stopped'


async def test_tcpquality_failure_is_not_a_successful_report(env, monkeypatch):
    node, sid = await bench_env(env, monkeypatch)
    run = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'tcpquality'}))['runs'][0]
    job = await a_job(node)
    node.finish(job, log='dependency failed\n', code=1)
    done = await run_state(env, run['id'], 'failed', 'done')
    assert done['state'] == 'failed' and done['error'] == 'bench_script_failed'
    assert done['sections'][0]['ansi'] == 'dependency failed\n'


async def test_tcpquality_checksum_failure_repushes_even_with_a_log(env, monkeypatch):
    node, sid = await bench_env(env, monkeypatch)
    run = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'tcpquality'}))['runs'][0]
    job = await a_job(node)
    node.finish(job, log='评测包文件校验失败，下次运行时重新推送\n', code=86)
    retried = await a_job(node, 2)
    node.finish(retried, log='completed\n', code=0)
    done = await run_state(env, run['id'], 'done')
    assert done['jobId'] == retried['id'] and len(node.started()) == 2


@pytest.mark.parametrize('report', [False, True])
async def test_hardware_pins_geekbench_only_for_online_report(env, monkeypatch, report):
    node, sid = await bench_env(env, monkeypatch)
    calls, real = [], packages.files

    async def files(tool, *args, **kwargs):
        calls.append(tool)
        return await real(tool, *args, **kwargs)

    monkeypatch.setattr(packages, 'files', files)
    run = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'hwcheck', 'options': {'report': report}}))['runs'][0]
    job = await a_job(node)
    base = '/var/tmp/openbear-bench/hardware.check.place-v0.1.0/'
    assert ('geekbench5' in calls) == report
    assert any(path.startswith(base + 'bin/geekbench/') for path in node.files[sid]) == report
    assert ('  bin/geekbench/' in node.files[sid][base + '.openbear-files.sha256'].decode()) == report
    node.finish(job, files={'report-4.ansi': 'hardware report\n'})
    await run_state(env, run['id'], 'done')


def extract_function(text, name, end='}'):
    start = re.search(rf'^{name}\(\)\s*[{{(]', text, re.M).start()
    stop = text.index('\n' + end, start) + len(end) + 1
    return text[start:stop] + '\n'


@pytest.mark.host_commands
def test_geekbench_claim_filter_is_before_live_and_saved_output(tmp_path):
    tool = tmp_path / 'bin/geekbench'
    tool.mkdir(parents=True)
    binary = tool / 'geekbench5'
    binary.write_text('#!/bin/sh\nprintf "%s\\n" "https://browser.geekbench.com/v5/cpu/123/claim?key=private-test-claim"\nexit 7\n')
    binary.chmod(0o755)
    env = {**os.environ, 'OB_BENCH_DIR': str(tmp_path), 'OB_BENCH_OUT': str(tmp_path)}
    source = (packages.ROOT / 'hardware.check.place/v0.1.0/hardware.sh').read_text()
    wrapper = extract_function(source, 'geekbench5')
    result = subprocess.run(['bash', '-c', wrapper + 'geekbench5 --cpu'], env=env, text=True, capture_output=True, timeout=5)
    assert result.returncode == 7 and result.stdout == 'https://browser.geekbench.com/v5/cpu/123\n'
    source = (packages.ROOT / 'geekbench5/v0.1.0/run.sh').read_text()
    pipeline = next(line for line in source.splitlines() if line.startswith('"$gb" --upload'))
    result = subprocess.run(['bash', '-c', 'gb="$OB_BENCH_DIR/bin/geekbench/geekbench5"; out="$OB_BENCH_OUT";\n' + pipeline + '\nexit "${PIPESTATUS[0]}"'], env=env, text=True, capture_output=True, timeout=5)
    assert result.returncode == 7 and 'private-test-claim' not in result.stdout + result.stderr
    assert (tmp_path / 'geekbench.log').read_text() == 'https://browser.geekbench.com/v5/cpu/123\n'


@pytest.mark.host_commands
def test_tcpquality_probe_owns_its_signal_cleanup(tmp_path):
    source = (packages.ROOT / 'tcpquality/v0.1.0/runTcpQuality-core.sh').read_text()
    script = '\n'.join(extract_function(source, name) for name in ['speedtest_counter_start', 'speedtest_counter_stop_current'])
    script += extract_function(source, 'speedtest_run_probe', ')')
    script += '''
USE_SUDO=
SPEEDTEST_TOS_REGION=test
SPEEDTEST_TOS_TIMEOUT=15
SPEEDTEST_TOS_NETWORK=public
iptables() { printf '%s\n' "$*" >> "$OB_TEST_LOG"; }
speedtest_tos_bucket_host() { echo example.invalid; }
speedtest_tos_object_size_bytes() { echo 1024; }
speedtest_counter_bytes() { kill -TERM "$PROBE_OWNER"; echo 0; }
'''
    # Record the owner inside the same subshell that creates the chain, not the caller.
    script = script.replace('speedtest_run_probe() (', 'speedtest_run_probe() (\n  PROBE_OWNER=$BASHPID')
    script += 'result=$(speedtest_run_probe download "$OB_TEST_LOG.out" 192.0.2.1); exit "$?"\n'
    log = tmp_path / 'iptables.log'
    result = subprocess.run(['bash', '-c', script], env={**os.environ, 'OB_TEST_LOG': str(log)}, text=True, capture_output=True, timeout=5)
    assert result.returncode == 143
    lines = log.read_text().splitlines()
    chain = next(line.split()[1] for line in lines if line.startswith('-N '))
    assert f'-D INPUT -j {chain}' in lines and f'-F {chain}' in lines and f'-X {chain}' in lines


def test_benchmark_network_requests_are_bounded_and_upload_is_https():
    for tool, entry in [('ip.check.place', 'ip.sh'), ('net.check.place', 'net.sh'), ('hardware.check.place', 'hardware.sh')]:
        source = (packages.ROOT / tool / 'v0.1.0' / entry).read_text()
        upload = next(line for line in source.splitlines() if '-X POST' in line and 'upload.check.place' in line)
        assert '--connect-timeout 10' in upload and '--max-time 60' in upload and 'https://upload.check.place' in upload
    hardware = (packages.ROOT / 'hardware.check.place/v0.1.0/hardware.sh').read_text()
    install = extract_function(hardware, 'install_geekbench5')
    assert 'OB_BENCH_DIR' in install and 'return 1' in install
    for line in hardware.splitlines():
        if 'tmpresu="$(curl-impersonate' in line:
            assert '--connect-timeout' in line and '--max-time' in line
