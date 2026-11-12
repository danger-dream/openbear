"""Per-event declarations and one bounded, read-only receipt repair."""
from __future__ import annotations

import json

from app.webhooks.contracts import WebhookError, digest, dumps
from app.webhooks.repository import insert, many, one, uid

# A run that failed before any tool ran changed nothing outside: its events go back for another
# run after these pauses of the endpoint (seconds), then fail plainly. No review either way.
UNSTARTED_RETRY_S = (60, 300, 900)


async def receipt(s,conn,event_id,*,source,request_key,outcome=None,disposition=None,member=None,attempt=None,run_id=None,result=None,summary='',reason=None,evidence=None,expected_version=None):
    content=dict(eventId=event_id,source=source,outcome=outcome,disposition=disposition,result=result,summary=summary,reason=reason,evidence=evidence or [])
    fp=digest(content)
    old=await one(conn,'SELECT * FROM webhook_receipts WHERE request_key=?',(request_key,))
    if old:
        if old['request_sha256']!=fp: raise WebhookError('receipt_conflict',status=409)
        return old['receipt_id']
    latest=await one(conn,'SELECT MAX(result_version) v FROM webhook_receipts WHERE event_id=?',(event_id,))
    version=latest['v'] or 0
    if source=='model' and version and expected_version!=version: raise WebhookError('receipt_version_conflict',status=409,currentRevision=version)
    rid=uid()
    await insert(conn,'webhook_receipts',receipt_id=rid,event_id=event_id,result_version=version+1,assignment_event_id=member,stage_attempt_id=attempt,source=source,runtime_run_id=run_id,
                 request_key=request_key,request_sha256=fp,outcome=outcome,disposition=disposition,review_required=int(outcome=='unknown' or disposition=='protocol_incomplete'),summary=summary,reason=reason,
                 evidence_refs_json=dumps(evidence or []),result_json=dumps(result) if result is not None else None,reported_at_ms=s.clock())
    return rid


async def report(s,assignment_id,run_id,params):
    if not isinstance(params.get('receiptId'),str) or not params['receiptId'] or not isinstance(params.get('results'),list) or not params['results'] or any(not isinstance(r,dict) or not isinstance(r.get('eventId'),str) for r in params['results']): raise WebhookError('invalid_report')
    async with s.db.webhook_transaction() as conn:
        a=await one(conn,'SELECT * FROM webhook_assignments WHERE assignment_id=?',(assignment_id,))
        link=await one(conn,'SELECT * FROM webhook_runtime_links WHERE run_id=? AND assignment_id=?',(run_id,assignment_id))
        run=await one(conn,'SELECT status FROM runtime_runs WHERE run_id=?',(run_id,))
        if not a or not link or not run: raise WebhookError('report_not_owned',status=403)
        fingerprint=digest(params)
        previous=await one(conn,'SELECT * FROM webhook_report_requests WHERE assignment_id=? AND request_id=?',(assignment_id,params['receiptId']))
        if previous:
            if previous['request_sha256']!=fingerprint: raise WebhookError('receipt_conflict',status=409)
            return json.loads(previous['result_json'])
        if a['state']=='finalized' or a['recovery_state']!='healthy' or run['status'] not in ('running','waiting'):
            raise WebhookError('report_not_owned',status=403)
        if len({r.get('eventId') for r in params['results']})!=len(params['results']): raise WebhookError('duplicate_report_event')
        ids=[]
        for result in params['results']:
            eid=result.get('eventId'); outcome=result.get('outcome')
            if outcome not in ('completed','skipped','failed','unknown'): raise WebhookError('invalid_outcome')
            m=await one(conn,'SELECT * FROM webhook_assignment_events WHERE assignment_id=? AND event_id=? AND released_at_ms IS NULL',(assignment_id,eid))
            if not m or m['delivered_at_ms'] is None: raise WebhookError('event_not_delivered_or_owned',status=403)
            evidence=result.get('evidenceRefs',[])
            for ref in evidence:
                action_id=ref.removeprefix('tool-action:').removeprefix('runtime-action:') if isinstance(ref,str) else ''
                action=await one(conn,'SELECT a.* FROM runtime_actions a JOIN webhook_runtime_links l ON a.run_id=l.run_id WHERE a.action_id=? AND l.assignment_id=?',(action_id,assignment_id))
                if not action or ref.startswith('tool-action:') and action['kind']!='tool' or action['status']=='started':
                    raise WebhookError('invalid_evidence_reference',details={'reference':ref})
            ids.append(await receipt(s,conn,eid,source='model',request_key=f"model:{assignment_id}:{params['receiptId']}:{eid}",outcome=outcome,
                                     member=m['assignment_event_id'],run_id=run_id,result=result.get('result'),summary=result.get('summary',''),reason=result.get('reason'),evidence=evidence,expected_version=params.get('expectedReceiptVersion')))
        response={'receiptIds':ids,'businessDeclarationSource':'model','chainFinalized':False}
        await insert(conn,'webhook_report_requests',assignment_id=assignment_id,request_id=params['receiptId'],request_sha256=fingerprint,result_json=dumps(response),created_at_ms=s.clock())
    return response


