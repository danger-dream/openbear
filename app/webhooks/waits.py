"""Durable subscriptions, separate claims and delivery, event-driven awaiting."""
from __future__ import annotations

import asyncio
import json

from app.webhooks.contracts import Batching, WebhookError, digest, dumps, validate_match
from app.webhooks.receipts import dispose_member
from app.webhooks.repository import insert, many, one, uid
from app.webhooks.routing import expire, route


async def register(s,assignment_id,owner,params):
    predicate=validate_match(params.get('match')); eid=params.get('endpointId'); rid=params.get('requestId')
    if not isinstance(rid,str) or not rid: raise WebhookError('request_id_required')
    timeout=params.get('timeoutSeconds')
    if timeout is not None and (type(timeout) not in (int,float) or timeout<=0): raise WebhookError('invalid_wait_timeout')
    collection=Batching.model_validate(params['collection']) if params.get('collection') else None
    if collection and (collection.max_events>s.config.batching.max_batch_events or collection.max_bytes>s.config.batching.max_batch_bytes): raise WebhookError('system_limit_exceeded')
    async with s.db.webhook_transaction() as conn:
        await s.endpoint_row(conn,eid,owner)
        a=await one(conn,"SELECT * FROM webhook_assignments WHERE assignment_id=? AND state='open'",(assignment_id,))
        if not a: raise WebhookError('assignment_not_open',status=409)
        old=await one(conn,'SELECT * FROM webhook_waits WHERE assignment_id=? AND register_request_id=?',(assignment_id,rid))
        fp=digest(params)
        if old:
            if json.loads(old['scope_snapshot_json']).get('requestHash')!=fp: raise WebhookError('wait_request_conflict',status=409)
            return public(old)
        after=params.get('afterSeq',a['task_start_cursor'])
        high=(await one(conn,'SELECT COALESCE(MAX(receive_seq),0) n FROM webhook_events'))['n']
        if type(after) is not int or after<0 or after>high: raise WebhookError('invalid_receive_cursor')
        conflict=await one(conn,"SELECT * FROM webhook_waits WHERE endpoint_id=? AND predicate_sha256=? AND state IN ('registered','collecting')",(eid,digest(predicate)))
        if conflict:
            if conflict['assignment_id']==assignment_id: return public(conflict)
            raise WebhookError('wait_conflict',status=409,details={'waitId':conflict['wait_id']})
        wid=uid(); now=s.clock()
        await insert(conn,'webhook_waits',wait_id=wid,assignment_id=assignment_id,endpoint_id=eid,register_request_id=rid,predicate_json=dumps(predicate),predicate_sha256=digest(predicate),
                     scope_snapshot_json=dumps({'owner':owner,'requestHash':fp}),after_receive_seq=after,scan_through_seq=high,mode='collect' if collection else 'single',
                     max_events=collection.max_events if collection else 1,max_bytes=collection.max_bytes if collection else s.config.batching.max_batch_bytes,
                     idle_ms=int(collection.idle_seconds*1000) if collection and collection.idle_seconds else None,collect_max_ms=int(collection.max_wait_seconds*1000) if collection and collection.max_wait_seconds else None,
                     registered_at_ms=now,deadline_ms=now+int(timeout*1000) if timeout is not None else None)
        for e in await many(conn,"SELECT * FROM webhook_events WHERE endpoint_id=? AND receive_seq>? AND receive_seq<=? AND route_state IN ('eligible','batch') ORDER BY receive_seq",(eid,after,high)):
            await route(s,conn,e,only_wait=True)
        result=public(await one(conn,'SELECT * FROM webhook_waits WHERE wait_id=?',(wid,)))
    s.wake.set(); return result


def public(w):
    return dict(waitId=w['wait_id'],assignmentId=w['assignment_id'],endpointId=w['endpoint_id'],match=json.loads(w['predicate_json']),afterSeq=w['after_receive_seq'],deadline=w['deadline_ms'],state=w['state'],version=w['row_version'],reason=w['close_reason'])


async def close_wait(s,conn,w,reason='cancelled'):
    if w['state'] in ('delivered','cancelled','timed_out','closed'): return
    state='timed_out' if reason=='timeout' else 'cancelled'
    await conn.execute('UPDATE webhook_waits SET state=?,closed_at_ms=?,close_reason=?,row_version=row_version+1 WHERE wait_id=?',(state,s.clock(),reason,w['wait_id']))
    for m in await many(conn,'SELECT * FROM webhook_assignment_events WHERE wait_id=? AND delivered_at_ms IS NULL',(w['wait_id'],)):
        await dispose_member(s,conn,m,'not_delivered' if reason=='timeout' else 'cancelled',reason)
    await conn.execute("UPDATE webhook_wait_candidates SET resolved_at_ms=?,resolution='cancelled' WHERE wait_id=? AND resolved_at_ms IS NULL",(s.clock(),w['wait_id']))


async def cancel_endpoint_waits(s,conn,eid,reason):
    for w in await many(conn,"SELECT * FROM webhook_waits WHERE endpoint_id=? AND state IN ('registered','collecting','sealed')",(eid,)):
        await close_wait(s,conn,w,reason)


