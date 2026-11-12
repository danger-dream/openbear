"""Versioned controls; explicit confirmation binds exact parameters and impact."""
from __future__ import annotations

import json
import secrets

from app.tools.confirmation import argument_digest, constraints_within
from app.webhooks.contracts import Scope, WebhookError, digest, dumps, iso, parse_config
from app.webhooks.repository import many, one
from app.webhooks.waits import cancel_endpoint_waits


async def impact(s,conn,eid):
    count=await one(conn,'SELECT COUNT(*) n FROM webhook_events WHERE endpoint_id=? AND (terminal_at_ms IS NULL OR review_required=1)',(eid,))
    waits=await many(conn,"SELECT wait_id FROM webhook_waits WHERE endpoint_id=? AND state IN ('registered','collecting','sealed') ORDER BY wait_id",(eid,))
    jobs=await many(conn,"SELECT job_id,stage,event_id,assignment_id FROM webhook_stage_jobs WHERE endpoint_id=? AND state='running' ORDER BY job_id",(eid,))
    assignments=await many(conn,"SELECT assignment_id,conversation_uuid FROM webhook_assignments WHERE origin_endpoint_id=? AND state!='finalized' ORDER BY assignment_id",(eid,))
    return dict(pendingEvents=count['n'],activeWaits=len(waits),runningStages=jobs+assignments,waitIds=[w['wait_id'] for w in waits],stopped=[],unconfirmed=[])


async def discard_backlog(conn,eid=None):
    """What a deleted endpoint (eid, or every deleted one) still held for dispatch: its batches,
    whose events are already terminal, and the review holds on those events."""
    deleted='SELECT endpoint_id FROM webhook_endpoints WHERE deleted_at_ms IS NOT NULL AND (? IS NULL OR endpoint_id=?)'
    await conn.execute(f"UPDATE webhook_batches SET state='empty' WHERE state IN ('collecting','sealed') AND endpoint_id IN ({deleted})",(eid,eid))
    await conn.execute(f'UPDATE webhook_events SET review_required=0 WHERE review_required=1 AND terminal_at_ms IS NOT NULL AND endpoint_id IN ({deleted})',(eid,eid))
    for e in await many(conn,f"SELECT endpoint_id,model_blockers_json FROM webhook_endpoints WHERE endpoint_id IN ({deleted}) AND model_blockers_json LIKE '%review:%'",(eid,eid)):
        blockers={k:v for k,v in json.loads(e['model_blockers_json']).items() if not k.startswith('review:')}
        await conn.execute('UPDATE webhook_endpoints SET model_blockers_json=? WHERE endpoint_id=?',(dumps(blockers),e['endpoint_id']))


async def tool_constraints(conn, eid, info, action, request):
    """Known Webhook effects, never counts or guessed fields from an external tool."""
    events = await many(conn, 'SELECT event_id FROM webhook_events WHERE endpoint_id=? AND (terminal_at_ms IS NULL OR review_required=1)', (eid,))
    queued = await many(conn, "SELECT job_id FROM webhook_stage_jobs WHERE endpoint_id=? AND state IN ('pending','held')", (eid,))
    stops = []
    if action == 'delete' and request.get('stopRunning', False):
        stops = ['model'] + (['scripts'] if request.get('scope', 'all') == 'all' else [])
    return {'sets': {'events': [row['event_id'] for row in events],
                     'queuedJobs': [row['job_id'] for row in queued],
                     'waits': info['waitIds'],
                     'running': [dumps(row) for row in info['runningStages']],
                     'stops': stops}}


