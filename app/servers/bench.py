"""Server benchmarks. A run waits in its server's queue (one at a time per server, so tests do
not disturb each other's measurements), has its package pushed to the node, runs as a node
job, and once the job ends its report is collected into the database and the node's copy of
the run deleted. A controller restart picks the queues up where they were."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import shlex
import time
import zlib
from pathlib import Path

from app.instances.contracts import InstanceError
from app.servers import bench_catalog as catalog
from app.servers import bench_packages as packages
from app.servers.bench_terminal import after_last_clear, screen
from app.webhooks.repository import many, one

log = logging.getLogger(__name__)

POLL_S = 5
AWAY_POLL_S = 30
AWAY_LIMIT_S = 6 * 3600  # a node away this long ends its run as interrupted
CHUNK = 1024 * 1024
MAX_FILE = 8 << 20
MAX_LOG_READ = 8 << 20
KEEP_LOG = 512 * 1024
LISTING = catalog.LISTING
WAITING = ('queued', 'preparing', 'running', 'collecting')
NODE_AWAY = {'node_offline', 'node_connection_lost'}
LINKS = re.compile(r'https://(?:Report\.Check\.Place|tcpquality\.ibsgss\.uk/r|browser\.geekbench\.com/v5/cpu)/[A-Za-z0-9_./-]+', re.I)
SUMMARY = ('id', 'server_id', 'test', 'package', 'suite', 'state', 'stop', 'job_id', 'exit_code', 'error', 'detail', 'created_ms', 'started_ms', 'ended_ms')
# stop: 0, ASKED once the user asked, SENT once the node was told to end the job.
ASKED, SENT = 1, 2
# The error of a run stopped at its time limit (catalog.time_limit), set as it is told to stop.
TIMED_OUT = 'bench_timeout'


def now_ms():
    return int(time.time() * 1000)


async def _screen(text, **kwargs):
    # A long log takes a while to replay; the controller keeps serving meanwhile.
    return await asyncio.to_thread(screen, text, **kwargs)


def pack(value):
    return zlib.compress(json.dumps(value, ensure_ascii=False).encode(), 6)


def unpack(blob):
    return json.loads(zlib.decompress(blob)) if blob else None


def run_view(row):
    opts = json.loads(row.get('options_json') or '{}')
    test = catalog.TESTS.get(row['test'], {})
    view = {'id': row['id'], 'serverId': row['server_id'], 'test': row['test'], 'name': test.get('name', row['test']),
            'category': test.get('category', ''), 'options': opts, 'label': catalog.label(row['test'], opts) if test else '',
            'package': row['package'], 'suite': row['suite'], 'state': row['state'], 'stopping': bool(row['stop']) and row['state'] in WAITING,
            # The node job whose output the page shows while the run is under way.
            'jobId': row['job_id'],
            'exitCode': row['exit_code'], 'error': row['error'], 'detail': row['detail'],
            'createdMs': row['created_ms'], 'startedMs': row['started_ms'] or None, 'endedMs': row['ended_ms'] or None}
    if 'links_json' in row:
        view['links'] = json.loads(row['links_json'] or '[]')
    return view


class Benchmarks:
    def __init__(self, service):
        self.service = service
        self.workers = {}  # server id -> the task working through its queue
        self.pending = set()  # servers whose queue changed while their worker was looking
        self.paused = set()  # lifecycle operations keep scheduling closed until resume_server
        self.starts = {}  # run id -> an in-flight job.start, shielded until its receipt is known
        self.digests = {}  # local file -> (size, mtime, sha256)
        self.cache = None
        self.measurements = {}  # shared with full unlock, including package cleanup

    def measurement_lock(self, server_id):
        # Semaphore.locked() includes queued waiters, so a non-waiting full check cannot
        # slip in while a benchmark is being woken for the released slot.
        return self.measurements.setdefault(server_id, asyncio.Semaphore(1))

    @property
    def db(self):
        return self.service.db

    async def start(self):
        self.cache = Path(self.db.path).expanduser().parent / 'benchmark-cache'
        async with self.db.write_transaction(label='bench-resume') as conn:
            # Older preparing rows may already have sent job.start without receiving its reply.
            await conn.execute("UPDATE server_bench_runs SET job_state='starting' WHERE state='preparing' AND job_id=''")
        for row in await many(self.db.conn, f"SELECT DISTINCT server_id FROM server_bench_runs WHERE state IN {WAITING}"):
            self._kick(row['server_id'])
        for row in await many(self.db.conn, "SELECT * FROM server_bench_runs WHERE state='interrupted' AND error=? AND stop=? AND job_id!=''",
                              (TIMED_OUT, ASKED)):
            self.service.spawn(self._stop_later(row))

    # ----- the page's calls -----
    def catalog(self):
        return {'ok': True, **catalog.view()}

    async def runs(self, owner):
        rows = await many(self.db.conn, f'SELECT {",".join(SUMMARY)}, options_json, links_json FROM server_bench_runs WHERE owner=? ORDER BY created_ms DESC',
                          (owner,))
        return {'ok': True, 'runs': [run_view(r) for r in rows]}

    async def run(self, owner, run_id):
        row = await self._row(owner, run_id)
        return {'ok': True, 'run': {**run_view(row), 'sections': unpack(row['sections']) or [], 'log': unpack(row['log']) or ''}}

    async def summary(self, owner, run_id):
        """The run without its report and log."""
        row = await one(self.db.conn, f'SELECT owner, {",".join(SUMMARY)}, options_json, links_json FROM server_bench_runs WHERE id=?', (run_id,)) \
            if isinstance(run_id, str) else None
        if not row or row['owner'] != owner:
            raise InstanceError('bench_run_not_found', 404)
        return run_view(row)

    async def create(self, owner, data):
        if not isinstance(data, dict) or set(data) - {'serverId', 'test', 'tests', 'suite', 'options'}:
            raise InstanceError('invalid_request')
        extra = data.get('tests')
        # Several tests at once, beside the suite or on their own; each is still its own run.
        if extra is not None and (not isinstance(extra, list) or not extra or 'test' in data
                                  or any(not isinstance(t, dict) or set(t) - {'test', 'options'} for t in extra)):
            raise InstanceError('invalid_request')
        server_id = data.get('serverId')
        if not isinstance(server_id, str) or not server_id:
            raise InstanceError('server_required')
        await self.service.registry.get(owner, server_id)
        if data.get('suite') is True:
            family = catalog.options('ipcheck', {'family': (data.get('options') or {}).get('family', '4')})['family']
            if set(data.get('options') or {}) - {'family'}:
                raise InstanceError('bench_invalid_option')
            plan = [(t, catalog.options(t, {'family': family} if any(o['key'] == 'family' for o in catalog.TESTS[t]['options']) else {}))
                    for t in catalog.SUITE['tests']]
            suite = secrets.token_hex(6)
        elif extra is not None:
            if 'options' in data:
                raise InstanceError('invalid_request')
            plan, suite = [], ''
        else:
            plan = [(data.get('test'), catalog.options(data.get('test'), data.get('options')))]
            suite = ''
        for item in extra or []:
            plan.append((item.get('test'), catalog.options(item.get('test'), item.get('options'))))
        if len({test for test, _ in plan}) != len(plan):
            raise InstanceError('bench_duplicate_test')
        for test, _ in plan:
            await self._platform(server_id, test)
        created, base = [], now_ms()
        async with self.db.write_transaction(label='bench-create') as conn:
            if server_id in self.paused:
                raise InstanceError('bench_run_active', 409)
            for index, (test, opts) in enumerate(plan):
                run_id = secrets.token_hex(8)
                tool = catalog.TESTS[test]['package']
                await conn.execute('INSERT INTO server_bench_runs(id,owner,server_id,test,options_json,package,suite,state,created_ms) VALUES(?,?,?,?,?,?,?,?,?)',
                                   (run_id, owner, server_id, test, json.dumps(opts, ensure_ascii=False), f'{tool} {catalog.PACKAGES[tool]}',
                                    suite if test in catalog.SUITE['tests'] else '', 'queued', base + index))
                created.append(run_id)
        self._kick(server_id)
        rows = [await self._row(owner, run_id) for run_id in created]
        return {'ok': True, 'runs': [run_view(r) for r in rows]}

    async def stop(self, owner, run_id):
        row = await self._row(owner, run_id)
        if row['state'] == 'queued':
            await self._set(run_id, state='stopped', stop=ASKED, ended_ms=now_ms())
        elif row['state'] in ('preparing', 'running') and not row['stop']:
            # A node that cannot be told now is told by the run's waiter once it is back.
            await self._set(run_id, stop=ASKED)
            if row['state'] == 'running' and row['job_id']:
                await self._stop_job(row)
        elif row['state'] == 'collecting' and not row['stop']:
            # The job has ended; what is left to stop is waiting for a node that went away while its report was read.
            await self._set(run_id, stop=ASKED)
        return {'ok': True, 'run': run_view(await self._row(owner, run_id))}

    async def stop_server(self, owner, server_id):
        """Quiesce before deleting/uninstalling a server. Scheduling stays closed after return (or failure)
        until resume_server. An exception means a remote stop is NOT confirmed: the caller must not
        describe it as stopped or discard the node on that basis."""
        await self.service.registry.get(owner, server_id)
        self.paused.add(server_id)
        self.pending.discard(server_id)
        task = self.workers.get(server_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self.db.write_transaction(label='bench-stop-server') as conn:
            rows = await many(conn, f"SELECT * FROM server_bench_runs WHERE owner=? AND server_id=? "
                              f"AND (state IN {WAITING} OR (error=? AND stop=?)) ORDER BY created_ms",
                              (owner, server_id, TIMED_OUT, ASKED))
            for row in rows:
                await conn.execute('UPDATE server_bench_runs SET stop=? WHERE id=?', (ASKED, row['id']))
        stopped = []
        for row in rows:
            # Cancelling the worker must not cancel an already-sent start. Wait for its receipt before
            # listing/stopping jobs, or the job could appear just after the node was removed.
            starting = self.starts.get(row['id'])
            if starting and not starting.cancelled():
                try:
                    result = await asyncio.shield(starting)
                except InstanceError:
                    self.starts.pop(row['id'], None)
                    raise
                row['job_id'] = result['job']['id']
                self.starts.pop(row['id'], None)
                await self._set(row['id'], job_id=row['job_id'])
            elif not row['job_id'] and row['job_state'] == 'starting':
                found = await self._find_job(owner, server_id, self._out_dir(row))
                if found:
                    row['job_id'] = found['id']
                    await self._set(row['id'], job_id=row['job_id'])
            if row['job_id']:
                try:
                    result = await self.service.job_stop(owner, server_id, row['job_id'], source='bench', log=self._label(row, '停止'))
                    if result.get('job', {}).get('state') not in {'stopped', 'exited', 'interrupted'}:
                        raise InstanceError('bench_run_active', 409)
                except InstanceError as exc:
                    if exc.error != 'job_not_found':
                        raise
            state = 'interrupted' if row['error'] == TIMED_OUT else 'stopped'
            await self._set(row['id'], state=state, stop=SENT, ended_ms=row['ended_ms'] or now_ms())
            stopped.append(row['id'])
        return {'ok': True, 'runs': stopped}

    def resume_server(self, server_id):
        """Release the lifecycle fence after an aborted removal, or after detaching a retained server."""
        self.paused.discard(server_id)
        self._kick(server_id)

    async def delete(self, owner, run_id):
        row = await self._row(owner, run_id)
        if row['state'] in WAITING or row['error'] == TIMED_OUT and row['stop'] == ASKED:
            # The pending stop must remain recoverable while the node is offline.
            raise InstanceError('bench_run_active', 409)
        async with self.db.write_transaction(label='bench-delete') as conn:
            await conn.execute('DELETE FROM server_bench_runs WHERE id=? AND owner=?', (run_id, owner))
        return {'ok': True}

    async def forget_server(self, owner, server_id):
        """A deleted server's queue stops and its runs go with it."""
        task = self.workers.pop(server_id, None)
        if task:
            task.cancel()
        async with self.db.write_transaction(label='bench-forget') as conn:
            await conn.execute('DELETE FROM server_bench_runs WHERE server_id=? AND owner=?', (server_id, owner))

    # ----- rows -----
    async def _row(self, owner, run_id):
        row = await one(self.db.conn, 'SELECT * FROM server_bench_runs WHERE id=? AND owner=?', (run_id, owner)) if isinstance(run_id, str) else None
        if not row:
            raise InstanceError('bench_run_not_found', 404)
        return row

    async def _get(self, run_id):
        return await one(self.db.conn, 'SELECT * FROM server_bench_runs WHERE id=?', (run_id,))

    async def _set(self, run_id, **fields):
        async with self.db.write_transaction(label='bench-update') as conn:
            await conn.execute(f'UPDATE server_bench_runs SET {",".join(f"{k}=?" for k in fields)} WHERE id=?', (*fields.values(), run_id))

    # ----- the queue -----
    def _kick(self, server_id):
        if server_id in self.paused:
            return
        self.pending.add(server_id)
        task = self.workers.get(server_id)
        if not task or task.done():
            self.workers[server_id] = self.service.spawn(self._work(server_id))

    async def _work(self, server_id):
        try:
            while True:
                if server_id in self.paused:
                    return
                self.pending.discard(server_id)
                row = await one(self.db.conn, f"""SELECT * FROM server_bench_runs WHERE server_id=? AND state IN {WAITING}
                                                 ORDER BY state='queued', created_ms LIMIT 1""", (server_id,))
                if not row:
                    if server_id in self.pending:
                        continue
                    # A full unlock check put off while the queue ran goes now rather than at its next round.
                    if server_id in self.service.unlock_full.deferred:
                        self.service.unlock_full.soon(server_id)
                    return
                try:
                    await self._advance(row)
                except asyncio.CancelledError:
                    raise
                except InstanceError as exc:
                    await self._set(row['id'], state='failed', error=exc.error, ended_ms=now_ms())
                except packages.PackageError as exc:
                    await self._set(row['id'], state='failed', error=exc.code, detail=exc.detail[:300], ended_ms=now_ms())
                except Exception as exc:
                    log.exception('benchmark run %s failed', row['id'])
                    await self._set(row['id'], state='failed', error='bench_internal', detail=str(exc)[:300], ended_ms=now_ms())
        finally:
            if self.workers.get(server_id) is asyncio.current_task():
                del self.workers[server_id]

    async def _advance(self, row):
        async with self.measurement_lock(row['server_id']):
            # A queued run may have been stopped/deleted while full unlock held the slot.
            fresh = await self._get(row['id'])
            if fresh and fresh['state'] in WAITING:
                await self._advance_locked(fresh)

    async def _advance_locked(self, row):
        if row['state'] in ('queued', 'preparing'):
            await self._launch(row)
            row = await self._get(row['id'])
        if row and row['state'] == 'running':
            await self._wait(row)
            row = await self._get(row['id'])
        if row and row['state'] == 'collecting':
            await self._collect(row)

    async def _info(self, server_id):
        session = self.service.hub.sessions.get(server_id)
        info, caps = (session.info, session.caps) if session else (None, None)
        if info is None:
            row = await one(self.db.conn, 'SELECT info_json FROM server_nodes WHERE server_id=?', (server_id,))
            if not row:
                raise InstanceError('node_not_enrolled', 404)
            stored = json.loads(row['info_json'] or '{}')
            info, caps = stored.get('info') or {}, stored.get('caps')
        return info or {}, caps

    async def _platform(self, server_id, test):
        info, caps = await self._info(server_id)
        if (caps or {}).get('exec') is False:
            raise InstanceError('bench_exec_disabled', 409)
        platform = f'{info.get("platform") or "linux"}/{info.get("arch") or ""}'
        tool = catalog.TESTS[test]['package']
        if platform not in packages.manifest(tool, catalog.PACKAGES[tool]).get('platforms', []):
            raise InstanceError('bench_platform_unsupported', 409)
        if info.get('openwrt') and test not in catalog.OPENWRT_TESTS:
            raise InstanceError('bench_openwrt_unsupported', 409)
        return platform

    @staticmethod
    def _out_dir(row):
        """The run's directory on the node; one of its own for each attempt, so its job can be told apart."""
        return f'{catalog.NODE_ROOT}/runs/{row["id"]}' + (f'.{row["attempts"]}' if row.get('attempts') else '')

    @staticmethod
    def _label(row, verb):
        """What the server's command record says, e.g. 评测：运行 Net.Check.Place（完整模式）."""
        test = catalog.TESTS.get(row['test'])
        words = catalog.label(row['test'], json.loads(row.get('options_json') or '{}')) if test else ''
        return f'评测：{verb} {test["name"] if test else row["test"]}' + (f'（{words}）' if words else '')

    async def _patiently(self, run_id, step):
        """step(), again each time the node turns out not to be there, until it is or has been gone
        too long; (False, None) once the run was stopped meanwhile."""
        away = None
        while True:
            row = await self._get(run_id)
            try:
                if row['stop']:
                    job = await self._find_job(row['owner'], row['server_id'], self._out_dir(row)) if row['job_state'] == 'starting' else None
                    if job:
                        await self._adopt(run_id, job)
                    else:
                        await self._set(run_id, state='stopped', ended_ms=now_ms())
                    return False, None
                return True, await step()
            except InstanceError as exc:
                if exc.error not in NODE_AWAY:
                    raise
                away = away or time.monotonic()
                if not await self._away(away):
                    raise

    async def _launch(self, row):
        owner, server_id, test, run_id = row['owner'], row['server_id'], row['test'], row['id']
        await self._set(run_id, state='preparing')
        starting = self.starts.get(run_id)
        if starting and not starting.cancelled():
            try:
                job = (await asyncio.shield(starting))['job']
            except InstanceError as exc:
                self.starts.pop(run_id, None)
                if exc.error not in NODE_AWAY:
                    raise
            else:
                self.starts.pop(run_id, None)
                await self._adopt(run_id, job)
                return
        if row['job_state'] == 'starting':
            found, job = await self._patiently(run_id, lambda: self._find_job(owner, server_id, self._out_dir(row)))
            if not found:
                return
            if job:
                await self._adopt(run_id, job)
                return
        platform = await self._platform(server_id, test)
        pushed, _ = await self._patiently(run_id, lambda: self._push(row, platform))
        if not pushed:
            return
        out = self._out_dir(row)
        command = catalog.command(test, json.loads(row['options_json']), out)

        async def start():
            # A start whose answer was lost, or that the controller stopped following when it restarted,
            # may have started the job: look for it before starting another.
            found = await self._find_job(owner, server_id, out)
            if found:
                return found
            await self._set(run_id, job_state='starting')
            if (await self._get(run_id))['stop']:
                return None
            await self._stop_leftovers(server_id)
            # Like the node's own checks, the tests measure the machine's line, not a proxy's.
            if server_id in self.paused:
                return None
            task = self.service.spawn(self.service.job_start(owner, server_id, command, source='bench', log=self._label(row, '运行'), bypass=True))
            self.starts[run_id] = task
            # A cancelled waiter must not leave an unobserved exception from the RPC task.
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            try:
                return (await asyncio.shield(task))['job']
            finally:
                if task.done() and not asyncio.current_task().cancelling():
                    self.starts.pop(run_id, None)
        started, job = await self._patiently(run_id, start)
        if not started:
            return
        if job is None:
            await self._set(run_id, state='stopped', ended_ms=now_ms())
            return
        await self._adopt(run_id, job)

    async def _adopt(self, run_id, job):
        await self._set(run_id, state='running', job_id=job['id'], job_state=job['state'], started_ms=job.get('startedAtMs') or now_ms())

    async def _find_job(self, owner, server_id, out):
        marker = f'OB_BENCH_OUT={shlex.quote(out)} '
        jobs = (await self.service.jobs_list(owner, server_id))['jobs']
        return next((j for j in jobs if marker in j['command']), None)

    async def _stop_job(self, row):
        """Tell the node to end the run's job; False when the node is not there to tell."""
        try:
            await self.service.job_stop(row['owner'], row['server_id'], row['job_id'], source='bench', log=self._label(row, '停止'))
        except InstanceError as exc:
            if exc.error in NODE_AWAY:
                return False
            if exc.error != 'job_not_found':
                raise
        fresh = await self._get(row['id'])
        detail = {}
        if fresh and fresh['error'] == TIMED_OUT:
            minutes = catalog.time_limit(row['test'], json.loads(row['options_json'] or '{}'))
            detail['detail'] = f'超过 {catalog.limit_words(minutes)}未完成，已自动停止'
        await self._set(row['id'], stop=SENT, **detail)
        return True

    async def _stop_leftovers(self, server_id):
        """End the jobs of runs that reached their time limit while their node was away, before
        another run measures beside them."""
        for left in await many(self.db.conn, "SELECT * FROM server_bench_runs WHERE server_id=? AND state='interrupted' AND error=? AND stop=? AND job_id!=''",
                               (server_id, TIMED_OUT, ASKED)):
            if not await self._stop_job(left):
                raise InstanceError('node_offline', 503)

    async def _stop_later(self, row):
        while True:
            fresh = await self._get(row['id'])
            if not fresh or fresh['stop'] != ASKED:
                return
            try:
                if await self._stop_job(fresh):
                    return
            except InstanceError:
                log.warning('benchmark run %s: its job could not be stopped', row['id'], exc_info=True)
            # Unlike report collection, stopping a timed-out job must survive a long disconnection.
            await asyncio.sleep(AWAY_POLL_S)

    async def _time_out(self, row, minutes):
        await self._set(row['id'], stop=ASKED, error=TIMED_OUT, detail=f'超过 {catalog.limit_words(minutes)}未完成，已自动停止')
        await self._stop_job(row)

    async def _digest(self, path):
        stat = path.stat()
        known = self.digests.get(path)
        if known and known[:2] == (stat.st_size, stat.st_mtime_ns):
            return known[2]
        digest = await asyncio.to_thread(packages.sha256_file, path)
        self.digests[path] = (stat.st_size, stat.st_mtime_ns, digest)
        return digest

    async def _push(self, row, platform):
        """Put the test's package on the node unless the copy there is already this one; a new
        version replaces the tool's earlier ones there."""
        owner, server_id = row['owner'], row['server_id']
        tool = catalog.TESTS[row['test']]['package']
        version, base = catalog.PACKAGES[tool], catalog.package_dir(row['test'])
        files = await packages.files(tool, version, platform, self.cache)
        if row['test'] == 'hwcheck' and json.loads(row['options_json']).get('report'):
            dependency = await packages.files('geekbench5', catalog.PACKAGES['geekbench5'], platform, self.cache)
            files += [(rel, path, mode) for rel, path, mode in dependency if rel.startswith('bin/')]
        listing = ''
        for rel, path, _ in sorted(files):
            listing += f'{await self._digest(path)}  {rel}\n'
        try:
            present = (await self.service.read_file(owner, server_id, f'{base}/{LISTING}', 0, 1 << 20))['data'].decode('utf-8', 'replace')
        except InstanceError as exc:
            if exc.error in NODE_AWAY:
                raise
            present = None
        if present == listing:
            return
        started, size = now_ms(), 0
        for rel, path, mode in files:
            size += await self._upload(owner, server_id, f'{base}/{rel}', await asyncio.to_thread(path.read_bytes), f'{mode:o}')
        await self._upload(owner, server_id, f'{base}/{LISTING}', listing.encode(), '644')
        await self.service._log(owner, server_id, 'bench', f'评测：推送 {tool} {version}（{len(files)} 个文件，{size / 1e6:.1f} MB）', started,
                                {'exitCode': 0, 'outcome': 'ok'})
        others = f'for d in {shlex.quote(f"{catalog.NODE_ROOT}/{tool}")}-v*; do [ "$d" = {shlex.quote(base)} ] || rm -rf -- "$d"; done'
        try:
            await self.service.exec(owner, server_id, others, timeout=60, shell='sh', source='bench', log=f'评测：删除 {tool} 的旧版本')
        except InstanceError:
            pass  # a later push tries again

    async def _upload(self, owner, server_id, path, data, mode):
        upload = secrets.token_hex(12)
        offsets = range(0, max(len(data), 1), CHUNK)
        for offset in offsets:
            await self.service._write_chunk(owner, server_id, path, data[offset:offset + CHUNK], upload, offset,
                                            offset + CHUNK >= len(data), mode, True)
        return len(data)

    async def _away(self, since):
        """Wait out a node that is not connected; False once it has been gone too long."""
        if time.monotonic() - since > AWAY_LIMIT_S:
            return False
        await asyncio.sleep(AWAY_POLL_S)
        return True

    async def _wait(self, row):
        owner, server_id = row['owner'], row['server_id']
        minutes = catalog.time_limit(row['test'], json.loads(row['options_json'] or '{}'))
        # How long the job has run: by the node's own clock while it answers (its clock may not be
        # this one's), counted on from its last answer while it does not.
        seen = None

        def ran_ms():
            return seen[0] + (time.monotonic() - seen[1]) * 1000 if seen else now_ms() - row['started_ms']
        away = None
        while True:
            fresh = await self._get(row['id'])
            if fresh['stop'] == ASKED:
                await self._stop_job(row)
            try:
                job = (await self.service.job_status(owner, server_id, row['job_id'], limit=1))['job']
            except InstanceError as exc:
                if exc.error == 'job_not_found':
                    await self._set(row['id'], state='interrupted', error=TIMED_OUT if fresh['error'] == TIMED_OUT else 'bench_job_lost',
                                    ended_ms=now_ms())
                    return
                if exc.error not in NODE_AWAY:
                    raise
                if not fresh['stop'] and ran_ms() >= minutes * 60000:
                    await self._time_out(row, minutes)
                    fresh = await self._get(row['id'])
                if fresh['error'] == TIMED_OUT:
                    # Not held up for the node: its job is stopped once it is back.
                    await self._set(row['id'], state='interrupted', ended_ms=now_ms(),
                                    detail=f'超过 {catalog.limit_words(minutes)}未完成，已中断，等待节点上线后停止作业')
                    self.service.spawn(self._stop_later(fresh))
                    return
                away = away or time.monotonic()
                if not await self._away(away):
                    await self._set(row['id'], state='interrupted', error=exc.error, ended_ms=now_ms())
                    return
                continue
            away = None
            if job['state'] != 'running':
                await self._set(row['id'], state='collecting', job_state=job['state'], exit_code=job['exitCode'],
                                ended_ms=job.get('endedAtMs') or now_ms())
                return
            if isinstance(job.get('durationMs'), int):
                seen = (job['durationMs'], time.monotonic())
            if not fresh['stop'] and ran_ms() >= minutes * 60000:
                await self._time_out(row, minutes)
                continue
            await asyncio.sleep(POLL_S)

    async def _collect(self, row):
        away = None
        while True:
            try:
                return await self._gather(row)
            except InstanceError as exc:
                if exc.error not in NODE_AWAY:
                    raise
                fresh = await self._get(row['id'])
                if fresh['stop']:
                    # Stopped: the report is given up rather than waited for, freeing the server's queue.
                    await self._set(row['id'], state='interrupted' if fresh['error'] == TIMED_OUT else 'stopped')
                    return
                away = away or time.monotonic()
                if not await self._away(away):
                    await self._set(row['id'], state='interrupted', error=exc.error)
                    return

    async def _gather(self, row):
        owner, server_id, test, run_id = row['owner'], row['server_id'], row['test'], row['id']
        spec, out = catalog.TESTS[test], self._out_dir(row)
        raw_log = await self._read_log(owner, server_id, row['job_id'])
        sections = []
        if spec['collect'] == 'files':
            names = await self._names(owner, server_id, out)
            labelled = any(o['key'] == 'family' for o in spec['options'])
            for family, name in (('4', 'IPv4'), ('6', 'IPv6')):
                if f'report-{family}.ansi' not in names:
                    continue
                section = {'label': name if labelled else '', 'ansi': await _screen(await self._text(owner, server_id, f'{out}/report-{family}.ansi'))}
                if f'result-{family}.json' in names:
                    section['json'] = self._json(await self._text(owner, server_id, f'{out}/result-{family}.json'))
                if f'link-{family}.txt' in names:
                    section['link'] = self._link(await self._text(owner, server_id, f'{out}/link-{family}.txt'))
                sections.append(section)
        elif spec['collect'] == 'envelope':
            if 'envelope.json' in await self._names(owner, server_id, out):
                envelope = self._json(await self._text(owner, server_id, f'{out}/envelope.json'))
                if isinstance(envelope, dict) and isinstance(envelope.get('terminal'), str):
                    sections.append({'label': '', 'ansi': await _screen(envelope['terminal']), 'json': envelope.get('report')})
        else:
            report = await _screen(after_last_clear(raw_log))
            if report.strip():
                sections.append({'label': '', 'ansi': report, 'link': self._link(report)})
        if test == 'geekbench5':
            for section in sections:
                section['link'] = (section.get('json') or {}).get('url') or self._link(section['ansi'])
        for section in sections:
            if not section.get('link'):
                section.pop('link', None)
        fresh = await self._get(run_id)
        corrupt = row['exit_code'] == catalog.CHECKSUM_FAILED
        if corrupt and not fresh['stop'] and not fresh['attempts']:
            # The node's copy of the package was changed or cleaned away: push it again and run once more.
            await self._clean(row, listing=True)
            await self._set(run_id, state='queued', job_id='', job_state='', exit_code=None, started_ms=0, ended_ms=0, attempts=1)
            return
        timed_out = fresh['error'] == TIMED_OUT
        if timed_out and fresh['job_state'] == 'stopped':
            state, error = 'interrupted', TIMED_OUT
        elif fresh['stop'] and fresh['job_state'] == 'stopped':
            state, error = 'stopped', ''
        elif corrupt:
            state, error = 'failed', 'bench_package_corrupt'
        elif (spec['collect'] == 'log' or test == 'geekbench5') and row['exit_code'] != 0:
            # Geekbench's report is written whatever it did; its exit says whether it ran (127: it cannot here).
            state, error = 'failed', 'bench_script_failed'
        elif sections:
            state, error = 'done', ''
        else:
            state, error = 'failed', 'bench_package_corrupt' if corrupt else 'bench_no_report'
        links = [s['link'] for s in sections if s.get('link')]
        kept = (await _screen(raw_log, clears='mark')).encode()[-KEEP_LOG:].decode('utf-8', 'ignore')
        # One that ended on its own just as it was told to stop at its limit is not said to have been stopped.
        await self._set(run_id, state=state, error=error, sections=pack(sections) if sections else None, log=pack(kept),
                        links_json=json.dumps(links, ensure_ascii=False), **({'detail': ''} if timed_out and error != TIMED_OUT else {}))
        if test == 'unlock' and state == 'done':
            await self.service.unlock_full.adopt(owner, server_id, sections[0].get('json'), row['ended_ms'] or now_ms())
        await self._clean(row, listing=corrupt, package=bool((await self._info(server_id))[0].get('openwrt')))

    async def _clean(self, row, *, listing=False, package=False):
        """Remove the run's directory from the node (and the package's file list, so it is pushed again;
        or the whole package, off an OpenWrt node's RAM disk)."""
        command = f'rm -rf {shlex.quote(self._out_dir(row))}'
        if package:
            command += f' {shlex.quote(catalog.package_dir(row["test"]))}'
        elif listing:
            command += f' {shlex.quote(catalog.package_dir(row["test"]) + "/" + LISTING)}'
        try:
            await self.service.exec(row['owner'], row['server_id'], command, timeout=30, shell='sh', source='bench', log=self._label(row, '清理'))
        except InstanceError:
            pass  # the next run's command removes leftovers older than a day

    @staticmethod
    def _json(text):
        try:
            return json.loads(text)
        except ValueError:
            # A dual-stack run appends one document per family; keep the first.
            try:
                return json.JSONDecoder().raw_decode(text.lstrip())[0]
            except ValueError:
                return None

    @staticmethod
    def _link(text):
        found = LINKS.search(text or '')
        return found.group(0).rstrip('.') if found else ''

    async def _names(self, owner, server_id, path):
        try:
            listing = await self.service._node_call(owner, server_id, 'fs.list', {'path': path})
        except InstanceError as exc:
            if exc.error == 'not_found':
                return set()
            raise
        return {e.get('name') for e in listing.get('entries') or [] if isinstance(e, dict) and not e.get('dir')}

    async def _text(self, owner, server_id, path):
        data, offset = bytearray(), 0
        while offset < MAX_FILE:
            part = await self.service.read_file(owner, server_id, path, offset, CHUNK)
            data += part['data']
            offset += len(part['data'])
            if not part['data'] or offset >= int(part.get('size') or 0):
                break
        return bytes(data).decode('utf-8', 'replace')

    async def _read_log(self, owner, server_id, job_id):
        """The job's output, at most its last MAX_LOG_READ bytes."""
        try:
            first = (await self.service.job_status(owner, server_id, job_id, limit=1))['log']
        except InstanceError as exc:
            if exc.error == 'job_not_found':
                return ''
            raise
        offset = max(first['first'], first['size'] - MAX_LOG_READ)
        parts = []
        while offset < first['size']:
            part = (await self.service.job_status(owner, server_id, job_id, offset=offset, limit=256 * 1024))['log']
            if part['next'] <= offset:
                break
            parts.append(part['data'])
            offset = part['next']
        return ''.join(parts)
