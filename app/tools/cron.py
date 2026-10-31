"""Main-controller Cron tool; actor identity comes from the host, not parameters."""
from pathlib import Path

from pydantic import ValidationError

from app.cron.contracts import CronError, JobConfig
from app.runtime.tool_result import ToolOutcome
from app.tools.base import current_tool_context
from app.tools.servers import automatic_turn
from app.webhooks.contracts import WebhookError, dumps
from app.webhooks.repository import one

ACTIONS = ('describe', 'list', 'get', 'create', 'update', 'delete', 'set_enabled', 'run', 'stop', 'runs', 'statistics', 'preview')
GUIDANCE = (Path(__file__).resolve().parents[2] / 'prompts' / 'guides' / 'cron.md').read_text(encoding='utf-8')


async def dispatch(s, action, params, ctx):
    if action not in ACTIONS or not isinstance(params, dict): raise CronError('invalid_action')
    if ctx.agent_session_uuid: raise CronError('main_controller_only', status=403)
    if any(k in params for k in ('owner', 'ownerId', 'ownerChatId', 'trusted', 'confirmed', 'confirmationToken')):
        raise CronError('host_identity_not_a_parameter', status=403)
    conversation = await one(s.db.conn, 'SELECT owner_chat_id FROM web_conversations WHERE conversation_uuid=? AND internal_chat_id=?', (ctx.conversation_uuid, ctx.chat_id))
    if not conversation: raise CronError('host_identity_required', status=403)
    owner = conversation['owner_chat_id']
    if action == 'describe':
        return {'actions': list(ACTIONS), 'guidance': GUIDANCE, 'configSchema': JobConfig.model_json_schema(by_alias=True),
            'contracts': {
                'list': {'folderId': 'optional; omitted = all directories', 'folders': 'true returns available directories', 'limit': '1..100', 'offset': '0-based'},
                'get': {'jobId': 'required'},
                'create': {'folderId': 'required', 'name': 'required', 'description': 'optional', 'enabled': 'default false', 'config': 'configSchema', 'requestId': 'stable operation ID'},
                'update': {'jobId': 'required', 'name': 'name', 'description': 'description', 'enabled': 'boolean', 'config': 'complete config, get first', 'expectedRevision': 'from get', 'requestId': 'stable ID'},
                'set_enabled': {'jobId': 'required', 'enabled': 'boolean; future schedule only', 'expectedRevision': 'from get', 'requestId': 'stable ID'},
                'run': {'jobId': 'required; immediate new conversation, schedule unchanged', 'expectedRevision': 'from get', 'requestId': 'stable ID'},
                'stop': {'runId': 'required; existing conversation stop, does not disable plan', 'requestId': 'stable ID'},
                'delete': {'jobId': 'required', 'expectedRevision': 'from get', 'requestId': 'stable ID; real user gate, runs/conversations retained'},
                'runs': {'runId': 'optional exact detail', 'jobId': 'optional', 'folderId': 'optional', 'limit': '1..100', 'offset': '0-based'},
                'statistics': {'jobId': 'optional', 'folderId': 'optional', 'start': 'optional ISO', 'end': 'optional ISO'},
                'preview': {'schedule': 'configSchema.schedule', 'count': 'default 5'},
            }}
    if action == 'list': return await s.folders(owner) if params.get('folders') else await s.list(owner, params)
    if action == 'get': return await s.get(owner, params.get('jobId'))
    if action == 'runs': return await s.get_run(owner, params['runId']) if params.get('runId') else await s.runs(owner, params)
    if action == 'statistics': return await s.statistics(owner, params)
    if action == 'preview': return s.preview(params)
    job_id = params.get('jobId')
    if action in ('update', 'delete', 'set_enabled', 'run') and (not isinstance(job_id, str) or not job_id.strip()):
        raise CronError('job_id_required')
    body = {k: v for k, v in params.items() if k not in ('jobId', 'runId')}
    if action in ('create', 'update', 'set_enabled', 'run') and await automatic_turn(s.db.conn, ctx):
        # An unattended run never grants a task the right to change servers and instances, and never rewrites, enables
        # or starts a task that has it: that would run whatever it wrote with the permission. Disabling stays allowed.
        disabling = action == 'set_enabled' and body.get('enabled') is False
        if action != 'create' and not disabling and (await s.get(owner, job_id))['job']['config'].get('allowServerChanges'):
            raise CronError('automatic_privileged_task_forbidden', '自动执行的对话不能修改、启用或运行已允许改动服务器和实例的定时任务', status=403)
        if action in ('create', 'update') and grants_server_changes(body.get('config')):
            raise CronError('automatic_permission_forbidden', '自动执行的对话不能开启“允许改动服务器和实例”', status=403)
    if action == 'create': return await s.save(owner, body)
    if action == 'update': return await s.save(owner, body, job_id)
    if action == 'set_enabled': return await s.set_enabled(owner, job_id, body)
    if action == 'run': return await s.run(owner, job_id, body)
    if action == 'stop': return await s.stop(owner, params.get('runId'), body)
    if action == 'delete':
        previous = await s.tool_delete_result(owner, job_id, body)
        if previous is not None:
            return previous
        if ctx.web_confirm is None: raise CronError('user_confirmation_unavailable', status=403)
        prepared = await s.delete_impact(owner, job_id, body, tool_approval=True)
        answer = await ctx.web_confirm({'_sourceTool': 'Cron', '_requiresAuthorization': True,
            '_approvalScope': prepared['approvalScope'], '_approvalConstraints': prepared['approvalConstraints'],
            'title': '删除定时任务', 'body': '停用并删除定义；保留执行记录及会话，当前执行继续。\n'+dumps(prepared['impact']),
            'type': 'warning', 'confirmText': '删除此任务', 'cancelText': '取消', 'timeoutSeconds': 300})
        if not answer.get('confirmed') or answer.get('text') or answer.get('feedback'):
            return {'cancelled': True, 'userFeedback': answer}
        return await s.delete(owner, job_id, {'confirmationToken': prepared['confirmationToken'], 'requestId': body.get('requestId')}, tool_request=body)
    raise CronError('invalid_action')


def grants_server_changes(config):
    return isinstance(config, dict) and (config.get('allowServerChanges') is True or config.get('allow_server_changes') is True)


def register_cron_tool(registry, service):
    async def handler(args):
        try:
            return dumps(await dispatch(service, args.get('action'), args.get('params', {}), current_tool_context()))
        except (CronError, WebhookError) as exc:
            return ToolOutcome(dumps(exc.payload), 'failed', 'not_started')
        except (ValidationError, ValueError, TypeError) as exc:
            return ToolOutcome(dumps({'code': 'invalid_parameters', 'message': str(exc)}), 'failed', 'not_started')
    registry.add('Cron', 'Manage authorized directory schedules. Each run starts a new conversation; no catch-up or queue. Call describe before create/update; it returns the contract and design guidance. set_enabled controls future scheduling; run starts once; stop reuses conversation stop. Owner identity is host-supplied. Amended delete receipts keep the same job and revision; existing runs may finish, but new/replacement active runs require fresh confirmation.',
        {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': list(ACTIONS)}, 'params': {'type': 'object'}}, 'required': ['action'], 'additionalProperties': False},
        handler, visibility={'main'}, effect_scope='scheduled_task')
