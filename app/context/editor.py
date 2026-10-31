"""Full-fidelity, model-free context editing and explicit protocol compilation."""
from __future__ import annotations
import copy
import hashlib
import json
import uuid
from app.agent.native_continuation import deserialize_messages, serialize_messages, validate_model_context

def clone(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def document(system, tools, messages, *, origin=None, runtime=None):
    return {"format": "openbear-context-edit/1", "revision": 1,
            "origin": clone(origin or {}), "runtime": clone(runtime or {}),
            "system": system, "tools": clone(tools),
            "entries": [{"entryId": str(uuid.uuid4()), "message": raw}
                        for raw in serialize_messages(messages)]}


def export_package(base, edited):
    """Self-contained export: comparison baseline travels with the edited document."""
    return json.dumps({"format": "openbear-context-edit-package/1", "baseline": base, "working": edited},
                      ensure_ascii=False, indent=2, allow_nan=False)


def import_package(text):
    package = json.loads(text)
    if package.get("format") != "openbear-context-edit-package/1":
        raise ValueError("unsupported context edit package")
    return package["baseline"], package["working"]


def patch(doc, op, pointer, value=None):
    """Minimal JSON-pointer add/replace/remove; all document fields remain addressable."""
    parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer.split("/")[1:]]
    if not pointer.startswith("/") or not parts:
        raise ValueError("a non-root JSON pointer is required")
    owner = doc
    for key in parts[:-1]:
        owner = owner[int(key)] if isinstance(owner, list) else owner[key]
    key = parts[-1]
    if isinstance(owner, list):
        index = len(owner) if key == "-" else int(key)
        if op == "add": owner.insert(index, clone(value))
        elif op == "replace": owner[index] = clone(value)
        elif op == "remove": owner.pop(index)
        else: raise ValueError(op)
    else:
        if op == "remove": del owner[key]
        elif op in {"add", "replace"}: owner[key] = clone(value)
        else: raise ValueError(op)
    doc["revision"] = int(doc.get("revision", 0)) + 1


def visible_native(items):
    text, calls = [], []
    for item in items:
        if item.get("type") == "message":
            for block in item.get("content", []):
                if block.get("type") in {"output_text", "text"}:
                    text.append(block.get("text", ""))
        elif item.get("type") == "function_call":
            calls.append({"id": item.get("call_id") or item.get("id", ""),
                          "name": item.get("name", ""), "arguments": item.get("arguments") or "{}"})
    return "".join(text), calls


def readable_pair(message):
    return message.get("content") or "", message.get("tool_calls") or []


def visible_claude(items):
    text, calls = [], []
    for item in items:
        if item.get('type') == 'text':
            text.append(item.get('text', ''))
        elif item.get('type') == 'tool_use':
            calls.append({'id': item.get('id', ''), 'name': item.get('name', ''),
                          'arguments': json.dumps(item.get('input') or {}, ensure_ascii=False)})
    return ''.join(text), calls


def claude_matches_neutral(native, message):
    text, calls = visible_claude(native)
    neutral_text, neutral_calls = readable_pair(message)
    if text != neutral_text or len(calls) != len(neutral_calls):
        return False
    try:
        return all(a['id'] == b['id'] and a['name'] == b['name']
                   and json.loads(a['arguments']) == json.loads(b['arguments'])
                   for a, b in zip(calls, neutral_calls))
    except (KeyError, TypeError, ValueError):
        return False