async def prepare(s,owner,eid,action,request,*,tool_approval=False):
    if action not in ('delete','rotate_key'): raise WebhookError('invalid_confirmation_action')
    if tool_approval and action == 'delete':
        if type(request.get('stopRunning', False)) is not bool: raise WebhookError('invalid_stop_running')
        if request.get('scope', 'all') not in ('all', 'current'): raise WebhookError('invalid_stop_scope')
    async with s.db.webhook_transaction() as conn:
        e=await s.endpoint_row(conn,eid,owner)
        if request.get('expectedControlRevision',e['control_version'])!=e['control_version']: raise WebhookError('control_conflict',status=409,currentRevision=e['control_version'])
        info=await impact(s,conn,eid)
        token=secrets.token_urlsafe(32); expiry=s.clock()+300000
        s.confirmations={k:v for k,v in s.confirmations.items() if v['expiry']>s.clock()}
        if len(s.confirmations)>1000: raise WebhookError('too_many_confirmations',status=429)
        grant={'owner':owner,'endpoint':eid,'action':action,'revision':e['current_revision'],'control':e['control_version'],'params':{k:v for k,v in request.items() if k!='prepare'},'impact':info,'expiry':expiry}
        result={'impact':info,'confirmationToken':token,'expiresAt':iso(expiry)}
        if tool_approval:
            # Only deleting may narrow stops. Rotation grace and all other
            # business parameters stay exact; no safety ordering is assumed.
            excluded = {'requestId', 'expectedControlRevision'} | ({'stopRunning', 'scope'} if action == 'delete' else set())
            grant['approvalConstraints'] = await tool_constraints(conn, eid, info, action, request)
            result.update(approvalScope={'contract': 'webhook-control-v1', 'endpointId': eid, 'action': action,
                'revision': e['current_revision'], 'controlRevision': e['control_version'],
                'parameters': argument_digest({k: v for k, v in grant['params'].items() if k not in excluded})},
                approvalConstraints=grant['approvalConstraints'])
        s.confirmations[digest(token)]=grant
    return result


async def confirmed(s,conn,owner,eid,action,submitted):
    grant=s.confirmations.get(digest(submitted.get('confirmationToken')))
    e=await s.endpoint_row(conn,eid,owner)
    if not grant or grant['owner']!=owner or grant['endpoint']!=eid or grant['action']!=action or grant['expiry']<=s.clock(): raise WebhookError('confirmation_required',status=403)
    actual=await impact(s,conn,eid)
    expected=grant['impact']
    within = (constraints_within(grant['approvalConstraints'], await tool_constraints(conn, eid, actual, action, grant['params']))
              if 'approvalConstraints' in grant else actual == expected)
    if e['current_revision']!=grant['revision'] or e['control_version']!=grant['control'] or not within:
        raise WebhookError('confirmation_stale',status=409,details={'impact':actual})
    return {**grant['params'],'requestId':submitted['requestId'],'expectedControlRevision':grant['control']}


