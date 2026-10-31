"""Data R01-R14 acceptance from immutable independent repros; isolated DB/runtime only."""
import asyncio
import json
import pytest
from app.webhooks import queries
from app.webhooks.contracts import WebhookError, dumps
from app.webhooks.repository import one, many, insert, uid
from app.webhooks.telemetry import observe
from app.webhooks.retention import prune
from app.llm.events import StreamEvent, ToolCall, Usage
from tests.test_web_admin import _login_cookie, FakeStreamBackend, FakeRunFactory
from tests.test_webhooks_backend import env, create, accept, drain_scripts

async def http_stats(env, eid, view, **params):
    cookie=await _login_cookie(env)
    r=await env.client.get('/api/webhooks/statistics',params={'endpointId':eid,'view':view,**params},cookies={'openbear_web_session':cookie})
    return r.status,await r.json()

def out(tag, result):
    print('FIX_VERIFIED '+tag+' '+json.dumps(result,ensure_ascii=False,sort_keys=True))

async def test_R07_gauge_range_end_inside_minute(env):
    now=[1_260_000]; env.s.clock=lambda:now[0]
    c=await create(env,statistics={'metricDefinitions':[{'name':'custom_g','type':'gauge','gaugeStaleSeconds':30}]}); eid=c['endpoint']['id']
    for op,t,v in [('early',1_201_000,8),('later',1_210_000,3)]:
        await observe(env.s,eid,{'operationId':op,'metrics':[{'name':'custom_g','value':v,'observedAtMs':t}]},source='script')
    status,result=await http_stats(env,eid,'metrics',start=1_200_000,end=1_205_000)
    m=result['metrics'][0]; assert status==200 and m['count']==1 and m['sum']==8
    assert m['lastValue']==8
    out('gauge_range',m)

async def test_R08_http_overview_excludes_outside_range(env):
    now=[1_201_000]; env.s.clock=lambda:now[0]; c=await create(env); eid=c['endpoint']['id']
    h={'Authorization':'Bearer '+c['credential']['key'],'Idempotency-Key':'one'}
    r=await env.client.post('/webhook/'+eid,json={},headers=h); assert r.status==202
    now[0]=1_210_000
    status,result=await http_stats(env,eid,'overview',start=1_205_000,end=1_220_000)
    o=result['overview']; assert status==200 and o['accepted']==0 and o['requests']==0
    out('http_time_boundary',{'range':result['range'],'requests':o['requests'],'accepted':o['accepted']})

async def test_production_receipt_records_reach_ranking(env):
    c=await create(env,pre={'enabled':True,'code':'import json,sys\nx=json.load(sys.stdin)\nprint(json.dumps({"decision":"skip_model","outcome":"handled","records":[{"event":"action","fields":{"sender":"producer"}}]}))'},statistics={'businessFields':[{'name':'sender','path':'body.sender'}]}); eid=c['endpoint']['id']
    r=await env.client.post('/webhook/'+eid,json={},headers={'Authorization':'Bearer '+c['credential']['key']})
    assert r.status==202
    await drain_scripts(env)
    status,result=await http_stats(env,eid,'ranking',field='sender')
    assert status==200 and result['items'][0]['value']=='producer' and result['items'][0]['count']==1
    _,business=await http_stats(env,eid,'business')
    assert 'test' not in business['items'][0]
    out('production_ranking',{'items':result['items']})

async def test_R04_late_observation_uses_frozen_declaration(env):
    c=await create(env,statistics={'dimensions':[{'name':'category','kind':'category','path':'body.c'}], 'metricDefinitions':[{'name':'custom_n','type':'counter','allowedLabels':['category']}]}); eid=c['endpoint']['id']
    event=await accept(env,c,'{"c":"old"}')
    config=c['endpoint']['config']; config['statistics']['dimensions'][0]['kind']='identifier'
    await env.s.update(123,eid,{'requestId':'change-dimension','expectedRevision':1,'config':config})
    result=await observe(env.s,eid,{'operationId':'old-event','metrics':[{'name':'custom_n','value':1,'labels':{'category':'old'}}]},source='script',event_id=event['eventId'])
    assert result['state']=='applied'
    assert (await one(env.db.conn,"SELECT revision FROM webhook_telemetry_operations WHERE operation_id='old-event'"))['revision']==1
    out('revision_observation',{'eventRevision':1,'currentRevision':2,'result':result})

