"""One action-scoped tool. Execution identity and owner come only from the host."""
from __future__ import annotations

import json
from pathlib import Path

from app.tools.base import current_tool_context
from app.tools.servers import automatic_turn
from app.runtime.tool_result import ToolOutcome
from app.webhooks.contracts import EndpointConfig, Scope, WebhookError, dumps
from app.webhooks.repository import many, one
from app.webhooks import queries, receipts, waits

ACTIONS=('describe','list','get','create','update','delete','set_enabled','pause','resume','stop','events','retry','wait','report','telemetry')
READONLY={'describe','events','get','report'}
AUTOMATIC={'describe','get','events','wait','report','telemetry'}
GUIDANCE=(Path(__file__).resolve().parents[2]/'prompts'/'guides'/'webhook.md').read_text(encoding='utf-8')
CREATE_REQUIRED=('scope','requestId')
UPDATE_REQUIRED=('endpointId','requestId','expectedRevision')
MANAGEMENT_FIELDS={'scope','requestId','endpointId','name','description','enabled','expectedRevision','expectedControlRevision','reason','operation','graceSeconds'}


def management_contracts(action):
    common={
        'requestId':'Required stable operation ID, string length 1..128. Reuse only for an identical retry.',
        'name':'Optional endpoint name; omitted means an empty name, including on update.',
        'description':'Optional endpoint description; omitted means an empty description, including on update.',
        'config':'EndpointConfig object described by configSchema. Full replacement, not a patch; omitted fields use defaults.',
        'reason':'Optional operation reason.'}
    contracts={
        'create':{'required':list(CREATE_REQUIRED),'fields':{**common,
            'scope':'Required scopeSchema object. Use list with params.targets=true to find an owned folder/conversation ID.',
            'enabled':'Optional boolean, default false. Enabling requires nonempty config.processing.instructions.'},
            'example':{'action':'create','params':{
                'requestId':'create-example-1','scope':{'type':'folder','id':'<folder ID from list>'},
                'name':'Example trigger','description':'Process authorized external events','enabled':True,
                'config':{'target':{'mode':'newConversation'},'processing':{'instructions':'Process only the authorized event and report its outcome.'}}}}},
        'update':{'required':list(UPDATE_REQUIRED),'fields':{**common,
            'endpointId':'Required endpoint.id from get.',
            'expectedRevision':'Required endpoint.revision from get.',
            'expectedControlRevision':'Required endpoint.controlRevision from get whenever enabled is supplied, even if unchanged.',
            'enabled':'Optional boolean; omission preserves the current enabled state. Enabling requires config.processing.instructions.'},
            'rules':['Get the endpoint first. Copy its complete config, name and description, then change only the requested fields.',
                     'The binding scope is immutable; do not send scope on update.',
                     'Omitting config resets it to defaults; omitting name or description clears them. This is not a partial update.'],
            'example':{'action':'update','params':{
                'endpointId':'<endpoint.id from get>','requestId':'update-example-1','expectedRevision':1,
                'name':'Example trigger','description':'Process authorized external events',
                'config':{'target':{'mode':'newConversation'},'processing':{'instructions':'Process only the authorized event and report its outcome.'}}}},
            'rotation':{'operation':'rotate_key','required':['endpointId','requestId','operation'],
                'expectedControlRevision':'Use endpoint.controlRevision from get; omission binds the current version at confirmation.',
                'graceSeconds':'Optional integer 0..3600, default 0. Invokes the real user confirmation gate; no config or expectedRevision required.'}}}
    return {'contracts':{k:v for k,v in contracts.items() if not action or k==action},
        'scopeSchema':Scope.model_json_schema(by_alias=True),
        'configSchema':EndpointConfig.model_json_schema(by_alias=True),
        'parameterLayout':'Only action and params belong at the tool root. Put requestId, scope, name, enabled and config beside each other inside params, never inside config.',
        'targetRules':['A folder scope defaults to target.mode=newConversation. fixedConversation must select a conversation in that folder or its descendants.',
                       'A conversation scope must target that same conversation. Omit config.target for automatic fixedConversation selection, or set mode=fixedConversation and conversationId=scope.id.']}


def validate_management_params(action,params):
    if action not in ('create','update'): return
    config=params.get('config')
    if isinstance(config,dict):
        misplaced=sorted(MANAGEMENT_FIELDS & config.keys())
        if misplaced:
            key=misplaced[0]
            raise WebhookError('invalid_parameter_location',f'params.config.{key} belongs at params.{key}, beside config; fields are not moved automatically.',
                details={'field':f'params.config.{key}','expectedPath':f'params.{key}'})
    required=CREATE_REQUIRED if action=='create' else UPDATE_REQUIRED
    if action=='update' and params.get('operation')=='rotate_key': required=('endpointId','requestId')
    elif action=='update' and 'enabled' in params: required=(*required,'expectedControlRevision')
    for key in required:
        if params.get(key) is None:
            raise WebhookError('missing_parameter',f'params.{key} is required for {action}. Call describe with params.action={action} for the contract and example.',
                details={'field':f'params.{key}'})