async def control(s,owner,eid,submitted,*,action=None):
    action=action or submitted.get('action')
    if action not in ('set_enabled','pause','resume','stop','delete','rotate_key'): raise WebhookError('invalid_control')
    credential=None; token_key=None
    # The management ledger supports one exact replay; never stores Bearer credentials.
    audit_request={**submitted}
    if 'confirmationToken' in audit_request: audit_request['confirmationToken']=digest(audit_request['confirmationToken'])
    ledger_action=action
    async with s.db.webhook_transaction() as conn:
        old,fp=await s.operation(conn,owner,audit_request,ledger_action,eid)
        if old: return json.loads(old['result_json'])
        request=submitted
        if action in ('delete','rotate_key'):
            request=await confirmed(s,conn,owner,eid,action,submitted); token_key=digest(submitted['confirmationToken'])
        e=await s.endpoint_row(conn,eid,owner)
        if request.get('expectedControlRevision')!=e['control_version']: raise WebhookError('control_conflict',status=409,currentRevision=e['control_version'])
        info=await impact(s,conn,eid); now=s.clock()
        if action=='set_enabled':
            if type(request.get('enabled')) is not bool: raise WebhookError('enabled_must_be_boolean')
            _,c=await s.revision(conn,eid,e['current_revision']); scope=Scope(type=e['binding_kind'],id=e['binding_uuid'])
            parse_config(c.wire(),scope,s.config,enabled=request['enabled']); await s.validate_target(conn,owner,scope,c,enabled=request['enabled'])
            await conn.execute('UPDATE webhook_endpoints SET enabled=? WHERE endpoint_id=?',(int(request['enabled']),eid))
        elif action in ('pause','resume'):
            if request.get('scope')=='model':
                blockers=json.loads(e['model_blockers_json'])
                if action=='pause': blockers['manual:model']={'source':'manual','setAtMs':now}
                else:
                    blockers.pop('manual:model',None)
                    blockers={k:v for k,v in blockers.items() if v.get('source') not in ('stop','retry')}
                await conn.execute('UPDATE webhook_endpoints SET model_blockers_json=? WHERE endpoint_id=?',(dumps(blockers),eid))
            else:
                await conn.execute('UPDATE webhook_endpoints SET paused=?,pause_reason=? WHERE endpoint_id=?',(int(action=='pause'),'manual' if action=='pause' else None,eid))
                if action=='resume':
                    blockers={k:v for k,v in json.loads(e['dispatch_blockers_json']).items() if v.get('source')!='stop'}
                    await conn.execute('UPDATE webhook_endpoints SET dispatch_blockers_json=? WHERE endpoint_id=?',(dumps(blockers),eid))
        elif action=='rotate_key':
            grace=request.get('graceSeconds',0)
            if type(grace) is not int or not 0<=grace<=3600: raise WebhookError('invalid_grace')
            if grace: await conn.execute('UPDATE webhook_credentials SET expires_at_ms=MIN(COALESCE(expires_at_ms,?),?) WHERE endpoint_id=? AND revoked_at_ms IS NULL AND (expires_at_ms IS NULL OR expires_at_ms>?)',(now+grace*1000,now+grace*1000,eid,now))
            else: await conn.execute('UPDATE webhook_credentials SET revoked_at_ms=? WHERE endpoint_id=? AND revoked_at_ms IS NULL',(now,eid))
            credential=await s.issue(conn,eid)
        elif action in ('stop','delete'):
            scope=request.get('scope','all')
            if scope not in ('all','current'): raise WebhookError('invalid_stop_scope')
            if action=='delete':
                await conn.execute('UPDATE webhook_endpoints SET deleted_at_ms=?,enabled=0 WHERE endpoint_id=?',(now,eid))
                await conn.execute('UPDATE webhook_credentials SET revoked_at_ms=? WHERE endpoint_id=? AND revoked_at_ms IS NULL',(now,eid))
                await conn.execute("UPDATE webhook_stage_jobs SET state='cancelled',terminal_at_ms=? WHERE endpoint_id=? AND state IN ('pending','held')",(now,eid))
                await conn.execute("UPDATE webhook_batch_members SET invalidated_at_ms=?,invalidated_reason='deleted' WHERE invalidated_at_ms IS NULL AND event_id IN (SELECT event_id FROM webhook_events WHERE endpoint_id=?)",(now,eid))
                await conn.execute("UPDATE webhook_events SET route_state='terminal',terminal_at_ms=?,terminal_reason='endpoint_deleted' WHERE endpoint_id=? AND route_state IN ('pre','eligible','batch','hold','match_conflict')",(now,eid))
                await discard_backlog(conn,eid)
            else:
                column='model_blockers_json' if scope=='current' else 'dispatch_blockers_json'; blockers=json.loads(e[column])
                conversations=[r['conversation_uuid'] for r in info['runningStages'] if 'conversation_uuid' in r]
                if scope=='current':
                    _,frozen=await s.revision(conn,eid,e['current_revision'])
                    if frozen.target.conversation_id: conversations.append(frozen.target.conversation_id)
                blockers['stop:'+request['requestId']]={'source':'stop','scope':scope,'conversationIds':sorted(set(conversations)) if scope=='current' else [],'setAtMs':now}
                await conn.execute(f'UPDATE webhook_endpoints SET {column}=? WHERE endpoint_id=?',(dumps(blockers),eid))
            if action=='stop' and scope=='current':
                from app.webhooks.waits import close_wait
                for w in await many(conn,"SELECT w.* FROM webhook_waits w JOIN webhook_assignments a USING(assignment_id) WHERE a.origin_endpoint_id=? AND a.state!='finalized' AND w.state IN ('registered','collecting','sealed')",(eid,)):
                    await close_wait(s,conn,w,action)
            else: await cancel_endpoint_waits(s,conn,eid,reason=action)
        await conn.execute('UPDATE webhook_endpoints SET control_version=control_version+1,updated_at_ms=? WHERE endpoint_id=?',(now,eid))
        result={'endpoint':await s.public(conn,eid,owner),'impact':info}
        if action=='rotate_key': result['previousValidUntil']=iso(now+request.get('graceSeconds',0)*1000)
        await s.record_operation(conn,owner,audit_request,ledger_action,eid,fp,result)
    if token_key: s.confirmations.pop(token_key,None)
    if action=='stop' or action=='delete' and request.get('stopRunning'):
        if s.worker: result['impact'].update(await s.worker.stop(eid,request.get('scope','all')))
        if s.bridge: result['impact'].update(await s.bridge.stop(eid))
        async with s.db.webhook_transaction() as conn:
            await conn.execute('UPDATE webhook_control_operations SET result_json=? WHERE owner_chat_id=? AND request_id=?',(dumps(result),owner,submitted['requestId']))
    s.wake.set(); return {**result,**({'credential':credential} if credential else {})}