async def test_R13_null_business_filter_and_ranking(env):
    c=await create(env,statistics={'businessFields':[{'name':'optional','path':'body.optional','valueType':'null'}]}); eid=c['endpoint']['id']
    await observe(env.s,eid,{'operationId':'null','records':[{'fields':{'optional':None}}]},source='script')
    filter_status,filtered=await http_stats(env,eid,'business',filters='{"optional":null}')
    rank_status,ranked=await http_stats(env,eid,'ranking',field='optional')
    assert filter_status==200 and len(filtered['items'])==1
    assert rank_status==200 and ranked['items'][0]['value'] is None and ranked['items'][0]['count']==1
    out('null_business',{'storedFields':await many(env.db.conn,'SELECT field_name,value_type FROM webhook_business_fields'),'filterStatus':filter_status,'filtered':filtered['items'],'ranking':ranked['items']})

async def test_R09_retention_reports_gaps_not_false_zero(env):
    now=[1_200_000]; env.s.clock=lambda:now[0]
    c=await create(env,statistics={'metricDefinitions':[{'name':'custom_n','type':'counter'}], 'businessFields':[{'name':'sender','path':'body.sender'}]}); eid=c['endpoint']['id']
    await observe(env.s,eid,{'operationId':'old','metrics':[{'name':'custom_n','value':7}],'records':[{'fields':{'sender':'old'}}]},source='script')
    env.s.config.retention.metric_sample_days=1; env.s.config.retention.metric_rollup_days=2; env.s.config.retention.business_record_days=1
    now[0]+=4*86400000; removed=await prune(env.s)
    _,m=await http_stats(env,eid,'metrics',start=1_200_000,end=1_260_000)
    _,b=await http_stats(env,eid,'ranking',start=1_200_000,end=1_260_000,field='sender')
    assert removed['rollups'] and removed['businessRecords']
    assert m['metrics'][0]['sum'] is None and m['metrics'][0]['precision']=='unavailable' and m['warnings'] and m['range']['coverageStart'] is None
    assert b['items']==[] and 'retention_gap:business' in b['warnings']
    out('retention_coverage',{'removed':removed,'metrics':m,'ranking':b})

async def test_R10_processing_before_payload_also_clears_wait_copy(env):
    now=[1_800_000_000_000]; env.s.clock=lambda:now[0]
    c=await create(env); eid=c['endpoint']['id']; event=await accept(env,c,'{"private":"retention-fixture"}')
    material=await env.s.event_material(env.db.conn,event['eventId'])
    aid=uid(); wid=uid()
    # Minimal persisted finalized human-wait graph. No runtime or side effects.
    async with env.db.webhook_transaction() as conn:
        await insert(conn,'webhook_assignments',assignment_id=aid,origin_kind='human_wait',conversation_uuid='soft-fixture',internal_chat_id=-100,root_turn_uuid='root',task_start_cursor=0,authorization_snapshot_json='{}',created_at_ms=now[0],state='open')
        await insert(conn,'webhook_waits',wait_id=wid,assignment_id=aid,endpoint_id=eid,register_request_id='w',predicate_json=dumps({'all':[{'path':'body.private','op':'exists','value':True}]}),predicate_sha256='fixture',scope_snapshot_json='{}',after_receive_seq=0,scan_through_seq=0,mode='single',state='registered',registered_at_ms=now[0])
        await insert(conn,'webhook_assignment_events',assignment_event_id=uid(),assignment_id=aid,event_id=event['eventId'],source_kind='wait',wait_id=wid,claimed_at_ms=now[0],delivered_at_ms=now[0])
        member=await one(conn,'SELECT assignment_event_id FROM webhook_assignment_events WHERE assignment_id=?',(aid,))
        await insert(conn,'webhook_receipts',receipt_id=uid(),event_id=event['eventId'],result_version=1,assignment_event_id=member['assignment_event_id'],source='administrator',outcome='completed',request_key='retention-fixture',request_sha256='fixture',reported_at_ms=now[0])
        await conn.execute("UPDATE webhook_waits SET state='delivered',delivery_json=? WHERE wait_id=?",(dumps({'events':[material]}),wid))
        await conn.execute("UPDATE webhook_assignments SET state='finalized',finalized_at_ms=?,final_snapshot_json=? WHERE assignment_id=?",(now[0],dumps({'materials':[material]}),aid))
        await conn.execute("UPDATE webhook_events SET route_state='terminal',terminal_at_ms=? WHERE event_id=?",(now[0],event['eventId']))
    env.s.config.retention.processing_days=1; env.s.config.retention.payload_days=7
    now[0]+=3*86400000; first=await prune(env.s)
    now[0]+=6*86400000; second=await prune(env.s)
    row=await one(env.db.conn,'SELECT delivery_json FROM webhook_waits WHERE wait_id=?',(wid,))
    detail=(await queries.detail(env.s,123,event['eventId']))['event']
    assert detail['payloadStatus']=='purged' and 'retention-fixture' not in row['delivery_json']
    out('retention_hidden_wait_copy',{'first':first,'second':second,'payloadStatus':detail['payloadStatus'],'waitCopyStillContainsOriginal':False})

