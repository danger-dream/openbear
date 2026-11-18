"""The Servers tool across servers: jobs listed and filtered on many at once, several jobs waited for or
stopped in one call, and the conversation behind each job and command."""
import asyncio
import json
import time

from app.db.engine import DB
from app.instances import schema as instance_schema
from app.servers import schema as server_schema
from app.tools.base import ToolRegistry, ToolRuntimeContext
from app.tools.servers import ACTIONS, ServersTool, register_server_tools
from app.webhooks.repository import many, one
from tests.test_servers import (  # noqa: F401
    _offline,
    _online,
    api,
    base_env,
    env,
    shipped,
    start_fake,
    wait_for,
)


async def _caller(e, chat_id, conversation, title, turn=''):
    registry = ToolRegistry()
    register_server_tools(registry, e.servers)
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute('INSERT INTO web_conversations(conversation_uuid,internal_chat_id,owner_chat_id,title) VALUES(?,?,123,?)', (conversation, chat_id, title))
    ctx = ToolRuntimeContext(chat_id=chat_id, conversation_uuid=conversation, turn_uuid=turn)

    async def call(action, **params):
        return json.loads(await registry.dispatch('Servers', json.dumps({'action': action, 'params': params}), context=ctx))
    return call


async def test_jobs_across_servers_with_filters_origins_and_batched_wait_and_stop(env):
    e = env
    call = await _caller(e, 801, 'conv-batch-1', '部署会话', turn='turn-1')
    other = await _caller(e, 802, 'conv-batch-2', '另一个会话')
    a, b, c = [(await api(e, 'POST', '/api/servers', {'name': name, 'profile': {'group': '批量'}}))['item']['id'] for name in ('批量-甲', '批量-乙', '批量-丙')]
    nodes = {}
    for sid in (a, b, c):
        nodes[sid] = (await start_fake(e, sid))[0]
        await wait_for(lambda sid=sid: _online(e, sid))
    await nodes[c].kill()
    await wait_for(lambda: _offline(e, c))

    # Started and waited for in one call: until a match, or until the job ends.
    began = time.monotonic()
    served = await call('job_start', serverId=a, command='echo booting; sleep 1; echo "ready on 9000"; sleep 60', until=r'ready on \d+', waitSeconds=20)
    assert served['ok'] and served['wait']['reason'] == 'matched' and served['wait']['match'] == 'ready on 9000', served
    assert served['job']['state'] == 'running' and '批量-甲' in served['untrustedData'] and time.monotonic() - began < 15
    quick = await call('job_start', serverId=a, command='echo finished; exit 4', waitSeconds=20)
    assert quick['wait']['reason'] == 'exited' and quick['job']['exitCode'] == 4 and quick['wait']['tail'] == 'finished\n', quick
    plain = await call('job_start', serverId=a, command='sleep 60')
    assert plain['ok'] and 'wait' not in plain and plain['job']['state'] == 'running'
    assert (await call('job_start', serverId=a, command='true', until='('))['error'] == 'invalid_pattern'
    assert (await call('job_start', serverId=a, command='true', waitSeconds=301))['error'] == 'invalid_wait'
    elsewhere = (await other('job_start', serverId=b, command='sleep 60'))['job']['id']
    by_hand = (await e.servers.job_start(123, b, 'sleep 61'))['job']['id']
    served, quick, plain = served['job']['id'], quick['job']['id'], plain['job']['id']

    # One call over the group: only servers with a match are listed, an offline one apart, one data label for all.
    running = await call('jobs', group='批量', state='running')
    assert running['checked'] == 2 and running['matchedJobs'] == 4 and running['serversWithoutMatches'] == 0, running
    assert running['unavailable'] == [{'serverId': c, 'name': '批量-丙', 'error': 'node_offline'}]
    assert running['untrustedData'] and json.dumps(running['servers']).count('untrustedData') == 0
    jobs = {j['id']: j for s in running['servers'] for j in s['jobs']}
    assert set(jobs) == {served, plain, elsewhere, by_hand}
    assert jobs[served]['startedBy'] == {'kind': 'conversation', 'conversationUuid': 'conv-batch-1', 'title': '部署会话', 'turnUuid': 'turn-1', 'current': True}
    assert jobs[elsewhere]['startedBy'] == {'kind': 'conversation', 'conversationUuid': 'conv-batch-2', 'title': '另一个会话', 'turnUuid': ''}
    assert jobs[by_hand]['startedBy']['kind'] == 'web'
    mine = await call('jobs', all=True, startedBy='current', state=['running', 'exited'])
    assert {j['id'] for s in mine['servers'] for j in s['jobs']} == {served, quick, plain} and mine['serversWithoutMatches'] == 1
    assert [s['serverId'] for s in mine['servers']] == [a] and mine['filter']['startedBy'] == 'current'
    found = await call('jobs', serverIds=[a, b], commandContains='SLEEP 61')
    assert [(s['serverId'], [j['id'] for j in s['jobs']]) for s in found['servers']] == [(b, [by_hand])]
    assert (await call('jobs', serverIds=[a], since='1m'))['matchedJobs'] == 3
    assert (await call('jobs', serverIds=[a], since=int(time.time() * 1000) + 60000))['servers'] == []
    # One server alone answers as it always did, with the filters too.
    single = await call('jobs', serverId=a)
    assert single['count'] == 3 and {j['id'] for j in single['jobs']} == {served, quick, plain} and 'filter' not in single
    filtered = await call('jobs', serverId=a, state='exited')
    assert filtered['count'] == 1 and filtered['total'] == 3 and filtered['jobs'][0]['id'] == quick
    for bad, error in (({'state': 'sleeping'}, 'invalid_state'), ({'startedBy': 'me'}, 'invalid_started_by'), ({'since': 'yesterday'}, 'invalid_since')):
        assert (await call('jobs', all=True, **bad))['error'] == error

    # Several jobs in one call; one wrong id fails alone.
    status = await call('job_status', jobs=[{'serverId': a, 'jobId': quick}, {'serverId': b, 'jobId': elsewhere}, {'serverId': b, 'jobId': 'nosuchjob'}])
    assert status['count'] == 3 and not status['ok'] and status['states'] == {'exited': 1, 'running': 1}, status
    assert status['results'][0]['output'] == 'finished\n' and status['results'][0]['name'] == '批量-甲'
    assert status['results'][2] == {'serverId': b, 'name': '批量-乙', 'jobId': 'nosuchjob', 'ok': False, 'error': 'job_not_found'}
    soon = (await call('job_start', serverId=b, command='sleep 1; echo up'))['job']['id']
    began = time.monotonic()
    first = await call('job_wait', jobs=[{'serverId': a, 'jobId': plain}, {'serverId': b, 'jobId': soon}], mode='any', timeoutSeconds=30)
    assert first['reason'] == 'done' and first['finished'] == 1 and time.monotonic() - began < 20, first
    assert [r['reason'] for r in first['results']] == ['pending', 'exited'] and first['results'][0]['job']['state'] == 'running'
    every = await call('job_wait', jobs=[{'serverId': a, 'jobId': served}, {'serverId': a, 'jobId': quick}], until='ready', timeoutSeconds=10)
    assert every['mode'] == 'all' and every['reason'] == 'done' and [r['reason'] for r in every['results']] == ['matched', 'exited'], every
    late = await call('job_wait', jobs=[{'serverId': a, 'jobId': plain}], timeoutSeconds=1)
    assert late['reason'] == 'timeout' and late['results'][0]['reason'] == 'timeout'
    assert (await call('job_wait', jobs=[{'serverId': a, 'jobId': plain}], mode='first'))['error'] == 'invalid_mode'
    assert (await call('job_wait', jobs=[{'serverId': a, 'jobId': plain}], timeoutSeconds=301))['error'] == 'invalid_timeout'
    assert (await call('job_status', jobs=[{'serverId': a}]))['error'] == 'invalid_jobs'

    # Stopping takes the jobs named, never a filter.
    assert (await call('job_stop', group='批量', state='running'))['error'] == 'explicit_jobs_required'
    assert (await call('job_stop', all=True, jobs=[{'serverId': a, 'jobId': plain}]))['error'] == 'explicit_jobs_required'
    stopped = await call('job_stop', jobs=[{'serverId': a, 'jobId': served}, {'serverId': a, 'jobId': plain}, {'serverId': b, 'jobId': elsewhere}])
    assert stopped['ok'] and stopped['stopped'] == 3 and {r['job']['state'] for r in stopped['results']} == {'stopped'}, stopped
    assert (await call('job_stop', serverId=b, jobId=by_hand))['stopped']
    assert (await call('jobs', group='批量', state='running'))['matchedJobs'] == 0

    # The command record names the conversation; the page's own runs name none.
    assert (await call('exec', serverIds=[a, b], command='true'))['ok']
    await e.servers.exec(123, a, 'echo from-web')
    log = (await api(e, 'GET', f'/api/servers/{a}'))['item']['execLog']
    assert log[0]['command'] == 'echo from-web' and 'conversationUuid' not in log[0]
    assert log[1]['command'] == 'true' and log[1]['conversationUuid'] == 'conv-batch-1' and log[1]['conversationTitle'] == '部署会话'
    row = await one(e.db.conn, 'SELECT * FROM server_exec_log WHERE server_id=? ORDER BY id DESC LIMIT 1 OFFSET 1', (a,))
    assert row['turn_uuid'] == 'turn-1'
    assert any(r['command'].startswith(f'停止后台任务 {served}') and r.get('conversationUuid') == 'conv-batch-1' for r in log)


