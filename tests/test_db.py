"""DB DAO 测试。"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.db.dao import MessageDAO, SummaryDAO
from app.db.engine import DB
from app.llm.events import ToolCall, Usage


@pytest.fixture
async def db(tmp_path):
    d = DB(str(tmp_path / "t.db"))
    await d.connect()
    yield d
    await d.close()


async def test_add_and_recent(db):
    dao = MessageDAO(db)
    await dao.ensure_session(1)
    await dao.add(1, "user", "你好", tokens=2)
    await dao.add(1, "assistant", "在的", tokens=2)
    rows = await dao.recent(1)
    assert len(rows) == 2
    assert rows[0].role == "user" and rows[0].content == "你好"
    assert rows[1].to_message() == {"role": "assistant", "content": "在的"}


async def test_message_ownership_metadata_not_sent_to_model(db):
    dao = MessageDAO(db)
    await dao.ensure_session(1)
    message_id = await dao.add(
        1,
        "user",
        "绑定测试",
        tokens=2,
        conversation_uuid="conv-1",
        turn_uuid="turn-1",
        parent_turn_uuid="parent-1",
        run_root_turn_uuid="root-1",
        task_uuid="task-1",
        agent_session_uuid="agent-session-1",
    )
    assert message_id > 0
    rows = await dao.recent(1)
    assert len(rows) == 1
    row = rows[0]
    assert row.conversation_uuid == "conv-1"
    assert row.turn_uuid == "turn-1"
    assert row.parent_turn_uuid == "parent-1"
    assert row.run_root_turn_uuid == "root-1"
    assert row.task_uuid == "task-1"
    assert row.agent_session_uuid == "agent-session-1"
    # Ownership metadata is storage-only and must never leak into model history.
    assert row.to_message() == {"role": "user", "content": "绑定测试"}


async def test_delete_transcript_suffix_invalidates_covering_summary_and_uncompacts_survivors(db):
    dao = MessageDAO(db)
    summaries = SummaryDAO(db)
    await dao.ensure_session(1)
    message_ids = [await dao.add(1, "user", f"msg-{index}") for index in range(1, 9)]
    await summaries.add(1, "summary-through-2", message_ids[1], 10)
    await summaries.add(1, "summary-through-6", message_ids[5], 20)
    await dao.mark_compacted(1, message_ids[5])
    await db.conn.execute(
        "INSERT INTO web_operation_messages (conversation_uuid, op_id, message_id, relation_kind, created_at_ms) VALUES (?,?,?,?,?)",
        ("conv-1", "msg:delete", message_ids[4], "transcript", 1),
    )
    await db.conn.commit()
    await dao.save_controller_model_context(
        1,
        conversation_uuid="conv-1",
        session_id="session-1",
        protocol="responses",
        model="gpt",
        model_label="provider/gpt",
        state={"version": 1, "messages": []},
    )

    result = await dao.delete_from_message_id(1, message_ids[4])

    assert result == {"messages": 4, "summaries": 1, "links": 1, "remainingSummaryUpTo": message_ids[1]}
    cur = await db.conn.execute(
        "SELECT id, compacted FROM messages WHERE chat_id=1 ORDER BY id"
    )
    rows = await cur.fetchall()
    assert [(int(row["id"]), int(row["compacted"])) for row in rows] == [
        (message_ids[0], 1),
        (message_ids[1], 1),
        (message_ids[2], 0),
        (message_ids[3], 0),
    ]
    latest = await summaries.latest(1)
    assert latest is not None
    assert latest["summary"] == "summary-through-2"
    cur = await db.conn.execute(
        "SELECT COUNT(*) AS n FROM controller_model_contexts WHERE chat_id=1"
    )
    assert int((await cur.fetchone())["n"]) == 0


async def test_recent_defaults_to_all_uncompacted_messages(db):
    dao = MessageDAO(db)
    for i in range(250):
        await dao.add(1, "user", f"msg-{i}")
    rows = await dao.recent(1)
    assert len(rows) == 250
    assert rows[0].content == "msg-0"
    assert rows[-1].content == "msg-249"
    limited = await dao.recent(1, 10)
    assert [r.content for r in limited] == [f"msg-{i}" for i in range(10)]


async def test_tool_call_roundtrip_in_db(db):
    dao = MessageDAO(db)
    await dao.add(1, "assistant", "", tool_calls=[ToolCall(id="c1", name="Bash", arguments='{"command":"ls"}')],
                  reasoning="想", signature="SIG")
    await dao.add(1, "tool", "out", tool_call_id="c1", name="Bash")
    rows = await dao.recent(1)
    asst = rows[0]
    assert asst.tool_calls[0].name == "Bash"
    m = asst.to_message()
    assert m["tool_calls"][0].id == "c1"
    assert m["reasoning"] == "想" and m["signature"] == "SIG"
    tool = rows[1].to_message()
    assert tool == {"role": "tool", "content": "out", "tool_call_id": "c1", "name": "Bash"}


async def test_clear(db):
    dao = MessageDAO(db)
    await dao.add(1, "user", "x")
    await dao.clear(1)
    assert await dao.recent(1) == []


async def test_compact_marks(db):
    dao = MessageDAO(db)
    id1 = await dao.add(1, "user", "old")
    await dao.add(1, "user", "new")
    await dao.mark_compacted(1, id1)
    rows = await dao.recent(1)
    assert len(rows) == 1
    assert rows[0].content == "new"


async def test_summary(db):
    sdao = SummaryDAO(db)
    assert await sdao.latest(1) is None
    await sdao.add(1, "摘要内容", 5, 10)
    latest = await sdao.latest(1)
    assert latest["summary"] == "摘要内容"
    assert latest["up_to_message_id"] == 5


async def test_accounting_transaction_commits_atomically(db):
    async with db.accounting_transaction() as connection:
        accounting = MessageDAO(db, connection=connection)
        await accounting.add_usage(
            81,
            Usage(input_tokens=10, output_tokens=2),
            0.001,
            commit=False,
        )
        await accounting.add_turn_stats(81, commit=False, model_calls=1, model_ok=1)
        await accounting.add_model_call(
            81,
            commit=False,
            session_uuid="billing-session",
            model="openai/gpt",
            usage=Usage(input_tokens=10, output_tokens=2),
        )

    totals = await MessageDAO(db).usage_totals(81)
    assert totals.input_tokens == 10
    calls = await MessageDAO(db).recent_model_calls(81)
    assert len(calls) == 1
    assert calls[0].session_uuid == "billing-session"


async def test_accounting_model_call_revision_is_monotonic_and_commit_false_rolls_back(db):
    dao = MessageDAO(db)
    await dao.ensure_session(84)
    session_uuid = await dao.get_or_create_session_uuid(84)

    async with db.accounting_transaction() as connection:
        accounting = MessageDAO(db, connection=connection)
        first_id = await accounting.add_model_call(
            84,
            commit=False,
            session_uuid=session_uuid,
            call_kind="controller_request",
            usage=Usage(input_tokens=1),
        )
        second_id = await accounting.add_model_call(
            84,
            commit=False,
            session_uuid=session_uuid,
            call_kind="agent_request",
            usage=Usage(input_tokens=2),
        )
    assert second_id > first_id > 0

    with pytest.raises(RuntimeError, match="rollback revision"):
        async with db.accounting_transaction() as connection:
            accounting = MessageDAO(db, connection=connection)
            rolled_back_id = await accounting.add_model_call(
                84,
                commit=False,
                session_uuid=session_uuid,
                call_kind="agent_request",
                usage=Usage(input_tokens=999),
            )
            assert rolled_back_id > second_id
            raise RuntimeError("rollback revision")

    calls = await dao.recent_model_calls(84)
    assert [call.id for call in reversed(calls)] == [first_id, second_id]


async def test_cancelled_accounting_transaction_releases_writer_lock(db):
    entered = asyncio.Event()
    never = asyncio.Event()

    async def interrupted_billing() -> None:
        async with db.accounting_transaction() as connection:
            await connection.execute(
                "INSERT INTO sessions (chat_id, created_at, updated_at) VALUES (?,?,?)",
                (82, 1, 1),
            )
            entered.set()
            await never.wait()

    task = asyncio.create_task(interrupted_billing())
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The cancelled transaction must roll back, and the normal application
    # connection must be able to become the next writer immediately.
    cur = await db.conn.execute("SELECT 1 FROM sessions WHERE chat_id=82")
    assert await cur.fetchone() is None
    await asyncio.wait_for(MessageDAO(db).ensure_session(83), timeout=1)
    cur = await db.conn.execute("SELECT 1 FROM sessions WHERE chat_id=83")
    assert await cur.fetchone() is not None


async def test_cancelled_web_operation_transaction_releases_writer_lock(db):
    entered = asyncio.Event()
    never = asyncio.Event()

    async def interrupted_publication() -> None:
        async with db.web_operation_transaction() as connection:
            await connection.execute(
                "INSERT INTO sessions (chat_id, created_at, updated_at) VALUES (?,?,?)",
                (84, 1, 1),
            )
            entered.set()
            await never.wait()

    task = asyncio.create_task(interrupted_publication())
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    cur = await db.conn.execute("SELECT 1 FROM sessions WHERE chat_id=84")
    assert await cur.fetchone() is None
    await asyncio.wait_for(MessageDAO(db).ensure_session(85), timeout=1)
    cur = await db.conn.execute("SELECT 1 FROM sessions WHERE chat_id=85")
    assert await cur.fetchone() is not None


@pytest.mark.parametrize("transaction_kind", ["web", "accounting"])
async def test_single_writer_serializes_side_transaction_before_main_write(db, transaction_kind):
    entered = asyncio.Event()
    release = asyncio.Event()
    transaction = (
        db.web_operation_transaction
        if transaction_kind == "web"
        else db.accounting_transaction
    )

    async def hold_side_writer() -> None:
        async with transaction() as connection:
            await connection.execute(
                "INSERT INTO sessions (chat_id, created_at, updated_at) VALUES (?,?,?)",
                (90, 1, 1),
            )
            entered.set()
            await release.wait()

    holder = asyncio.create_task(hold_side_writer())
    await asyncio.wait_for(entered.wait(), timeout=1)
    main_write = asyncio.create_task(MessageDAO(db).ensure_session(91))
    await asyncio.sleep(0.05)
    assert not main_write.done()

    release.set()
    await asyncio.wait_for(asyncio.gather(holder, main_write), timeout=1)
    cur = await db.conn.execute("SELECT chat_id FROM sessions WHERE chat_id IN (90,91) ORDER BY chat_id")
    assert [int(row["chat_id"]) for row in await cur.fetchall()] == [90, 91]


@pytest.mark.parametrize("transaction_kind", ["web", "accounting"])
async def test_single_writer_serializes_main_write_before_side_transaction(db, transaction_kind):
    entered = asyncio.Event()
    release = asyncio.Event()
    transaction = (
        db.web_operation_transaction
        if transaction_kind == "web"
        else db.accounting_transaction
    )

    async def hold_main_writer() -> None:
        await db.conn.execute(
            "INSERT INTO sessions (chat_id, created_at, updated_at) VALUES (?,?,?)",
            (92, 1, 1),
        )
        entered.set()
        await release.wait()
        await db.conn.commit()

    async def side_write() -> None:
        async with transaction() as connection:
            await connection.execute(
                "INSERT INTO sessions (chat_id, created_at, updated_at) VALUES (?,?,?)",
                (93, 1, 1),
            )

    holder = asyncio.create_task(hold_main_writer())
    await asyncio.wait_for(entered.wait(), timeout=1)
    side = asyncio.create_task(side_write())
    await asyncio.sleep(0.05)
    assert not side.done()

    release.set()
    await asyncio.wait_for(asyncio.gather(holder, side), timeout=1)
    cur = await db.conn.execute("SELECT chat_id FROM sessions WHERE chat_id IN (92,93) ORDER BY chat_id")
    assert [int(row["chat_id"]) for row in await cur.fetchall()] == [92, 93]


async def test_abandoned_main_write_is_rolled_back_and_writer_recovers(db):
    async def abandon_write() -> None:
        await db.conn.execute(
            "INSERT INTO sessions (chat_id, created_at, updated_at) VALUES (?,?,?)",
            (94, 1, 1),
        )

    await asyncio.create_task(abandon_write())
    await asyncio.wait_for(MessageDAO(db).ensure_session(95), timeout=1)

    cur = await db.conn.execute("SELECT chat_id FROM sessions WHERE chat_id IN (94,95) ORDER BY chat_id")
    assert [int(row["chat_id"]) for row in await cur.fetchall()] == [95]


async def test_reader_does_not_observe_uncommitted_single_writer_state(db):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_writer() -> None:
        await db.conn.execute(
            "INSERT INTO sessions (chat_id, created_at, updated_at) VALUES (?,?,?)",
            (96, 1, 1),
        )
        entered.set()
        await release.wait()
        await db.conn.commit()

    holder = asyncio.create_task(hold_writer())
    await asyncio.wait_for(entered.wait(), timeout=1)
    cur = await db.conn.execute("SELECT 1 FROM sessions WHERE chat_id=96")
    assert await cur.fetchone() is None
    release.set()
    await asyncio.wait_for(holder, timeout=1)
    cur = await db.conn.execute("SELECT 1 FROM sessions WHERE chat_id=96")
    assert await cur.fetchone() is not None


async def test_latest_controller_prompt_tokens_filters_child_and_old_compaction_epoch(db):
    dao = MessageDAO(db)
    await dao.add_model_call(
        77,
        session_uuid="session-77",
        call_kind="controller_request",
        status="ok",
        last_usage=Usage(input_tokens=8000, cache_read_tokens=500, cache_write_tokens=100),
    )
    await dao.add_model_call(
        77,
        session_uuid="session-77",
        call_kind="agent_request",
        status="ok",
        last_usage=Usage(input_tokens=90_000),
    )

    assert await dao.latest_controller_prompt_tokens(77, session_uuid="session-77") == 8600

    # A summary written after that controller call starts a new context epoch.
    # Same-second ordering is intentionally treated as ambiguous/stale.
    await SummaryDAO(db).add(77, "压缩后的摘要", 0, 100)
    assert await dao.latest_controller_prompt_tokens(77, session_uuid="session-77") == 0


async def test_model_and_tool_call_logs(db):
    dao = MessageDAO(db)
    await dao.add_model_call(
        1,
        session_uuid="s1",
        model="openai/gpt",
        protocol="chat",
        think_level="xhigh",
        usage=Usage(input_tokens=10, output_tokens=3, cache_read_tokens=20, cache_write_tokens=1),
        cost_usd=0.001,
        connect_ms=11,
        first_token_ms=222,
        total_time_ms=3333,
        peak_tps=42.5,
        min_tps=12.25,
        model_call_count=3,
        model_ok_count=3,
    )
    await dao.add_tool_call(
        1,
        session_uuid="s1",
        tool_name="Bash",
        result_size=123,
    )
    models = await dao.recent_model_calls(1)
    assert len(models) == 1
    assert models[0].model == "openai/gpt"
    assert models[0].cache_read_tokens == 20
    assert models[0].peak_tps == pytest.approx(42.5)
    assert models[0].min_tps == pytest.approx(12.25)
    tools = await dao.recent_tool_calls(1)
    assert len(tools) == 1
    assert tools[0].tool_name == "Bash"
    assert tools[0].result_size == 123
    model_summary = await dao.model_call_summary(1)
    assert model_summary[0]["model"] == "openai/gpt"
    assert model_summary[0]["total"] == 3
    assert int(model_summary[0]["avg_total_ms"]) == 1111
    assert int(model_summary[0]["avg_first_ms"]) == 74
    tool_summary = await dao.tool_call_summary(1)
    assert tool_summary[0]["tool_name"] == "Bash"
    assert tool_summary[0]["total"] == 1
    totals = await dao.all_time_totals(1)
    assert totals["conversation_count"] == 3
    assert totals["model_call_count"] == 3
    provider_summary = await dao.provider_call_summary(1)
    assert provider_summary[0]["peak_tps"] == pytest.approx(42.5)
    assert provider_summary[0]["min_tps"] == pytest.approx(12.25)
    assert int(provider_summary[0]["avg_total_ms"]) == 1111
    assert int(provider_summary[0]["avg_connect_ms"]) == 3
    assert int(provider_summary[0]["avg_first_ms"]) == 74
    detail_summary = await dao.model_detail_summary(1, "openai")
    assert detail_summary[0]["peak_tps"] == pytest.approx(42.5)
    assert detail_summary[0]["min_tps"] == pytest.approx(12.25)
    assert int(detail_summary[0]["avg_total_ms"]) == 1111

    await dao.add_model_call(2, model="openai/mini", status="ok", model_call_count=2)
    multi_provider_summary = await dao.provider_call_summary([1, 2])
    assert sum(int(row["calls"] or 0) for row in multi_provider_summary if row["provider"] == "openai") == 5
    multi_detail_summary = await dao.model_detail_summary([1, 2], "openai")
    assert {row["model"] for row in multi_detail_summary} == {"openai/gpt", "openai/mini"}


async def test_recent_errors_combines_model_and_tool_logs(db):
    dao = MessageDAO(db)
    await dao.add_model_call(1, model="openai/gpt", status="error", error_type="rate_limit")
    await dao.add_tool_call(1, tool_name="Bash", status="error", error_type="timeout")
    errors = await dao.recent_errors(1)
    assert {e["kind"] for e in errors} == {"model", "tool"}
    assert {e["error_type"] for e in errors} == {"rate_limit", "timeout"}


async def test_session_thinking_level(db):
    dao = MessageDAO(db)
    assert await dao.get_thinking_level(42) == ""
    await dao.set_thinking_level(42, "max")
    assert await dao.get_thinking_level(42) == "max"
    await dao.set_thinking_level(42, "off")
    assert await dao.get_thinking_level(42) == "off"


async def test_session_show_thinking_override(db):
    dao = MessageDAO(db)
    assert await dao.get_show_thinking_override(42) is None
    assert await dao.get_show_thinking(42, default=False) is False
    assert await dao.get_show_thinking(42, default=True) is True
    await dao.set_show_thinking(42, True)
    assert await dao.get_show_thinking_override(42) is True
    assert await dao.get_show_thinking(42, default=False) is True
    await dao.set_show_thinking(42, False)
    assert await dao.get_show_thinking_override(42) is False
    assert await dao.get_show_thinking(42, default=True) is False


async def test_usage_totals_are_persisted_and_accumulated(db):
    dao = MessageDAO(db)
    await dao.add_usage(
        7,
        Usage(input_tokens=40, output_tokens=5, cache_read_tokens=60, cache_write_tokens=7),
        0.0012,
        connect_ms=12,
        first_token_ms=345,
        total_time_ms=6789,
        model="openai/deepseek",
        protocol="chat",
        think_level="max",
    )
    await dao.add_usage(7, Usage(input_tokens=10, output_tokens=2,
                                 cache_read_tokens=5, cache_write_tokens=1), 0.0003)
    totals = await dao.usage_totals(7)
    assert totals.input_tokens == 50
    assert totals.output_tokens == 7
    assert totals.cache_read_tokens == 65
    assert totals.cache_write_tokens == 8
    assert totals.cost_usd == pytest.approx(0.0015)
    assert totals.last_input_tokens == 10
    assert totals.last_output_tokens == 2
    assert totals.last_cache_read_tokens == 5
    assert totals.last_cache_write_tokens == 1
    assert totals.last_cost_usd == pytest.approx(0.0003)
    assert totals.last_connect_ms == 0


async def test_usage_totals_store_last_request_metadata(db):
    dao = MessageDAO(db)
    await dao.add_usage(
        8,
        Usage(input_tokens=140, output_tokens=15, cache_read_tokens=260, cache_write_tokens=17),
        0.0099,
        last_usage=Usage(input_tokens=40, output_tokens=5, cache_read_tokens=60, cache_write_tokens=7),
        last_cost_usd=0.0012,
        connect_ms=12,
        first_token_ms=345,
        total_time_ms=6789,
        run_total_time_ms=123456,
        run_model_calls=4,
        run_tool_calls=9,
        model="openai/deepseek",
        protocol="chat",
        think_level="max",
    )
    totals = await dao.usage_totals(8)
    assert totals.cost_usd == pytest.approx(0.0099)
    assert totals.last_input_tokens == 40
    assert totals.last_output_tokens == 5
    assert totals.last_cache_read_tokens == 60
    assert totals.last_cache_write_tokens == 7
    assert totals.last_cost_usd == pytest.approx(0.0012)
    assert totals.last_connect_ms == 12
    assert totals.last_first_token_ms == 345
    assert totals.last_total_time_ms == 6789
    assert totals.last_run_cost_usd == pytest.approx(0.0099)
    assert totals.last_run_total_time_ms == 123456
    assert totals.last_run_model_calls == 4
    assert totals.last_run_tool_calls == 9
    assert totals.last_model == "openai/deepseek"
    assert totals.last_protocol == "chat"
    assert totals.last_think_level == "max"
    assert totals.last_created_at > 0


async def test_failed_run_does_not_zero_last_snapshot(db):
    """回归：上游 400 在首调就拒收（last_usage 全 0）的失败轮，不能用 0 覆盖
    上一次成功的 last_* 快照——否则 /status 的「上下文」「最近一轮」全被打成 0。
    """
    dao = MessageDAO(db)
    cid = 4242
    # 先来一次成功轮，落下真实快照
    await dao.add_usage(
        cid,
        Usage(input_tokens=100, output_tokens=20, cache_read_tokens=300, cache_write_tokens=0),
        0.05,
        last_usage=Usage(input_tokens=100, output_tokens=20, cache_read_tokens=300),
        last_cost_usd=0.05,
        connect_ms=9, first_token_ms=120, total_time_ms=3000,
        run_total_time_ms=3000, run_model_calls=1, run_tool_calls=0,
        model="openai/gpt", protocol="chat", think_level="xhigh",
    )
    # 再来一次失败轮：last_usage 全 0、各项 0（模拟 400 首调拒收）
    await dao.add_usage(
        cid,
        Usage(),  # 累计用量本轮为 0
        0.0,
        last_usage=Usage(),  # prompt 体积 0 → 不应覆盖快照
        last_cost_usd=0.0,
        connect_ms=0, first_token_ms=0, total_time_ms=0,
        run_total_time_ms=10000, run_model_calls=1, run_tool_calls=0,
        model="openai/gpt", protocol="chat", think_level="xhigh",
    )
    totals = await dao.usage_totals(cid)
    # 快照仍是上一次成功的值，不是 0
    assert totals.last_input_tokens == 100
    assert totals.last_cache_read_tokens == 300
    assert totals.last_output_tokens == 20
    assert totals.last_connect_ms == 9
    assert totals.last_total_time_ms == 3000
    assert totals.last_cost_usd == pytest.approx(0.05)
    # 累计花费不受影响（失败轮加 0）
    assert totals.cost_usd == pytest.approx(0.05)


async def test_turn_stats_accumulate_and_reset(db):
    dao = MessageDAO(db)
    cid = 99
    # 两条用户消息：user_turns=2，turn_started_at 只在首条设定
    await dao.bump_user_turn(cid)
    t1 = (await dao.usage_totals(cid)).turn_started_at
    assert t1 > 0
    await dao.bump_user_turn(cid)
    totals = await dao.usage_totals(cid)
    assert totals.stat_user_turns == 2
    assert totals.turn_started_at == t1  # 不被第二条覆盖

    # 两次 run 的统计累加
    await dao.add_turn_stats(cid, tool_calls=3, model_calls=4, model_ok=3,
                             model_retry=1, model_fail=0,
                             connect_ms_sum=300, first_token_ms_sum=600,
                             total_time_ms_sum=9000, output_tokens_sum=120)
    await dao.add_turn_stats(cid, tool_calls=1, model_calls=1, model_ok=1,
                             connect_ms_sum=100, first_token_ms_sum=200,
                             total_time_ms_sum=3000, output_tokens_sum=60)
    totals = await dao.usage_totals(cid)
    assert totals.stat_tool_calls == 4
    assert totals.stat_model_calls == 5
    assert totals.stat_model_ok == 4
    assert totals.stat_model_retry == 1
    assert totals.stat_connect_ms_sum == 400
    assert totals.stat_total_time_ms_sum == 12000
    assert totals.stat_output_tokens_sum == 180

    # reset 清零本轮统计 + 累计用量 + 最近一轮快照
    await dao.add_usage(cid, Usage(input_tokens=10, output_tokens=2,
                                   cache_read_tokens=3, cache_write_tokens=1), 0.5,
                        connect_ms=11, first_token_ms=22, total_time_ms=33,
                        model="m", protocol="chat", think_level="max")
    await dao.reset_turn_stats(cid)
    totals = await dao.usage_totals(cid)
    assert totals.stat_user_turns == 0
    assert totals.stat_model_calls == 0
    assert totals.turn_started_at == 0
    assert totals.stat_total_time_ms_sum == 0
    assert totals.cost_usd == 0.0
    assert totals.input_tokens == 0
    # last_* 快照也必须清零（上下文 / 最近一轮读它）
    assert totals.last_input_tokens == 0
    assert totals.last_cache_read_tokens == 0
    assert totals.last_total_time_ms == 0
    assert totals.last_run_total_time_ms == 0
    assert totals.last_run_cost_usd == 0
    assert totals.last_run_model_calls == 0
    assert totals.last_run_tool_calls == 0
    assert totals.last_model == ""
    assert totals.last_created_at == 0


async def test_has_history(db):
    dao = MessageDAO(db)
    cid = 1234
    assert await dao.has_history(cid) is False
    await dao.add(cid, "user", "hi", tokens=3)
    assert await dao.has_history(cid) is True
    await dao.clear(cid)
    assert await dao.has_history(cid) is False
    # 仅有摘要也算有历史
    await SummaryDAO(db).add(cid, "摘要", 1, 5)
    assert await dao.has_history(cid) is True


async def test_session_uuid_stable_and_reset(db):
    dao = MessageDAO(db)
    cid = 700
    u1 = await dao.get_or_create_session_uuid(cid)
    assert u1
    # 稳定：再取还是同一个（重启后从 DB 读回也是这个语义）
    assert await dao.get_or_create_session_uuid(cid) == u1
    # 新会话清空 → 换新
    await dao.reset_turn_stats(cid)
    u2 = await dao.get_or_create_session_uuid(cid)
    assert u2 and u2 != u1


async def test_system_snapshot_locks(db):
    dao = MessageDAO(db)
    cid = 701
    # 首轮锁定
    s1 = await dao.get_or_set_system_snapshot(cid, "系统提示词 v1")
    assert s1 == "系统提示词 v1"
    # 后续轮即便传了不同内容，也复用锁定的快照（防缓存撕裂）
    s2 = await dao.get_or_set_system_snapshot(cid, "系统提示词 v2-变了")
    assert s2 == "系统提示词 v1"
    assert await dao.get_system_snapshot(cid) == "系统提示词 v1"
    # 新会话清空 → 可重新锁定新值
    await dao.reset_turn_stats(cid)
    assert await dao.get_system_snapshot(cid) == ""
    s3 = await dao.get_or_set_system_snapshot(cid, "新会话提示词")
    assert s3 == "新会话提示词"


async def test_repair_dangling_tool_calls_fills_and_idempotent(db):
    """光杆 tool_call 补占位 + 幂等(再调一次不重复补)。"""
    dao = MessageDAO(db)
    await dao.ensure_session(1)
    await dao.add(1, "user", "查样式")
    # 一条 assistant 三个并行 tool_call(模拟 id=923)
    await dao.add(1, "assistant", "", tool_calls=[
        ToolCall(id="c1", name="Grep", arguments="{}"),
        ToolCall(id="c2", name="Grep", arguments="{}"),
        ToolCall(id="c3", name="Glob", arguments="{}"),
    ])
    # 只有 c1 有真实结果,c2/c3 是光杆
    await dao.add(1, "tool", "grep结果", tool_call_id="c1", name="Grep")

    added = await dao.repair_dangling_tool_calls(1)
    assert added == 2  # 只补 c2/c3

    rows = await dao.recent(1)
    tool_ids = {r.tool_call_id for r in rows if r.role == "tool"}
    assert tool_ids == {"c1", "c2", "c3"}

    # 幂等:再调一次,不应再补
    added2 = await dao.repair_dangling_tool_calls(1)
    assert added2 == 0


async def test_repair_dangling_noop_when_all_paired(db):
    """全部配对时不补任何东西。"""
    dao = MessageDAO(db)
    await dao.ensure_session(1)
    await dao.add(1, "assistant", "", tool_calls=[ToolCall(id="x", name="Read", arguments="{}")])
    await dao.add(1, "tool", "ok", tool_call_id="x", name="Read")
    added = await dao.repair_dangling_tool_calls(1)
    assert added == 0


async def test_repair_dangling_noop_when_no_tool_calls(db):
    """最近 assistant 没有 tool_calls → 返回 0。"""
    dao = MessageDAO(db)
    await dao.ensure_session(1)
    await dao.add(1, "user", "hi")
    await dao.add(1, "assistant", "你好")
    added = await dao.repair_dangling_tool_calls(1)
    assert added == 0


async def test_connect_supersedes_legacy_pending_steps_for_revise_requested_plans(tmp_path):
    path = tmp_path / "revised-plan-steps.db"
    first = DB(str(path))
    await first.connect()
    await first.conn.execute(
        """
        INSERT INTO rath_task_plan_versions
          (task_uuid, version, plan_type, parent_version, status, plan_json, plan_hash,
           change_reason, submit_request_id, submitted_at, decided_at)
        VALUES ('task-revised', 2, 'replan', 1, 'revise_requested', '{}', 'hash', '', 'submit-2', 1, 2)
        """
    )
    await first.conn.execute(
        """
        INSERT INTO rath_task_plan_step_runs
          (task_uuid, plan_version, step_id, status, updated_at, row_revision)
        VALUES ('task-revised', 2, 's1', 'pending', 1, 1)
        """
    )
    await first.conn.commit()
    await first.close()

    reopened = DB(str(path))
    await reopened.connect()
    try:
        cur = await reopened.conn.execute(
            "SELECT status, row_revision FROM rath_task_plan_step_runs WHERE task_uuid='task-revised'"
        )
        row = await cur.fetchone()
        assert row["status"] == "superseded"
        assert int(row["row_revision"]) == 2
    finally:
        await reopened.close()


async def test_connect_removes_structural_memory_categories_and_entries(tmp_path):
    path = tmp_path / "structural-memory.db"
    first = DB(str(path))
    await first.connect()
    for key, name in (("identity", "身份"), ("persona", "人格"), ("rule", "行为准则")):
        cur = await first.conn.execute(
            "INSERT INTO memory_categories (key, name, icon, render_type, schema_json, inject, sort) VALUES (?,?,?,?,?,?,?)",
            (key, name, "", "prose", '{"fields":[]}', 1, 1),
        )
        await first.conn.execute(
            "INSERT INTO memory_entries (category_id, title, body, enabled, archived, sort, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (int(cur.lastrowid), f"{name}条目", "legacy", 1, 0, 1, 1, 1),
        )
    await first.conn.commit()
    await first.close()

    reopened = DB(str(path))
    await reopened.connect()
    try:
        cur = await reopened.conn.execute(
            "SELECT COUNT(*) AS n FROM memory_categories WHERE key IN ('identity','persona','rule')"
        )
        assert int((await cur.fetchone())["n"]) == 0
        cur = await reopened.conn.execute("SELECT COUNT(*) AS n FROM memory_entries WHERE body='legacy'")
        assert int((await cur.fetchone())["n"]) == 0
        cur = await reopened.conn.execute("PRAGMA table_info(memory_entries)")
        assert "expanded" in {str(row["name"]) for row in await cur.fetchall()}
    finally:
        await reopened.close()


def _controller_context_state(marker: str = "test-private-reasoning") -> dict:
    return {
        "version": 1,
        "messages": [
            {"role": "user", "content": "visible user\n[runtime-only suffix]"},
            {
                "role": "assistant",
                "content": "visible answer",
                "native_output_items": [{
                    "type": "message",
                    "id": "private-message-1",
                    "content": [{"type": "output_text", "text": "visible answer"}],
                    "encrypted_content": marker,
                }],
            },
        ],
    }


async def test_controller_model_context_survives_database_reconnect(tmp_path):
    path = tmp_path / "controller-context-reconnect.db"
    first = DB(str(path))
    await first.connect()
    dao = MessageDAO(first)
    await dao.ensure_session(-10)
    await dao.add(-10, "user", "visible user")
    await dao.add(-10, "assistant", "visible answer")
    assert await dao.save_controller_model_context(
        -10,
        conversation_uuid="conversation-1",
        session_id="session-1",
        protocol="responses",
        model="gpt",
        model_label="provider/gpt",
        state=_controller_context_state(),
    ) == 1
    await first.close()

    reopened = DB(str(path))
    await reopened.connect()
    try:
        loaded = await MessageDAO(reopened).load_controller_model_context(
            -10,
            conversation_uuid="conversation-1",
            session_id="session-1",
            protocol="responses",
            model="gpt",
            model_label="provider/gpt",
        )
        assert loaded is not None
        assert "test-private-reasoning" in json.dumps(loaded, ensure_ascii=False)
        assert "runtime-only suffix" in json.dumps(loaded, ensure_ascii=False)
    finally:
        await reopened.close()


async def test_controller_model_context_identity_and_transcript_changes_clear_whole_state(db):
    dao = MessageDAO(db)
    await dao.ensure_session(-11)
    await dao.add(-11, "user", "visible user")
    await dao.add(-11, "assistant", "visible answer")
    identity = {
        "conversation_uuid": "conversation-1",
        "session_id": "session-1",
        "protocol": "responses",
        "model": "gpt",
        "model_label": "provider/gpt",
    }

    for changed in (
        {"conversation_uuid": "conversation-2"},
        {"session_id": "session-2"},
        {"protocol": "chat"},
        {"model": "gpt-next"},
        {"model_label": "other/gpt"},
    ):
        assert await dao.save_controller_model_context(
            -11,
            **identity,
            state=_controller_context_state(),
        ) > 0
        assert await dao.load_controller_model_context(
            -11,
            **{**identity, **changed},
        ) is None
        cur = await db.conn.execute(
            "SELECT COUNT(*) AS n FROM controller_model_contexts WHERE chat_id=-11"
        )
        assert int((await cur.fetchone())["n"]) == 0

    await dao.save_controller_model_context(
        -11,
        **identity,
        state=_controller_context_state(),
    )
    await dao.add(-11, "user", "edited transcript")
    assert await dao.load_controller_model_context(-11, **identity) is None

    await dao.save_controller_model_context(
        -11,
        **identity,
        state=_controller_context_state(),
    )
    await dao.reset_turn_stats(-11)
    cur = await db.conn.execute(
        "SELECT COUNT(*) AS n FROM controller_model_contexts WHERE chat_id=-11"
    )
    assert int((await cur.fetchone())["n"]) == 0


async def test_controller_model_context_table_migrates_old_database_idempotently(tmp_path):
    path = tmp_path / "old-controller-context.db"
    old = DB(str(path))
    await old.connect()
    await MessageDAO(old).add(-12, "user", "legacy transcript")
    await old.conn.execute("DROP TABLE controller_model_contexts")
    await old.conn.commit()
    await old.close()

    migrated = DB(str(path))
    await migrated.connect()
    try:
        cur = await migrated.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='controller_model_contexts'"
        )
        assert await cur.fetchone() is not None
        assert [row.content for row in await MessageDAO(migrated).recent(-12)] == ["legacy transcript"]
    finally:
        await migrated.close()

    idempotent = DB(str(path))
    await idempotent.connect()
    try:
        cur = await idempotent.conn.execute("PRAGMA table_info(controller_model_contexts)")
        assert {str(row["name"]) for row in await cur.fetchall()} >= {
            "chat_id", "conversation_uuid", "session_id", "protocol",
            "model", "model_label", "state_json", "revision",
        }
    finally:
        await idempotent.close()