async def test_R02_actual_billing_survives_ledger_deletion(env):
    c=await create(env,batching={'enabled':False}); eid=c['endpoint']['id']; event=await accept(env,c)
    class Backend(FakeStreamBackend):
        async def stream(self,messages,**kwargs):
            self.calls+=1
            yield StreamEvent(kind='usage',usage=Usage(input_tokens=100,output_tokens=20),details={'providerCostUsd':.125})
            if self.calls==1:
                yield StreamEvent(kind='tool_call',tool_calls=[ToolCall('report','Webhook',json.dumps({'action':'report','params':{'receiptId':'r','results':[{'eventId':event['eventId'],'outcome':'completed'}]}}))])
                yield StreamEvent(kind='finish',finish_reason='tool_calls')
            else:
                yield StreamEvent(kind='content',text='Done'); yield StreamEvent(kind='finish',finish_reason='stop')
    backend=Backend(); env.server.llm_factory=FakeRunFactory(backend,context_window=128000)
    await env.worker.tick(); await asyncio.wait_for(asyncio.gather(*env.server.runs.scheduler.tasks(kind='controller')),10)
    before=await queries.usage(env.s,env.db.conn,endpoint_id=eid); assert before['costUsd']==.25 and before['modelCalls']==2
    assignment=await one(env.db.conn,'SELECT * FROM webhook_assignments')
    # Exact ledger deletion performed by the legacy conversation-delete handler.
    # The separate lifecycle protocol is outside this review; simulate just its allowed deletion.
    async with env.db.write_transaction() as conn:
        await conn.execute('DELETE FROM model_calls WHERE chat_id=?',(assignment['internal_chat_id'],))
    after=await queries.usage(env.s,env.db.conn,endpoint_id=eid)
    assert after['costUsd']==.25 and after['modelCalls']==2 and after['ledgerDeletedCalls']==2
    assert after['providerReportedCostUsd']==.25 and after['coverage']=='projected'
    out('deleted_billing',{'before':before,'after':after,'snapshotStillHasCost':json.loads(assignment['final_snapshot_json'])['usage']['costUsd']})

async def test_R14_framework_counter_respects_total_series_budget(env):
    env.s.config.telemetry.max_series_per_endpoint=1
    c=await create(env,statistics={'metricDefinitions':[{'name':'custom_n','type':'counter'}]}); eid=c['endpoint']['id']
    await observe(env.s,eid,{'operationId':'custom','metrics':[{'name':'custom_n','value':1}]},source='script')
    r=await env.client.post('/webhook/'+eid,json={},headers={'Authorization':'Bearer '+c['credential']['key']}); assert r.status==202
    rows=await many(env.db.conn,'SELECT d.name FROM webhook_metric_series s JOIN webhook_metric_definitions d USING(definition_id) WHERE s.endpoint_id=?',(eid,))
    assert len(rows)==1
    _,stats=await http_stats(env,eid,'overview')
    assert 'retention_gap:framework_series' in stats['warnings']
    out('framework_series_budget',{'maximum':1,'actual':len(rows),'series':rows})
@pytest.mark.parametrize('raw',[b'{"x":1e309}',b'{"x":"\\ud800"}'])
async def test_R01_invalid_json_rejected_before_persistence(env,raw):
    c=await create(env); eid=c['endpoint']['id']
    r=await env.client.post('/webhook/'+eid,data=raw,headers={'Authorization':'Bearer '+c['credential']['key'],'Content-Type':'application/json'})
    assert r.status==422 and (await r.json())['code']=='invalid_body'
    assert not await many(env.db.conn,'SELECT * FROM webhook_events')
    from tests.test_webhooks_query_business import endpoint
    other=await endpoint(env,'unrelated')
    good=await accept(env,other,'{"valid":true}')
    await env.worker.tick()
    assert await many(env.db.conn,'SELECT * FROM webhook_batches')
    out('invalid_json_rejected',{'raw':raw.decode(),'http':422,'unrelatedEndpointBlocked':False})