async def dispose_member(s,conn,m,disposition,reason=None):
    if await one(conn,'SELECT receipt_id FROM webhook_receipts WHERE assignment_event_id=?',(m['assignment_event_id'],)): return
    await receipt(s,conn,m['event_id'],source='framework',request_key='disposition:'+m['assignment_event_id'],disposition=disposition,
                  outcome='unknown' if m['delivered_at_ms'] is not None and disposition in ('cancelled','protocol_incomplete') else None,
                  member=m['assignment_event_id'],reason=reason or disposition)


async def candidate(s,assignment_id,*,abnormal=None,final_text='',finalize=True,seal=False):
    """Prepare membership separately from the authoritative runtime terminal.

    Runtime passes finalize=False until its run has ended. Recovery callers may
    finalize directly only after establishing the runner is no longer active.
    """
    async with s.db.webhook_transaction() as conn:
        a=await one(conn,'SELECT * FROM webhook_assignments WHERE assignment_id=?',(assignment_id,))
        if not a or a['state']=='finalized': return {'action':'close'}
        members=await many(conn,'SELECT * FROM webhook_assignment_events WHERE assignment_id=? ORDER BY claimed_at_ms,event_id',(assignment_id,))
        waits=await many(conn,"SELECT * FROM webhook_waits WHERE assignment_id=? AND state IN ('registered','collecting','sealed')",(assignment_id,))
        if not abnormal and a['state']=='open':
            pending=[m for m in members if m['delivered_at_ms'] is None and not await one(conn,'SELECT receipt_id FROM webhook_receipts WHERE assignment_event_id=?',(m['assignment_event_id'],))]
            if pending or waits: return {'action':'wait','waitIds':[w['wait_id'] for w in waits],'pendingEventIds':[m['event_id'] for m in pending]}
        missing=[]
        for m in members:
            r=await one(conn,'SELECT receipt_id FROM webhook_receipts WHERE assignment_event_id=?',(m['assignment_event_id'],))
            if not r: missing.append(m)
        if finalize or seal or missing:
            await conn.execute("UPDATE webhook_assignments SET state='closing',closing_at_ms=COALESCE(closing_at_ms,?),row_version=row_version+1 WHERE assignment_id=? AND state='open'",(s.clock(),assignment_id))
            await conn.execute("UPDATE webhook_waits SET state='closed',closed_at_ms=?,close_reason=?,row_version=row_version+1 WHERE assignment_id=? AND state IN ('registered','collecting','sealed')",(s.clock(),abnormal or 'closing',assignment_id))
        if missing and not abnormal and not a['repair_count']:
            await conn.execute('UPDATE webhook_assignments SET repair_count=1,repair_command_id=?,repair_deadline_ms=? WHERE assignment_id=?',('receipt-repair:'+assignment_id,s.clock()+60000,assignment_id))
            return {'action':'repair','eventIds':[m['event_id'] for m in missing]}
        if not finalize:
            return {'action':'close','outcome':'protocol_incomplete' if missing else 'ready'}
        retried,backoff=set(),0
        if abnormal=='failed' and a['origin_kind']=='webhook' and not await one(conn,"SELECT 1 FROM runtime_actions x JOIN webhook_runtime_links l USING(run_id) WHERE l.assignment_id=? AND x.kind!='model' LIMIT 1",(assignment_id,)):
            for m in missing:
                tried=(await one(conn,'SELECT COUNT(*) n FROM webhook_assignment_events WHERE event_id=? AND released_at_ms IS NOT NULL',(m['event_id'],)))['n']
                await receipt(s,conn,m['event_id'],source='framework',request_key='disposition:'+m['assignment_event_id'],outcome='failed',member=m['assignment_event_id'],reason='failed')
                if tried<len(UNSTARTED_RETRY_S):
                    retried.add(m['assignment_event_id']); backoff=max(backoff,UNSTARTED_RETRY_S[tried])
            missing=[]
            if len(retried)==len(members):
                abnormal='retried'
        for m in missing:
            disposition='cancelled' if abnormal=='cancelled' else 'not_delivered' if m['delivered_at_ms'] is None else 'protocol_incomplete'
            await dispose_member(s,conn,m,disposition,abnormal)
        results=await many(conn,'SELECT r.* FROM webhook_receipts r JOIN webhook_assignment_events m ON r.assignment_event_id=m.assignment_event_id WHERE m.assignment_id=? AND r.result_version=(SELECT MAX(r2.result_version) FROM webhook_receipts r2 WHERE r2.assignment_event_id=m.assignment_event_id)',(assignment_id,))
        if missing and not abnormal: abnormal='protocol_incomplete'
        outcome=abnormal or ('normal' if all(r['outcome'] in ('completed','skipped') for r in results) else 'partialFailure' if any(r['outcome']=='completed' for r in results) else 'failed')
        from app.webhooks.queries import usage
        snapshot={'usage':await usage(s,conn,assignment_id=assignment_id),'conversationId':a['conversation_uuid'],'rootTurnId':a['root_turn_uuid'],'materials':[await s.event_material(conn,m['event_id']) for m in members], 'assignmentId':assignment_id,'initialBatchId':a['initial_batch_id'],'events':results,'finalText':final_text,'outcome':outcome,
                  'runs':await many(conn,'SELECT * FROM webhook_runtime_links WHERE assignment_id=?',(assignment_id,))}
        await conn.execute("UPDATE webhook_assignments SET state='finalized',finalized_at_ms=?,terminal_reason=?,final_snapshot_json=?,row_version=row_version+1 WHERE assignment_id=?",(s.clock(),outcome,dumps(snapshot),assignment_id))
        owner=(await s.endpoint_row(conn,a['origin_endpoint_id'],deleted=True))['owner_chat_id'] if retried else None
        for m in members:
            if m['assignment_event_id'] in retried:
                # Released only now: membership ends with a finalized assignment.
                op=uid()
                await insert(conn,'webhook_control_operations',operation_id=op,owner_chat_id=owner,request_id='auto-retry:'+m['assignment_event_id'],endpoint_id=a['origin_endpoint_id'],
                             action='retry',request_sha256=digest({'assignmentEventId':m['assignment_event_id']}),actor_ref='framework',
                             reason=f'run failed before any tool ran; retried after {backoff}s',selection_json=dumps({'eventId':m['event_id']}),created_at_ms=s.clock())
                await conn.execute('UPDATE webhook_assignment_events SET released_at_ms=?,release_operation_id=? WHERE assignment_event_id=?',(s.clock(),op,m['assignment_event_id']))
                await conn.execute("UPDATE webhook_events SET route_state='eligible',route_version=route_version+1 WHERE event_id=?",(m['event_id'],))
                continue
            await conn.execute("UPDATE webhook_events SET route_state='terminal',terminal_at_ms=COALESCE(terminal_at_ms,?),terminal_reason=COALESCE(terminal_reason,?),review_required=MAX(review_required,?) WHERE event_id=?",(s.clock(),outcome,int(outcome=='protocol_incomplete' or any(r['event_id']==m['event_id'] and r['review_required'] for r in results)),m['event_id']))
        if a['origin_kind']=='webhook':
            _,c=await s.revision(conn,a['origin_endpoint_id'],a['origin_revision'])
            e=await s.endpoint_row(conn,a['origin_endpoint_id'],deleted=True)
            review=outcome in ('protocol_incomplete','interrupted') or any(r['review_required'] for r in results)
            blockers=json.loads(e['model_blockers_json'])
            if review:
                blockers['review:'+assignment_id]={'source':'review','assignmentId':assignment_id,'setAtMs':s.clock()}
            if retried:
                # A backoff, not a hold: it ends by itself (WebhookService.model_blockers).
                blockers['retry']={'source':'retry','untilMs':max(s.clock()+backoff*1000,(blockers.get('retry') or {}).get('untilMs') or 0)}
            if review or retried:
                await conn.execute('UPDATE webhook_endpoints SET model_blockers_json=?,control_version=control_version+? WHERE endpoint_id=?',(dumps(blockers),int(review),a['origin_endpoint_id']))
            if c.post.enabled and outcome in c.post.on_outcomes and e['deleted_at_ms'] is None:
                # Queueing is not launching; current controls are rechecked by the worker.
                await insert(conn,'webhook_stage_jobs',job_id=uid(),stage='post',assignment_id=assignment_id,endpoint_id=a['origin_endpoint_id'],revision=a['origin_revision'],
                             action_key='post:'+assignment_id,queued_at_ms=s.clock(),execution_input_json=dumps(snapshot),idempotency_policy_json=dumps(c.post.retry.wire()))
    s.wake.set()
    return {'action':'close','outcome':outcome}
