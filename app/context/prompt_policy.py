"""Conversation prompt policies: local text owns the complete inherited policy."""
from __future__ import annotations

import copy
import json

FLAGS = ('overrideSystemPrompt', 'toolsEnabled', 'userMessageTemplateEnabled')
FIELDS = frozenset(('text', 'toolNames', *FLAGS))


def policy(value=None, *, text=None, strict=False):
    value = {} if value is None else value
    if not isinstance(value, dict):
        raise ValueError('提示词配置必须是对象')
    result = {'text': value.get('text', '') if text is None else text,
              **{name: value.get(name, False) for name in FLAGS}, 'toolNames': value.get('toolNames', [])}
    if not isinstance(result['text'], str) or len(result['text']) > 200_000:
        raise ValueError('提示词必须是文本且不超过200000字符')
    if any(type(result[name]) is not bool for name in FLAGS):
        raise ValueError('提示词开关必须是布尔值')
    names = result['toolNames']
    if not isinstance(names, list) or len(names) > 500 or any(not isinstance(n, str) or not n or len(n) > 256 for n in names):
        raise ValueError('工具列表必须是工具名称数组')
    result['toolNames'] = list(dict.fromkeys(names))
    if strict and set(value) - FIELDS:
        raise ValueError('未知提示词配置字段')
    return result


def conversation_policy(value=None):
    value = value or {}
    # Legacy inherit mode may retain an inactive draft: it must not become live.
    text = value.get('contextText', '')
    if value.get('contextMode') == 'inherit':
        text = ''
    return policy({key: value[key] for key in (*FLAGS, 'toolNames') if key in value}, text=text)


def folder_policy(folder_id, folders):
    current, seen = str(folder_id or '__temporary'), set()
    while current and current not in seen:
        seen.add(current)
        row = folders.get(current)
        if not row:
            break
        value = policy(row.get('prompt_policy'), text=row.get('prompt_markdown') or '')
        if value['text'].strip():
            return value, current
        current = str(row.get('parent_uuid') or '')
    return policy(), ''


async def read_json(conn, key, default=None):
    cursor = await conn.execute('SELECT value FROM app_state WHERE key=?', (key,))
    row = await cursor.fetchone()
    return json.loads(row['value']) if row else default


async def write_json(conn, key, value):
    await conn.execute('INSERT INTO app_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       (key, json.dumps(value, ensure_ascii=False)))


def snapshot_key(conversation_uuid):
    return 'conversation_prompt_snapshot:' + conversation_uuid


class PromptToolRegistry:
    """Filter both advertisement and dispatch; real tool handlers own their gates."""
    def __init__(self, registry, names):
        self.registry = registry
        self.allowed = frozenset(names)
        self.file_state = getattr(registry, 'file_state', None)

    def schemas(self, scope=None):
        from app.tools.agent_actions import CONTROLLER, phase_schemas
        out = []
        for schema in self.registry.schemas(scope=scope):
            name = schema.get('name', schema.get('function', {}).get('name'))
            if name == 'Agent' and 'Agent' in self.allowed:
                # The merged tool itself, as today's selection stores it: every action.
                out.append(schema)
            elif name == 'Agent' and 'action' in schema.get('parameters', {}).get('properties', {}):
                # The earlier tools' names select their own actions of the merged tool.
                offered = schema['parameters']['properties']['action'].get('enum', [])
                allowed = {CONTROLLER[a] for a in offered if a in CONTROLLER} & self.allowed
                for narrowed in phase_schemas([schema], allowed):
                    narrowed = copy.deepcopy(narrowed)
                    if 'Agent' not in allowed:
                        narrowed['parameters']['required'] = list(dict.fromkeys([
                            *narrowed['parameters'].get('required', []), 'action']))
                    out.append(narrowed)
            elif name in self.allowed:
                out.append(schema)
        return out

    async def dispatch(self, name, arguments, **kwargs):
        from app.tools.agent_actions import CONTROLLER
        error = ''
        if name == 'Agent' and 'Agent' not in self.allowed:
            # Fail closed before the registry's tolerant JSON repair: the bytes
            # checked here must select exactly the action that gets dispatched.
            try:
                args = json.loads(arguments.strip() or '{}') if isinstance(arguments, str) else None
            except (TypeError, ValueError):
                args = None
            action = str(args.get('action') or 'start') if isinstance(args, dict) else ''
            if CONTROLLER.get(action) not in self.allowed:
                actions = [a for a, target in CONTROLLER.items() if target in self.allowed]
                error = f'error: Agent action 不在当前会话允许列表中: {action or "invalid_arguments"}; allowed actions: {actions}'
        elif name not in self.allowed:
            error = 'error: 工具不在当前会话允许列表中: ' + name
        if error:
            from app.runtime.tool_result import ToolOutcome
            from app.tools.base import current_tool_context
            context = kwargs.get('context') or current_tool_context()
            result = ToolOutcome(error, 'failed', 'not_started')
            context.tool_outcome = result
            return result.content
        return await self.registry.dispatch(name, arguments, **kwargs)


def policy_tool_names(value):
    return value['toolNames'] if value['toolsEnabled'] else []


def filter_prompt_tools(params, value):
    if not value['overrideSystemPrompt']:
        return params
    out = copy.deepcopy(params)
    allowed = set(policy_tool_names(value))
    from app.tools.agent_actions import CONTROLLER
    if allowed.intersection(CONTROLLER.values()):
        allowed.add('Agent')
    for name in ('toolNames', 'builtinToolNames', 'mcpToolNames'):
        out[name] = [n for n in out.get(name, []) if n in allowed]
    for name in ('toolSummaries', 'builtinToolSummaries', 'mcpToolSummaries'):
        out[name] = {n: d for n, d in out.get(name, {}).items() if n in allowed}
    # Rebuild derived groups, so templates cannot advertise unselected tools.
    from app.context.builder import build_system_prompt_params, _mcp_tool_groups
    servers = {group['server'] for group in _mcp_tool_groups(out['mcpToolNames'])}
    instructions = [item for item in out.get('mcpServerInstructions', []) if item.get('server') in servers]
    filtered = build_system_prompt_params(tool_names=out['toolNames'], tool_summaries=out['toolSummaries'],
        builtin_tool_names=out['builtinToolNames'], builtin_tool_summaries=out['builtinToolSummaries'],
        mcp_tool_names=out['mcpToolNames'], mcp_tool_summaries=out['mcpToolSummaries'],
        mcp_server_instructions=instructions)
    for key in ('tools', 'mcpToolGroups', 'mcpServerInstructions'):
        out[key] = filtered[key]
    return out
