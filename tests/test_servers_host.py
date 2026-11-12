"""The busiest processes, the machine in detail and ending a process: host.* calls the node answers."""
from app.webhooks.repository import many
from tests.test_servers import _online, api, base_env, env, shipped, start_fake, wait_for  # noqa: F401


async def _server(e):
    sid = (await api(e, 'POST', '/api/servers', {'name': '探针'}))['item']['id']
    await start_fake(e, sid)
    await wait_for(lambda: _online(e, sid))
    return sid


async def test_an_older_node_is_asked_to_update(env):
    e = env
    sid = await _server(e)
    # The fake node, like a build from before host.*, refuses the methods.
    for method, path in [('GET', '/processes?sort=cpu'), ('GET', '/details')]:
        assert (await api(e, method, f'/api/servers/{sid}{path}', status=409))['error'] == 'node_update_required'
    e.servers.hub.sessions[sid].features = frozenset(f for f in e.servers.hub.sessions[sid].features if f != 'host')
    assert (await api(e, 'POST', f'/api/servers/{sid}/processes/kill', {'pid': 42}, status=409))['error'] == 'node_update_required'


async def test_processes_details_and_ending_one(env, monkeypatch):
    e = env
    sid = await _server(e)
    calls = []
    replies = {
        'host.top': {'sort': 'mem', 'sorts': ['cpu', 'mem'], 'count': 2, 'processes': [{'pid': 42, 'name': 'nginx', 'cpu': 1.5, 'rss': 1024}]},
        'host.details': {'hostname': 'box', 'cpu': {'model': 'AMD EPYC', 'threads': 4}, 'disks': [], 'mounts': [], 'interfaces': []},
    }

    async def call(server_id, method, params, timeout):
        calls.append((server_id, method, params))
        if method == 'host.kill':
            return {'ok': True, 'result': {'pid': params['pid'], 'exited': not params['force']}}
        return {'ok': True, 'result': replies[method]}

    monkeypatch.setattr(e.servers.hub, 'call', call)
    top = await api(e, 'GET', f'/api/servers/{sid}/processes?sort=mem')
    assert top['processes'][0]['name'] == 'nginx' and top['sorts'] == ['cpu', 'mem'] and calls[-1] == (sid, 'host.top', {'sort': 'mem', 'limit': 10})
    assert (await api(e, 'GET', f'/api/servers/{sid}/processes?sort=gpu', status=400))['error'] == 'invalid_sort'
    assert (await api(e, 'GET', f'/api/servers/{sid}/details'))['details']['cpu']['model'] == 'AMD EPYC'

    for body in [{'pid': '42'}, {'pid': 0}, {'pid': 42, 'force': 'yes'}, {'pid': 42, 'name': 3}]:
        assert (await api(e, 'POST', f'/api/servers/{sid}/processes/kill', body, status=400))['error'] == 'invalid_pid'
    done = await api(e, 'POST', f'/api/servers/{sid}/processes/kill', {'pid': 42, 'name': 'nginx'})
    assert done == {'ok': True, 'pid': 42, 'exited': True} and calls[-1][2] == {'pid': 42, 'force': False}
    still = await api(e, 'POST', f'/api/servers/{sid}/processes/kill', {'pid': 43, 'force': True})
    assert still['exited'] is False
    logged = [r['command'] for r in await many(e.db.conn, 'SELECT command FROM server_exec_log WHERE server_id=? ORDER BY id', (sid,))]
    assert logged[-2:] == ['结束进程 nginx（PID 42）', '强制结束进程 43（PID 43），未退出']

    async def refused(server_id, method, params, timeout):
        return {'ok': False, 'error': 'capability_disabled'}

    monkeypatch.setattr(e.servers.hub, 'call', refused)
    assert (await api(e, 'POST', f'/api/servers/{sid}/processes/kill', {'pid': 42}, status=403))['error'] == 'capability_disabled'
