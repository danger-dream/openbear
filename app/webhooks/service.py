"""Single shared service for HTTP, runtime, and action-scoped tools."""
from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.webhooks.contracts import EndpointConfig, Scope, WebhookError, WebhooksConfig, digest, dumps, iso, parse_config
from app.webhooks.repository import insert, many, one, uid


class WebhookService:
    def __init__(self, db, *, config=None, pepper: bytes, host=None, clock=None):
        if not pepper:
            raise ValueError('credential pepper required outside SQLite')
        self.db, self.host, self.pepper = db, host, pepper
        # Domain-separated from authentication HMACs; reuse the existing local
        # runtime secret, never add a database-resident or separately managed key.
        self._key_cipher = AESGCM(HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
            info=b'openbear:webhooks:credential-encryption:v1').derive(pepper))
        self._config = config or WebhooksConfig()
        self.clock = clock or (lambda: int(time.time()*1000))
        self.wake = asyncio.Event()
        self.worker = self.bridge = None
        self.rate_windows = {}
        self.confirmations = {}

    @property
    def config(self):
        return self._config() if callable(self._config) else self._config

    def verifier(self, key):
        return hmac.digest(self.pepper, key.encode(), 'sha256')

    async def endpoint_row(self, conn, endpoint_id, owner=None, *, deleted=False):
        row = await one(conn, 'SELECT * FROM webhook_endpoints WHERE endpoint_id=?', (endpoint_id,))
        if not row or (owner is not None and row['owner_chat_id'] != owner) or (row['deleted_at_ms'] is not None and not deleted):
            raise WebhookError('not_found', status=404)
        return row

    async def revision(self, conn, endpoint_id, revision):
        row = await one(conn, 'SELECT * FROM webhook_revisions WHERE endpoint_id=? AND revision=?', (endpoint_id,revision))
        if not row: raise WebhookError('revision_not_found', status=404)
        data = json.loads(row['config_json']); data.pop('name',None); data.pop('description',None)
        data['target'] = {**data.get('target',{}),'mode':row['target_mode'],'conversationId':row['target_conversation_uuid']}
        return row, EndpointConfig.model_validate(data)

    async def validate_target(self, conn, owner, scope, config, *, enabled):
        table,key = ('web_conversation_folders','folder_uuid') if scope.type=='folder' else ('web_conversations','conversation_uuid')
        row = await one(conn,f'SELECT * FROM {table} WHERE {key}=? AND owner_chat_id=?',(scope.id,owner))
        if not row: raise WebhookError('scope_not_found',status=404)
        if config.target.mode=='fixedConversation':
            target=await one(conn,'SELECT * FROM web_conversations WHERE conversation_uuid=? AND owner_chat_id=?',(config.target.conversation_id,owner))
            if not target or target['archived_at']: raise WebhookError('target_unavailable')
            if not await self.target_in_scope(conn,owner,scope,target): raise WebhookError('target_outside_scope')
        elif any(v is not None for v in config.target.run_config.wire().values()):
            await self.target_run_defaults(owner,scope.id,config)
        from app.webhooks.scripts import resolve_environment
        for s in (config.pre, config.post):
            if s.enabled: resolve_environment(s,self.config.scripts)
        from app.webhooks.templates import validate_template, validate_result_schema
        validate_template(config.processing.event_template)
        validate_result_schema(config.processing.result_schema)

    async def target_in_scope(self,conn,owner,scope,target):
        if scope.type=='conversation': return target['conversation_uuid']==scope.id
        folder=target['folder_uuid']; seen=set()
        while folder and folder not in seen:
            if folder==scope.id: return True
            seen.add(folder)
            row=await one(conn,'SELECT parent_uuid FROM web_conversation_folders WHERE folder_uuid=? AND owner_chat_id=?',(folder,owner))
            if not row: break
            folder=row['parent_uuid']
        return False

    async def target_run_defaults(self,owner,folder_id,config):
        overrides={k:v for k,v in config.target.run_config.wire().items() if v is not None}
        if self.host is None:
            if overrides: raise WebhookError('runtime_unavailable',status=503)
            return {}
        h=self.host
        folder=await h._tree_folder_run_defaults(owner,folder_id)
        _,defaults=await h._web_run_defaults_candidate(owner)
        # Overlay per field, then normalize against the final model using the
        # same host rules as folder/new-conversation defaults. False is explicit.
        effective=h._apply_folder_run_defaults(defaults,{**folder,**overrides})
        if overrides:
            validated,error=h._validate_web_defaults_patch(overrides,effective)
            if error: raise WebhookError(error[0],status=error[1],details={'field':'target.runConfig'})
            effective=h._normalize_web_run_defaults({**h._web_defaults_storage(effective),**validated})
        return effective

    async def operation(self,conn,owner,request,action,endpoint_id=None):
        rid=request.get('requestId')
        if not isinstance(rid,str) or not 1<=len(rid)<=128: raise WebhookError('request_id_required')
        fp=digest({'action':action,'endpointId':endpoint_id,'request':request})
        old=await one(conn,'SELECT * FROM webhook_control_operations WHERE owner_chat_id=? AND request_id=?',(owner,rid))
        if old and old['request_sha256']!=fp: raise WebhookError('request_id_conflict',status=409)
        return old,fp

    async def record_operation(self,conn,owner,request,action,endpoint_id,fp,result):
        await insert(conn,'webhook_control_operations',operation_id=uid(),owner_chat_id=owner,request_id=request['requestId'],endpoint_id=endpoint_id,
                     action=action,request_sha256=fp,actor_ref='owner:'+str(owner),reason=str(request.get('reason') or action),
                     expected_version=request.get('expectedRevision',request.get('expectedControlRevision')),selection_json=dumps(request),result_json=dumps(result),created_at_ms=self.clock())

    def credential_aad(self, endpoint_id, credential_id):
        return dumps(['openbear:webhooks:credential:v1', endpoint_id, credential_id]).encode()

    async def issue(self,conn,endpoint_id):
        key='wh_'+secrets.token_urlsafe(32)
        credential_id=uid(); nonce=secrets.token_bytes(12); now=self.clock()
        encrypted=b'\x01'+nonce+self._key_cipher.encrypt(nonce,key.encode(),self.credential_aad(endpoint_id,credential_id))
        await insert(conn,'webhook_credentials',credential_id=credential_id,endpoint_id=endpoint_id,key_prefix=secrets.token_hex(4),verifier=self.verifier(key),
                     verifier_algorithm='hmac-sha256',pepper_version='1',created_at_ms=now,valid_from_ms=now,encrypted_key=encrypted)
        return {'key':key,'recoverable':True,'displayOnce':False}

    async def current_credential(self,conn,eid):
        # rowid breaks same-millisecond issuance ties in actual insertion order,
        # not random UUID order. Never return an expired/revoked/future key.
        now=self.clock()
        return await one(conn,'''SELECT * FROM webhook_credentials WHERE endpoint_id=?
            AND revoked_at_ms IS NULL AND valid_from_ms<=? AND (expires_at_ms IS NULL OR expires_at_ms>?)
            ORDER BY created_at_ms DESC,rowid DESC LIMIT 1''',(eid,now,now))

    async def web_credential(self,owner,eid):
        """Owner Web UI only: never include this projection in model/list/audit data."""
        async with self.db.webhook_transaction() as conn:
            await self.endpoint_row(conn,eid,owner)
            row=await self.current_credential(conn,eid)
            if not row:
                return {'credential':{'hasCredential':False,'recoverable':False,'key':None,'unavailableReason':'no_active_credential'}}
            if row['encrypted_key'] is None:
                return {'credential':{'hasCredential':True,'recoverable':False,'key':None,'unavailableReason':'legacy_not_recoverable'}}
            try:
                encrypted=bytes(row['encrypted_key'])
                if encrypted[:1]!=b'\x01': raise ValueError('unsupported credential envelope')
                key=self._key_cipher.decrypt(encrypted[1:13],encrypted[13:],self.credential_aad(eid,row['credential_id'])).decode()
                if not hmac.compare_digest(self.verifier(key),bytes(row['verifier'])): raise ValueError('credential mismatch')
            except (InvalidTag,ValueError,UnicodeError):
                # Never return cipher text or crypto exception details to the UI.
                raise WebhookError('credential_unavailable',status=503) from None
            return {'credential':{'hasCredential':True,'recoverable':True,'key':key,'unavailableReason':None}}

    async def save_revision(self,conn,endpoint_id,revision,config,request,owner):
        data=config.wire(); data.update(name=str(request.get('name') or ''),description=str(request.get('description') or ''))
        await insert(conn,'webhook_revisions',endpoint_id=endpoint_id,revision=revision,target_mode=config.target.mode,target_conversation_uuid=config.target.conversation_id,
                     config_json=dumps(data),config_sha256=digest(data),created_by='owner:'+str(owner),created_at_ms=self.clock())
        from app.webhooks.telemetry import register_definitions
        await register_definitions(self,conn,endpoint_id,config)

    async def create(self,owner,request):
        try: scope=Scope.model_validate(request.get('scope'))
        except ValueError as e: raise WebhookError('invalid_scope',str(e)) from None
        enabled=request.get('enabled',False)
        if type(enabled) is not bool: raise WebhookError('enabled_must_be_boolean')
        config=parse_config(request.get('config'),scope,self.config,enabled=enabled)
        async with self.db.webhook_transaction() as conn:
            old,fp=await self.operation(conn,owner,request,'create')
            if old: return {'endpoint':await self.public(conn,old['endpoint_id'],owner),'keyIssued':True}
            await self.validate_target(conn,owner,scope,config,enabled=enabled)
            if await one(conn,'SELECT endpoint_id FROM webhook_endpoints WHERE owner_chat_id=? AND binding_kind=? AND binding_uuid=? AND deleted_at_ms IS NULL',(owner,scope.type,scope.id)):
                raise WebhookError('scope_has_endpoint',status=409)
            eid=uid(); now=self.clock()
            await insert(conn,'webhook_endpoints',endpoint_id=eid,owner_chat_id=owner,binding_kind=scope.type,binding_uuid=scope.id,create_request_id=request['requestId'],enabled=int(enabled),created_at_ms=now,updated_at_ms=now)
            await self.save_revision(conn,eid,1,config,request,owner)
            credential=await self.issue(conn,eid)
            result={'endpoint':await self.public(conn,eid,owner)}
            await self.record_operation(conn,owner,request,'create',eid,fp,result)
        return {**result,'credential':credential}

    def blockers(self,e,*,model=False):
        reasons=list(json.loads(e['dispatch_blockers_json']))
        if not self.config.enabled: reasons.append('globalDisabled')
        if not e['enabled']: reasons.append('disabled')
        if e['deleted_at_ms'] is not None: reasons.append('deleted')
        if e['paused']: reasons.append('manual')
        if model: reasons.extend(json.loads(e['model_blockers_json']))
        return reasons

    async def target_blockers(self,conn,e,config):
        table,key=('web_conversation_folders','folder_uuid') if e['binding_kind']=='folder' else ('web_conversations','conversation_uuid')
        bound=await one(conn,f'SELECT * FROM {table} WHERE {key}=? AND owner_chat_id=?',(e['binding_uuid'],e['owner_chat_id']))
        if not bound: return ['bindingDeleted']
        if bound.get('archived_at'): return ['targetArchived']
        if config.target.mode=='fixedConversation':
            target=await one(conn,'SELECT * FROM web_conversations WHERE conversation_uuid=? AND owner_chat_id=?',(config.target.conversation_id,e['owner_chat_id']))
            if not target: return ['targetDeleted']
            if target['archived_at']: return ['targetArchived']
            scope=Scope(type=e['binding_kind'],id=e['binding_uuid'])
            if not await self.target_in_scope(conn,e['owner_chat_id'],scope,target): return ['targetOutsideScope']
        return []

    async def stop_conversation(self,owner,conversation,reason):
        async with self.db.webhook_transaction() as conn:
            await conn.execute('INSERT INTO webhook_conversation_stops(conversation_uuid,owner_chat_id,stopped_at_ms,reason) VALUES(?,?,?,?) ON CONFLICT(conversation_uuid) DO UPDATE SET stopped_at_ms=excluded.stopped_at_ms,reason=excluded.reason',(conversation,owner,self.clock(),reason))
        self.wake.set()

    async def accept_human_conversation(self,owner,conversation):
        async with self.db.webhook_transaction() as conn:
            await conn.execute('DELETE FROM webhook_conversation_stops WHERE conversation_uuid=? AND owner_chat_id=?',(conversation,owner))
        self.wake.set()

    async def conversation_stopped(self,conn,owner,conversation):
        stopped=await one(conn,'SELECT conversation_uuid FROM webhook_conversation_stops WHERE conversation_uuid=? AND owner_chat_id=?',(conversation,owner))
        if stopped: return stopped
        return await one(conn,"""SELECT e.endpoint_id FROM webhook_endpoints e,json_each(e.model_blockers_json) b,
            json_each(b.value,'$.conversationIds') c WHERE e.owner_chat_id=? AND e.deleted_at_ms IS NULL AND json_extract(b.value,'$.source')='stop'
            AND json_extract(b.value,'$.scope')='current' AND c.value=? LIMIT 1""",(owner,conversation))

    async def public(self,conn,eid,owner=None):
        e=await self.endpoint_row(conn,eid,owner,deleted=True); r,c=await self.revision(conn,eid,e['current_revision']); meta=json.loads(r['config_json'])
        pending=await one(conn,'SELECT COUNT(*) n FROM webhook_events WHERE endpoint_id=? AND (terminal_at_ms IS NULL OR review_required=1)',(eid,))
        waits=await one(conn,"SELECT COUNT(*) n FROM webhook_waits WHERE endpoint_id=? AND state IN ('registered','collecting','sealed')",(eid,))
        credential=await self.current_credential(conn,eid)
        reasons=self.blockers(e)+await self.target_blockers(conn,e,c); model_reasons=list(json.loads(e['model_blockers_json']))
        if c.target.conversation_id and await self.conversation_stopped(conn,e['owner_chat_id'],c.target.conversation_id): model_reasons.append('conversationStopped')
        base=self.config.public_base_url or (getattr(getattr(self.host,'config',None),'web',None) and self.host.config.web.custom_url) or ''
        return dict(id=eid,scope={'type':e['binding_kind'],'id':e['binding_uuid']},name=meta.get('name',''),description=meta.get('description',''),config=c.wire(),target=c.target.wire(),
                    enabled=bool(e['enabled']),revision=e['current_revision'],controlRevision=e['control_version'],effectiveStatus='deleted' if e['deleted_at_ms'] is not None else 'globalDisabled' if not self.config.enabled else 'disabled' if not e['enabled'] else 'paused' if reasons else 'receiving',
                    pauseReasons=reasons+model_reasons,dispatchPaused=bool(reasons),modelPaused=bool(reasons or model_reasons),pendingEvents=pending['n'],activeWaits=waits['n'],url=base.rstrip('/')+'/webhook/'+eid,
                    credential={'hasCredential':bool(credential),'recoverable':bool(credential and credential['encrypted_key'] is not None),'prefix':credential['key_prefix'] if credential else None,'createdAt':iso(credential['created_at_ms']) if credential else None,'expiresAt':iso(credential['expires_at_ms']) if credential else None},
                    createdAt=iso(e['created_at_ms']),updatedAt=iso(e['updated_at_ms']))

    async def get(self,owner,eid):
        return {'endpoint':await self.public(self.db.conn,eid,owner)}

    async def list(self,owner,params):
        status=params.get('effectiveStatus') or ''
        if status not in ('','receiving','globalDisabled','disabled','paused','deleted','needs_review'):
            raise WebhookError('invalid_effective_status')
        clauses=['owner_chat_id=?']; args=[owner]
        if status!='deleted': clauses.append('deleted_at_ms IS NULL')
        for key,col in [('scopeType','binding_kind'),('scopeId','binding_uuid')]:
            if params.get(key): clauses.append(col+'=?'); args.append(params[key])
        limit=min(100,max(1,int(params.get('limit') or 50))); cursor=params.get('cursor') or ''
        search=str(params.get('search') or '').strip().casefold(); items=[]
        # Filter the entire ordered candidate stream, not a sparse page. Target
        # availability and live global controls use the same public projection.
        while len(items)<=limit:
            rows=await many(self.db.conn,'SELECT endpoint_id FROM webhook_endpoints WHERE '+' AND '.join(clauses)+' AND endpoint_id>? ORDER BY endpoint_id LIMIT 100',(*args,cursor))
            if not rows: break
            for row in rows:
                cursor=row['endpoint_id']; item=await self.public(self.db.conn,cursor,owner)
                if search and not any(search in str(item[k]).casefold() for k in ('id','name','description')): continue
                if status=='needs_review':
                    if not any(r=='review' or r.startswith('review:') for r in item['pauseReasons']): continue
                elif status and item['effectiveStatus']!=status: continue
                items.append(item)
                if len(items)>limit: break
            if len(rows)<100: break
        return {'items':items[:limit],'nextCursor':items[limit-1]['id'] if len(items)>limit else None}

    async def update(self,owner,eid,request):
        async with self.db.webhook_transaction() as conn:
            old,fp=await self.operation(conn,owner,request,'update',eid)
            if old: return json.loads(old['result_json'])
            e=await self.endpoint_row(conn,eid,owner)
            if request.get('expectedRevision')!=e['current_revision']:
                raise WebhookError('revision_conflict',status=409,currentRevision=e['current_revision'],details={'current':await self.public(conn,eid,owner)})
            enabled=request.get('enabled',bool(e['enabled']))
            if type(enabled) is not bool: raise WebhookError('enabled_must_be_boolean')
            if 'enabled' in request and request.get('expectedControlRevision')!=e['control_version']: raise WebhookError('control_conflict',status=409,currentRevision=e['control_version'])
            scope=Scope(type=e['binding_kind'],id=e['binding_uuid']); c=parse_config(request.get('config'),scope,self.config,enabled=enabled)
            await self.validate_target(conn,owner,scope,c,enabled=enabled)
            await self.save_revision(conn,eid,e['current_revision']+1,c,request,owner)
            await conn.execute('UPDATE webhook_endpoints SET current_revision=current_revision+1,enabled=?,control_version=control_version+?,updated_at_ms=? WHERE endpoint_id=?',(int(enabled),int(enabled!=bool(e['enabled'])),self.clock(),eid))
            result={'endpoint':await self.public(conn,eid,owner)}
            await self.record_operation(conn,owner,request,'update',eid,fp,result)
        self.wake.set()
        if hasattr(self.host,'_schedule_conversation_retention'): self.host._schedule_conversation_retention()
        return result

    async def authenticate(self,conn,eid,key):
        if not key: raise WebhookError('credential_required',status=401)
        e=await one(conn,'SELECT * FROM webhook_endpoints WHERE endpoint_id=?',(eid,))
        if not e or not self.config.enabled or not e['enabled'] or e['deleted_at_ms'] is not None: raise WebhookError('not_receiving',status=403)
        table,column=('web_conversation_folders','folder_uuid') if e['binding_kind']=='folder' else ('web_conversations','conversation_uuid')
        target=await one(conn,f'SELECT * FROM {table} WHERE {column}=? AND owner_chat_id=?',(e['binding_uuid'],e['owner_chat_id']))
        if not target or target.get('archived_at'): raise WebhookError('not_receiving',status=403)
        rows=await many(conn,'SELECT verifier FROM webhook_credentials WHERE endpoint_id=? AND revoked_at_ms IS NULL AND valid_from_ms<=? AND (expires_at_ms IS NULL OR expires_at_ms>?)',(eid,self.clock(),self.clock()))
        check=self.verifier(key)
        if not any(hmac.compare_digest(check,bytes(c['verifier'])) for c in rows): raise WebhookError('invalid_credential',status=403)
        return e

    async def receive(self,*args,**kwargs):
        from app.webhooks.ingress import receive
        return await receive(self,*args,**kwargs)

    async def event_material(self,conn,event_id):
        from app.webhooks.ingress import event_material
        return await event_material(conn,event_id)

    async def control(self,owner,eid,request,*,action=None):
        from app.webhooks.controls import control
        return await control(self,owner,eid,request,action=action)

    async def prepare(self,owner,eid,action,request,*,tool_approval=False):
        from app.webhooks.controls import prepare
        return await prepare(self,owner,eid,action,request,tool_approval=tool_approval)
