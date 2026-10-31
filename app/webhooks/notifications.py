"""Webhook final delivery uses the existing notification outbox and channel workers.

This adapter never starts a controller to summarize an already completed webhook.
Channel enqueue ACK and actual external delivery remain separately queryable facts.
"""
from __future__ import annotations

import json

from app.webhooks.contracts import WebhookError, dumps
from app.webhooks.repository import insert, many, one, uid


async def owns_event(s,conversation,root):
    if not root: return False
    return bool(await one(s.db.conn,"SELECT assignment_id FROM webhook_assignments WHERE conversation_uuid=? AND root_turn_uuid=? AND origin_kind='webhook'",(conversation,root)))


async def tick(s):
    if s.host is None: return
    async with s.db.webhook_transaction() as conn:
        assignments=await many(conn,"""SELECT a.* FROM webhook_assignments a WHERE a.state='finalized' AND a.origin_kind='webhook' AND a.notification_key IS NULL
            AND NOT EXISTS(SELECT 1 FROM webhook_stage_jobs j WHERE j.assignment_id=a.assignment_id AND j.state IN ('pending','running','held','unknown')) LIMIT 100""")
        for a in assignments:
            e=await s.endpoint_row(conn,a['origin_endpoint_id'],deleted=True); _,config=await s.revision(conn,a['origin_endpoint_id'],a['origin_revision'])
            post=await one(conn,"SELECT state FROM webhook_stage_jobs WHERE assignment_id=? AND stage='post'",(a['assignment_id'],))
            status='failed' if post and post['state']=='failed' else 'completed' if a['terminal_reason']=='normal' else 'interrupted' if a['terminal_reason'] in ('cancelled','interrupted') else 'failed'
            silent=config.notifications.policy=='silent' or config.notifications.policy=='errorsDigest' and status=='completed'
            key='webhook:assignment:'+a['assignment_id']+':final'; now=s.clock()//1000
            payload={'assignmentId':a['assignment_id'],'rootTurnId':a['root_turn_uuid'],'status':status,'modelOutcome':a['terminal_reason'],'postState':post['state'] if post else 'notConfigured','summary':'Webhook '+status,'finalText':json.loads(a['final_snapshot_json']).get('finalText',''),'startedAtMs':a['created_at_ms']}
            digest_policy=config.notifications.policy=='errorsDigest' and not silent
            group=None
            if digest_policy:
                group=await one(conn,"SELECT * FROM web_task_notifications WHERE kind='webhook-result' AND state='pending' AND next_attempt_at>? AND json_extract(payload_json,'$.digestEndpoint')=? AND json_extract(payload_json,'$.digestRevision')=? ORDER BY id LIMIT 1",(now,a['origin_endpoint_id'],a['origin_revision']))
                item={'assignmentId':a['assignment_id'],'status':status,'modelOutcome':a['terminal_reason'],'postState':payload['postState'],'finalText':payload['finalText']}
                if group:
                    payload=json.loads(group['payload_json']); key=group['notification_key']
                    payload['assignments'].append(item)
                else:
                    payload.update(digestEndpoint=a['origin_endpoint_id'],digestRevision=a['origin_revision'],assignments=[item])
                    payload['rootTurnId']='webhook-digest:'+a['assignment_id']
                payload['summary']=f"Webhook errors digest: {len(payload['assignments'])} assignments"
                payload['finalText']=payload['summary']+'\n'+'\n'.join(x['assignmentId']+' ['+x['status']+'] '+x['finalText'] for x in payload['assignments'])
            if group:
                await conn.execute('UPDATE web_task_notifications SET payload_json=?,updated_at=? WHERE notification_uuid=?',(dumps(payload),now,group['notification_uuid']))
            else:
                await insert(conn,'web_task_notifications',notification_uuid=uid(),notification_key=key,conversation_uuid=a['conversation_uuid'],internal_chat_id=a['internal_chat_id'],owner_chat_id=e['owner_chat_id'],
                             kind='webhook-result',task_status=status,payload_json=dumps(payload),state='suppressed' if silent else 'pending',next_attempt_at=now+(config.notifications.digest_seconds or s.config.notifications.digest_seconds) if digest_policy else now,created_at=now,updated_at=now)
            await conn.execute('UPDATE webhook_assignments SET notification_key=? WHERE assignment_id=?',(key,a['assignment_id']))
    for n in await many(s.db.conn,"SELECT * FROM web_task_notifications WHERE kind='webhook-result' AND state='pending' AND next_attempt_at<=? ORDER BY id LIMIT 100",(s.clock()//1000,)):
        await deliver(s,n)


async def deliver(s,n):
    now=s.clock()//1000
    async with s.db.webhook_transaction() as conn:
        cur=await conn.execute("UPDATE web_task_notifications SET state='processing',attempts=attempts+1,claimed_at=?,claim_token=? WHERE notification_uuid=? AND state='pending'",(now,n['notification_uuid'],n['notification_uuid']))
        if cur.rowcount!=1: return
    try:
        payload=json.loads(n['payload_json']); h=s.host
        conv=await one(s.db.conn,'SELECT * FROM web_conversations WHERE conversation_uuid=? AND owner_chat_id=?',(n['conversation_uuid'],n['owner_chat_id']))
        if not conv: raise WebhookError('notification_target_deleted',status=409)
        event={'conversationUuid':n['conversation_uuid'],'rootTurnUuid':payload['rootTurnId'],'turnUuid':payload['rootTurnId'],'runUuid':payload['rootTurnId'],'source':'webhook'}
        # Reuse stable root/event keys; a crash can repeat enqueue but never business work.
        notifier=getattr(h,'web_task_telegram',None)
        if notifier:
            async with s.db.webhook_transaction() as conn:
                await conn.execute('INSERT OR IGNORE INTO webhook_notification_routes(root_turn_uuid,notification_key,created_at_ms) VALUES(?,?,?)',(payload['rootTurnId'],n['notification_key'],s.clock()))
            await notifier.observe({**event,'type':'accepted','ts':payload['startedAtMs']},owner_chat_id=n['owner_chat_id'],internal_chat_id=n['internal_chat_id'])
            await notifier.observe({**event,'type':'final','text':payload['finalText']},owner_chat_id=n['owner_chat_id'],internal_chat_id=n['internal_chat_id'])
            await notifier.observe({**event,'type':'done' if payload['status']=='completed' else 'error' if payload['status']=='failed' else 'stopped'},owner_chat_id=n['owner_chat_id'],internal_chat_id=n['internal_chat_id'])
        push=getattr(h,'browser_push',None)
        if push:
            # Outbox creation follows the completed processing chain. Do not
            # count delivery retries/digest waiting as task execution time.
            digest = bool(payload.get('digestEndpoint'))
            started = int(payload.get('startedAtMs') or 0)
            elapsed = int(n['created_at']) - started // 1000 if started > 0 and not digest else None
            title = f'触发器异常汇总（{len(payload.get("assignments") or [])}项）' if digest else ''
            await push.enqueue(n['owner_chat_id'],n['conversation_uuid'],n['notification_key'],payload['status'],
                               task_title=title,elapsed_seconds=elapsed)
        await h._live_for(conv).publish({'type':'webhook_result','turnUuid':payload['rootTurnId'],'eventUuid':n['notification_uuid'],'assignmentId':payload['assignmentId'],'notificationKey':n['notification_key'],'status':payload['status'],'postState':payload['postState']})
    except Exception as exc:
        async with s.db.webhook_transaction() as conn:
            await conn.execute("UPDATE web_task_notifications SET state='paused',last_error=?,claim_token='',updated_at=? WHERE notification_uuid=?",(type(exc).__name__,now,n['notification_uuid']))
        return
    async with s.db.webhook_transaction() as conn:
        await conn.execute("UPDATE web_task_notifications SET state='delivered',delivered_at=?,updated_at=?,claim_token='',last_error='' WHERE notification_uuid=?",(now,now,n['notification_uuid']))
    if hasattr(s.host,'_schedule_conversation_retention'): s.host._schedule_conversation_retention()


async def channel_delivery_states(s,notification_key):
    """Channel observations, not the adapter's durable enqueue acknowledgement."""
    rows=await many(s.db.conn,"SELECT o.state,o.last_error FROM web_tg_notification_outbox o JOIN webhook_notification_routes r USING(root_turn_uuid) WHERE r.notification_key=? ORDER BY o.id",(notification_key,))
    states = [{'channel':'telegram','state':r['state'],'error':r['last_error'],'verificationRequired':r['state']=='unknown'} for r in rows]
    push_rows = await many(s.db.conn, "SELECT state,last_status,last_error FROM web_push_deliveries WHERE event_key=? ORDER BY id", (notification_key,))
    states.extend({'channel':'browser_push','state':r['state'],'error':r['last_error'],
                   'providerStatus':r['last_status'],'verificationRequired':r['state']=='sent',
                   'deviceReceipt':'unavailable'} for r in push_rows)
    return states


async def retry(s,owner,aid,request):
    from app.webhooks.recovery import operation_start, operation_done, check_version
    async with s.db.webhook_transaction() as conn:
        a=await one(conn,'SELECT a.* FROM webhook_assignments a JOIN webhook_endpoints e ON e.endpoint_id=a.origin_endpoint_id WHERE a.assignment_id=? AND e.owner_chat_id=?',(aid,owner))
        if not a: raise WebhookError('not_found',status=404)
        old,op=await operation_start(s,conn,owner,request,'retry',a['origin_endpoint_id'])
        if old: return json.loads(old['result_json'])
        check_version(a,request,'row_version')
        n=await one(conn,'SELECT * FROM web_task_notifications WHERE notification_key=?',(a['notification_key'],))
        if not n or n['state'] not in ('paused','processing'): raise WebhookError('notification_not_retryable',status=409)
        # The adapter only enqueues idempotent durable channel IDs. External channel
        # unknowns are not reset here; their worker's verification policy remains authoritative.
        await conn.execute("UPDATE web_task_notifications SET state='pending',claim_token='',next_attempt_at=?,updated_at=? WHERE notification_key=?",(s.clock()//1000,s.clock()//1000,a['notification_key']))
        result={'notificationKey':a['notification_key'],'state':'pending'}; await operation_done(conn,op,result)
    s.wake.set(); return result