async def test_a_command_record_says_how_the_script_ended(env):
    """The record of a command or background job says how it ended: by exit code 0, or by the judge
    its caller gives for a program whose exit code does not tell; a job's once this controller sees
    it end, a stop as stopped, one whose caller judges it by the caller's word, and one its node
    no longer lists as not known."""
    e = env
    sid = (await api(e, 'POST', '/api/servers', {'name': '记录-甲'}))['item']['id']
    await start_fake(e, sid)
    await wait_for(lambda: _online(e, sid))

    async def record(job_id):
        return await one(e.db.conn, 'SELECT outcome,exit_code,duration_ms,job_judge FROM server_exec_log WHERE server_id=? AND job_id=?', (sid, job_id))

    async def ended(job_id):
        row = await record(job_id)
        return row['outcome'], row['exit_code']

    async def last():
        return await one(e.db.conn, 'SELECT outcome,exit_code FROM server_exec_log WHERE server_id=? ORDER BY id DESC LIMIT 1', (sid,))
    await e.servers.exec(123, sid, 'exit 1')
    assert dict(await last()) == {'outcome': 'failed', 'exit_code': 1}
    await e.servers.exec(123, sid, 'echo report; exit 1', judge=lambda r: 'report' in r['stdout'])
    assert dict(await last()) == {'outcome': 'ok', 'exit_code': 1}
    await e.servers.exec(123, sid, 'exit 1', judge=lambda r: 'report' in r['stdout'])
    assert dict(await last()) == {'outcome': 'failed', 'exit_code': 1}

    done = (await e.servers.job_start(123, sid, 'sleep 0.3'))['job']['id']
    bad = (await e.servers.job_start(123, sid, 'exit 3'))['job']['id']
    judged = (await e.servers.job_start(123, sid, 'exit 1', judged=True))['job']['id']
    long = (await e.servers.job_start(123, sid, 'sleep 60'))['job']['id']
    assert (await record(done))['outcome'] == 'started' and (await record(judged))['job_judge'] == 'caller'
    for job_id in (done, bad, judged):
        assert (await e.servers.job_wait(123, sid, job_id, timeout=10))['reason'] == 'exited'
    assert await ended(done) == ('ok', 0) and (await record(done))['duration_ms'] >= 250
    assert await ended(bad) == ('failed', 3)
    # Exited, its caller has not said yet how it went.
    assert (await record(judged))['outcome'] == 'started'
    await e.servers.job_ended(123, sid, judged, 'ok', 1)
    assert await ended(judged) == ('ok', 1)
    await e.servers.job_ended(123, sid, judged, 'failed')
    assert (await record(judged))['outcome'] == 'ok', 'an ended record is left as it is'
    await e.servers.job_stop(123, sid, long)
    assert (await record(long))['outcome'] == 'stopped'

    # Jobs no one looks at again: the sweep reads them on their node; one the node forgot is not known.
    unseen = (await e.servers.job_start(123, sid, 'true'))['job']['id']
    async with e.db.write_transaction(label='test') as conn:
        await conn.execute("INSERT INTO server_exec_log(owner,server_id,source,command,started_ms,outcome,job_id) VALUES(123,?,'ai','后台任务 forgotten1：true',?,'started','forgotten1')",
                           (sid, int(time.time() * 1000) - 3600_000))
        await conn.execute("INSERT INTO server_exec_log(owner,server_id,source,command,started_ms,outcome,job_id) VALUES(123,?,'ai','后台任务 justnow1：true',?,'started','justnow1')",
                           (sid, int(time.time() * 1000)))
    for _ in range(50):
        await e.servers.sweep_job_ends()
        if (await record(unseen))['outcome'] != 'started':
            break
        await asyncio.sleep(0.05)
    assert (await record(unseen))['outcome'] == 'ok'
    assert (await record('forgotten1'))['outcome'] == 'unknown'
    assert (await record('justnow1'))['outcome'] == 'started', 'just started, not yet listed: not taken as forgotten'
    log = (await api(e, 'GET', f'/api/servers/{sid}'))['item']['execLog']
    assert next(entry['outcome'] for entry in log if entry['command'] == f'后台任务 {long}：sleep 60') == 'stopped'