async def test_R06_missing_and_length_policies_skip_only_metrics(env):
    c=await create(env,statistics={'dimensions':[{'name':'cat','kind':'category','path':'body.cat','maxLength':2,'missing':'rejectMetric'}], 'metricDefinitions':[{'name':'custom_n','type':'counter','allowedLabels':['cat']}]}); eid=c['endpoint']['id']
    event=await accept(env,c,'{}')
    results=[]
    for op,labels in [('absent',{}),('too-long',{'cat':'abcdef'})]:
        results.append(await observe(env.s,eid,{'operationId':op,'metrics':[{'name':'custom_n','value':1,'labels':labels}]},source='script',event_id=event['eventId']))
    stored=await many(env.db.conn,'SELECT labels_json FROM webhook_metric_series WHERE endpoint_id=?',(eid,))
    assert not stored and all(r['warnings'] for r in results)
    projection=await one(env.db.conn,'SELECT dimensions_json FROM webhook_event_payloads')
    assert projection['dimensions_json']=='{}'
    out('dimension_policy_ignored',{'maxLength':2,'missing':'rejectMetric','stored':stored,'projection':projection,'observations':results})

async def test_R03_R11_actual_cost_source_and_derived_lifecycle_spans(env):
    env.server.config.models.resolve('openai/gpt')[1].cost={'input':1.0,'output':2.0}
    c=await create(env,batching={'enabled':False}); eid=c['endpoint']['id']; event=await accept(env,c)
    class Backend(FakeStreamBackend):
        async def stream(self,messages,**kwargs):
            self.calls+=1
            yield StreamEvent(kind='usage',usage=Usage(input_tokens=100,output_tokens=20))
            if self.calls==1:
                yield StreamEvent(kind='tool_call',tool_calls=[ToolCall('report','Webhook',json.dumps({'action':'report','params':{'receiptId':'r','results':[{'eventId':event['eventId'],'outcome':'completed'}]}}))])
                yield StreamEvent(kind='finish',finish_reason='tool_calls')
            else:
                yield StreamEvent(kind='content',text='Done'); yield StreamEvent(kind='finish',finish_reason='stop')
    env.server.llm_factory=FakeRunFactory(Backend(),context_window=128000)
    await env.worker.tick(); await asyncio.wait_for(asyncio.gather(*env.server.runs.scheduler.tasks(kind='controller')),10)
    status,result=await http_stats(env,eid,'overview'); u=result['overview']['usage']
    # Backend above intentionally emits no provider price: these are local estimates.
    assert status==200
    assert u['costUsd']==pytest.approx(.00028) and u['unknownCostCalls']==0
    assert u['estimatedCostUsd']==pytest.approx(.00028) and u['costSources']['estimated']==2 and 'estimated_cost' in result['warnings']
    out('estimate_source_lost',{'providerCostUsd':None,'localPrice':{'input':1.0,'output':2.0},'usage':u,'warnings':result['warnings']})
    await env.worker.tick()  # finish the genuine notification adapter too
    status,phases=await http_stats(env,eid,'phases')
    keys=[p['phase'] for p in phases['phases']]
    n=await one(env.db.conn,"SELECT * FROM web_task_notifications WHERE kind='webhook-result'")
    delivery=next(p for p in phases['phases'] if p['phase']=='delivery')
    assert delivery['count']==1 and delivery['averageSeconds']==n['delivered_at']-n['created_at']
    detail=(await queries.detail(env.s,123,event['eventId']))['event']
    assert {'end_to_end','delivery'} <= {p['phase'] for p in detail['phaseSpans']}
    notification=await one(env.db.conn,"SELECT state FROM web_task_notifications WHERE kind='webhook-result'")
    assert notification['state']=='delivered' and 'end_to_end' in keys and 'delivery' in keys
    out('missing_end_to_end_delivery',{'phases':keys,'eventTerminal':(await one(env.db.conn,'SELECT route_state FROM webhook_events'))['route_state'],'notificationState':notification['state']})
