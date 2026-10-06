"""Private full-context editor API. Preview never invokes an upstream model."""
from __future__ import annotations

import json
import uuid

from aiohttp import web

from app.agent.native_continuation import deserialize_messages, serialize_messages, validate_model_context
from app.context.editor import branch_settings, clone, compile_document, digest, document, validate_document
from app.context.configuration import conversation_strategy
from app.context.prompts import effective_context_prompt
from app.context.runtime import ContextManager
from app.context.store import ContextOwner, WindowStore
from app.context.window import WindowPolicy
from app.config import fast_request_mode
from app.db.dao import MessageDAO
from app.db.engine import now_ts
from app.references import BUNDLE_FIELD
from app.task_memory import TaskMemoryDAO, reconcile_task_memory_runtime_state, task_memory_runtime_epoch
from app.web_console.core import _WEB_SESSION_KEY


def response(data, status=200):
    return web.json_response(data, status=status, headers={'Cache-Control': 'no-store'})


class WebAdminContextEditorMixin:
    async def _editor_body(self, request):
        # Complete baseline + draft can be much larger than aiohttp's 1 MiB
        # default. This bounded endpoint reports 413, never truncates a document.
        try:
            data = await request.clone(client_max_size=64 * 1024 * 1024).json()
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest(text='invalid_editor_json') from None
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text='invalid_editor_request')
        return data

    async def _editor_busy(self, row):
        return bool(self._web_starting_turns.get(row['conversation_uuid']) or await self._web_conversation_has_active_runtime(row))

    async def _editor_snapshot(self, row):
        dao = MessageDAO(self.db)
        chat, conv = int(row['internal_chat_id']), row['conversation_uuid']
        label = row['model'] or self.config.models.primary
        resolved = self.config.models.resolve(label)
        if not resolved:
            raise ValueError('model_not_found')
        provider, model = resolved
        settings = await branch_settings(self.db, conv)
        system = await dao.get_system_snapshot(chat)
        if settings is None:
            system = system or await self._build_system_prompt_for_chat(conversation_uuid=conv)
            system = effective_context_prompt(system, await conversation_strategy(self.db, conv, self.config.context_management.default_strategy))
        else:
            system = settings['system']
        sid = await dao.get_or_create_session_uuid(chat)
        anchor = await dao._controller_context_anchor(chat)
        # Read only: unlike runtime loading, opening an editor must not delete a
        # mismatched checkpoint or mutate a window selection.
        cur = await self.db.conn.execute('SELECT * FROM controller_model_contexts WHERE chat_id=?', (chat,))
        private = await cur.fetchone()
        history = None
        if private:
            state = json.loads(private['state_json'])
            identity = (private['conversation_uuid'], private['session_id'], private['protocol'], private['model'], private['model_label'])
            if identity == (conv, sid, provider.protocol, model.id, label) and state.get('anchor') == anchor and validate_model_context(state.get('messages') or []):
                history = deserialize_messages(state['messages'])
        if history is None:
            history = await self._build_history(chat)
        if settings is None:
            history = await reconcile_task_memory_runtime_state(history, TaskMemoryDAO(self.db), conversation_uuid=conv, epoch=task_memory_runtime_epoch(history))
            history = await self._reference_store().overlay(history, conversation_uuid=conv)
        tools = settings['tools'] if settings else (self.tools.schemas(scope='main') if self.tools else [])
        doc = document(system, tools, history, origin={'protocol': provider.protocol, 'model': model.id, 'modelLabel': label}, runtime={'frozen': True, 'sourceConversationUuid': conv, 'sessionId': sid, 'thinkingLevel': await self._effective_thinking_level(chat, label), 'fastMode': await dao.get_fast_mode(chat), 'maxTokens': model.max_tokens})
        for index, entry in enumerate(doc['entries']):
            entry['entryId'] = f'entry-{index}'
        # Runtime snapshot provenance can acquire a fresh UUID on each read;
        # compare its substantive bytes, not that transient allocation.
        stable = clone(doc)
        for entry in stable['entries']:
            meta = entry['message'].get('openbear_context_source') or {}
            if meta.get('kind') == 'runtime':
                meta.pop('id', None)
        token = digest({'document': stable, 'anchor': anchor, 'privateRevision': private['revision'] if private else 0})
        return doc, token

    async def handle_api_context_editor(self, request):
        row = await self._conversation_from_request(request)
        chat = int(row['internal_chat_id'])
        async with self.operation_locks.try_chat(chat, 'context_editor_read') as acquired:
            if not acquired or await self._editor_busy(row):
                return response({'ok': False, 'error': 'busy'}, 409)
            try:
                baseline, token = await self._editor_snapshot(row)
            except ValueError as exc:
                return response({'ok': False, 'error': str(exc)}, 400)
            cur = await self.db.conn.execute('SELECT * FROM context_editor_drafts WHERE conversation_uuid=?', (row['conversation_uuid'],))
            saved = await cur.fetchone()
            draft = {**json.loads(saved['document_json']), 'revision': saved['revision']} if saved else None
            models = [{'label': f'{name}/{m.id}', 'model': f'{name}/{m.id}', 'modelId': m.id, 'protocol': p.protocol} for name, p in self.config.models.providers.items() for m in p.models]
            return response(dict(ok=True, baseline=baseline, snapshotToken=token, draft=draft, models=models, targetModel=baseline['origin']['modelLabel']))

    async def _editor_compile(self, row, body):
        label = body.get('targetModel') or row['model'] or self.config.models.primary
        if not isinstance(label, str) or not self.config.models.resolve(label):
            raise ValueError('model_not_found')
        if body.get('mode', 'compatible') not in {'compatible', 'raw'}:
            raise ValueError('invalid_editor_mode')
        backend, model, max_tokens = self.llm_factory.backend_for(label)
        protocol = self.config.models.resolve(label)[0].protocol
        doc, issues = compile_document(body.get('baseline'), body.get('working'), protocol=protocol, model_label=label,
                                       available_tools=self.tools.names(scope='main') if self.tools else [])
        payload = None
        if not any(x['severity'] == 'error' for x in issues):
            dao = MessageDAO(self.db)
            chat = int(row['internal_chat_id'])
            options = {'think_level': await self._effective_thinking_level(chat, label),
                       'service_tier': '', 'fast_request': {'body': {}, 'headers': {}},
                       'native_continuation': protocol == 'responses'}
            provider, definition = self.config.models.resolve(label)
            if await dao.get_fast_mode(chat) and definition.supports_fast:
                if definition.fast_request is not None:
                    options['fast_request'] = definition.fast_request.model_dump(mode='json')
                else:
                    options['service_tier'] = fast_request_mode(provider, definition)
            # Preview the exact existing serializer, not a UI approximation.
            payload = backend.build_payload(deserialize_messages([e['message'] for e in doc['entries']]), model=model,
                                            system=doc['system'], tools=doc['tools'], max_tokens=max_tokens, stream=True,
                                            **options)
        return doc, issues, payload, label, backend, model

    async def handle_api_context_editor_preview(self, request):
        row = await self._conversation_from_request(request)
        body = await self._editor_body(request)
        try:
            doc, issues, payload, *_ = await self._editor_compile(row, body)
        except (ValueError, TypeError, KeyError) as exc:
            return response({'ok': False, 'error': str(exc)}, 400)
        return response(dict(ok=True, document=doc, issues=issues, payload=payload))

    async def handle_api_context_editor_draft(self, request):
        row = await self._conversation_from_request(request)
        body = await self._editor_body(request)
        try:
            validate_document(body.get('baseline'))
            validate_document(body.get('working'))
            revision = body.get('revision', 0)
            if type(revision) is not int or revision < 0:
                raise ValueError('invalid_revision')
            if body.get('mode') not in {'raw', 'compatible'} or not isinstance(body.get('snapshotToken'), str):
                raise ValueError('invalid_draft')
            draft = {k: body.get(k) for k in ('baseline', 'working', 'snapshotToken', 'mode', 'targetModel')}
            encoded = json.dumps(draft, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError) as exc:
            return response({'ok': False, 'error': str(exc)}, 400)
        async with self.db.conn.transaction(label='context-editor-draft') as conn:
            cur = await conn.execute('SELECT revision FROM context_editor_drafts WHERE conversation_uuid=?', (row['conversation_uuid'],))
            old = await cur.fetchone()
            if (old['revision'] if old else 0) != revision:
                return response({'ok': False, 'error': 'draft_conflict'}, 409)
            await conn.execute('INSERT INTO context_editor_drafts VALUES(?,?,?,?) ON CONFLICT(conversation_uuid) DO UPDATE SET revision=excluded.revision,document_json=excluded.document_json,updated_at=excluded.updated_at',
                               (row['conversation_uuid'], revision + 1, encoded, now_ts()))
        return response({'ok': True, 'draft': {**draft, 'revision': revision + 1}})

    async def handle_api_context_editor_branch(self, request):
        row = await self._conversation_from_request(request)
        body = await self._editor_body(request)
        if body.get('mode', 'compatible') != 'compatible':
            return response({'ok': False, 'error': 'raw_draft_not_executable'}, 400)
        async with self.operation_locks.try_chat(int(row['internal_chat_id']), 'context_editor_branch') as acquired:
            if not acquired or await self._editor_busy(row):
                return response({'ok': False, 'error': 'busy'}, 409)
            row = await self._conversation_row(request[_WEB_SESSION_KEY].chat_id, row['conversation_uuid'], require=True)
            try:
                _, token = await self._editor_snapshot(row)
                if body.get('snapshotToken') != token:
                    return response({'ok': False, 'error': 'context_source_changed'}, 409)
                doc, issues, _, label, backend, model = await self._editor_compile(row, body)
                if any(x['severity'] == 'error' for x in issues):
                    return response({'ok': False, 'error': 'context_validation_failed', 'issues': issues}, 400)
                title = body.get('title') or f"{row['title']} · 上下文分支"
                if not isinstance(title, str):
                    raise ValueError('invalid_title')
                async with self._web_conversation_create_lock:
                    async with self.db.conn.transaction(label='context-editor-create-branch') as conn:
                        branch = await self._create_web_conversation(row['owner_chat_id'], title=title, model=label,
                            run_config={'main_model': label, 'main_thinking_level': await MessageDAO(self.db).get_thinking_level(int(row['internal_chat_id'])),
                                        'main_fast_mode': await MessageDAO(self.db).get_fast_mode(int(row['internal_chat_id'])),
                                        'agent_model': row.get('agent_model', ''), 'agent_think_level': row.get('agent_think_level', ''),
                                        'agent_fast_mode': row.get('agent_fast_mode', -1),
                                        'context_strategy': row.get('context_strategy', 'sliding_window')},
                            folder_uuid=str(row.get('folder_uuid') or ''), create_lock_held=True)
                        bid, chat = branch['conversation_uuid'], int(branch['internal_chat_id'])
                        # A user-chosen branch title or inherited manual title
                        # keeps the same protection as a renamed conversation.
                        branch['title_manual'] = 1 if body.get('title') or int(row.get('title_manual') or 0) else 0
                        await conn.execute('UPDATE web_conversations SET title_manual=? WHERE conversation_uuid=?', (branch['title_manual'], bid))
                        dao = MessageDAO(self.db)
                        sid = await dao.get_or_create_session_uuid(chat)
                        await conn.execute('UPDATE sessions SET system_snapshot=? WHERE chat_id=?', (doc['system'], chat))
                        settings = {'system': doc['system'], 'tools': doc['tools'], 'frozen': True}
                        await conn.execute('INSERT INTO context_editor_branches VALUES(?,?,?,?,?)', (bid, row['conversation_uuid'], json.dumps(settings, ensure_ascii=False), json.dumps({'format': 'openbear-context-edit-package/1', 'baseline': body['baseline'], 'working': body['working'], 'compiled': doc, 'editor': {'mode': 'compatible', 'targetModel': label}}, ensure_ascii=False), now_ts()))
                        history = deserialize_messages([e['message'] for e in doc['entries']])
                        turn = str(uuid.uuid4())
                        tool_ops = {}
                        for m in history:
                            if m['role'] == 'user':
                                turn = str(uuid.uuid4())
                            content = m.get('content') or ''
                            visible = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                            mid = await self._persist_web_transcript_message(dao, chat, m['role'], visible,
                                conversation_uuid=bid, turn_uuid=turn, run_root_turn_uuid=turn,
                                reasoning=m.get('reasoning') or '', signature=m.get('signature') or '',
                                tool_calls=m.get('tool_calls'), tool_call_id=m.get('tool_call_id', ''), name=m.get('name', ''))
                            op_ids = []
                            if m['role'] in {'user', 'assistant'} and (visible or m['role'] == 'user'):
                                op_id = f'edit-message:{mid}'
                                await self._publish_operation(bid, internal_chat_id=chat, op_id=op_id,
                                    op_type='user_message' if m['role'] == 'user' else 'assistant_message',
                                    action='end', turn_uuid=turn, run_root_turn_uuid=turn, lifecycle='terminal', status='completed',
                                    payload={'role': m['role'], 'text': visible, 'contextEdited': True}, conn=conn)
                                op_ids.append(op_id)
                            if m.get('reasoning'):
                                op_id = f'edit-reasoning:{mid}'
                                await self._publish_operation(bid, internal_chat_id=chat, op_id=op_id, op_type='reasoning',
                                    action='end', turn_uuid=turn, run_root_turn_uuid=turn, lifecycle='terminal', status='completed',
                                    payload={'text': m['reasoning'], 'contextEdited': True}, conn=conn)
                                op_ids.append(op_id)
                            for call in m.get('tool_calls') or []:
                                op_id = f'edit-tool:{mid}:{call.id}'
                                tool_ops[call.id] = op_id
                                await self._publish_operation(bid, internal_chat_id=chat, op_id=op_id, op_type='tool',
                                    action='start', turn_uuid=turn, run_root_turn_uuid=turn, lifecycle='active', status='running',
                                    payload={'toolCallId': call.id, 'name': call.name, 'arguments': call.arguments, 'contextEdited': True}, conn=conn)
                                op_ids.append(op_id)
                            if m['role'] == 'tool':
                                op_id = tool_ops.pop(m['tool_call_id'])
                                await self._publish_operation(bid, internal_chat_id=chat, op_id=op_id, op_type='tool',
                                    action='end', turn_uuid=turn, run_root_turn_uuid=turn, lifecycle='terminal', status='completed',
                                    payload={'resultText': visible, 'contextEdited': True}, conn=conn)
                                op_ids.append(op_id)
                            await self._attach_transcript_message_ids_tx(conn, bid, message_id=mid, target_op_ids=op_ids)
                            prior = m.get('openbear_context_source') or {}
                            m['openbear_context_source'] = {**prior, 'id': f'message:{mid}', 'derived_from': prior.get('id', ''),
                                'kind': 'human' if m['role'] == 'user' else 'execution', 'message_id': mid, 'reference_only': False,
                                'turn_uuid': turn, 'run_root_turn_uuid': turn, 'task_uuid': ''}
                            # Contents have already been materialized in the editor.
                            m.pop(BUNDLE_FIELD, None)
                        store = WindowStore(self.db, ContextOwner.controller(chat_id=chat, session_uuid=sid, conversation_uuid=bid))
                        manager = ContextManager(store, WindowPolicy(self.llm_factory.context_window(label)), backend=backend, model=model, model_label=label)
                        await manager.checkpoint(history)
                        await dao.save_controller_model_context(chat, conversation_uuid=bid, session_id=sid,
                            protocol=self.config.models.resolve(label)[0].protocol, model=model, model_label=label,
                            state={'messages': serialize_messages(history)})
            except (ValueError, TypeError, KeyError) as exc:
                return response({'ok': False, 'error': str(exc)}, 400)
            return response({'ok': True, 'conversation': self._web_conversation_json(branch, live=self._live_for(branch)), 'issues': issues})