async def test_origins_columns_join_an_older_database(tmp_path):
    db = DB(str(tmp_path / 'old.db'))
    await db.connect()
    try:
        async with db.write_transaction(label='test') as conn:
            await conn.execute('CREATE TABLE server_exec_log (id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER NOT NULL, server_id TEXT NOT NULL, '
                               "source TEXT NOT NULL, command TEXT NOT NULL, started_ms INTEGER NOT NULL, duration_ms INTEGER NOT NULL DEFAULT 0, "
                               "exit_code INTEGER, outcome TEXT NOT NULL DEFAULT '')")
            await conn.execute("INSERT INTO server_exec_log(owner,server_id,source,command,started_ms) VALUES(123,'s1','web','uptime',1)")
        await instance_schema.migrate(db)
        await server_schema.migrate(db)
        await server_schema.migrate(db)
        row = await one(db.conn, 'SELECT * FROM server_exec_log')
        assert row['command'] == 'uptime' and row['conversation_uuid'] == row['conversation_title'] == row['turn_uuid'] == ''
        columns = {r['name'] for r in await many(db.conn, 'PRAGMA table_info(server_job_origins)')}
        assert {'server_id', 'job_id', 'conversation_uuid', 'conversation_title', 'turn_uuid', 'source'} <= columns
    finally:
        await db.close()