async def test_R12_resumed_model_segments_count_as_one_assignment(env):
    from app.tools.base import current_tool_context
    from tests.test_webhooks_runtime_business import eventually
    now=[1_000_000]; env.s.clock=lambda:now[0]
    c=await create(env,batching={'enabled':False}); event=await accept(env,c)
    async def confirm(args):
        return str(await current_tool_context().web_confirm({'title':'Isolated review confirmation','body':'No external effect','default':False}))
    env.server.tools.add('Confirm','Isolated confirmation span',{'type':'object'},confirm)
    class Backend(FakeStreamBackend):
        async def stream(self,messages,**kwargs):
            self.calls+=1
            now[0]+={1:1000,2:9000}.get(self.calls,0)
            if self.calls==1:
                call=ToolCall('confirm','Confirm','{}')
            elif self.calls==2:
                call=ToolCall('report','Webhook',json.dumps({'action':'report','params':{'receiptId':'r','results':[{'eventId':event['eventId'],'outcome':'completed'}]}}))
            else:
                yield StreamEvent(kind='content',text='Done'); yield StreamEvent(kind='finish',finish_reason='stop'); return
            yield StreamEvent(kind='tool_call',tool_calls=[call]); yield StreamEvent(kind='finish',finish_reason='tool_calls')
    env.server.llm_factory=FakeRunFactory(Backend(),context_window=128000)
    await env.worker.tick(); tasks=env.server.runs.scheduler.tasks(kind='controller')
    await eventually(lambda:bool(env.server.interactions.pending))
    a=await one(env.db.conn,'SELECT * FROM webhook_assignments')
    p=env.server.interactions.pending_for(a['conversation_uuid'])[0]
    now[0]+=20000
    assert (await env.server.interactions.submit(p['interactionId'],123,{'confirmed':True,'revision':p['revision']}))['ok']
    await asyncio.wait_for(asyncio.gather(*tasks),10)
    status,result=await http_stats(env,c['endpoint']['id'],'phases')
    model=next(x for x in result['phases'] if x['phase']=='model_tool')
    assert status==200 and model['measurementLevel']=='assignment' and model['count']==1 and model['averageSeconds']==10 and model['maxSeconds']==10
    end_to_end=next(x for x in result['phases'] if x['phase']=='end_to_end')
    assert end_to_end['count']==1 and end_to_end['averageSeconds']==30
    out('assignment_duration_is_segment',{'assignments':1,'actualModelSeconds':10,'actualHumanWaitSeconds':20,'modelQuery':model})

async def test_R06_paths_missing_policies_keep_business_and_event(env):
    c=await create(env,statistics={
        'dimensions':[{'name':'cat','kind':'category','path':'body.nested.kind','missing':'rejectMetric','maxLength':3},
                      {'name':'omitted','kind':'category','path':'body.absent','missing':'omit'},
                      {'name':'unknown','kind':'category','path':'body.absent','missing':'unknown'},
                      {'name':'identifier','kind':'identifier','path':'body.id','maxLength':4}],
        'metricDefinitions':[{'name':'custom_n','type':'counter','allowedLabels':['cat','omitted','unknown']}],
        'businessFields':[{'name':'amount','path':'body.nested.amount','valueType':'number'},
                          {'name':'result','path':'fields.nested.result','valueType':'boolean'}]})
    event=await accept(env,c,'{"nested":{"kind":"abc","amount":0},"id":"123456"}')
    payload={'operationId':'paths','metrics':[{'name':'custom_n','value':1}], 'records':[{'fields':{'nested':{'result':False}}}]}
    result=await observe(env.s,c['endpoint']['id'],payload,source='script',event_id=event['eventId'])
    row=await one(env.db.conn,'SELECT dimensions_json FROM webhook_event_payloads')
    assert json.loads(row['dimensions_json'])=={'cat':'abc','unknown':'__unknown__','identifier':'1234'}
    labels=json.loads((await one(env.db.conn,'SELECT labels_json FROM webhook_metric_series'))['labels_json'])
    assert labels=={'cat':'abc','unknown':'__unknown__'}
    _,business=await http_stats(env,c['endpoint']['id'],'business',filters='{"amount":0,"result":false}')
    assert len(business['items'])==1
    missing=await accept(env,c,'{}',key='missing')
    result=await observe(env.s,c['endpoint']['id'],{**payload,'operationId':'missing'},source='script',event_id=missing['eventId'])
    assert 'missing_dimension:cat' in result['warnings']
    assert (await one(env.db.conn,'SELECT COUNT(*) n FROM webhook_events'))['n']==2
    assert (await one(env.db.conn,'SELECT COUNT(*) n FROM webhook_business_records'))['n']==2
    assert (await one(env.db.conn,'SELECT COUNT(*) n FROM webhook_metric_samples'))['n']==1
    out('dimension_and_business_paths',{'projection':json.loads(row['dimensions_json']),'labels':labels,'business':business['items'][0]['fields'],'missingMetricWarnings':result['warnings']})


