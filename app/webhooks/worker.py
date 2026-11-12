"""Lifecycle-owned bounded stage scheduler. SQLite, not tasks, owns eligibility."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from app.lifecycle import wait_for_startup
from app.webhooks import routing
from app.webhooks.contracts import dumps
from app.webhooks.receipts import receipt
from app.webhooks.repository import insert, many, one, uid
from app.webhooks.scripts import execute, StartDeferred
from app.webhooks.observability import span

log=logging.getLogger(__name__)


class Worker:
    def __init__(self,service):
        self.s=service; self.tasks={}; self.loop_task=None; self.closing=False; self.last_prune=0
        service.worker=self

    async def start(self):
        await self.recover()
        self.closing=False
        self.loop_task=asyncio.create_task(self.run(),name='webhook-discovery')

    async def close(self):
        self.closing=True; self.s.wake.set()
        if self.loop_task:
            self.loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError): await self.loop_task
        for task in list(self.tasks.values()): task.cancel()
        await asyncio.gather(*self.tasks.values(),return_exceptions=True)
        self.tasks.clear()

    async def recover(self):
        s=self.s
        if self.tasks or s.bridge and s.bridge.active:
            raise RuntimeError('Webhook startup recovery cannot run over live owned executions')
        async with s.db.webhook_transaction() as conn:
            from app.webhooks.observability import recover_unsettled_calls
            await recover_unsettled_calls(conn)
            # on_start commits running BEFORE spawn. Reserved proves that callback
            # never completed; a running spawn-gap remains conservatively unknown.
            await conn.execute("UPDATE webhook_stage_jobs SET state='pending',row_version=row_version+1 WHERE state='running' AND job_id IN (SELECT job_id FROM webhook_stage_attempts WHERE state='reserved' AND started_at_ms IS NULL)")
            await conn.execute("UPDATE webhook_stage_attempts SET state='cancelled',finished_at_ms=?,error_class='interrupted_before_start',side_effect_state='none' WHERE state='reserved' AND started_at_ms IS NULL",(s.clock(),))
            await conn.execute("UPDATE webhook_stage_attempts SET state='unknown',finished_at_ms=?,error_class='process_interrupted',side_effect_state='unknown' WHERE state='running'",(s.clock(),))
            await conn.execute("UPDATE webhook_stage_jobs SET state='unknown',row_version=row_version+1 WHERE state='running'")
            await conn.execute("UPDATE webhook_events SET route_state='hold',review_required=1,terminal_reason='unknown_script_effect' WHERE event_id IN (SELECT event_id FROM webhook_stage_jobs WHERE state='unknown')")
            await conn.execute("UPDATE webhook_assignments SET recovery_state='needs_control',row_version=row_version+1 WHERE state!='finalized'")
            # ModelCallDriver commits every model/tool action before transport.
            # No action, wait or receipt proves this reservation has not executed
            # business. Retain the same assignment/root/conversation for dispatch.
            safe=await many(conn,"""SELECT a.assignment_id FROM webhook_assignments a
                WHERE a.state='open' AND a.origin_kind='webhook'
                AND NOT EXISTS(SELECT 1 FROM webhook_runtime_links l JOIN runtime_actions r USING(run_id) WHERE l.assignment_id=a.assignment_id)
                AND NOT EXISTS(SELECT 1 FROM webhook_waits w WHERE w.assignment_id=a.assignment_id)
                AND NOT EXISTS(SELECT 1 FROM webhook_receipts r JOIN webhook_assignment_events m USING(assignment_event_id) WHERE m.assignment_id=a.assignment_id)
                AND NOT EXISTS(SELECT 1 FROM webhook_runtime_links l JOIN runtime_runs r USING(run_id) WHERE l.assignment_id=a.assignment_id AND r.status IN ('running','waiting'))""")
            for a in safe:
                await conn.execute("UPDATE webhook_assignments SET recovery_state='healthy' WHERE assignment_id=?",(a['assignment_id'],))
                await conn.execute('UPDATE webhook_assignment_events SET delivered_at_ms=NULL,delivery_command_id=NULL WHERE assignment_id=?',(a['assignment_id'],))
                await conn.execute("UPDATE webhook_phase_spans SET ended_at_ms=started_at_ms,end_reason='interrupted_before_execution' WHERE assignment_id=? AND ended_at_ms IS NULL",(a['assignment_id'],))
            await conn.execute("UPDATE web_task_notifications SET state='paused',last_error='interrupted delivery; verify channel state',claim_token='' WHERE kind='webhook-result' AND state='processing'")
            from app.webhooks.controls import discard_backlog
            await discard_backlog(conn)

    async def run(self):
        await wait_for_startup(self)
        self.last_prune = self.s.clock()
        wake=self.s.wake.listen()
        try:
            while not self.closing:
                wake.clear(); pause=1.0
                try: await self.tick(); pause=await self.pause()
                except asyncio.CancelledError: raise
                except Exception: log.exception('Webhook discovery failure; durable work retained')
                try: await asyncio.wait_for(wake.wait(),pause)
                except TimeoutError: pass
        finally: self.s.wake.forget(wake)

    async def pause(self):
        """Seconds until the next deadline a tick serves. New and finished work wake the loop; what
        frees up without a wake (a target conversation going idle) waits at most a second."""
        s=self.s; now=s.clock()
        row=await one(s.db.conn,"""SELECT MIN(t) t FROM (
                SELECT MIN(idle_deadline_ms) t FROM webhook_batches WHERE state='collecting' AND idle_deadline_ms>?
                UNION ALL SELECT MIN(max_deadline_ms) FROM webhook_batches WHERE state='collecting' AND max_deadline_ms>?
                UNION ALL SELECT MIN(expires_at_ms) FROM webhook_events WHERE terminal_at_ms IS NULL AND expires_at_ms>? AND route_state IN ('pre','eligible','batch','hold')
                UNION ALL SELECT MIN(deadline_ms) FROM webhook_waits WHERE state IN ('registered','collecting','sealed') AND deadline_ms>?
                UNION ALL SELECT MIN(last_claimed_at_ms+idle_ms) FROM webhook_waits WHERE state='collecting' AND last_claimed_at_ms+idle_ms>?
                UNION ALL SELECT MIN(first_claimed_at_ms+collect_max_ms) FROM webhook_waits WHERE state='collecting' AND first_claimed_at_ms+collect_max_ms>?
                UNION ALL SELECT MIN(next_attempt_at_ms) FROM webhook_stage_jobs WHERE state='pending' AND next_attempt_at_ms>?
                UNION ALL SELECT MIN(next_attempt_at_ms) FROM webhook_telemetry_operations WHERE state='pending' AND next_attempt_at_ms>?
                UNION ALL SELECT MIN(json_extract(model_blockers_json,'$.retry.untilMs')) FROM webhook_endpoints WHERE json_extract(model_blockers_json,'$.retry.untilMs')>?)""",(now,)*9)
        due=min(t for t in (row['t'] if row else None,self.last_prune+60000) if t is not None)
        return min(1.0,max(.01,(due-now)/1000))

    async def tick(self):
        s=self.s
        await routing.tick(s)
        for stage,capacity in [('pre',s.config.concurrency.pre_scripts),('post',s.config.concurrency.post_scripts)]:
            async with s.db.webhook_transaction() as conn:
                running=await one(conn,"SELECT COUNT(*) n FROM webhook_stage_jobs WHERE stage=? AND state='running'",(stage,))
                slots=max(0,capacity-running['n'])
                jobs=await many(conn,"SELECT * FROM webhook_stage_jobs WHERE stage=? AND state='pending' AND (next_attempt_at_ms IS NULL OR next_attempt_at_ms<=?) ORDER BY queued_at_ms,job_id",(stage,s.clock()))
            for job in jobs:
                if slots<=0: break
                if await self.launch(job): slots-=1
        from app.webhooks.telemetry import flush
        await flush(s)
        if s.bridge: await s.bridge.dispatch()
        from app.webhooks.notifications import tick as notifications_tick
        await notifications_tick(s)
        if s.clock()-self.last_prune>=60000:
            from app.webhooks.retention import prune
            await prune(s); self.last_prune=s.clock()

    async def launch(self,job):
        s=self.s
        async with s.db.webhook_transaction() as conn:
            job=await one(conn,"SELECT * FROM webhook_stage_jobs WHERE job_id=? AND state='pending'",(job['job_id'],))
            if not job: return False
            e=await s.endpoint_row(conn,job['endpoint_id'],deleted=True)
            if s.blockers(e): return False
            count=await one(conn,"SELECT COUNT(*) n FROM webhook_stage_jobs WHERE stage=? AND state='running'",(job['stage'],))
            if count['n']>=getattr(s.config.concurrency,job['stage']+'_scripts'): return False
            _,config=await s.revision(conn,job['endpoint_id'],job['revision']); script=getattr(config,job['stage'])
            if await s.target_blockers(conn,e,config): return False
            if job['stage']=='pre':
                event=await one(conn,'SELECT * FROM webhook_events WHERE event_id=?',(job['event_id'],))
                if event['terminal_at_ms'] is not None or await routing.expire(s,conn,event):
                    await conn.execute("UPDATE webhook_stage_jobs SET state='cancelled',terminal_at_ms=? WHERE job_id=?",(s.clock(),job['job_id'])); return False
                if config.processing.script_scheduling=='serial' and s.bridge and not await s.bridge.pre_available(conn,e,config): return False
                data=await routing.material(s,conn,event)
                if data is None:
                    await conn.execute("UPDATE webhook_stage_jobs SET state='held',row_version=row_version+1 WHERE job_id=?",(job['job_id'],))
                    return False
            if job['stage']=='post' and config.processing.result_schema is not None:
                from app.webhooks.templates import check_result
                from app.webhooks.contracts import WebhookError
                snapshot=json.loads(job['execution_input_json'])
                try:
                    for reported in snapshot.get('events',[]):
                        value=json.loads(reported['result_json']) if reported.get('result_json') else None
                        check_result(config.processing.result_schema,value)
                except WebhookError:
                    await conn.execute("UPDATE webhook_stage_jobs SET state='held',row_version=row_version+1 WHERE job_id=?",(job['job_id'],))
                    await conn.execute("UPDATE webhook_events SET review_required=1,terminal_reason='invalid_structured_result' WHERE event_id IN (SELECT event_id FROM webhook_assignment_events WHERE assignment_id=?)",(job['assignment_id'],))
                    return False
            limit=getattr(config.limits,job['stage']+'_concurrency')
            if limit:
                count=await one(conn,"SELECT COUNT(*) n FROM webhook_stage_jobs WHERE endpoint_id=? AND stage=? AND state='running'",(job['endpoint_id'],job['stage']))
                if count['n']>=limit: return False
            attempt=uid(); fence=job['execution_fence']+1; now=s.clock()
            await span(s,conn,job['stage']+'_queue',job['job_id']+':queue',start=job['queued_at_ms'],end=now,endpoint=job['endpoint_id'],event=job['event_id'],assignment=job['assignment_id'],level='event' if job['stage']=='pre' else 'assignment')
            await conn.execute("UPDATE webhook_stage_jobs SET state='running',execution_fence=?,row_version=row_version+1 WHERE job_id=?",(fence,job['job_id']))
            await insert(conn,'webhook_stage_attempts',attempt_id=attempt,job_id=job['job_id'],attempt_no=fence,execution_fence=fence,state='reserved',worker_token=attempt,lease_until_ms=now+int((script.timeout_seconds or s.config.scripts.default_timeout_seconds)*1000)+30000,reserved_at_ms=now)
            if job['stage']=='pre':
                payload={'protocol_version':1,'phase':'pre','endpoint_id':job['endpoint_id'],'event_id':job['event_id'],'attempt_id':attempt,'actionKey':job['action_key'],
                         'config_revision':job['revision'],'received_at':data['receivedAt'],'query':data['query'],'body':data['body'],'content':data['content']}
            else:
                payload={'protocol_version':1,'phase':'post','endpoint_id':job['endpoint_id'],'assignment_id':job['assignment_id'],'attempt_id':attempt,'actionKey':job['action_key'],'config_revision':job['revision'],'assignment':json.loads(job['execution_input_json'])}
        task=asyncio.create_task(self.perform(job,attempt,fence,script,payload),name='webhook-'+job['stage']+':'+attempt)
        self.tasks[job['job_id']]=task
        task.add_done_callback(lambda t,j=job['job_id']: self.tasks.pop(j,None))
        return True

    async def perform(self,job,attempt,fence,script,payload):
        s=self.s
        async def allowed(conn):
            endpoint=await s.endpoint_row(conn,job['endpoint_id'],deleted=True)
            _,config=await s.revision(conn,job['endpoint_id'],job['revision'])
            return not (s.blockers(endpoint) or await s.target_blockers(conn,endpoint,config))
        async def defer(conn):
            await conn.execute("UPDATE webhook_stage_jobs SET state='pending',row_version=row_version+1 WHERE job_id=? AND execution_fence=? AND state='running'",(job['job_id'],fence))
            await conn.execute("UPDATE webhook_stage_attempts SET state='cancelled',finished_at_ms=?,error_class='control_deferred',side_effect_state='none' WHERE attempt_id=?",(s.clock(),attempt))
        @contextlib.asynccontextmanager
        async def start_guard():
            async with s.db.webhook_transaction() as conn:
                permitted=await allowed(conn)
                if permitted: yield
                else: await defer(conn)
            if not permitted: raise StartDeferred()
        async def started(info):
            async with s.db.webhook_transaction() as conn:
                permitted=await allowed(conn)
                if not permitted: await defer(conn)
            if not permitted: raise StartDeferred()
            async with s.db.webhook_transaction() as conn:
                current=await one(conn,"SELECT * FROM webhook_stage_jobs WHERE job_id=? AND state='running' AND execution_fence=?",(job['job_id'],fence))
                if not current: raise asyncio.CancelledError()
                changed=await conn.execute("UPDATE webhook_stage_attempts SET state='running',started_at_ms=?,execution_json=? WHERE attempt_id=? AND state='reserved'",(s.clock(),dumps(info),attempt))
                if changed.rowcount!=1: raise asyncio.CancelledError()
                await span(s,conn,job['stage']+'_execute',attempt,start=s.clock(),endpoint=job['endpoint_id'],event=job['event_id'],assignment=job['assignment_id'],attempt=attempt,level='attempt')
        try:
            from app.webhooks.capabilities import environment
            result=await execute(script,s.config.scripts,payload,on_start=started,capability_env=environment(s,attempt,script.timeout_seconds or s.config.scripts.default_timeout_seconds),start_guard=start_guard)
        except StartDeferred:
            return
        except asyncio.CancelledError:
            await asyncio.shield(self.complete(job,attempt,fence,script,{'errorClass':'cancelled','effectState':'unknown','result':None}))
            raise
        except Exception as exc:
            result={'errorClass':'environment_error','errorSummary':str(exc),'effectState':'none','result':None}
        await self.complete(job,attempt,fence,script,result)

    async def complete(self,job,attempt,fence,script,result):
        s=self.s
        async with s.db.webhook_transaction() as conn:
            current=await one(conn,'SELECT * FROM webhook_stage_jobs WHERE job_id=?',(job['job_id'],))
            active=await one(conn,'SELECT * FROM webhook_stage_attempts WHERE attempt_id=?',(attempt,))
            if not current or not active or current['execution_fence']!=fence or current['state']!='running' or active['state'] not in ('reserved','running'):
                # Preserve the first late observation on its own old attempt,
                # never as permission to mutate the job or execute a next stage.
                if active and active['job_id']==job['job_id'] and active['execution_fence']==fence:
                    await conn.execute("UPDATE webhook_stage_attempts SET execution_json=json_insert(execution_json,'$.lateCompletion',json(?)) WHERE attempt_id=?",(dumps({'observedAtMs':s.clock(),'applied':False,'result':result}),attempt))
                return
            error=result.get('errorClass'); output=result.get('result'); now=s.clock()
            await span(s,conn,job['stage']+'_execute',attempt,start=active['started_at_ms'] or active['reserved_at_ms'],end=now,endpoint=job['endpoint_id'],event=job['event_id'],assignment=job['assignment_id'],attempt=attempt,level='attempt',reason=error or 'completed')
            await conn.execute('UPDATE webhook_stage_attempts SET state=?,finished_at_ms=?,exit_code=?,error_class=?,error_summary=?,side_effect_state=?,result_json=?,stderr_text=?,stderr_truncated=? WHERE attempt_id=?',
                               ('unknown' if error and result.get('effectState')=='unknown' else 'failed' if error else 'succeeded',now,result.get('exitCode'),error,result.get('errorSummary'),result.get('effectState') if result.get('effectState') in ('none','unknown') else 'unknown',dumps(output) if output else None,result.get('stderr',''),int(result.get('stderrTruncated',False)),attempt))
            state='succeeded'; next_at=None
            if error:
                state='unknown' if result.get('effectState')=='unknown' else 'failed'
                if error!='cancelled' and script.retry.idempotency_declaration and fence<script.retry.max_attempts:
                    state='pending'; intervals=script.retry.backoff_seconds; next_at=now+int((intervals[min(fence-1,len(intervals)-1)] if intervals else 0)*1000)
                await conn.execute('UPDATE webhook_stage_jobs SET state=?,next_attempt_at_ms=?,terminal_at_ms=?,row_version=row_version+1 WHERE job_id=?',(state,next_at,None if state=='pending' else now,job['job_id']))
                if job['stage']=='pre' and state!='pending':
                    if script.on_error=='continueModel' and error!='cancelled':
                        data={'errorClass':error,'sideEffectState':result.get('effectState','unknown'),'warning':'前置可能已产生副作用，不得假定未执行或重复动作'}
                        await conn.execute('UPDATE webhook_event_payloads SET pre_result_json=?,model_data_json=? WHERE event_id=?',(dumps(data),dumps(data),job['event_id']))
                        await conn.execute("UPDATE webhook_events SET route_state='eligible',pre_completed_at_ms=? WHERE event_id=? AND terminal_at_ms IS NULL",(now,job['event_id']))
                    else:
                        await conn.execute("UPDATE webhook_events SET route_state=CASE WHEN terminal_at_ms IS NULL THEN 'hold' ELSE route_state END,review_required=1,terminal_reason=COALESCE(terminal_reason,?) WHERE event_id=?",(error,job['event_id']))
            else:
                from app.webhooks.telemetry import enqueue_script
                await enqueue_script(s,conn,job,attempt,output)
                await conn.execute("UPDATE webhook_stage_jobs SET state='succeeded',terminal_at_ms=?,row_version=row_version+1 WHERE job_id=?",(now,job['job_id']))
                if job['stage']=='post':
                    for member in await many(conn,'SELECT * FROM webhook_assignment_events WHERE assignment_id=?',(job['assignment_id'],)):
                        latest=await one(conn,'SELECT review_required FROM webhook_receipts WHERE assignment_event_id=? ORDER BY result_version DESC LIMIT 1',(member['assignment_event_id'],))
                        unresolved=await one(conn,"SELECT 1 FROM webhook_stage_jobs WHERE state IN ('unknown','held') AND (event_id=? OR assignment_id=?)",(member['event_id'],job['assignment_id']))
                        await conn.execute('UPDATE webhook_events SET review_required=? WHERE event_id=?',(int(bool(unresolved or latest and latest['review_required'])),member['event_id']))
                if job['stage']=='pre':
                    await conn.execute('UPDATE webhook_event_payloads SET pre_result_json=?,model_data_json=? WHERE event_id=?',(dumps(output),dumps(output.get('model_data')) if 'model_data' in output else None,job['event_id']))
                    event=await one(conn,'SELECT * FROM webhook_events WHERE event_id=?',(job['event_id'],))
                    if output['decision']=='skip_model':
                        outcome='completed' if output['outcome']=='handled' else 'skipped'
                        await receipt(s,conn,job['event_id'],source='script',request_key='script:'+attempt,outcome=outcome,attempt=attempt,result=output.get('result'),reason=output.get('reason'))
                        await conn.execute("UPDATE webhook_events SET route_state='terminal',terminal_at_ms=COALESCE(terminal_at_ms,?),pre_completed_at_ms=?,terminal_reason=COALESCE(terminal_reason,?) WHERE event_id=?",(now,now,outcome,job['event_id']))
                    else:
                        await conn.execute("UPDATE webhook_events SET route_state='eligible',pre_completed_at_ms=? WHERE event_id=? AND terminal_at_ms IS NULL",(now,job['event_id']))
        s.wake.set()

    async def stop(self,eid,scope):
        if scope=='current': return {'stopped':[],'unconfirmed':[]}
        jobs=await many(self.s.db.conn,"SELECT job_id FROM webhook_stage_jobs WHERE endpoint_id=? AND state='running'",(eid,))
        tasks=[self.tasks[j['job_id']] for j in jobs if j['job_id'] in self.tasks]
        for t in tasks: t.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        return {'stopped':[j['job_id'] for j in jobs],'unconfirmed':[j['job_id'] for j in jobs]}