async def dispatch(service,action,params,ctx):
    if action not in ACTIONS or not isinstance(params,dict): raise WebhookError('invalid_action')
    if ctx.receipt_repair and action not in READONLY: raise WebhookError('receipt_repair_read_only',status=403)
    if ctx.webhook_automatic and action not in AUTOMATIC: raise WebhookError('automatic_configuration_forbidden',status=403)
    if ctx.agent_session_uuid and action not in ('describe','events','report','telemetry'): raise WebhookError('agent_action_forbidden',status=403)
    owner=ctx.webhook_owner_id
    if not owner or not ctx.conversation_uuid: raise WebhookError('host_identity_required',status=403)
    for forbidden in ('owner','ownerId','runId','assignmentId','attemptId','source','trusted','confirm'):
        if forbidden in params: raise WebhookError('host_identity_not_a_parameter',status=403)
    a=await one(service.db.conn,'SELECT * FROM webhook_assignments WHERE conversation_uuid=? AND root_turn_uuid=?',(ctx.conversation_uuid,ctx.run_root_turn_uuid))
    if a: ctx.webhook_assignment_id=a['assignment_id']
    if action=='describe':
        contract=management_contracts(params.get('action')) if params.get('action') in (None,'create','update') else {}
        return {'actions':list(ACTIONS),'requested':params.get('action'),'guidance':GUIDANCE,**contract,'protocol':{
            'wait':{'op':'register | await | cancel','register':{'requestId':'stable ID','endpointId':'owned endpoint','match':{'all':[{'path':'body.orderId','op':'eq','value':'123'}]},'afterSeq':'optional task-start cursor override','timeoutSeconds':'positive seconds or null','collection':'optional batching settings'},'await':{'waitId':'registered ID'}},
            'report':{'receiptId':'stable ID','results':[{'eventId':'owned and delivered event','outcome':'completed | skipped | failed | unknown','summary':'supported claim','evidenceRefs':['tool-action:existing-action-id'],'result':'optional structured result'}]},
            'management':'get before update; expectedRevision/expectedControlRevision + stable requestId; sensitive operations invoke the real user confirmation gate; keys never returned to model',
            'telemetry':'operationId + declared metrics/records; platform metrics are read-only'}}
    if action=='list':
        return await queries.targets(service,owner,params) if params.get('targets') else await service.list(owner,params)
    if action=='get':
        if params.get('current') or ctx.receipt_repair:
            if not a: raise WebhookError('assignment_required',status=403)
            members=await many(service.db.conn,'SELECT event_id,delivered_at_ms FROM webhook_assignment_events WHERE assignment_id=?',(a['assignment_id'],))
            actions=await many(service.db.conn,'SELECT r.* FROM runtime_actions r JOIN webhook_runtime_links l ON r.run_id=l.run_id WHERE l.assignment_id=? ORDER BY r.created_at,r.action_id',(a['assignment_id'],))
            return {'assignmentId':a['assignment_id'],'state':a['state'],'events':members,'executionHistory':actions}
        eid=params.get('endpointId')
        if ctx.webhook_automatic and a and eid!=a['origin_endpoint_id']: raise WebhookError('endpoint_outside_assignment',status=403)
        return await service.get(owner,eid)
    if action=='events':
        eid=params.get('eventId')
        if ctx.webhook_automatic or ctx.receipt_repair:
            if not eid or not a or not await one(service.db.conn,'SELECT event_id FROM webhook_assignment_events WHERE assignment_id=? AND event_id=?',(a['assignment_id'],eid)): raise WebhookError('event_not_owned',status=403)
        return await queries.detail(service,owner,eid) if eid else await queries.events(service,owner,params)
    if action=='wait':
        if not service.bridge: raise WebhookError('runtime_unavailable',status=503)
        aid=await service.bridge.ensure_human_assignment(ctx)
        op=params.get('op','register')
        if op=='register': return await waits.register(service,aid,owner,{k:v for k,v in params.items() if k!='op'})
        if op=='await': return await waits.await_wait(service,aid,params.get('waitId'))
        if op=='cancel': return await waits.cancel(service,aid,params.get('waitId'))
        raise WebhookError('invalid_wait_operation')
    if action=='report':
        return await receipts.report(service,ctx.webhook_assignment_id,ctx.run_id,params)
    if action=='telemetry':
        from app.webhooks.telemetry import observe
        if not a: raise WebhookError('assignment_required',status=403)
        event_id=params.get('eventId')
        if event_id:
            m=await one(service.db.conn,'SELECT e.endpoint_id FROM webhook_assignment_events m JOIN webhook_events e USING(event_id) WHERE m.assignment_id=? AND m.event_id=?',(a['assignment_id'],event_id))
            if not m: raise WebhookError('event_not_owned',status=403)
            eid=m['endpoint_id']
        else: eid=a['origin_endpoint_id']
        return await observe(service,eid,params,source='model',event_id=event_id,assignment_id=a['assignment_id'],action_id=ctx.execution_id)
    validate_management_params(action,params)
    eid=params.get('endpointId'); body={k:v for k,v in params.items() if k!='endpointId'}
    if action in ('create','update','set_enabled','resume','retry') and await automatic_turn(service.db.conn,ctx):
        # An unattended run never grants an endpoint the right to change servers and instances, and never rewrites, enables
        # or re-runs an endpoint that has it: that would process events with whatever it wrote. Disabling stays allowed.
        if action=='retry':
            event=await one(service.db.conn,'SELECT endpoint_id FROM webhook_events WHERE event_id=?',(params.get('eventId'),))
            eid=event and event['endpoint_id']
        disabling=action=='set_enabled' and body.get('enabled') is False
        if action!='create' and eid and not disabling and (await service.get(owner,eid))['endpoint']['config']['processing'].get('allowServerChanges'):
            raise WebhookError('automatic_privileged_endpoint_forbidden','自动执行的对话不能修改、启用或重试已允许改动服务器和实例的Webhook',status=403)
        if action in ('create','update') and grants_server_changes(body.get('config')):
            raise WebhookError('automatic_permission_forbidden','自动执行的对话不能开启“允许改动服务器和实例”',status=403)
    if action=='delete' or action=='update' and params.get('operation')=='rotate_key':
        sensitive='rotate_key' if action=='update' else action
        if ctx.web_confirm is None: raise WebhookError('user_confirmation_unavailable',status=403)
        body.pop('operation',None)
        prepared=await service.prepare(owner,eid,sensitive,body,tool_approval=True)
        effects = ({'stopRunning': body.get('stopRunning', False), 'scope': body.get('scope', 'all')}
                   if sensitive == 'delete' else {'graceSeconds': body.get('graceSeconds', 0)})
        answer=await ctx.web_confirm({'_sourceTool':'Webhook','_requiresAuthorization':True,
                                      '_approvalScope':prepared['approvalScope'],'_approvalConstraints':prepared['approvalConstraints'],
                                      'title':'确认Webhook '+sensitive,'body':dumps({'impact':prepared['impact'], **effects}),
                                      'type':'warning','default':False,'confirmText':'执行明确操作','cancelText':'取消','timeoutSeconds':300})
        if not answer.get('confirmed') or answer.get('text') or answer.get('feedback'): return {'cancelled':True,'userFeedback':answer}
        result=await service.control(owner,eid,{'confirmationToken':prepared['confirmationToken'],'requestId':body.get('requestId')},action=sensitive)
        result.pop('credential',None); result['credentialDelivery']='Key omitted from model; view/copy the current Key in the logged-in Web UI'
        return result
    if action=='create':
        result=await service.create(owner,body); result.pop('credential',None)
        result['credentialDelivery']='Key omitted from model; view/copy it again in the logged-in Web UI; legacy Keys require manual rotation'
        return result
    if action=='update': return await service.update(owner,eid,body)
    if action in ('set_enabled','pause','resume','stop'): return await service.control(owner,eid,body,action=action)
    if action=='retry':
        from app.webhooks.recovery import retry
        return await retry(service,owner,params.get('eventId'),body)
    raise WebhookError('invalid_action')


