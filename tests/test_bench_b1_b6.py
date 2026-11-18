"""BENCH-B1–B6: fake-node lifecycle and extracted, offline shell production functions.

Never source either script's main, execute a benchmark, resolve DNS, or install tools.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess

import pytest

from app.instances.contracts import InstanceError
from app.servers import bench as bench_module, bench_packages as packages, ip_quality
from tests.test_servers import api, env, shipped  # noqa: F401
from tests.test_servers_bench import a_job, bench_env, own_package_locks, run_state, until  # noqa: F401
from tests.test_webhooks_backend import env as base_env  # noqa: F401


def source(tool):
    return (packages.ROOT / f'{tool}.check.place/v0.1.0/{tool}.sh').read_text()


def function(text, name):
    header = re.search(rf'^(?:function )?{re.escape(name)}\(\)\s*\{{', text, re.M)
    assert header is not None, name
    # Searching a slice starting at start+1 makes ^ match the same name with its
    # first letter removed. Start after the complete header instead.
    following = re.search(r'^(?:function )?[A-Za-z_][A-Za-z_0-9]*\(\)\s*\{', text[header.end():], re.M)
    end = header.end() + following.start() if following else len(text)
    # Every extracted function here is followed by another named function; no main can be executed.
    assert following is not None
    return text[header.start():end]


def shell(tool, names, code, **values):
    if shutil.which('jq') is None:
        pytest.skip('offline serialization contracts require local jq')
    text = source(tool)
    # Only the top-level declaration preamble: matching the whole source used to pick up
    # `declare -A browsers=(` inside generate_random_user_agent, swallowing functions and
    # fixture assignments as array elements (sometimes with a misleading exit status 0).
    # check_bash precedes the declarations; set_language ends the shared globals block.
    preamble = text[:re.search(r'^set_language\(\)\s*\{', text, re.M).start()]
    declarations = '\n'.join(re.findall(r'^declare -A [A-Za-z_][A-Za-z_0-9]*(?: [A-Za-z_][A-Za-z_0-9]*)*$', preamble, re.M))
    assert declarations, 'global associative-array declarations were not extracted'
    definitions = declarations + '\n' + '\n'.join(function(text, name) for name in names)
    checks = '\n'.join(f'declare -F {name} >/dev/null || exit 97' for name in names)
    script = definitions + '\n' + checks + '\n' + code
    syntax = subprocess.run(['bash', '-n'], input=script, text=True, capture_output=True, timeout=5)
    assert syntax.returncode == 0, syntax.stderr
    result = subprocess.run(['bash', '-c', script], text=True, capture_output=True, timeout=15,
                            env={**os.environ, **{k: str(v) for k, v in values.items()}})
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize('recovery', ['restart', 'next', 'lifecycle'])
async def test_manual_stop_survives_long_offline_and_owns_record(env, monkeypatch, recovery):
    node, sid = await bench_env(env, monkeypatch)
    bench = env.servers.bench
    recovery_tasks = []
    original_spawn = env.servers.spawn

    def spawn(coro):
        task = original_spawn(coro)
        if coro.cr_code.co_name == '_stop_later':
            recovery_tasks.append(task)
        return task

    monkeypatch.setattr(env.servers, 'spawn', spawn)
    run = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'tcpquality'}))['runs'][0]
    job = await a_job(node)
    await run_state(env, run['id'], 'running')
    # The shared fake has a fixed historical startedAt. Keep its clock aligned here:
    # disconnecting before _wait's first status otherwise looks weeks overdue, racing
    # the manual stop against the automatic deadline rather than exercising this case.
    monkeypatch.setattr(bench_module, 'now_ms', lambda: job['startedAt'])
    session = env.servers.hub.sessions.pop(sid)
    await api(env, 'POST', f'/api/servers/bench/runs/{run["id"]}/stop')
    monkeypatch.setattr(bench_module, 'AWAY_LIMIT_S', 0)
    ended = await run_state(env, run['id'], 'interrupted')
    assert ended['error'] == 'node_offline' and ended['stopPending']
    assert recovery_tasks and job['state'] == 'running'
    await asyncio.sleep(0.15)  # several recovery retries beyond the ordinary offline deadline
    row = await bench._get(run['id'])
    assert row['stop'] == bench_module.ASKED
    await api(env, 'DELETE', f'/api/servers/bench/runs/{run["id"]}', status=409)
    with pytest.raises(InstanceError, match='bench_run_active'):
        await bench.forget_server(row['owner'], sid)
    with pytest.raises(InstanceError, match='node_offline'):
        await bench._stop_leftovers(sid)
    for task in recovery_tasks:
        task.cancel()
    await asyncio.gather(*recovery_tasks, return_exceptions=True)
    if recovery == 'restart':
        await bench.start()  # SQL recovery must not be restricted to bench_timeout
        env.servers.hub.sessions[sid] = session
    elif recovery == 'next':
        # Queue recovery must work even with no background stop task left in this process.
        env.servers.hub.sessions[sid] = session
        next_run = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'tcpquality'}))['runs'][0]
        next_job = await a_job(node, 2)
        assert job['state'] == 'stopped'
        stop_index = next(i for i, (_, method, p) in enumerate(node.calls) if method == 'job.stop' and p['id'] == job['id'])
        start_index = max(i for i, (_, method, _) in enumerate(node.calls) if method == 'job.start')
        assert stop_index < start_index
        node.finish(next_job, log='done\n')
        await run_state(env, next_run['id'], 'done')
    else:
        env.servers.hub.sessions[sid] = session
        assert run['id'] in (await bench.stop_server(row['owner'], sid))['runs']

    async def confirmed():
        return (await bench._get(run['id']))['stop'] == bench_module.SENT
    await until(confirmed)
    assert job['state'] == 'stopped'
    await api(env, 'DELETE', f'/api/servers/bench/runs/{run["id"]}')


@pytest.mark.parametrize('job_exists', [True, False])
async def test_pending_stop_with_lost_start_receipt_keeps_marker_responsibility(env, monkeypatch, job_exists):
    node, sid = await bench_env(env, monkeypatch)
    bench = env.servers.bench
    run = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'tcpquality'}))['runs'][0]
    job = await a_job(node)
    await run_state(env, run['id'], 'running')
    tasks = list(bench.workers.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await bench._set(run['id'], state='failed', error='node_offline', stop=bench_module.ASKED, job_id='', job_state='starting')
    session = env.servers.hub.sessions.pop(sid)
    await api(env, 'DELETE', f'/api/servers/bench/runs/{run["id"]}', status=409)
    assert (await bench.summary((await bench._get(run['id']))['owner'], run['id']))['stopPending']
    with pytest.raises(InstanceError, match='node_offline'):
        await bench._stop_leftovers(sid)
    if not job_exists:
        node.jobs.clear()
    env.servers.hub.sessions[sid] = session
    await bench.start()

    async def confirmed():
        return (await bench._get(run['id']))['stop'] == bench_module.SENT
    await until(confirmed)
    if job_exists:
        assert job['state'] == 'stopped'
    await api(env, 'DELETE', f'/api/servers/bench/runs/{run["id"]}')


async def test_stop_receipt_without_terminal_job_is_not_confirmation(env, monkeypatch):
    node, sid = await bench_env(env, monkeypatch)
    bench = env.servers.bench
    run = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': 'tcpquality'}))['runs'][0]
    await a_job(node)
    await run_state(env, run['id'], 'running')

    async def receipt(*a, **kw):
        return {'ok': True, 'job': {'state': 'running'}}
    monkeypatch.setattr(env.servers, 'job_stop', receipt)
    stopped = (await api(env, 'POST', f'/api/servers/bench/runs/{run["id"]}/stop'))['run']
    assert stopped['stopPending']
    assert (await bench._get(run['id']))['stop'] == bench_module.ASKED


@pytest.mark.parametrize('tool', ['ipcheck', 'netcheck', 'netroute'])
@pytest.mark.parametrize('broken', [None, '', '{not JSON}', 'null', '[]'])
async def test_ansi_does_not_hide_invalid_structured_report(env, monkeypatch, tool, broken):
    node, sid = await bench_env(env, monkeypatch)
    bench = env.servers.bench
    if tool == 'ipcheck':
        old = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': tool}))['runs'][0]
        job = await a_job(node)
        node.finish(job, files={'report-4.ansi': 'old\n', 'result-4.json': json.dumps({'Info': {'ASN': '1'}, 'Score': {'IPQS': 0}})})
        await run_state(env, old['id'], 'done')
        # Fake nodes use a fixed end time; make ordering deterministic.
        await bench._set(old['id'], ended_ms=1)
    run = (await api(env, 'POST', '/api/servers/bench/runs', {'serverId': sid, 'test': tool}))['runs'][0]
    job = await a_job(node, 2 if tool == 'ipcheck' else 1)
    package = 'ip' if tool == 'ipcheck' else 'net'
    base = f'/var/tmp/openbear-bench/{package}.check.place-v0.1.0/'
    assert node.files[sid][base + f'{package}.sh'].decode() == source(package)
    files = {'report-4.ansi': 'still readable\n'}
    if broken is not None:
        files['result-4.json'] = broken
    node.finish(job, files=files, code=1)
    done = await run_state(env, run['id'], 'done', 'failed')
    assert done['state'] == 'failed' and done['error'] == 'bench_json_invalid'
    assert done['sections'][0]['ansi'] == 'still readable\n'
    if tool == 'ipcheck':
        quality = await api(env, 'GET', f'/api/servers/{sid}/ip-quality')
        assert quality['report'] is None and quality['error'] == 'bench_json_invalid'
        assert quality['failedRunId'] == run['id']


TEXT = 'Example "Cloud" \\ path\n组织\tname'


@pytest.mark.host_commands
@pytest.mark.parametrize('lite', [0, 1])
def test_ipqs_producer_to_ansi_json_and_quality_and_text_roundtrip(lite):
    out = shell('ip', ['clean_ansi', 'factor_bool', 'json_field', 'json_document', 'save_json', 'db_ipqs', 'sscore_text', 'show_score'], r'''
fullIP=1; mode_lite=0; IP=203.0.113.1
show_progress_bar(){ :; }; kill_progress_bar(){ :; }
curl(){ printf '%s' '{"fraud_score":99,"country_code":"SG","proxy":false}'; }
db_ipqs 4
maxmind[org]="$TEXT"; ipinfo[org]="$TEXT"; shead[bash]="$TEXT"
maxmind[city]="$TEXT"; ipinfo[city]="$TEXT"
ipqs[countrycode]='"X'
# Other sources absent; exercise the real score renderer with valid numeric defaults.
abuseipdb[score]=0; dbip[score]=0
show_score
printf '\n---JSON---\n'
mode_lite="$LITE"; ipjson='{"Head":{},"Info":{},"Type":{},"Score":{},"Factor":{},"Media":{},"Mail":{}}'
smail[t]=4; smail[c]=1; smail[m]=0; smail[b]=1; smail[f]=1; smail[u]=1; smail[answered]=2
save_json
printf '%s' "$ipjson"
''', TEXT=TEXT, LITE=lite)
    ansi, raw = out.split('\n---JSON---\n')
    assert re.search(r'IPQS.*99', ansi)
    report = json.loads(raw)
    assert report['Score']['IPQS'] == '99'
    assert report['Info']['Organization'] == TEXT and report['Info']['City']['Name'] == TEXT
    assert report['Head']['Command'] == TEXT and report['Factor']['CountryCode']['IPQS'] == '"X'
    quality = ip_quality.summarize(report)
    assert quality['risk']['value'] == 99 and quality['purity']['value'] < 50
    assert quality['mail'] == {'port25': False, 'listed': 1, 'marked': 0, 'checked': 4, 'failed': 1, 'unknown': 1, 'answered': 2}


@pytest.mark.host_commands
def test_disney_json_request_token_is_data_not_a_template_substitution():
    # This assignment is the actual request construction, not a duplicate implementation.
    statement = next(line for line in source('ip').splitlines() if line.startswith('local disneycontent='))
    template = (packages.ROOT / 'ip.check.place/v0.1.0/main/ref/cookies.txt').read_text().splitlines()[7]
    out = shell('ip', [], 'request(){\n' + statement + '\nprintf "%s" "$disneycontent"\n}\nrequest',
                fakecontent=template, refreshToken=TEXT)
    assert json.loads(out)['variables']['input']['refreshToken'] == TEXT


@pytest.mark.host_commands
def test_net_all_json_sections_keep_text_as_data():
    out = shell('net', ['json_field', 'json_document', 'save_json'], r'''
fullIP=0; IPhide=203.0.*.*; ping_test_count=2
netdata='{"Head":{},"BGP":{},"Local":{},"Connectivity":[],"Delay":[],"Speedtest":[],"Transfer":[]}'
shead[bash]="$TEXT"; bgp[org]="$TEXT"; bgp[address]="$TEXT"; bgp[asn]=AS64500; bgp[prefixnum]='not a number'
getnat[type]=0; slocal[0]="$TEXT"; gettcp[tcpcc]="$TEXT"
casn[2]=64501; corg[2]="$TEXT"; ctarget[2]=true
pcode[1]="$TEXT"; pshort[1]="$TEXT"; pavg[114]="$TEXT"; pout[1141]="$TEXT"
icity[1]="$TEXT"; isout[141]="$TEXT"; ipout[141]="$TEXT"
scity[1]="$TEXT"; spv[1]="$TEXT"; sid[1]="$TEXT"; sout[11]=1
save_json 4
printf '%s' "$netdata"
''', TEXT=TEXT)
    doc = json.loads(out)
    assert doc['BGP']['Organization'] == doc['BGP']['Address'] == doc['Head']['Command'] == TEXT
    assert doc['BGP']['Prefix'] is None and doc['BGP']['ASN'] == '64500'
    assert doc['Local']['TCPCongestionControl'] == TEXT and '"Cloud"' in doc['Local']['NATDescribe']
    assert doc['Connectivity'][0]['Org'] == TEXT and doc['Connectivity'][0]['IsTarget'] is True
    assert doc['Delay'][0]['Code'] == doc['Delay'][0]['Name'] == TEXT
    assert doc['Delay'][0]['CT']['Average'] == doc['Delay'][0]['CT']['1'] == TEXT
    assert doc['Transfer'][0]['City'] == doc['Transfer'][0]['SendSpeed'] == doc['Transfer'][0]['Delay']['1'] == TEXT
    assert doc['Speedtest'][0]['City'] == doc['Speedtest'][0]['Provider'] == doc['Speedtest'][0]['ID'] == TEXT


@pytest.mark.host_commands
@pytest.mark.parametrize('tool,document', [('ip', 'ipjson'), ('net', 'netdata')])
def test_serializer_failure_clears_previous_document(tool, document):
    names = ['json_field', 'json_document', 'save_json']
    if tool == 'ip':
        names += ['clean_ansi', 'factor_bool']
    out = shell(tool, names, f'''
fullIP=0; mode_lite=0; ping_test_count=0
{document}='{{}}'
jq(){{ return 3; }}
if save_json 4;then exit 90;fi
printf '%s' "${{{document}}}"
''')
    assert out == ''


@pytest.mark.host_commands
@pytest.mark.parametrize('rcode,exit_code,answers,expected', [
    ('NXDOMAIN', 0, '', 'Clean'), ('NOERROR', 0, '', 'Clean'),
    ('REFUSED', 0, '', 'Failed'), ('SERVFAIL', 0, '', 'Failed'),
    ('NOERROR', 9, '', 'Failed'), ('', 0, '', 'Unknown'),
    ('NOERROR', 0, '127.255.255.254', 'Failed'), ('NOERROR', 0, '127.255.0.1', 'Failed'),
    ('NOERROR', 0, '127.0.0.2', 'Blacklisted'), ('NOERROR', 0, '127.0.0.4', 'Other'),
    ('NOERROR', 0, '198.51.100.1', 'Unknown'), ('NOERROR', 0, '127.0.0.2 127.255.255.255', 'Failed'),
])
def test_dnsbl_process_status_rcode_and_provider_answers(rcode, exit_code, answers, expected):
    out = shell('ip', ['dnsbl_query'], r'''
dig(){
printf ';; ->>HEADER<<- opcode: QUERY, status: %s, id: 1\n' "$RCODE"
for ip in $ANSWERS;do printf 'test.invalid. 60 IN A %s\n' "$ip";done
return "$EXIT_CODE"
}
dnsbl_query 1.113.0.203.test.invalid
''', RCODE=rcode, EXIT_CODE=exit_code, ANSWERS=answers)
    assert out.strip() == expected


@pytest.mark.host_commands
def test_dnsbl_parallel_counts_and_display_include_unknown_coverage():
    out = shell('ip', ['dnsbl_query', 'check_dnsbl_parallel', 'check_dnsbl', 'set_language'], r'''
YY=en; set_language; IP=203.0.113.1
show_progress_bar(){ :; }; kill_progress_bar(){ :; }
curl(){ printf '%s\n' clean.invalid refused.invalid listed.invalid error.invalid unknown.invalid; }
dig(){
case "$*" in
*refused.invalid*) echo ';; ->>HEADER<<- status: REFUSED, id: 1';;
*listed.invalid*) printf ';; ->>HEADER<<- status: NOERROR, id: 1\nx 60 IN A 127.0.0.2\n';;
*error.invalid*) printf ';; ->>HEADER<<- status: NOERROR, id: 1\nx 60 IN A 127.255.255.254\n';;
*unknown.invalid*) echo 'malformed DNS response';;
*) echo ';; ->>HEADER<<- status: NXDOMAIN, id: 1';;
esac
}
export -f dig
check_dnsbl
printf '%s\n' "${smail[t]} ${smail[c]} ${smail[m]} ${smail[b]} ${smail[f]} ${smail[u]} ${smail[answered]}" "${smail[sdnsbl]}"
''')
    assert out.splitlines()[0] == '5 1 0 1 2 1 2'
    assert 'Failed: 2' in out and 'Unknown: 1' in out and 'Answered: 2/5' in out


@pytest.mark.host_commands
@pytest.mark.parametrize('available,install_fixes,expected_installs,expected_ok', [(1, 0, 0, 1), (0, 1, 1, 1), (0, 0, 1, 0)])
def test_bc_dependency_detection_and_post_install_verification(available, install_fixes, expected_installs, expected_ok):
    out = shell('net', ['net_dependencies_ready', 'install_dependencies'], r'''
mode_yes=1; installs=0
uname(){ echo Darwin; }; id(){ echo 0; }
jq(){ :; }; curl(){ :; }; bc(){ [[ $AVAILABLE == 1 ]]; }
convert(){ :; }; mtr(){ :; }; iperf3(){ :; }; stun(){ :; }; free(){ :; }; nexttrace(){ :; }; speedtest(){ :; }
install_packages(){ ((installs+=1)); [[ $INSTALL_FIXES == 1 ]]&&AVAILABLE=1; return 0; }
# No actual installer is present in the extracted program, even if the detection regresses.
install_speedtest(){ exit 91; }; install_stun(){ exit 92; }
if install_dependencies;then ok=1;else ok=0;fi
printf '\nRESULT %s %s\n' "$installs" "$ok"
''', AVAILABLE=available, INSTALL_FIXES=install_fixes)
    assert f'RESULT {expected_installs} {expected_ok}' in out


@pytest.mark.host_commands
@pytest.mark.parametrize('asns,expected', [
    (['AS10099', 'AS9929'], '9929'), (['AS4134', 'AS4809'], 'CN2GT'),
    (['4134', '4809'], 'CN2GT'), (['AS4837', 'AS58807'], 'CMIN2'),
    (['AS10099', 'AS19929'], '10099'), (['AS4134', 'AS14809'], '163'),
    (['AS10099', '???', 'AS19929'], '10099'),
])
def test_ipv6_udp_real_mtr_parser_uses_exact_asn_tokens(asns, expected):
    rows = '\n'.join(f'MTR,x,OK,x,{i},2001:db8::{i},{asn}' for i, asn in enumerate(['AS64500', *asns, 'AS64501'], 1))
    out = shell('net', ['mtr_test'], r'''
timeout(){ shift; "$@"; }
mtr(){ printf '%s\n' "$ROWS"; }
mtr_test example.invalid udp 7 6
''', ROWS=rows)
    assert out.strip() == f'7 AS64500 {expected}'


@pytest.mark.host_commands
@pytest.mark.parametrize('tool,document', [('ip', 'ipjson'), ('net', 'netdata')])
def test_json_stdout_preserves_serialized_bytes(tool, document):
    if shutil.which('jq') is None:
        pytest.skip('offline serialization contracts require local jq')
    text = source(tool)
    # Test the actual final -j output statement, not a safer output invented by the fixture.
    [emit] = [line for line in text.splitlines() if line.startswith('[[ mode_json -eq 1 ]]&&')]
    payload = '中文 "quote" \\backslash \\n \\c \\x41\nactual newline\ttab\rcarriage'
    definitions = '\n'.join(function(text, name) for name in ('json_field', 'json_document'))
    script = definitions + f'''
declare -a json_fields=()
json_field value s "$TEXT"
{document}=$(json_document '{{}}')
mode_json=1
{emit}
'''
    result = subprocess.run(['bash', '-c', script], capture_output=True, timeout=5, env={**os.environ, 'TEXT': payload})
    assert result.returncode == 0, result.stderr
    assert result.stdout == (json.dumps({'value': payload}, ensure_ascii=False, indent=2) + '\n').encode()
    assert json.loads(result.stdout)['value'] == payload


def test_updated_scripts_are_manifest_pinned_image_inputs():
    inputs = set(packages.source_files())
    for tool in ('ip', 'net'):
        name = f'{tool}.check.place'
        base = packages.ROOT / name / 'v0.1.0'
        manifest = packages.manifest(name, 'v0.1.0')
        script = base / f'{tool}.sh'
        assert script in inputs and base / 'openbear.patch' in inputs
        assert packages.sha256_file(script) == manifest['files'][script.name]