@pytest.mark.parametrize('capacity',['endpointEvents','globalEvents','endpointBytes','globalBytes'])
async def test_runtime_R12_unknown_terminal_still_uses_capacity(env,capacity):
    limits={'pendingEvents':1} if capacity=='endpointEvents' else {'pendingBytes':65536} if capacity=='endpointBytes' else {}
    if capacity=='globalEvents': env.s.config.queue.max_events=1
    if capacity=='globalBytes': env.s.config.queue.max_bytes=65536
    c=await create(env,batching={'enabled':False},limits=limits)
    raw=dumps({'payload':'x'*32000}) if capacity.endswith('Bytes') else '{}'
    first=await accept(env,c,raw)
    backend=FakeStreamBackend([
        [StreamEvent(kind='tool_call',tool_calls=[ToolCall('report','Webhook',json.dumps({'action':'report','params':{'receiptId':'r','results':[{'eventId':first['eventId'],'outcome':'unknown'}]}}))]),StreamEvent(kind='finish',finish_reason='tool_calls')],
        [StreamEvent(kind='content',text='Unknown outcome; verify first'),StreamEvent(kind='finish',finish_reason='stop')]])
    env.server.llm_factory=FakeRunFactory(backend,context_window=128000)
    await env.worker.tick(); await asyncio.gather(*env.server.runs.scheduler.tasks(kind='controller'))
    row=await one(env.db.conn,'SELECT terminal_at_ms,review_required FROM webhook_events')
    assert row['terminal_at_ms'] is not None and row['review_required']==1
    with pytest.raises(WebhookError,match='queue_full'):
        await accept(env,c,raw,key='second')
    stats=await queries.statistics(env.s,123,{'endpointId':c['endpoint']['id']})
    assert stats['overview']['pendingEvents']==stats['overview']['needsReview']==1
    out('unknown_capacity_'+capacity,{'rejectedSecond':True,'pending':stats['overview']['pendingEvents']})


async def test_UI05_wait_filters_before_independent_cursor(env):
    from tests.test_webhooks_query_business import endpoint
    a=await endpoint(env,'wait-folder-one'); b=await endpoint(env,'wait-folder-two'); hidden=await endpoint(env,'hidden',owner=999)
    aid=uid()
    async with env.db.webhook_transaction() as conn:
        await insert(conn,'webhook_assignments',assignment_id=aid,origin_kind='human_wait',conversation_uuid='wait-fixture',internal_chat_id=-100,root_turn_uuid='r',task_start_cursor=0,authorization_snapshot_json='{}',created_at_ms=env.s.clock())
        for i in range(36):
            eid=(b if i==0 else hidden if i==35 else a)['endpoint']['id']
            await insert(conn,'webhook_waits',wait_id=f'W{i:02}',assignment_id=aid,endpoint_id=eid,register_request_id=f'w{i}',predicate_json='{}',predicate_sha256='fixture',scope_snapshot_json='{}',after_receive_seq=0,scan_through_seq=0,mode='single',state='cancelled' if 0<i<31 else 'registered',registered_at_ms=env.s.clock())
    cookie=await _login_cookie(env)
    async def page(**params):
        r=await env.client.get('/api/webhooks/waits',params=params,cookies={'openbear_web_session':cookie})
        assert r.status==200; return await r.json()
    params={'active':'true','scopeType':'folder','scopeId':'wait-folder-one','search':' WAIT-FOLDER-ONE ','effectiveStatus':'receiving','limit':2}
    first=await page(**params); second=await page(**params,cursor=first['nextCursor'])
    assert [x['waitId'] for x in first['items']]==['W31','W32']
    assert [x['waitId'] for x in second['items']]==['W33','W34'] and second['nextCursor'] is None
    assert not (await page(**{**params,'search':'wait-folder-two'}))['items']
    assert not (await page(**{**params,'effectiveStatus':'paused'}))['items']
    historical=await page(active='false',scopeType='folder',scopeId='wait-folder-one',limit=100)
    assert len(historical['items'])==30
    out('wait_filtered_pages',{'first':[x['waitId'] for x in first['items']],'second':[x['waitId'] for x in second['items']],'historical':len(historical['items'])})


