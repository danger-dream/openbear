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
        self.digests = {}  # local file -> (size, mtime, sha256)
        self.cache = None

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
        if not isinstance(data, dict) or set(data) - {'serverId', 'test', 'suite', 'options'}:
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
        else:
            plan = [(data.get('test'), catalog.options(data.get('test'), data.get('options')))]
            suite = ''
        for test, _ in plan:
            await self._platform(server_id, test)
        created, base = [], now_ms()
        async with self.db.write_transaction(label='bench-create') as conn:
            for index, (test, opts) in enumerate(plan):
                run_id = secrets.token_hex(8)
                tool = catalog.TESTS[test]['package']
                await conn.execute('INSERT INTO server_bench_runs(id,owner,server_id,test,options_json,package,suite,state,created_ms) VALUES(?,?,?,?,?,?,?,?,?)',
                                   (run_id, owner, server_id, test, json.dumps(opts, ensure_ascii=False), f'{tool} {catalog.PACKAGES[tool]}', suite,
                                    'queued', base + index))
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
        return {'ok': True, 'run': run_view(await self._row(owner, run_id))}

    async def delete(self, owner, run_id):
        row = await self._row(owner, run_id)
        if row['state'] in WAITING:
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
        self.pending.add(server_id)
        task = self.workers.get(server_id)
        if not task or task.done():
            self.workers[server_id] = self.service.spawn(self._work(server_id))

    async def _work(self, server_id):
        try:
            while True:
                self.pending.discard(server_id)
                row = await one(self.db.conn, f"""SELECT * FROM server_bench_runs WHERE server_id=? AND state IN {WAITING}
                                                 ORDER BY state='queued', created_ms LIMIT 1""", (server_id,))
                if not row:
                    if server_id in self.pending:
                        continue
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
        if row['state'] in ('queued', 'preparing'):
            await self._launch(row)
            row = await self._get(row['id'])
        if row and row['state'] == 'running':
            await self._wait(row)
            row = await self._get(row['id'])
        if row and row['state'] == 'collecting':
            await self._collect(row)

    async def _platform(self, server_id, test):
        session = self.service.hub.sessions.get(server_id)
        info, caps = (session.info, session.caps) if session else (None, None)
        if info is None:
            row = await one(self.db.conn, 'SELECT info_json FROM server_nodes WHERE server_id=?', (server_id,))
            if not row:
                raise InstanceError('node_not_enrolled', 404)
            stored = json.loads(row['info_json'] or '{}')
            info, caps = stored.get('info') or {}, stored.get('caps')
        if (caps or {}).get('exec') is False:
            raise InstanceError('bench_exec_disabled', 409)
        platform = f'{info.get("platform") or "linux"}/{info.get("arch") or ""}'
        tool = catalog.TESTS[test]['package']
        if platform not in packages.manifest(tool, catalog.PACKAGES[tool]).get('platforms', []):
            raise InstanceError('bench_platform_unsupported', 409)
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
            return (await self.service.job_start(owner, server_id, command, source='bench', log=self._label(row, '运行')))['job']
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
        await self._set(row['id'], stop=SENT)
        return True

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
            size += await self._upload(owner, server_id, f'{base}/{rel}', path.read_bytes(), f'{mode:o}')
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
        away = None
        while True:
            if (await self._get(row['id']))['stop'] == ASKED:
                await self._stop_job(row)
            try:
                job = (await self.service.job_status(owner, server_id, row['job_id'], limit=1))['job']
            except InstanceError as exc:
                if exc.error == 'job_not_found':
                    await self._set(row['id'], state='interrupted', error='bench_job_lost', ended_ms=now_ms())
                    return
                if exc.error not in NODE_AWAY:
                    raise
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
            await asyncio.sleep(POLL_S)

    async def _collect(self, row):
        away = None
        while True:
            try:
                return await self._gather(row)
            except InstanceError as exc:
                if exc.error not in NODE_AWAY:
                    raise
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
        if fresh['stop'] and fresh['job_state'] == 'stopped':
            state, error = 'stopped', ''
        elif corrupt:
            state, error = 'failed', 'bench_package_corrupt'
        elif spec['collect'] == 'log' and row['exit_code'] != 0:
            state, error = 'failed', 'bench_script_failed'
        elif sections:
            state, error = 'done', ''
        else:
            state, error = 'failed', 'bench_package_corrupt' if corrupt else 'bench_no_report'
        links = [s['link'] for s in sections if s.get('link')]
        kept = (await _screen(raw_log, clears='mark')).encode()[-KEEP_LOG:].decode('utf-8', 'ignore')
        await self._set(run_id, state=state, error=error, sections=pack(sections) if sections else None, log=pack(kept),
                        links_json=json.dumps(links, ensure_ascii=False))
        await self._clean(row, listing=corrupt)

    async def _clean(self, row, *, listing=False):
        """Remove the run's directory from the node (and the package's file list, so it is pushed again)."""
        command = f'rm -rf {shlex.quote(self._out_dir(row))}'
        if listing:
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