def test_describe_gives_one_action_a_topic_or_a_short_index():
    index = ServersTool.describe()
    assert 'contracts' not in index and len(index['actions']) == len(ACTIONS)
    assert set(index['topics']) == {'diagnostics', 'browser_all', 'commands', 'files', 'mesh_all', 'ddns', 'bench'} and index['actions']['jobs'] and index['rules']
    assert len(json.dumps(index, ensure_ascii=False)) < 8000
    assert ServersTool.describe('commands')['actions'] == ['exec', 'job_start', 'jobs', 'job_status', 'job_wait', 'job_stop']
    # jobs is an action, so it is never a topic name: it gives that action alone.
    assert list(ServersTool.describe('jobs')['contracts']) == ['jobs'] and ServersTool.describe('jobs')['group'].startswith('action=commands')
    one = ServersTool.describe('job_wait')
    assert list(one['contracts']) == ['job_wait'] and 'mode' in one['contracts']['job_wait'] and len(json.dumps(one)) < 3000
    assert 'guide' in ServersTool.describe('mesh_apply') and list(ServersTool.describe('mesh_apply')['contracts']) == ['mesh_apply']
    assert ServersTool.describe('files')['actions'] == ['upload_file', 'download_file', 'transfer_status', 'transfer_cancel', 'transfers']
    unknown = ServersTool.describe('servers')
    assert unknown['ok'] is False and unknown['error'] == 'unknown_action' and 'contracts' not in unknown
    assert 'jobs' in unknown['actions'] and 'files' in unknown['topics']
