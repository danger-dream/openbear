"""Cron definitions, occurrence acceptance and one lifecycle-owned calendar timer."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from app.lifecycle import wait_for_startup
from app.tools.confirmation import argument_digest, constraints_within
from app.cron.contracts import CronError, JobConfig, Schedule, timestamp
from app.cron.schedule import next_state, preview_details
from app.cron.holidays import MissingCalendar
from app.cron.holiday_sync import HolidayStore
from app.webhooks.contracts import digest, dumps, iso
from app.webhooks.repository import insert, many, one, uid
from app.webhooks.scripts import resolve_environment

log = logging.getLogger(__name__)
ACTIVE = ('starting', 'running')


class CronService:
    def __init__(self, db, host, *, clock=None, holiday_store=None):
        self.db, self.host = db, host
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.wake = asyncio.Event()
        self.tasks = {}
        self.loop_task = None
        self.closing = False
        self.confirmations = {}
        self.holidays = holiday_store or HolidayStore(Path(db._path).expanduser().parent / 'cron-holidays-cn.json', self.clock)
        self.holiday_loop_task = self.holiday_refresh_task = None

    def holiday_status(self):
        return {**self.holidays.status(), 'syncing': bool(self.holiday_refresh_task and not self.holiday_refresh_task.done())}

    def request_holiday_sync(self):
        if not self.holiday_refresh_task or self.holiday_refresh_task.done():
            self.holiday_refresh_task = asyncio.create_task(self._sync_holidays(), name='cron-holiday-sync')
        return {'holidayCalendar': self.holiday_status()}

    async def _sync_holidays(self):
        try:
            return await self.holidays.refresh(self._apply_calendar)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.holidays.error = f'日历更新失败，保留已有数据：{exc}'
            log.exception('Cron holiday sync failure')

    async def holiday_loop(self):
        await wait_for_startup(self)
        while not self.closing:
            self.request_holiday_sync()
            await asyncio.shield(self.holiday_refresh_task)
            await asyncio.sleep(86400)

    async def _apply_calendar(self, snapshot):
        async with self.db.write_transaction(label='cron-holiday-update') as conn:
            now = self.clock()
            for row in await many(conn, 'SELECT * FROM cron_jobs WHERE enabled=1 AND deleted_at_ms IS NULL'):
                schedule = JobConfig.model_validate_json(row['config_json']).schedule
                if not schedule.skip_holidays or row['schedule_state'] == 'target_missing':
                    continue
                next_at, state = next_state(schedule, now, snapshot)
                await conn.execute('UPDATE cron_jobs SET next_run_at_ms=?,schedule_state=? WHERE job_id=?', (next_at, state, row['job_id']))
        # No await between publishing the DB schedule and its matching snapshot.
        self.holidays.snapshot = snapshot
        self.wake.set()

    def schedule_next(self, schedule, now):
        return next_state(schedule, now, self.holidays.snapshot, exhausted='exhausted' if schedule.end_at else 'missed')

    @property
    def script_environment(self):
        # Interpreter/cwd/output limits are execution infrastructure, not event policy.
        return self.host.config.webhooks.scripts

    async def row(self, conn, owner, job_id, *, deleted=False):
        row = await one(conn, 'SELECT * FROM cron_jobs WHERE job_id=? AND owner_chat_id=?', (job_id, owner))
        if not row or row['deleted_at_ms'] is not None and not deleted:
            raise CronError('not_found', status=404)
        return row

    async def folders(self, owner):
        folders = await self.host._tree_folders(owner)
        children = {}
        for folder in folders.values():
            children.setdefault(folder.get('parent_uuid') or '', []).append(folder)
        result = []
        def walk(parent, seen):
            for f in sorted(children.get(parent, []), key=lambda x: (not bool(x.get('pinned_at')), x.get('display_order') or 0, -(x.get('created_at') or 0), -(x.get('id') or 0))):
                fid = f['folder_uuid']
                if fid in seen:
                    continue
                result.append({'id': fid, 'name': f['name'], 'parentId': parent,
                               'path': self.host._tree_folder_path_text(fid, folders)})
                walk(fid, seen | {fid})
        walk('', set())
        return {'items': result}

    async def validate(self, owner, folder_id, config, enabled):
        # Unlike ordinary conversations, Cron cannot target the temporary root.
        if not isinstance(folder_id, str) or not folder_id.strip():
            raise CronError('folder_required', '定时任务必须绑定真实目录')
        if not await self.host._tree_folder_owned(owner, folder_id):
            raise CronError('folder_not_found', status=404)
        if enabled and not config.instructions.strip():
            raise CronError('instructions_required', '启用或运行任务必须填写处理指令')
        for script in (config.pre, config.post):
            if script.enabled:
                resolve_environment(script, self.script_environment)
                if script.timeout_seconds > self.script_environment.max_timeout_seconds:
                    raise CronError('script_timeout_exceeds_system_limit')
        return await self.run_defaults(owner, folder_id, config)

    async def run_defaults(self, owner, folder_id, config):
        overrides = {k: v for k, v in config.run_config.wire().items() if v is not None}
        folder = await self.host._tree_folder_run_defaults(owner, folder_id)
        _, defaults = await self.host._web_run_defaults_candidate(owner)
        effective = self.host._apply_folder_run_defaults(defaults, {**folder, **overrides})
        if overrides:
            validated, error = self.host._validate_web_defaults_patch(overrides, effective)
            if error:
                raise CronError('invalid_run_config', str(error[0]))
            return self.host._normalize_web_run_defaults({**self.host._web_defaults_storage(effective), **validated})
        return effective

    async def operation(self, conn, owner, action, request):
        request_id = request.get('requestId')
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise CronError('request_id_required')
        fp = digest({'action': action, 'request': request})
        old = await one(conn, 'SELECT * FROM cron_operations WHERE owner_chat_id=? AND request_id=?', (owner, request_id))
        if old and old['fingerprint'] != fp:
            raise CronError('request_id_conflict', status=409)
        return json.loads(old['result_json']) if old else None, fp

    async def record(self, conn, owner, request, fp, result):
        await insert(conn, 'cron_operations', owner_chat_id=owner, request_id=request['requestId'], fingerprint=fp,
                     result_json=dumps(result), created_at_ms=self.clock())

    @staticmethod
    def check_revision(row, request):
        if request.get('expectedRevision') != row['revision']:
            raise CronError('revision_conflict', '配置已变化，请重新读取', status=409, currentRevision=row['revision'])

    def normalize(self, value, now):
        config = JobConfig.model_validate(value or {})
        if config.schedule.kind == 'every' and not config.schedule.anchor_at:
            config.schedule.anchor_at = config.schedule.start_at or iso(now)
        # An unpublished year is a recoverable wait, not invalid configuration.
        self.schedule_next(config.schedule, now)
        return config

    async def public(self, conn, row, *, folders=None):
        if folders is None:
            folders = await self.host._tree_folders(row['owner_chat_id'])
        folder = folders.get(row['folder_id'])
        active = await one(conn, "SELECT COUNT(*) n FROM cron_runs WHERE job_id=? AND status IN ('starting','running')", (row['job_id'],))
        last = await one(conn, 'SELECT * FROM cron_runs WHERE job_id=? ORDER BY started_at_ms DESC,run_id DESC LIMIT 1', (row['job_id'],))
        return dict(id=row['job_id'], folderId=row['folder_id'], folderName=folder['name'] if folder else '',
                    folderPath=self.host._tree_folder_path_text(row['folder_id'], folders) if folder else '', name=row['name'], description=row['description'],
                    enabled=bool(row['enabled']), revision=row['revision'], config=json.loads(row['config_json']),
                    nextRunAt=iso(row['next_run_at_ms']), scheduleState='deleted' if row['deleted_at_ms'] else 'target_missing' if not folder else row['schedule_state'],
                    activeRuns=active['n'], lastRun=self.run_public(last) if last else None,
                    createdAt=iso(row['created_at_ms']), updatedAt=iso(row['updated_at_ms']))

    async def get(self, owner, job_id):
        return {'job': await self.public(self.db.conn, await self.row(self.db.conn, owner, job_id))}

    async def list(self, owner, params):
        where, args = ['owner_chat_id=?', 'deleted_at_ms IS NULL'], [owner]
        if params.get('folderId'):
            where.append('folder_id=?'); args.append(params['folderId'])
        if params.get('enabled') not in (None, ''):
            if str(params['enabled']).lower() not in ('true', 'false'):
                raise CronError('invalid_enabled')
            where.append('enabled=?'); args.append(int(str(params['enabled']).lower() == 'true'))
        if params.get('search'):
            where.append('(instr(lower(name),lower(?)) OR instr(lower(description),lower(?)))')
            args.extend([params['search']] * 2)
        clause = ' AND '.join(where)
        count = await one(self.db.conn, 'SELECT COUNT(*) n FROM cron_jobs WHERE ' + clause, args)
        limit, offset = self.pagination(params)
        rows = await many(self.db.conn, 'SELECT * FROM cron_jobs WHERE ' + clause + ' ORDER BY created_at_ms DESC,job_id LIMIT ? OFFSET ?', (*args, limit, offset))
        folders = await self.host._tree_folders(owner)
        return {'items': [await self.public(self.db.conn, r, folders=folders) for r in rows], 'total': count['n']}

    async def calendar(self, owner, params):
        from app.cron.calendar import calendar
        return await calendar(self, owner, params)

    @staticmethod
    def pagination(params):
        return min(100, max(1, int(params.get('limit') or 30))), max(0, int(params.get('offset') or 0))

    async def save(self, owner, request, job_id=None):
        now = self.clock()
        async with self.db.write_transaction(label='cron-config') as conn:
            previous, fp = await self.operation(conn, owner, 'update:' + job_id if job_id else 'create', request)
            if previous:
                return previous
            old = await self.row(conn, owner, job_id) if job_id else None
            if old:
                self.check_revision(old, request)
                if 'folderId' in request and request['folderId'] != old['folder_id']:
                    raise CronError('folder_immutable')
                if not isinstance(request.get('config'), dict):
                    raise CronError('config_required', '更新任务必须提供完整配置')
            folder = old['folder_id'] if old else request.get('folderId', '')
            enabled = request.get('enabled', bool(old['enabled']) if old else False)
            if type(enabled) is not bool:
                raise CronError('invalid_enabled')
            name = str(request.get('name', old['name'] if old else '')).strip()
            if not name or len(name) > 200:
                raise CronError('invalid_name', '任务名称必填且不超过200字')
            config = self.normalize(request.get('config'), now)
            await self.validate(owner, folder, config, enabled)
            same_schedule = old and JobConfig.model_validate_json(old['config_json']).schedule == config.schedule
            if old and enabled and old['enabled'] and same_schedule:
                next_at, state = old['next_run_at_ms'], old['schedule_state']
            else:
                next_at, state = self.schedule_next(config.schedule, now) if enabled else (None, 'disabled')
            job_id = job_id or uid()
            if old:
                await conn.execute('UPDATE cron_jobs SET name=?,description=?,enabled=?,revision=revision+1,config_json=?,next_run_at_ms=?,schedule_state=?,updated_at_ms=? WHERE job_id=?',
                    (name, str(request.get('description', old['description'])), int(enabled), dumps(config.wire()), next_at, state, now, job_id))
            else:
                await insert(conn, 'cron_jobs', job_id=job_id, owner_chat_id=owner, folder_id=folder, name=name,
                    description=str(request.get('description') or ''), enabled=int(enabled), config_json=dumps(config.wire()),
                    next_run_at_ms=next_at, schedule_state=state, created_at_ms=now, updated_at_ms=now)
            result = {'job': await self.public(conn, await self.row(conn, owner, job_id))}
            await self.record(conn, owner, request, fp, result)
        self.wake.set()
        if hasattr(self.host, '_schedule_conversation_retention'):
            self.host._schedule_conversation_retention()
        return result

    async def set_enabled(self, owner, job_id, request):
        async with self.db.write_transaction(label='cron-enable') as conn:
            previous, fp = await self.operation(conn, owner, 'enable:' + job_id, request)
            if previous: return previous
            row = await self.row(conn, owner, job_id)
            self.check_revision(row, request)
            enabled = request.get('enabled')
            if type(enabled) is not bool: raise CronError('invalid_enabled')
            config = JobConfig.model_validate_json(row['config_json'])
            if enabled: await self.validate(owner, row['folder_id'], config, True)
            if enabled and row['enabled']:
                next_at, state = row['next_run_at_ms'], row['schedule_state']
            else:
                next_at, state = self.schedule_next(config.schedule, self.clock()) if enabled else (None, 'disabled')
            await conn.execute('UPDATE cron_jobs SET enabled=?,next_run_at_ms=?,schedule_state=?,revision=revision+1,updated_at_ms=? WHERE job_id=?',
                               (int(enabled), next_at, state, self.clock(), job_id))
            result = {'job': await self.public(conn, await self.row(conn, owner, job_id))}
            await self.record(conn, owner, request, fp, result)
        self.wake.set()
        return result

    async def _delete_constraints(self, conn, job_id):
        runs = await many(conn, "SELECT run_id FROM cron_runs WHERE job_id=? AND status IN ('starting','running')", (job_id,))
        return {'sets': {'activeRuns': [row['run_id'] for row in runs]}}

    async def delete_impact(self, owner, job_id, request, *, tool_approval=False):
        async with self.db.write_transaction(label='cron-delete-preview') as conn:
            row = await self.row(conn, owner, job_id); self.check_revision(row, request)
            counts = await one(conn, "SELECT COUNT(*) total,COUNT(CASE WHEN status IN ('starting','running') THEN 1 END) active FROM cron_runs WHERE job_id=?", (job_id,))
            token = secrets.token_urlsafe(32)
            self.confirmations = {k: v for k, v in self.confirmations.items() if v['expiry'] > self.clock()}
            grant = dict(owner=owner, job=job_id, revision=row['revision'], expiry=self.clock()+300000)
            result = {'impact': {'activeRuns': counts['active'], 'retainedRuns': counts['total']}, 'confirmationToken': token}
            if tool_approval:
                # Identity/revision are exact; existing active runs may finish.
                # Run IDs, not counts, prevent a replacement run reusing a grant.
                grant['approvalConstraints'] = await self._delete_constraints(conn, job_id)
                result.update(approvalScope={'contract': 'cron-delete-v1', 'jobId': job_id, 'revision': row['revision'],
                    'parameters': argument_digest({k: v for k, v in request.items() if k not in ('requestId', 'expectedRevision')})},
                    approvalConstraints=grant['approvalConstraints'])
            self.confirmations[digest(token)] = grant
            return result

    async def tool_delete_result(self, owner, job_id, request):
        # A replay is identified by the original tool intent, not a fresh grant.
        previous, _ = await self.operation(self.db.conn, owner, 'tool-delete:' + job_id, request)
        return previous

    async def delete(self, owner, job_id, request, *, tool_request=None):
        key = digest(request.get('confirmationToken'))
        intent = tool_request if tool_request is not None else {**request, 'confirmationToken': key}
        action = ('tool-delete:' if tool_request is not None else 'delete:') + job_id
        async with self.db.write_transaction(label='cron-delete') as conn:
            previous, fp = await self.operation(conn, owner, action, intent)
            if previous: return previous
            row = await self.row(conn, owner, job_id)
            grant = self.confirmations.get(key)
            if not grant or grant['owner'] != owner or grant['job'] != job_id or grant['expiry'] <= self.clock():
                raise CronError('confirmation_required', status=403)
            if grant['revision'] != row['revision']: raise CronError('confirmation_stale', status=409)
            if ('approvalConstraints' in grant and not constraints_within(
                    grant['approvalConstraints'], await self._delete_constraints(conn, job_id))):
                raise CronError('confirmation_stale', status=409)
            await conn.execute("UPDATE cron_jobs SET enabled=0,next_run_at_ms=NULL,schedule_state='deleted',deleted_at_ms=?,updated_at_ms=?,revision=revision+1 WHERE job_id=?", (self.clock(), self.clock(), job_id))
            result = {'job': await self.public(conn, await self.row(conn, owner, job_id, deleted=True))}
            await self.record(conn, owner, intent, fp, result)
        self.confirmations.pop(key, None); self.wake.set()
        return result

    async def accept_run(self, conn, row, scheduled, trigger, occurrence):
        run_id = uid()
        await insert(conn, 'cron_runs', run_id=run_id, job_id=row['job_id'], owner_chat_id=row['owner_chat_id'], folder_id=row['folder_id'],
                     job_name=row['name'], trigger_kind=trigger, scheduled_at_ms=scheduled, occurrence_key=occurrence,
                     config_json=row['config_json'], config_revision=row['revision'], root_turn_uuid=uid(), started_at_ms=self.clock())
        return await one(conn, 'SELECT * FROM cron_runs WHERE run_id=?', (run_id,))

    def launch(self, row):
        from app.cron.executor import execute
        task = asyncio.create_task(execute(self, row), name='cron:' + row['run_id'])
        self.tasks[row['run_id']] = task
        def done(finished):
            self.tasks.pop(row['run_id'], None)
            if not finished.cancelled() and finished.exception():
                log.error('Cron execution failed: %s', row['run_id'], exc_info=finished.exception())
        task.add_done_callback(done)

    async def run(self, owner, job_id, request):
        async with self.db.write_transaction(label='cron-manual') as conn:
            previous, fp = await self.operation(conn, owner, 'run:' + job_id, request)
            if previous: return previous
            row = await self.row(conn, owner, job_id); self.check_revision(row, request)
            await self.validate(owner, row['folder_id'], JobConfig.model_validate_json(row['config_json']), True)
            run = await self.accept_run(conn, row, self.clock(), 'manual', f'manual:{owner}:{request["requestId"]}')
            result = {'run': self.run_public(run)}
            await self.record(conn, owner, request, fp, result)
        self.launch(run)
        return result

    async def start(self):
        now = self.clock()
        async with self.db.write_transaction(label='cron-startup') as conn:
            await conn.execute("UPDATE cron_runs SET status='interrupted',outcome='interrupted',phase='finished',error='服务重启中断，未自动重跑',finished_at_ms=?,notification_state='pending' WHERE status IN ('starting','running')", (now,))
            for row in await many(conn, 'SELECT * FROM cron_jobs WHERE enabled=1 AND deleted_at_ms IS NULL'):
                cfg = JobConfig.model_validate_json(row['config_json'])
                if row['schedule_state'] == 'target_missing':
                    continue
                if cfg.schedule.skip_holidays or row['next_run_at_ms'] is not None and row['next_run_at_ms'] < now:
                    next_at, state = self.schedule_next(cfg.schedule, now)
                    await conn.execute('UPDATE cron_jobs SET next_run_at_ms=?,schedule_state=? WHERE job_id=?', (next_at, state, row['job_id']))
        from app.cron.notifications import deliver
        for run in await many(self.db.conn, "SELECT * FROM cron_runs WHERE notification_state='pending' AND phase='finished'"):
            await deliver(self, run)  # Only idempotent channel enqueue, never business replay.
        self.closing = False
        self.loop_task = asyncio.create_task(self.loop(), name='cron-calendar')
        self.holiday_loop_task = asyncio.create_task(self.holiday_loop(), name='cron-holiday-daily')

    async def close(self):
        self.closing = True; self.wake.set()
        if self.loop_task:
            self.loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError): await self.loop_task
        for task in (self.holiday_loop_task, self.holiday_refresh_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError): await task
        for task in list(self.tasks.values()): task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)

    async def loop(self):
        await wait_for_startup(self)
        while not self.closing:
            self.wake.clear()
            try: await self.tick()
            except asyncio.CancelledError: raise
            except Exception: log.exception('Cron timer failure')
            soon = await one(self.db.conn, 'SELECT MIN(next_run_at_ms) n FROM cron_jobs WHERE enabled=1 AND deleted_at_ms IS NULL')
            delay = min(60, max(.01, (soon['n'] - self.clock()) / 1000)) if soon['n'] is not None else 60
            try: await asyncio.wait_for(self.wake.wait(), delay)
            except TimeoutError: pass

    async def tick(self):
        accepted = []
        now = self.clock()
        async with self.db.write_transaction(label='cron-tick') as conn:
            for row in await many(conn, 'SELECT * FROM cron_jobs WHERE enabled=1 AND deleted_at_ms IS NULL AND next_run_at_ms<=? ORDER BY next_run_at_ms,job_id', (now,)):
                if not await one(conn, 'SELECT 1 FROM web_conversation_folders WHERE folder_uuid=? AND owner_chat_id=?', (row['folder_id'], row['owner_chat_id'])):
                    await conn.execute("UPDATE cron_jobs SET next_run_at_ms=NULL,schedule_state='target_missing' WHERE job_id=?", (row['job_id'],)); continue
                cfg = JobConfig.model_validate_json(row['config_json'])
                due = row['next_run_at_ms']
                if cfg.schedule.end_at and now >= timestamp(cfg.schedule.end_at):
                    await conn.execute("UPDATE cron_jobs SET next_run_at_ms=NULL,schedule_state='exhausted' WHERE job_id=?", (row['job_id'],))
                    continue
                # Recheck a persisted due time against the current calendar.
                # Missing next-year data must not roll back today's accepted run.
                allowed = True
                if cfg.schedule.skip_holidays:
                    try:
                        allowed = not self.holidays.snapshot.is_day_off(datetime.fromtimestamp(due / 1000, ZoneInfo(cfg.schedule.timezone)).date())
                    except MissingCalendar:
                        allowed = False
                if allowed:
                    run = await self.accept_run(conn, row, due, 'scheduled', f'scheduled:{row["job_id"]}:{due}')
                    accepted.append(run)
                next_at, state = next_state(cfg.schedule, max(now, due), self.holidays.snapshot)
                await conn.execute('UPDATE cron_jobs SET next_run_at_ms=?,schedule_state=? WHERE job_id=?', (next_at, state, row['job_id']))
        for row in accepted: self.launch(row)

    @staticmethod
    def run_public(r, detail=False):
        result = dict(id=r['run_id'], jobId=r['job_id'], jobName=r['job_name'], folderId=r['folder_id'], trigger=r['trigger_kind'],
            scheduledAt=iso(r['scheduled_at_ms']), startedAt=iso(r['started_at_ms']), finishedAt=iso(r['finished_at_ms']),
            conversationId=r['conversation_uuid'], rootTurnId=r['root_turn_uuid'], status=r['status'], phase=r['phase'], outcome=r['outcome'],
            error=r['error'], totalTokens=r['total_tokens'], costUsd=r['cost_usd'], modelCalls=r['model_calls'],
            durationSeconds=(r['finished_at_ms']-r['started_at_ms'])/1000 if r['finished_at_ms'] is not None else None,
            notificationState=r['notification_state'])
        if detail:
            result.update(configSnapshot=json.loads(r['config_json']), preAttempts=json.loads(r['pre_attempts_json']), postAttempts=json.loads(r['post_attempts_json']), finalText=r['final_text'])
        return result

    async def get_run(self, owner, run_id):
        row = await one(self.db.conn, 'SELECT * FROM cron_runs WHERE run_id=? AND owner_chat_id=?', (run_id, owner))
        if not row: raise CronError('not_found', status=404)
        return {'run': self.run_public(row, True)}

    def run_filters(self, owner, params):
        from app.cron.contracts import timestamp
        where, args = ['owner_chat_id=?'], [owner]
        for key, col in [('jobId', 'job_id'), ('folderId', 'folder_id'), ('status', 'status')]:
            if params.get(key): where.append(col+'=?'); args.append(params[key])
        for key, op in [('start', '>='), ('end', '<')]:
            if params.get(key):
                value = params[key]; ms = int(value) if str(value).isdigit() else timestamp(value)
                where.append('started_at_ms'+op+'?'); args.append(ms)
        return ' AND '.join(where), args

    async def runs(self, owner, params):
        where, args = self.run_filters(owner, params)
        total = await one(self.db.conn, 'SELECT COUNT(*) n FROM cron_runs WHERE '+where, args)
        limit, offset = self.pagination(params)
        rows = await many(self.db.conn, 'SELECT * FROM cron_runs WHERE '+where+' ORDER BY started_at_ms DESC,run_id DESC LIMIT ? OFFSET ?', (*args, limit, offset))
        return {'items': [self.run_public(r) for r in rows], 'total': total['n']}

    async def statistics(self, owner, params):
        where, args = self.run_filters(owner, params)
        r = await one(self.db.conn, '''SELECT COUNT(*) runs,
            COUNT(CASE WHEN status='completed' THEN 1 END) completed,
            COUNT(CASE WHEN status IN ('failed','post_failed') THEN 1 END) failed,
            COUNT(CASE WHEN status='cancelled' THEN 1 END) cancelled,
            COUNT(CASE WHEN status='interrupted' THEN 1 END) interrupted,
            COUNT(CASE WHEN status IN ('timed_out','tokens_exceeded','cost_exceeded') THEN 1 END) limited,
            COUNT(CASE WHEN status IN ('starting','running') THEN 1 END) active,
            COALESCE(SUM(total_tokens),0) totalTokens,COALESCE(SUM(cost_usd),0) costUsd,
            AVG((finished_at_ms-started_at_ms)/1000.0) averageDurationSeconds FROM cron_runs WHERE '''+where, args)
        return r

    async def owns_event(self, conversation, root):
        return bool(root and await one(self.db.conn, 'SELECT 1 FROM cron_runs WHERE conversation_uuid=? AND root_turn_uuid=?', (conversation, root)))

    async def stop(self, owner, run_id, request):
        async with self.db.write_transaction(label='cron-stop-intent') as conn:
            previous, fp = await self.operation(conn, owner, 'stop:' + str(run_id), request)
            if previous: return previous
            row = await one(conn, 'SELECT * FROM cron_runs WHERE run_id=? AND owner_chat_id=?', (run_id, owner))
            if not row: raise CronError('not_found', status=404)
            conversation = await one(conn, 'SELECT * FROM web_conversations WHERE conversation_uuid=? AND owner_chat_id=?', (row['conversation_uuid'], owner))
            if row['status'] in ACTIVE and not conversation: raise CronError('conversation_starting', status=409)
            result = {'accepted': True, 'run': self.run_public(row, True)}
            # A lost response never turns a repeated stop into cancellation of cleanup.
            await self.record(conn, owner, request, fp, result)
        if row['status'] in ACTIVE:
            stopped = await self.host._stop_web_conversation(conversation, requested_by='cron-tool', message='已停止定时任务会话')
            result = {**stopped, **await self.get_run(owner, run_id)}
            async with self.db.write_transaction(label='cron-stop-result') as conn:
                await conn.execute('UPDATE cron_operations SET result_json=? WHERE owner_chat_id=? AND request_id=?', (dumps(result), owner, request['requestId']))
        return result

    def preview(self, request):
        return {**preview_details(Schedule.model_validate(request.get('schedule')), self.clock(), request.get('count', 5), self.holidays.snapshot),
                'holidayCalendar': self.holiday_status()}