def grants_server_changes(config):
    processing=config.get('processing') if isinstance(config,dict) else None
    return isinstance(processing,dict) and (processing.get('allowServerChanges') is True or processing.get('allow_server_changes') is True)


def register_webhook_tool(registry,service):
    async def handler(args):
        try:
            if args.get('action') in ('create','update'):
                misplaced=sorted(set(args)-{'action','params'})
                if misplaced:
                    key=misplaced[0]
                    raise WebhookError('invalid_parameter_location',f'{key} belongs inside params, not at the tool root.',
                        details={'field':key,'expectedPath':f'params.{key}'})
            return dumps(await dispatch(service,args.get('action'),args.get('params',{}),current_tool_context()))
        except WebhookError as exc: return ToolOutcome(dumps(exc.payload),'failed','not_started')
    registry.add('Webhook','Manage authorized external triggers; register/await external events, report owned outcomes, query observations. Call describe before create/update; it returns the action contracts and design guidance. Identity is supplied by runtime; keys never enter model context. Amended receipts keep endpoint/config/control versions fixed; deletion may reduce stopRunning or stop scope and affected resources may finish, never expand. Rotation parameters stay exact.',
                      {'type':'object','properties':{'action':{'type':'string','enum':list(ACTIONS)},'params':{'type':'object','description':'Action-specific parameters; call describe with params.action=create or update for contracts and examples. For create, scope and requestId go directly inside params, beside config, not inside config.'}},'required':['action'],'additionalProperties':False},handler,visibility={'main','agent'},effect_scope='external_event')