def sync_native(message):
    """Materialize edited neutral text/calls into readable native blocks.

    Unchanged fields/items are retained, including encrypted reasoning, item ids,
    status, annotations and vendor extensions. Only textual assistant content
    can be synchronized automatically.
    """
    text, calls = readable_pair(message)
    if not isinstance(text, str):
        raise ValueError("多模态助手正文不能自动同步；请在完整 JSON 中同时编辑两种表示")
    old = message["native_output_items"]
    old_text, old_calls = visible_native(old)
    text_changed, calls_changed = text != old_text, calls != old_calls
    if not text_changed and not calls_changed: return old
    by_id = {c["id"]: c for c in calls}
    emitted_calls = set()
    text_written = False
    result = []
    for raw in old:
        item = copy.deepcopy(raw)
        if item.get("type") == "message" and text_changed:
            if item.get("role", "assistant") != "assistant":
                raise ValueError("native message has non-assistant role")
            blocks = []
            for block in item.get("content", []):
                if block.get("type") in {"output_text", "text"}:
                    blocks.append({**block, "text": text if not text_written else ""})
                    text_written = True
                else: blocks.append(block)
            if blocks:
                item["content"] = blocks
                result.append(item)
        elif item.get("type") == "function_call" and calls_changed:
            call_id = item.get("call_id") or item.get("id", "")
            if call_id in by_id:
                c = by_id[call_id]
                item.update(call_id=c["id"], name=c["name"], arguments=c["arguments"])
                result.append(item)
                emitted_calls.add(call_id)
        else:
            result.append(item)
    if text_changed and text and not text_written:
        result.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})
    if calls_changed:
        # Use neutral call order, not the original function-call order.
        positions = [i for i, item in enumerate(result) if item.get("type") == "function_call"]
        existing = {(item.get("call_id") or item.get("id")): item for item in result if item.get("type") == "function_call"}
        insertion = positions[0] if positions else len(result)
        result = [item for item in result if item.get("type") != "function_call"]
        compiled_calls = []
        for c in calls:
            compiled_calls.append(existing.get(c["id"]) or {"type": "function_call", "call_id": c["id"], "name": c["name"], "arguments": c["arguments"]})
        result[insertion:insertion] = compiled_calls
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def validate_document(doc):
    if not isinstance(doc, dict) or doc.get('format') not in {'openbear-context-edit/1', 'openbear-context-edit-proof/1'}:
        raise ValueError('unsupported_document_format')
    if not isinstance(doc.get('origin', {}), dict) or not isinstance(doc.get('runtime', {}), dict):
        raise ValueError('invalid_document_metadata')
    if not isinstance(doc.get('system'), str) or not isinstance(doc.get('tools'), list) or not isinstance(doc.get('entries'), list):
        raise ValueError('invalid_document_shape')
    ids, names = set(), set()
    for tool in doc['tools']:
        if not isinstance(tool, dict) or not isinstance(tool.get('name'), str) or not tool['name'] or tool['name'] in names or not isinstance(tool.get('parameters'), dict):
            raise ValueError('invalid_or_duplicate_tool_schema')
        names.add(tool['name'])
    for entry in doc['entries']:
        if not isinstance(entry, dict) or not isinstance(entry.get('entryId'), str) or not entry['entryId'] or entry['entryId'] in ids or not isinstance(entry.get('message'), dict):
            raise ValueError('invalid_or_duplicate_entry')
        ids.add(entry['entryId'])
    clone(doc)  # Reject non-JSON/NaN without filtering unknown fields.