async def cancel(s,assignment_id,wait_id):
    async with s.db.webhook_transaction() as conn:
        w=await one(conn,'SELECT * FROM webhook_waits WHERE wait_id=? AND assignment_id=?',(wait_id,assignment_id))
        if not w: raise WebhookError('wait_not_owned',status=403)
        await close_wait(s,conn,w)
    s.wake.set(); return {'waitId':wait_id,'state':'cancelled'}


async def delivery(s,assignment_id,wait_id,*,reserve_execution=False):
    async with s.db.webhook_transaction() as conn:
        w=await one(conn,'SELECT * FROM webhook_waits WHERE wait_id=? AND assignment_id=?',(wait_id,assignment_id))
        if not w: raise WebhookError('wait_not_owned',status=403)
        if w['deadline_ms'] is not None and w['deadline_ms']<=s.clock() and w['state'] not in ('delivered','cancelled','timed_out','closed'):
            await close_wait(s,conn,w,'timeout')
            w=await one(conn,'SELECT * FROM webhook_waits WHERE wait_id=?',(wait_id,))
        # Status/replay callers may inspect a terminal wait while dispatch is
        # paused. Only an actual automatic continuation must reacquire controls.
        if not reserve_execution:
            if w['delivery_json']: return json.loads(w['delivery_json'])
            if w['state'] in ('cancelled','timed_out','closed'): return {'waitId':wait_id,'state':w['state'],'events':[]}
        await conn.execute('UPDATE webhook_waits SET await_at_ms=COALESCE(await_at_ms,?) WHERE wait_id=?',(s.clock(),wait_id))
        e=await s.endpoint_row(conn,w['endpoint_id'],deleted=True)
        a=await one(conn,'SELECT * FROM webhook_assignments WHERE assignment_id=?',(assignment_id,))
        _,config=await s.revision(conn,e['endpoint_id'],e['current_revision'])
        # A callback endpoint cannot bypass the original task's immediate
        # controls, including when an already-owned wait reaches its deadline.
        if s.blockers(e,model=True) or a['recovery_state']!='healthy' or await s.target_blockers(conn,e,config): return None
        if a['origin_endpoint_id']:
            origin=await s.endpoint_row(conn,a['origin_endpoint_id'],deleted=True)
            _,original=await s.revision(conn,origin['endpoint_id'],a['origin_revision'])
            if s.blockers(origin,model=True) or await s.target_blockers(conn,origin,original): return None
        if w['delivery_json']: return json.loads(w['delivery_json'])
        if w['state'] in ('cancelled','timed_out','closed'): return {'waitId':wait_id,'state':w['state'],'events':[]}
        if w['state']!='sealed': return None
        valid=[]
        for m in await many(conn,'SELECT * FROM webhook_assignment_events WHERE wait_id=? ORDER BY claimed_at_ms,event_id',(wait_id,)):
            event=await one(conn,'SELECT * FROM webhook_events WHERE event_id=?',(m['event_id'],))
            if await one(conn,'SELECT receipt_id FROM webhook_receipts WHERE assignment_event_id=?',(m['assignment_event_id'],)): continue
            if await expire(s,conn,event,member=m['assignment_event_id']): continue
            valid.append(m)
        if not valid:
            await conn.execute("UPDATE webhook_waits SET state='registered',first_claimed_at_ms=NULL,last_claimed_at_ms=NULL,row_version=row_version+1 WHERE wait_id=?",(wait_id,)); return None
        if reserve_execution and s.bridge and not await s.bridge.resume_available(conn,assignment_id): return None
        events=[await s.event_material(conn,m['event_id']) for m in valid]
        result={'waitId':wait_id,'state':'delivered','events':events}
        command='webhook-wait:'+wait_id
        # This await return is the material-delivery boundary, not merely enqueueing.
        for m in valid:
            await conn.execute('UPDATE webhook_assignment_events SET delivered_at_ms=?,delivery_command_id=? WHERE assignment_event_id=? AND delivered_at_ms IS NULL',(s.clock(),command,m['assignment_event_id']))
        await conn.execute("UPDATE webhook_waits SET state='delivered',closed_at_ms=?,delivery_command_id=?,delivery_json=?,row_version=row_version+1 WHERE wait_id=?",(s.clock(),command,dumps(result),wait_id))
        return result


async def await_wait(s,assignment_id,wait_id):
    if s.bridge: await s.bridge.waiting(assignment_id,True)
    from app.agent import steering
    assignment=await one(s.db.conn,'SELECT internal_chat_id FROM webhook_assignments WHERE assignment_id=?',(assignment_id,))
    human=False
    wake=s.wake.listen()
    try:
        while True:
            wake.clear()
            if steering.has_pending(assignment['internal_chat_id']):
                human=True
                return {'waitId':wait_id,'state':'human_interruption','events':[],'registrationPreserved':True}
            value=await delivery(s,assignment_id,wait_id,reserve_execution=True)
            if value is not None: return value
            # Timer discovers deadlines only; no model polling and no new execution identity.
            try: await asyncio.wait_for(wake.wait(),.25)
            except TimeoutError: pass
    finally:
        s.wake.forget(wake)
        if s.bridge: await s.bridge.waiting(assignment_id,False,human=human)
