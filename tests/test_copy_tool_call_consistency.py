"""Copy carriers must agree before a partial checkpoint can be consumed."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from app.agent.native_continuation import serialize_messages
from app.context.window import mark_source, neutral_context, source_of
from app.llm.events import ToolCall
from app.rath.schemas import RathAgentDef
from tests.test_conversation_copy_context import copied, runner_for
from tests.test_rath_web_api import web_env as shared_web_env

web_env = shared_web_env


@pytest.mark.parametrize("partial", [False, True])
async def test_copy_tool_arguments_agree_in_archive_window_and_checkpoint(web_env, partial):
    env = web_env
    await env.server.global_realtime.close()
    dao = env.server.rath_dao
    source = await env.server._create_web_conversation(123, title="args copy")
    conv, chat = source["conversation_uuid"], source["internal_chat_id"]
    workflow = await dao.workflow_by_slug("single-agent")
    instance = await dao.create_agent_instance(openbear_session_uuid=conv, chat_id=chat,
        workflow_uuid=workflow.workflow_uuid, agent_key="copy-agent")
    tid = await dao.create_task(chat_id=chat, workflow_uuid=workflow.workflow_uuid, parent_session_uuid=conv,
        title="partial", agent_session_uuid=instance.session_uuid)
    agent = RathAgentDef(workflow_uuid=workflow.workflow_uuid, agent_key="copy-agent", name="Copy", id=1,
        system_prompt="Inspect", tool_allowlist=[], model="gpt", enabled=True)
    runner = runner_for(env, source, instance, tid, agent)
    runner.session_id = "SOURCE_PROVIDER_SESSION"
    messages = [mark_source({"role": "user", "content": "Inspect the per-conversation artifact"}, kind="task", task_uuid=tid),
        mark_source({"role": "assistant", "content": "", "tool_calls": [
            ToolCall("read1", "Read", json.dumps({"path": f"/tmp/artifacts/{conv}/{tid}/report.md"})),
            ToolCall("read2", "Read", "{}")], "native_output_items": [{"type": "reasoning", "encrypted_content": "OLD_CHAIN"}]}, task_uuid=tid),
        mark_source({"role": "tool", "content": f"first file in {conv}", "name": "Read", "tool_call_id": "read1"}, task_uuid=tid)]
    if not partial:
        messages.append(mark_source({"role": "tool", "content": "second file", "name": "Read", "tool_call_id": "read2"}, task_uuid=tid))
    stage = "cancelled" if partial else "completed"
    await runner._checkpoint_model_context(messages, round_no=1, stage=stage,
        extra_state={"inflightTool": {"id": "read2", "name": "Read"}} if partial else {})
    await dao.update_task(tid, status=stage)
    original_checkpoint = copy.deepcopy(await dao.task_model_context(tid))
    original_window = copy.deepcopy(await runner._get_window_runtime().store.load())
    previous = source
    for _ in range(2):
        cp = await copied(env, previous)
        ni = (await dao.list_agent_sessions(openbear_session_uuid=cp["conversation_uuid"]))[0]
        ntid = ni.context_task_uuid
        checkpoint = await dao.task_model_context(ntid)
        nr = runner_for(env, cp, ni, ntid, agent)
        nr._round_input = {"contextSource": {"taskUuid": ntid, "revision": checkpoint["revision"],
                                           "state": "partial" if partial else "restored"}}
        store = nr._get_window_runtime().store
        window = await store.load()
        selected = {source_of(m)["id"]: m for m in window["state"]["messages"]}
        for message in checkpoint["state"]["messages"]:
            event_id = source_of(message)["id"]
            semantic = serialize_messages(neutral_context([message]))[0]
            semantic.pop("openbear_context_source", None)
            payload = (await store.event_payload(event_id))["payload"]
            selected_semantic = dict(selected[event_id])
            selected_semantic.pop("openbear_context_source", None)
            assert semantic == payload == selected_semantic
            digest = hashlib.sha256(json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            cur = await env.db.conn.execute("SELECT fingerprint FROM context_execution_events WHERE owner_key=? AND event_id=?", (store.owner.key, event_id))
            assert (await cur.fetchone())["fingerprint"] == digest
        assert "OLD_CHAIN" not in str(checkpoint)
        recovered = await nr._round_context_messages()
        call = next(m for m in recovered if m.get("tool_calls"))
        assert cp["conversation_uuid"] in str(call["tool_calls"]) and ntid in str(call["tool_calls"])
        assert conv not in str(call["tool_calls"]) and tid not in str(call["tool_calls"])
        if partial:
            assert "read2" not in str(call["tool_calls"])
            assert "不要自动重放" in str(recovered)
        previous = cp
    # Claim a genuine new round: idle copied tasks must not overwrite their
    # instance head. The production ownership guard remains in force.
    next_task = await dao.create_task(chat_id=cp["internal_chat_id"], workflow_uuid=workflow.workflow_uuid,
        parent_session_uuid=cp["conversation_uuid"], title="continue copied input", agent_session_uuid=ni.session_uuid,
        input_data=nr._round_input)
    continued = runner_for(env, cp, ni, next_task, agent)
    continued._round_input = nr._round_input
    recovered = await continued._round_context_messages()
    await continued._checkpoint_model_context(recovered, round_no=2, stage="before_request")
    assert await dao.task_model_context(next_task)
    assert await dao.task_model_context(tid) == original_checkpoint
    assert await runner._get_window_runtime().store.load() == original_window