def compile_document(baseline, working, *, protocol, model_label='', available_tools=(), repair_reasoning=True):
    """Compile a replayable view without modifying the lossless editor document."""
    validate_document(baseline)
    validate_document(working)
    doc = clone(working)
    originals = {e['entryId']: e['message'] for e in baseline['entries']}
    issues = []
    def issue(code, text, entry='', severity='error'):
        issues.append(dict(severity=severity, code=code, text=text, entryId=entry))
    for tool in doc['tools']:
        if tool['name'] not in available_tools:
            issue('unbound_tool', '工具没有当前主控可用的真实处理器：' + tool['name'])
    pending = {}
    prefix_changed = doc['system'] != baseline['system'] or doc['tools'] != baseline['tools']
    base_ids = [e['entryId'] for e in baseline['entries']]
    origin = baseline.get('origin') or {}
    route_changed = origin.get('protocol') != protocol or bool(origin.get('modelLabel') and model_label and origin['modelLabel'] != model_label)
    cleaned, preserved, unsigned, projected, empty = [], [], [], [], set()
    thinking_types = {'reasoning', 'thinking', 'redacted_thinking'}
    for index, entry in enumerate(doc['entries']):
        eid, m = entry['entryId'], entry['message']
        old = originals.get(eid, {})
        if index >= len(base_ids) or base_ids[index] != eid or m != old:
            prefix_changed = True
        reset_reasoning = repair_reasoning and (prefix_changed or route_changed)
        role = m.get('role')
        native = m.get('native_output_items') or []
        calls = m.get('tool_calls') or []
        if role not in {'user', 'assistant', 'tool'} or not isinstance(m.get('content'), (str, list, type(None))):
            issue('message_shape', '消息角色或正文类型无效；系统提示词请在 system 中编辑。', eid)
            continue
        if not isinstance(calls, list) or any(not isinstance(c, dict) for c in calls) or not isinstance(native, list) or any(not isinstance(n, dict) for n in native):
            issue('message_shape', 'tool_calls/native_output_items 必须是对象数组。', eid)
            continue
        if any(m.get(key) is not None and not isinstance(m[key], str) for key in ('reasoning', 'signature', 'tool_call_id', 'name')):
            issue('message_shape', 'reasoning/signature/name/tool_call_id 须为字符串。', eid)
            continue
        if any((n.get('type') == 'reasoning' and not isinstance(n.get('summary', []), list)) or (n.get('type') == 'message' and not isinstance(n.get('content', []), list)) for n in native):
            issue('native_shape', '原生 summary/content 须为块数组。', eid)
            continue
        if (calls or native) and role != 'assistant':
            issue('assistant_fields', '调用和原生输出块只能属于 assistant。', eid)
        anthro = bool(native) and all(n.get('type') in {'thinking', 'redacted_thinking', 'text', 'tool_use'} for n in native)
        if anthro and any(
            (n['type'] == 'text' and not isinstance(n.get('text'), str))
            or (n['type'] == 'thinking' and (not isinstance(n.get('thinking'), str)
                or not isinstance(n.get('signature', ''), str)))
            or (n['type'] == 'redacted_thinking' and not isinstance(n.get('data'), str))
            or (n['type'] == 'tool_use' and (not isinstance(n.get('id'), str) or not n['id']
                or not isinstance(n.get('name'), str) or not n['name']
                or not isinstance(n.get('input'), dict))) for n in native
        ):
            issue('native_shape', 'Claude 原生正文、签名与工具块字段类型无效。', eid)
            continue
        complete_claude = anthro and any(n.get('type') in {'text', 'tool_use'} for n in native)
        if complete_claude and (native != (old.get('native_output_items') or []) or readable_pair(m) != readable_pair(old)):
            if native != (old.get('native_output_items') or []) and readable_pair(m) == readable_pair(old):
                m['content'], m['tool_calls'] = visible_claude(native)
            elif not claude_matches_neutral(native, m):
                issue('native_conflict', 'Claude 原生块与正文/工具调用不一致；请保持原样或同时明确编辑两种表示。', eid)
        if native and not anthro:
            try:
                readable_changed = readable_pair(m) != readable_pair(old)
                native_changed = native != (old.get('native_output_items') or [])
                if native_changed and not readable_changed:
                    m['content'], next_calls = visible_native(native)
                    m['tool_calls'] = next_calls
                elif readable_changed and not native_changed:
                    m['native_output_items'] = sync_native(m)
                elif visible_native(native) != readable_pair(m):
                    issue('native_conflict', '正文/工具调用与 Responses 原生块不一致，请明确解决冲突。', eid)
            except (ValueError, TypeError, KeyError) as exc:
                issue('native_sync', str(exc), eid)
        if native and not anthro and not reset_reasoning:
            # Responses' readable reasoning summary is another representation;
            # editing it must not leave the actual outgoing summary unchanged.
            native_reasoning = [n for n in m['native_output_items'] if n.get('type') == 'reasoning']
            old_reasoning = [n for n in old.get('native_output_items', []) if n.get('type') == 'reasoning']
            def summary_text(items):
                return ''.join(b.get('text', '') for n in items for b in n.get('summary', []) if isinstance(b, dict) and isinstance(b.get('text'), str))
            if native_reasoning:
                neutral_changed = m.get('reasoning', '') != old.get('reasoning', '')
                summary_changed = summary_text(native_reasoning) != summary_text(old_reasoning)
                if neutral_changed and summary_changed and m.get('reasoning', '') != summary_text(native_reasoning):
                    issue('reasoning_conflict', 'reasoning 与原生 reasoning.summary 同时修改且不一致。', eid)
                elif neutral_changed and not summary_changed:
                    text = m.get('reasoning') or ''
                    if not isinstance(text, str):
                        issue('reasoning_shape', 'reasoning 须为字符串。', eid)
                    else:
                        written = False
                        for n in native_reasoning:
                            for block in n.get('summary', []):
                                if isinstance(block, dict) and 'text' in block:
                                    block['text'] = text if not written else ''
                                    written = True
                        if not written and text:
                            native_reasoning[0]['summary'] = [*native_reasoning[0].get('summary', []), {'type': 'summary_text', 'text': text}]
                elif summary_changed and not neutral_changed:
                    m['reasoning'] = summary_text(native_reasoning)
                if prefix_changed or m != old:
                    issue('opaque_reasoning_unverified', 'Responses 原生推理已保留；修改可读内容不等于改写加密推理，也不能保证上游接受续接。', eid, 'warning')
        signed = bool(m.get('signature')) or any(n.get('type') in {'thinking', 'redacted_thinking'} for n in native)
        has_reasoning = signed or 'reasoning' in m or 'signature' in m or any(n.get('type') in thinking_types for n in native)
        if reset_reasoning and has_reasoning:
            m.pop('reasoning', None)
            m.pop('signature', None)
            if native:
                dropped_responses_reasoning = any(n.get('type') == 'reasoning' for n in m['native_output_items'])
                native = [n for n in m['native_output_items'] if n.get('type') not in thinking_types]
                if dropped_responses_reasoning:
                    # Under store=false the server never resolves replayed item ids,
                    # but a msg_/fc_ id without its reasoning item can be rejected.
                    # Pairing uses call_id, so a function_call keeps its id only
                    # when it has no call_id to pair by.
                    native = [{k: v for k, v in n.items() if k != 'id'}
                              if n.get('type') == 'message' or (n.get('type') == 'function_call' and n.get('call_id'))
                              else n for n in native]
                if native:
                    m['native_output_items'] = native
                else:
                    m.pop('native_output_items', None)
            cleaned.append(eid)
            signed = False
            if role == 'assistant' and not m.get('content') and not m.get('tool_calls') and not native:
                empty.add(eid)
                continue
        calls = m.get('tool_calls') or []
        if role != 'tool' and pending:
            issue('missing_tool_results', '工具结果必须在下一条非工具消息之前完整回传。', eid)
            pending = {}
        for call in calls:
            if not all(isinstance(call.get(k), str) and call[k] for k in ('id', 'name', 'arguments')):
                issue('invalid_call', '工具调用须有非空 id/name/arguments。', eid)
                continue
            try:
                if not isinstance(json.loads(call['arguments']), dict):
                    raise ValueError()
            except (ValueError, TypeError):
                issue('invalid_arguments', '工具参数须为 JSON 对象字符串。', eid)
            if call['id'] in pending:
                issue('duplicate_call', '同一工具批次不能出现重复 ID。', eid)
            pending[call['id']] = call['name']
        if role == 'tool':
            key = m.get('tool_call_id')
            if not isinstance(key, str) or key not in pending:
                issue('orphan_result', '工具结果没有本批次对应的调用。', eid)
            elif m.get('name') and m['name'] != pending[key]:
                issue('tool_name_mismatch', '调用与结果的工具名不一致。', eid)
            else:
                pending.pop(key)
        if signed:
            if prefix_changed or m != old:
                issue('signed_prefix_changed', '签名/密文依赖原始回合和历史前缀；修改后不能保证续接。请撤销修改或显式删除本条及后续思考组。', eid)
            if origin.get('protocol') != protocol or (origin.get('modelLabel') and model_label and origin['modelLabel'] != model_label):
                issue('signed_model_change', '签名思考不能保证跨模型/协议重放；请显式删除思考组后再切换。', eid)
            preserved.append(eid)
        if not native and m.get('reasoning') is not None and m.get('signature') is None:
            unsigned.append(eid)
        if native and ((anthro and protocol != 'anthropic') or (not anthro and protocol != 'responses')):
            portable = (all(n.get('type') in {'text', 'tool_use'} for n in native) if anthro else
                        all(n.get('type') in {'message', 'function_call'} and (n.get('type') != 'message' or all(b.get('type') in {'text', 'output_text'} for b in n.get('content', []))) for n in native))
            if portable:
                m.pop('native_output_items', None)
                projected.append(eid)
            else:
                issue('native_protocol_mismatch', '目标协议不接受这组原生块；请显式删除思考/原生数据或选择原协议。', eid)
        if m != old:
            prefix_changed = True
    if cleaned:
        issue('reasoning_cleanup', f'框架已自动处理 {len(cleaned)} 组受编辑或模型切换影响的历史思考、签名和密文；保留正文、工具调用及返回，原始编辑稿和聊天记录不变。', severity='warning')
    if preserved:
        issue('signature_unverified', f'{len(preserved)} 组未受改动影响的签名思考已原样保留；是否接受续接由上游验证，无需逐条处理。', severity='warning')
    if unsigned:
        issue('unsigned_reasoning', f'{len(unsigned)} 条无签名思考仅保留在文档，目标协议不保证采用。', severity='warning')
    if projected:
        issue('readable_native_projection', f'{len(projected)} 条消息已自动适配为目标协议的正文和调用；原生数据保留在原始编辑包。', severity='warning')
    if empty:
        doc['entries'] = [entry for entry in doc['entries'] if entry['entryId'] not in empty]
    if pending:
        issue('missing_tool_results', '工具批次未闭合；不会自动补造返回值。异步 Responses 是专用模式，不是普通调用的例外。')
    if not any(x['severity'] == 'error' for x in issues):
        if not validate_model_context(deserialize_messages([e['message'] for e in doc['entries']])):
            issue('invalid_context', '上下文无法恢复为完整工具批次。')
    doc['format'] = 'openbear-context-edit/1'
    return doc, issues


class EditedToolRegistry:
    """Change advertised schemas only; dispatch retains real tools and their gates."""
    def __init__(self, registry, schemas):
        self.registry = registry
        self._schemas = clone(schemas)
        self.file_state = registry.file_state

    def schemas(self, scope=None):
        return clone(self._schemas)

    async def dispatch(self, name, arguments, **kwargs):
        if name not in {t['name'] for t in self._schemas}:
            return 'error: 工具不在编辑分支的工具列表中: ' + name
        return await self.registry.dispatch(name, arguments, **kwargs)


async def branch_settings(db, conversation_uuid):
    cur = await db.conn.execute('SELECT settings_json FROM context_editor_branches WHERE conversation_uuid=?', (conversation_uuid,))
    row = await cur.fetchone()
    return json.loads(row['settings_json']) if row else None