async def test_R04_pending_script_pins_revision_and_admin_revalidates_only_rejected(env,monkeypatch):
    from app.webhooks import telemetry
    code='print(\'{"decision":"skip_model","outcome":"handled","metrics":[{"name":"custom_n","value":1}]}\')'
    c=await create(env,pre={'enabled':True,'code':code},statistics={'dimensions':[{'name':'cat','path':'body.cat','kind':'category'}], 'metricDefinitions':[{'name':'custom_n','type':'counter','allowedLabels':['cat']}]})
    event=await accept(env,c,'{"cat":"abc"}')
    original=telemetry.flush
    async def deferred(s): return None
    with monkeypatch.context() as patch:
        patch.setattr(telemetry,'flush',deferred)
        await drain_scripts(env)
    saved=await one(env.db.conn,"SELECT * FROM webhook_telemetry_operations WHERE source='script'")
    assert saved['state']=='pending' and saved['revision']==1
    cfg=c['endpoint']['config']; cfg['statistics']['dimensions'][0]['kind']='identifier'
    await env.s.update(123,c['endpoint']['id'],{'requestId':'rev2','expectedRevision':1,'config':cfg})
    await original(env.s)
    row=await one(env.db.conn,'SELECT state,revision FROM webhook_telemetry_operations WHERE telemetry_id=?',(saved['telemetry_id'],))
    assert row=={'state':'applied','revision':1}
    labels=json.loads((await one(env.db.conn,'SELECT labels_json FROM webhook_metric_series'))['labels_json'])
    assert labels=={'cat':'abc'}
    out('pending_frozen_revision',row)


async def test_R01_legacy_bad_body_detail_is_inspectable(env):
    c=await create(env); event=await accept(env,c)
    # Simulates an accepted v1 row, not a route that bypasses new validation.
    async with env.db.webhook_transaction() as conn:
        await conn.execute('DELETE FROM webhook_event_payloads WHERE event_id=?',(event['eventId'],))
        await insert(conn,'webhook_event_payloads',event_id=event['eventId'],content_type='application/json',body_bytes=b'{"x":1e309}',query_pairs_json='[]')
    result=await queries.detail(env.s,123,event['eventId'])
    assert result['event']['raw']['content']=='{"x":1e309}' and result['event']['raw']['bodyError']=='invalid_body'
    assert result['event']['raw']['body'] is None
    # A strict client can parse the complete management response.
    json.loads(json.dumps(result,allow_nan=False))
    out('legacy_invalid_body_detail',result['event']['raw'])

async def test_runtime_R9_query_keeps_adapter_and_unknown_channel_distinct(env,monkeypatch):
    from tests.test_webhooks_runtime_review_fixes import test_fixed_webhook_channel_ack_loss_retries_non_idempotent_send
    await test_fixed_webhook_channel_ack_loss_retries_non_idempotent_send(env,monkeypatch)
    event=await one(env.db.conn,'SELECT event_id FROM webhook_events')
    result=await queries.detail(env.s,123,event['event_id'])
    assignment=result['event']['assignments'][0]
    assert assignment['notificationState']=='delivered'
    assert assignment['notificationEffectState']=='enqueued'
    assert assignment['notificationVerificationRequired'] and not assignment['canRetryNotification']
    assert assignment['notificationChannels'][0]['channel']=='telegram'
    assert assignment['notificationChannels'][0]['state']=='unknown'
    # This remains false even if adapter delivery itself is paused.
    async with env.db.webhook_transaction() as conn: await conn.execute("UPDATE web_task_notifications SET state='paused'")
    result=await queries.detail(env.s,123,event['event_id'])
    assert not result['event']['assignments'][0]['canRetryNotification']
    out('unknown_channel_query',{k:assignment[k] for k in ['notificationState','notificationEffectState','notificationVerificationRequired','notificationChannels','canRetryNotification']})
