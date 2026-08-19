"""数据访问 —— 消息历史 + 摘要读写。"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.agent.native_continuation import transcript_message_fingerprint
from app.db.engine import DB, now_ts
from app.llm.events import ToolCall, Usage


@dataclass(slots=True)
class ModelCallRow:
    id: int
    chat_id: int
    session_uuid: str = ""
    model: str = ""
    protocol: str = ""
    think_level: str = ""
    call_kind: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    connect_ms: int = 0
    first_token_ms: int = 0
    total_time_ms: int = 0
    peak_tps: float = 0.0
    min_tps: float = 0.0
    status: str = "ok"
    model_call_count: int = 1
    model_ok_count: int = 1
    model_retry_count: int = 0
    model_fail_count: int = 0
    expert_tool_calls: int = 0
    error_type: str = ""
    created_at: int = 0


@dataclass(slots=True)
class ToolCallRow:
    id: int
    chat_id: int
    session_uuid: str = ""
    tool_name: str = ""
    status: str = "ok"
    duration_ms: int = 0
    result_size: int = 0
    error_type: str = ""
    created_at: int = 0


@dataclass(slots=True)
class MsgRow:
    id: int
    chat_id: int
    role: str
    content: str = ""
    reasoning: str = ""
    signature: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str = ""
    name: str = ""
    tokens: int = 0
    created_at: int = 0
    # Ownership metadata (DB-only; never sent to model via to_message).
    conversation_uuid: str = ""
    turn_uuid: str = ""
    parent_turn_uuid: str = ""
    run_root_turn_uuid: str = ""
    task_uuid: str = ""
    agent_session_uuid: str = ""

    def to_message(self) -> dict[str, Any]:
        """转回中性消息格式（喂给 backend）。"""
        m: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.role == "assistant":
            if self.tool_calls:
                m["tool_calls"] = self.tool_calls
            if self.reasoning:
                m["reasoning"] = self.reasoning
            if self.signature:
                m["signature"] = self.signature
        elif self.role == "tool":
            m["tool_call_id"] = self.tool_call_id
            m["name"] = self.name
        return m


@dataclass(slots=True)
class UsageTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    last_input_tokens: int = 0
    last_output_tokens: int = 0
    last_cache_read_tokens: int = 0
    last_cache_write_tokens: int = 0
    last_cost_usd: float = 0.0          # 最近一次模型 API 调用费用
    last_connect_ms: int = 0
    last_first_token_ms: int = 0
    last_total_time_ms: int = 0
    last_run_cost_usd: float = 0.0      # 最近一次 Agent run 总费用
    last_run_total_time_ms: int = 0
    last_run_model_calls: int = 0
    last_run_tool_calls: int = 0
    last_model: str = ""
    last_protocol: str = ""
    last_think_level: str = ""
    last_created_at: int = 0
    # 本轮统计
    turn_started_at: int = 0
    stat_user_turns: int = 0
    stat_tool_calls: int = 0
    stat_model_calls: int = 0
    stat_model_ok: int = 0
    stat_model_retry: int = 0
    stat_model_fail: int = 0
    stat_connect_ms_sum: int = 0
    stat_first_token_ms_sum: int = 0
    stat_total_time_ms_sum: int = 0
    stat_output_tokens_sum: int = 0


class WebConversationDefaultsDAO:
    """按 Web owner 持久化新会话运行配置。"""

    _COLUMNS = {
        "main_model",
        "main_thinking_level",
        "main_fast_mode",
        "agent_model",
        "agent_think_level",
        "agent_fast_mode",
    }

    def __init__(self, db: DB, *, connection: Any | None = None) -> None:
        self._db = db
        self._conn_override = connection

    @property
    def _conn(self) -> Any:
        return self._conn_override if self._conn_override is not None else self._db.conn

    @staticmethod
    def _row(row: Any) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    async def get(self, owner_chat_id: int) -> dict[str, Any] | None:
        cur = await self._conn.execute(
            "SELECT * FROM web_conversation_defaults WHERE owner_chat_id=?",
            (int(owner_chat_id),),
        )
        return self._row(await cur.fetchone())

    @staticmethod
    async def _candidate_from_connection(conn: Any, owner: int, fallback: dict[str, Any]) -> dict[str, Any]:
        cur = await conn.execute(
            "SELECT * FROM web_conversation_defaults WHERE owner_chat_id=?",
            (owner,),
        )
        existing = await cur.fetchone()
        if existing is not None:
            return dict(existing)
        cur = await conn.execute(
            """
            SELECT c.model AS main_model,
                   COALESCE(s.thinking_level, '') AS main_thinking_level,
                   COALESCE(s.fast_mode, 0) AS main_fast_mode,
                   c.agent_model, c.agent_think_level, c.agent_fast_mode
            FROM web_conversations AS c
            LEFT JOIN sessions AS s ON s.chat_id=c.internal_chat_id
            WHERE c.owner_chat_id=? AND COALESCE(c.archived_at, 0)=0
            ORDER BY c.updated_at DESC, c.id DESC
            LIMIT 1
            """,
            (owner,),
        )
        recent = await cur.fetchone()
        source = dict(fallback)
        if recent is not None:
            source.update(dict(recent))
        source.update({"owner_chat_id": owner, "revision": 0, "updated_at": 0})
        return source

    async def candidate(self, owner_chat_id: int, fallback: dict[str, Any]) -> dict[str, Any]:
        """读取偏好或 seed 候选，但绝不写库，供请求验证优先使用。"""
        return await self._candidate_from_connection(self._conn, int(owner_chat_id), fallback)

    @staticmethod
    async def _insert(conn: Any, owner: int, source: dict[str, Any]) -> None:
        await conn.execute(
            """
            INSERT INTO web_conversation_defaults (
              owner_chat_id, main_model, main_thinking_level, main_fast_mode,
              agent_model, agent_think_level, agent_fast_mode, revision, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                owner,
                str(source.get("main_model") or ""),
                str(source.get("main_thinking_level") or ""),
                1 if bool(source.get("main_fast_mode")) else 0,
                str(source.get("agent_model") or ""),
                str(source.get("agent_think_level") or ""),
                int(source.get("agent_fast_mode") if source.get("agent_fast_mode") is not None else -1),
                1,
                now_ts(),
            ),
        )

    async def get_or_seed(self, owner_chat_id: int, fallback: dict[str, Any]) -> dict[str, Any]:
        """首次读取时从最近非归档会话 seed；无历史则使用调用方默认。"""
        owner = int(owner_chat_id)
        async with self._conn.transaction(label="web-conversation-defaults-seed") as conn:
            source = await self._candidate_from_connection(conn, owner, fallback)
            if int(source.get("revision") or 0) == 0:
                await self._insert(conn, owner, source)
            cur = await conn.execute(
                "SELECT * FROM web_conversation_defaults WHERE owner_chat_id=?",
                (owner,),
            )
            return dict(await cur.fetchone())

    async def patch_or_seed(
        self,
        owner_chat_id: int,
        fields: dict[str, Any],
        fallback: dict[str, Any],
    ) -> dict[str, Any]:
        """成功 PATCH 才在同一事务 seed/更新，失败验证不会产生偏好行。"""
        owner = int(owner_chat_id)
        updates = {key: value for key, value in fields.items() if key in self._COLUMNS}
        if not updates:
            raise ValueError("nothing_to_update")
        async with self._conn.transaction(label="web-conversation-defaults-patch-or-seed") as conn:
            source = await self._candidate_from_connection(conn, owner, fallback)
            if int(source.get("revision") or 0) == 0:
                source.update(updates)
                await self._insert(conn, owner, source)
            else:
                assignments = [f"{key}=?" for key in updates]
                params = list(updates.values())
                assignments.extend(["revision=revision+1", "updated_at=?"])
                params.extend([now_ts(), owner])
                await conn.execute(
                    f"UPDATE web_conversation_defaults SET {', '.join(assignments)} WHERE owner_chat_id=?",
                    tuple(params),
                )
            cur = await conn.execute(
                "SELECT * FROM web_conversation_defaults WHERE owner_chat_id=?",
                (owner,),
            )
            return dict(await cur.fetchone())

    async def patch(self, owner_chat_id: int, fields: dict[str, Any]) -> dict[str, Any]:
        updates = {key: value for key, value in fields.items() if key in self._COLUMNS}
        if not updates:
            row = await self.get(owner_chat_id)
            if row is None:
                raise LookupError("web_conversation_defaults_not_found")
            return row
        assignments = [f"{key}=?" for key in updates]
        params = list(updates.values())
        assignments.extend(["revision=revision+1", "updated_at=?"])
        params.extend([now_ts(), int(owner_chat_id)])
        async with self._conn.transaction(label="web-conversation-defaults-patch") as conn:
            cur = await conn.execute(
                f"UPDATE web_conversation_defaults SET {', '.join(assignments)} WHERE owner_chat_id=?",
                tuple(params),
            )
            if int(cur.rowcount or 0) != 1:
                raise LookupError("web_conversation_defaults_not_found")
            cur = await conn.execute(
                "SELECT * FROM web_conversation_defaults WHERE owner_chat_id=?",
                (int(owner_chat_id),),
            )
            return dict(await cur.fetchone())


class MessageDAO:
    def __init__(self, db: DB, *, connection: Any | None = None) -> None:
        self._db = db
        # Services constructs DAOs before DB.connect(); resolve the normal
        # connection lazily, while still allowing billing to pin a dedicated one.
        self._conn_override = connection

    @property
    def _conn(self) -> Any:
        return self._conn_override if self._conn_override is not None else self._db.conn

    @property
    def connection(self) -> Any:
        return self._conn

    async def ensure_session(self, chat_id: int, *, commit: bool = True) -> None:
        await self._conn.execute(
            "INSERT OR IGNORE INTO sessions (chat_id, created_at, updated_at) VALUES (?,?,?)",
            (chat_id, now_ts(), now_ts()))
        if commit:
            await self._conn.commit()

    async def current_session_uuid(self, chat_id: int) -> str:
        """只读取当前 session_uuid；不存在时不创建新会话/新 UUID。"""
        cur = await self._conn.execute(
            "SELECT session_uuid FROM sessions WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
        return (row["session_uuid"] if row else "") or ""

    async def get_or_create_session_uuid(self, chat_id: int) -> str:
        """返回会话稳定 session_uuid；不存在则生成并落库。

        进程重启后从 DB 读回，天然恢复；只有 reset_turn_stats 才换新值。
        """
        await self.ensure_session(chat_id)
        cur = await self._conn.execute(
            "SELECT session_uuid FROM sessions WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
        existing = (row["session_uuid"] if row else "") or ""
        if existing:
            return existing
        import uuid as _uuid
        new_uuid = str(_uuid.uuid4())
        await self._conn.execute(
            "UPDATE sessions SET session_uuid=?, updated_at=? WHERE chat_id=?",
            (new_uuid, now_ts(), chat_id))
        await self._conn.commit()
        return new_uuid

    async def _controller_context_anchor(self, chat_id: int, *, connection: Any | None = None) -> dict[str, Any]:
        """Fingerprint the exact ordinary transcript projection used for fallback.

        The anchor contains no private provider data. It lets a private model
        checkpoint prove that messages/summary were not edited, truncated, or
        compacted before any opaque item is replayed.
        """
        conn = connection if connection is not None else self._conn
        cur = await conn.execute(
            "SELECT id, summary, up_to_message_id FROM summaries "
            "WHERE chat_id=? ORDER BY id DESC LIMIT 1",
            (chat_id,),
        )
        summary_row = await cur.fetchone()
        summary_text = str(summary_row["summary"] or "") if summary_row else ""
        summary_anchor = {
            "id": int(summary_row["id"] or 0) if summary_row else 0,
            "upToMessageId": int(summary_row["up_to_message_id"] or 0) if summary_row else 0,
            "fingerprint": hashlib.sha256(summary_text.encode("utf-8")).hexdigest() if summary_text else "",
        }
        cur = await conn.execute(
            "SELECT * FROM messages WHERE chat_id=? AND compacted=0 ORDER BY id ASC",
            (chat_id,),
        )
        rows = await cur.fetchall()
        return {
            "summary": summary_anchor,
            "messages": [
                {
                    "id": int(row["id"] or 0),
                    "fingerprint": transcript_message_fingerprint(self._row(row).to_message()),
                }
                for row in rows
            ],
        }

    async def load_controller_model_context(
        self,
        chat_id: int,
        *,
        conversation_uuid: str,
        session_id: str,
        protocol: str,
        model: str,
        model_label: str,
    ) -> dict[str, Any] | None:
        """Load a private checkpoint only when identity and transcript both match.

        Any mismatch deletes the whole checkpoint. Opaque turns are never merged
        piecemeal with a changed visible transcript.
        """
        cur = await self._conn.execute(
            "SELECT * FROM controller_model_contexts WHERE chat_id=?",
            (chat_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        expected = (
            str(conversation_uuid or ""),
            str(session_id or ""),
            str(protocol or "").lower(),
            str(model or ""),
            str(model_label or ""),
        )
        saved = (
            str(row["conversation_uuid"] or ""),
            str(row["session_id"] or ""),
            str(row["protocol"] or "").lower(),
            str(row["model"] or ""),
            str(row["model_label"] or ""),
        )
        try:
            state = json.loads(str(row["state_json"] or "{}"))
        except Exception:
            state = {}
        anchor = await self._controller_context_anchor(chat_id)
        valid = (
            saved == expected
            and isinstance(state, dict)
            and int(state.get("version") or 0) == 1
            and isinstance(state.get("messages"), list)
            and state.get("anchor") == anchor
        )
        if valid:
            return dict(state)
        await self.clear_controller_model_context(chat_id)
        return None

    async def save_controller_model_context(
        self,
        chat_id: int,
        *,
        conversation_uuid: str,
        session_id: str,
        protocol: str,
        model: str,
        model_label: str,
        state: dict[str, Any],
    ) -> int:
        """Atomically replace one private Controller model-context checkpoint."""
        identity = {
            "conversation_uuid": str(conversation_uuid or ""),
            "session_id": str(session_id or ""),
            "protocol": str(protocol or "").lower(),
            "model": str(model or ""),
            "model_label": str(model_label or ""),
        }
        if not all(identity.values()):
            await self.clear_controller_model_context(chat_id)
            return 0
        async with self._conn.transaction(label="controller-model-context-save") as conn:
            payload = dict(state or {})
            payload["version"] = 1
            payload["anchor"] = await self._controller_context_anchor(chat_id, connection=conn)
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
            ts = now_ts()
            await conn.execute(
                """
                INSERT INTO controller_model_contexts (
                  chat_id, conversation_uuid, session_id, protocol, model,
                  model_label, state_json, revision, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,1,?,?)
                ON CONFLICT(chat_id) DO UPDATE SET
                  conversation_uuid=excluded.conversation_uuid,
                  session_id=excluded.session_id,
                  protocol=excluded.protocol,
                  model=excluded.model,
                  model_label=excluded.model_label,
                  state_json=excluded.state_json,
                  revision=controller_model_contexts.revision+1,
                  updated_at=excluded.updated_at
                """,
                (
                    chat_id,
                    identity["conversation_uuid"],
                    identity["session_id"],
                    identity["protocol"],
                    identity["model"],
                    identity["model_label"],
                    encoded,
                    ts,
                    ts,
                ),
            )
            cur = await conn.execute(
                "SELECT revision FROM controller_model_contexts WHERE chat_id=?",
                (chat_id,),
            )
            saved = await cur.fetchone()
            return int(saved["revision"] or 0) if saved else 0

    async def clear_controller_model_context(self, chat_id: int, *, commit: bool = True) -> bool:
        cur = await self._conn.execute(
            "DELETE FROM controller_model_contexts WHERE chat_id=?",
            (chat_id,),
        )
        if commit:
            await self._conn.commit()
        return int(cur.rowcount or 0) > 0

    async def get_system_snapshot(self, chat_id: int) -> str:
        await self.ensure_session(chat_id)
        cur = await self._conn.execute(
            "SELECT system_snapshot FROM sessions WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
        return (row["system_snapshot"] if row else "") or ""

    async def get_or_set_system_snapshot(self, chat_id: int, system: str) -> str:
        """会话首轮锁定系统提示词快照，后续轮复用同一份。

        保证整个会话发给上游的 system 逐字节一致，不撕裂 prompt cache。
        快照空时写入并返回当前值；已有则返回旧值（忽略本次 system）。新会话清空。
        """
        await self.ensure_session(chat_id)
        cur = await self._conn.execute(
            "SELECT system_snapshot FROM sessions WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
        existing = (row["system_snapshot"] if row else "") or ""
        if existing:
            return existing
        await self._conn.execute(
            "UPDATE sessions SET system_snapshot=?, updated_at=? WHERE chat_id=?",
            (system, now_ts(), chat_id))
        await self._conn.commit()
        return system

    async def get_thinking_level(self, chat_id: int) -> str:
        await self.ensure_session(chat_id)
        cur = await self._conn.execute(
            "SELECT thinking_level FROM sessions WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
        return (row["thinking_level"] or "") if row else ""

    async def set_thinking_level(self, chat_id: int, level: str) -> None:
        await self.ensure_session(chat_id)
        await self._conn.execute(
            "UPDATE sessions SET thinking_level=?, updated_at=? WHERE chat_id=?",
            (level, now_ts(), chat_id))
        await self._conn.commit()

    async def get_fast_mode(self, chat_id: int) -> bool:
        await self.ensure_session(chat_id)
        cur = await self._conn.execute(
            "SELECT fast_mode FROM sessions WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
        return bool(int(row["fast_mode"] or 0)) if row else False

    async def set_fast_mode(self, chat_id: int, enabled: bool) -> None:
        await self.ensure_session(chat_id)
        await self._conn.execute(
            "UPDATE sessions SET fast_mode=?, updated_at=? WHERE chat_id=?",
            (1 if enabled else 0, now_ts(), chat_id))
        await self._conn.commit()

    async def get_show_thinking_override(self, chat_id: int) -> bool | None:
        """读取当前会话 thinking 展示覆盖值。None 表示跟随配置默认值。"""
        await self.ensure_session(chat_id)
        cur = await self._conn.execute(
            "SELECT show_thinking FROM sessions WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
        raw = row["show_thinking"] if row and row["show_thinking"] is not None else -1
        value = int(raw)
        if value < 0:
            return None
        return bool(value)

    async def get_show_thinking(self, chat_id: int, *, default: bool = False) -> bool:
        override = await self.get_show_thinking_override(chat_id)
        return default if override is None else override

    async def set_show_thinking(self, chat_id: int, enabled: bool) -> None:
        await self.ensure_session(chat_id)
        await self._conn.execute(
            "UPDATE sessions SET show_thinking=?, updated_at=? WHERE chat_id=?",
            (1 if enabled else 0, now_ts(), chat_id))
        await self._conn.commit()

    async def add_model_call(
        self,
        chat_id: int,
        *,
        commit: bool = True,
        session_uuid: str = "",
        model: str = "",
        protocol: str = "",
        think_level: str = "",
        call_kind: str = "",
        usage: Usage | None = None,
        last_usage: Usage | None = None,
        expert_usage: Usage | None = None,
        cost_usd: float = 0.0,
        connect_ms: int = 0,
        first_token_ms: int = 0,
        total_time_ms: int = 0,
        peak_tps: float = 0.0,
        min_tps: float = 0.0,
        status: str = "ok",
        model_call_count: int = 1,
        model_ok_count: int = 1,
        model_retry_count: int = 0,
        model_fail_count: int = 0,
        expert_tool_calls: int = 0,
        error_type: str = "",
    ) -> int:
        u = usage or Usage()
        lu = last_usage or Usage()
        eu = expert_usage or Usage()
        cur = await self._conn.execute(
            """
            INSERT INTO model_calls (
              chat_id, session_uuid, model, protocol, think_level, call_kind,
              input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
              last_input_tokens, last_output_tokens, last_cache_read_tokens, last_cache_write_tokens,
              expert_input_tokens, expert_output_tokens, expert_cache_read_tokens, expert_cache_write_tokens,
              expert_tool_calls, cost_usd, connect_ms, first_token_ms, total_time_ms, peak_tps, min_tps,
              status, model_call_count, model_ok_count, model_retry_count, model_fail_count,
              error_type, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (chat_id, session_uuid, model, protocol, think_level, str(call_kind or ""),
             u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens,
             lu.input_tokens, lu.output_tokens, lu.cache_read_tokens, lu.cache_write_tokens,
             eu.input_tokens, eu.output_tokens, eu.cache_read_tokens, eu.cache_write_tokens,
             max(0, int(expert_tool_calls)), cost_usd, connect_ms, first_token_ms, total_time_ms,
             max(0.0, float(peak_tps or 0.0)), max(0.0, float(min_tps or 0.0)),
             status, max(0, int(model_call_count)), max(0, int(model_ok_count)),
             max(0, int(model_retry_count)), max(0, int(model_fail_count)),
             error_type, now_ts()))
        if commit:
            await self._conn.commit()
        return cur.lastrowid or 0

    async def add_tool_call(
        self,
        chat_id: int,
        *,
        session_uuid: str = "",
        tool_name: str = "",
        status: str = "ok",
        duration_ms: int = 0,
        result_size: int = 0,
        error_type: str = "",
    ) -> int:
        cur = await self._conn.execute(
            """
            INSERT INTO tool_calls (
              chat_id, session_uuid, tool_name, status, duration_ms,
              result_size, error_type, created_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (chat_id, session_uuid, tool_name, status, duration_ms,
             result_size, error_type, now_ts()))
        await self._conn.commit()
        return cur.lastrowid or 0

    async def recent_model_calls(self, chat_id: int, limit: int = 10) -> list[ModelCallRow]:
        cur = await self._conn.execute(
            "SELECT * FROM model_calls WHERE chat_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
            (chat_id, max(1, int(limit))))
        return [self._model_call_row(r) for r in await cur.fetchall()]

    async def set_controller_context_usage(
        self,
        chat_id: int,
        *,
        session_uuid: str,
        tokens: int | None,
        summary_id: int | None = None,
        commit: bool = True,
    ) -> None:
        """Store an exact provider snapshot or an explicit unknown tombstone."""
        session = str(session_uuid or "").strip()
        if not session:
            return
        generation = summary_id
        if generation is None:
            cur = await self._conn.execute(
                "SELECT COALESCE(MAX(id),0) AS generation FROM summaries WHERE chat_id=?",
                (int(chat_id),),
            )
            generation = int((await cur.fetchone())["generation"] or 0)
        known = tokens is not None
        await self._conn.execute(
            """INSERT INTO web_controller_context_snapshots(
                   chat_id, session_uuid, summary_id, known, tokens, updated_at
               ) VALUES(?,?,?,?,?,?)
               ON CONFLICT(chat_id) DO UPDATE SET
                   session_uuid=excluded.session_uuid,
                   summary_id=excluded.summary_id,
                   known=excluded.known,
                   tokens=excluded.tokens,
                   updated_at=excluded.updated_at""",
            (
                int(chat_id),
                session,
                max(0, int(generation or 0)),
                1 if known else 0,
                max(0, int(tokens or 0)) if known else 0,
                now_ts(),
            ),
        )
        if commit:
            await self._conn.commit()

    async def latest_controller_context_usage(self, chat_id: int, *, session_uuid: str = "") -> int | None:
        """Return exact latest successful controller prompt usage, or None when unknown.

        The summary generation binds each snapshot. An explicit unknown tombstone
        prevents legacy model-call fallback after compaction or transcript mutation.
        """
        if not session_uuid:
            return None
        cur = await self._conn.execute(
            """SELECT snapshot.known, snapshot.tokens FROM web_controller_context_snapshots snapshot
               WHERE snapshot.chat_id=? AND snapshot.session_uuid=?
                 AND snapshot.summary_id=COALESCE((SELECT MAX(id) FROM summaries WHERE chat_id=?),0)""",
            (int(chat_id), str(session_uuid), int(chat_id)),
        )
        snapshot = await cur.fetchone()
        if snapshot is not None:
            return max(0, int(snapshot["tokens"] or 0)) if bool(snapshot["known"]) else None

        where = "mc.chat_id=? AND mc.call_kind='controller_request' AND mc.status='ok'"
        params: list[Any] = [int(chat_id)]
        if session_uuid:
            where += " AND mc.session_uuid=?"
            params.append(str(session_uuid))
        cur = await self._conn.execute(
            f"""SELECT mc.last_input_tokens, mc.last_cache_read_tokens, mc.last_cache_write_tokens,
                       mc.input_tokens, mc.cache_read_tokens, mc.cache_write_tokens
                FROM model_calls mc WHERE {where}
                  AND mc.created_at > COALESCE((SELECT MAX(s.created_at) FROM summaries s WHERE s.chat_id=mc.chat_id),0)
                ORDER BY mc.created_at DESC, mc.id DESC LIMIT 1""", tuple(params))
        row = await cur.fetchone()
        if row is None:
            return None
        last_total = sum(int(row[key] or 0) for key in ("last_input_tokens", "last_cache_read_tokens", "last_cache_write_tokens"))
        aggregate_total = max(0, sum(int(row[key] or 0) for key in ("input_tokens", "cache_read_tokens", "cache_write_tokens")))
        # Legacy rows predate the explicit snapshot table.  A positive provider
        # total can be recovered, but an all-zero row is indistinguishable from
        # a call where the provider omitted usage and must remain unknown.
        return last_total if last_total > 0 else (aggregate_total if aggregate_total > 0 else None)

    async def latest_controller_prompt_tokens(self, chat_id: int, *, session_uuid: str = "") -> int:
        """Return the latest real controller-request prompt usage in this context epoch.

        Child Agent requests and aggregate run rows have different ``call_kind``
        values. A controller request at or before the latest summary may belong to
        the pre-compaction epoch, so ambiguous same-second ordering deliberately
        falls back to the caller's assembled-prompt estimate.
        """
        exact = await self.latest_controller_context_usage(chat_id, session_uuid=session_uuid)
        return max(0, int(exact or 0))


    async def recent_tool_calls(self, chat_id: int, limit: int = 10) -> list[ToolCallRow]:
        cur = await self._conn.execute(
            "SELECT * FROM tool_calls WHERE chat_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
            (chat_id, max(1, int(limit))))
        return [self._tool_call_row(r) for r in await cur.fetchall()]

    async def model_call_summary(self, chat_id: int, limit: int = 8) -> list[dict[str, Any]]:
        cur = await self._conn.execute(
            """
            SELECT model,
                   SUM(COALESCE(model_call_count, 1)) AS total,
                   SUM(COALESCE(model_ok_count, CASE WHEN status='ok' THEN 1 ELSE 0 END)) AS ok_count,
                   SUM(input_tokens + cache_read_tokens + cache_write_tokens) AS prompt_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(cache_read_tokens + cache_write_tokens) AS cache_tokens,
                   SUM(cost_usd) AS cost_usd,
                   SUM(first_token_ms) * 1.0 / NULLIF(SUM(COALESCE(model_ok_count, CASE WHEN status='ok' THEN 1 ELSE 0 END)), 0) AS avg_first_ms,
                   SUM(total_time_ms) * 1.0 / NULLIF(SUM(COALESCE(model_ok_count, CASE WHEN status='ok' THEN 1 ELSE 0 END)), 0) AS avg_total_ms
            FROM model_calls
            WHERE chat_id=?
            GROUP BY model
            ORDER BY total DESC, model ASC
            LIMIT ?
            """,
            (chat_id, max(1, int(limit))))
        return [dict(r) for r in await cur.fetchall()]

    async def tool_call_summary(self, chat_id: int, limit: int = 8) -> list[dict[str, Any]]:
        cur = await self._conn.execute(
            """
            SELECT tool_name, COUNT(*) AS total,
                   SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_count,
                   SUM(result_size) AS result_size,
                   AVG(NULLIF(duration_ms, 0)) AS avg_duration_ms
            FROM tool_calls
            WHERE chat_id=?
            GROUP BY tool_name
            ORDER BY total DESC, tool_name ASC
            LIMIT ?
            """,
            (chat_id, max(1, int(limit))))
        return [dict(r) for r in await cur.fetchall()]

    async def recent_errors(self, chat_id: int, limit: int = 10) -> list[dict[str, Any]]:
        cur = await self._conn.execute(
            """
            SELECT 'model' AS kind, model AS name, error_type, created_at
            FROM model_calls WHERE chat_id=? AND status!='ok'
            UNION ALL
            SELECT 'tool' AS kind, tool_name AS name, error_type, created_at
            FROM tool_calls WHERE chat_id=? AND status!='ok'
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (chat_id, chat_id, max(1, int(limit))))
        return [dict(r) for r in await cur.fetchall()]

    async def all_time_totals(self, chat_id: int) -> dict[str, Any]:
        cur = await self._conn.execute(
            """
            SELECT COUNT(DISTINCT CASE WHEN session_uuid!='' THEN session_uuid END) AS session_count,
                   SUM(COALESCE(model_call_count, 1)) AS conversation_count,
                   SUM(COALESCE(model_call_count, 1)) AS model_call_count,
                   SUM(COALESCE(model_ok_count, CASE WHEN status='ok' THEN 1 ELSE 0 END)) AS ok_count,
                   SUM(COALESCE(model_fail_count, CASE WHEN status!='ok' THEN 1 ELSE 0 END)) AS fail_count,
                   SUM(COALESCE(model_retry_count, 0)) AS retry_count,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(cache_read_tokens) AS cache_read_tokens,
                   SUM(cache_write_tokens) AS cache_write_tokens,
                   SUM(cost_usd) AS cost_usd,
                   SUM(total_time_ms) AS total_time_ms
            FROM model_calls
            WHERE chat_id=?
            """,
            (chat_id,))
        row = await cur.fetchone()
        data = dict(row) if row else {}
        cur2 = await self._conn.execute(
            "SELECT COUNT(*) AS tool_calls FROM tool_calls WHERE chat_id=?", (chat_id,))
        tool_row = await cur2.fetchone()
        data["tool_calls"] = int((tool_row["tool_calls"] if tool_row else 0) or 0)
        return data

    @staticmethod
    def _chat_id_filter(chat_ids: int | Iterable[int]) -> tuple[str, tuple[int, ...]]:
        if isinstance(chat_ids, int):
            ids = [chat_ids]
        else:
            ids = [int(item) for item in chat_ids]
        ids = sorted({int(item) for item in ids})
        if not ids:
            # Keep SQL valid and deliberately match nothing.
            return "chat_id IN (NULL)", ()
        placeholders = ",".join("?" for _ in ids)
        return f"chat_id IN ({placeholders})", tuple(ids)

    async def provider_call_summary(self, chat_id: int | Iterable[int]) -> list[dict[str, Any]]:
        chat_filter, params = self._chat_id_filter(chat_id)
        cur = await self._conn.execute(
            f"""
            SELECT CASE WHEN instr(model, '/') > 0 THEN substr(model, 1, instr(model, '/') - 1)
                        ELSE model END AS provider,
                   COUNT(*) AS runs,
                   SUM(COALESCE(model_call_count, 1)) AS calls,
                   SUM(COALESCE(model_ok_count, CASE WHEN status='ok' THEN 1 ELSE 0 END)) AS ok_count,
                   SUM(COALESCE(model_fail_count, CASE WHEN status!='ok' THEN 1 ELSE 0 END)) AS fail_count,
                   SUM(COALESCE(model_retry_count, 0)) AS retry_count,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(cache_read_tokens) AS cache_read_tokens,
                   SUM(cache_write_tokens) AS cache_write_tokens,
                   SUM(cost_usd) AS cost_usd,
                   SUM(total_time_ms) AS total_time_ms,
                   SUM(total_time_ms) * 1.0 / NULLIF(SUM(COALESCE(model_ok_count, CASE WHEN status='ok' THEN 1 ELSE 0 END)), 0) AS avg_total_ms,
                   SUM(connect_ms) * 1.0 / NULLIF(SUM(COALESCE(model_ok_count, CASE WHEN status='ok' THEN 1 ELSE 0 END)), 0) AS avg_connect_ms,
                   SUM(first_token_ms) * 1.0 / NULLIF(SUM(COALESCE(model_ok_count, CASE WHEN status='ok' THEN 1 ELSE 0 END)), 0) AS avg_first_ms,
                   SUM(output_tokens) * 1000.0 / NULLIF(SUM(total_time_ms), 0) AS avg_tps,
                   MAX(peak_tps) AS peak_tps,
                   MIN(CASE WHEN min_tps > 0 THEN min_tps END) AS min_tps
            FROM model_calls
            WHERE {chat_filter} AND model!=''
            GROUP BY provider
            """,
            params)
        return [dict(r) for r in await cur.fetchall()]

    async def model_detail_summary(self, chat_id: int | Iterable[int], provider_name: str) -> list[dict[str, Any]]:
        prefix = f"{provider_name}/%"
        chat_filter, params = self._chat_id_filter(chat_id)
        cur = await self._conn.execute(
            f"""
            SELECT model,
                   COUNT(*) AS runs,
                   SUM(COALESCE(model_call_count, 1)) AS calls,
                   SUM(COALESCE(model_ok_count, CASE WHEN status='ok' THEN 1 ELSE 0 END)) AS ok_count,
                   SUM(COALESCE(model_fail_count, CASE WHEN status!='ok' THEN 1 ELSE 0 END)) AS fail_count,
                   SUM(COALESCE(model_retry_count, 0)) AS retry_count,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(cache_read_tokens) AS cache_read_tokens,
                   SUM(cache_write_tokens) AS cache_write_tokens,
                   SUM(cost_usd) AS cost_usd,
                   SUM(total_time_ms) AS total_time_ms,
                   SUM(total_time_ms) * 1.0 / NULLIF(SUM(COALESCE(model_ok_count, CASE WHEN status='ok' THEN 1 ELSE 0 END)), 0) AS avg_total_ms,
                   SUM(connect_ms) * 1.0 / NULLIF(SUM(COALESCE(model_ok_count, CASE WHEN status='ok' THEN 1 ELSE 0 END)), 0) AS avg_connect_ms,
                   SUM(first_token_ms) * 1.0 / NULLIF(SUM(COALESCE(model_ok_count, CASE WHEN status='ok' THEN 1 ELSE 0 END)), 0) AS avg_first_ms,
                   SUM(output_tokens) * 1000.0 / NULLIF(SUM(total_time_ms), 0) AS avg_tps,
                   MAX(peak_tps) AS peak_tps,
                   MIN(CASE WHEN min_tps > 0 THEN min_tps END) AS min_tps
            FROM model_calls
            WHERE {chat_filter} AND model LIKE ?
            GROUP BY model
            """,
            (*params, prefix))
        return [dict(r) for r in await cur.fetchall()]

    # Tables/columns that store durable provider/model labels like "openai/gpt".
    # Channel rename must rewrite these, otherwise channel/model stats and
    # conversation model selections stay stuck on the old key.
    _MODEL_LABEL_COLUMNS: tuple[tuple[str, str], ...] = (
        ("model_calls", "model"),
        ("sessions", "last_model"),
        ("web_conversations", "model"),
        ("web_conversations", "agent_model"),
        ("web_conversation_defaults", "main_model"),
        ("web_conversation_defaults", "agent_model"),
        ("rath_agents", "model"),
        ("web_tg_notification_runs", "model"),
    )

    async def rewrite_model_label_prefix(
        self,
        old_provider: str,
        new_provider: str,
        *,
        commit: bool = True,
    ) -> dict[str, int]:
        """Case-sensitive rewrite of stored model labels after provider rename.

        Only rows starting with ``old_provider + '/'`` are touched. Returns
        ``{table.column: updated_rows}`` for non-zero updates.
        """
        old_name = str(old_provider or "").strip()
        new_name = str(new_provider or "").strip()
        if not old_name or not new_name or old_name == new_name:
            return {}
        old_prefix = old_name + "/"
        new_prefix = new_name + "/"
        prefix_len = len(old_prefix)
        updated: dict[str, int] = {}
        for table, column in self._MODEL_LABEL_COLUMNS:
            cur = await self._conn.execute(
                f"""
                UPDATE {table}
                SET {column} = ? || substr({column}, ?)
                WHERE substr({column}, 1, ?) = ?
                """,
                (new_prefix, prefix_len + 1, prefix_len, old_prefix),
            )
            count = int(cur.rowcount or 0)
            if count:
                updated[f"{table}.{column}"] = count
        if commit:
            await self._conn.commit()
        return updated

    async def rewrite_model_label(
        self,
        old_fullname: str,
        new_fullname: str,
        *,
        commit: bool = True,
    ) -> dict[str, int]:
        """Exact rewrite of one stored model fullname (e.g. model id rename)."""
        old_name = str(old_fullname or "").strip()
        new_name = str(new_fullname or "").strip()
        if not old_name or not new_name or old_name == new_name:
            return {}
        updated: dict[str, int] = {}
        for table, column in self._MODEL_LABEL_COLUMNS:
            cur = await self._conn.execute(
                f"""
                UPDATE {table}
                SET {column} = ?
                WHERE {column} = ?
                """,
                (new_name, old_name),
            )
            count = int(cur.rowcount or 0)
            if count:
                updated[f"{table}.{column}"] = count
        if commit:
            await self._conn.commit()
        return updated

    @staticmethod
    def _model_call_row(r: Any) -> ModelCallRow:
        return ModelCallRow(
            id=r["id"], chat_id=r["chat_id"], session_uuid=r["session_uuid"] or "",
            model=r["model"] or "", protocol=r["protocol"] or "",
            think_level=r["think_level"] or "",
            call_kind=(r["call_kind"] if "call_kind" in r.keys() else "") or "",
            input_tokens=r["input_tokens"] or 0,
            output_tokens=r["output_tokens"] or 0,
            cache_read_tokens=r["cache_read_tokens"] or 0,
            cache_write_tokens=r["cache_write_tokens"] or 0,
            cost_usd=r["cost_usd"] or 0.0,
            connect_ms=r["connect_ms"] or 0,
            first_token_ms=r["first_token_ms"] or 0,
            total_time_ms=r["total_time_ms"] or 0,
            peak_tps=(r["peak_tps"] if "peak_tps" in r.keys() else 0.0) or 0.0,
            min_tps=(r["min_tps"] if "min_tps" in r.keys() else 0.0) or 0.0,
            status=r["status"] or "ok",
            model_call_count=(r["model_call_count"] if "model_call_count" in r.keys() else 1) or 0,
            model_ok_count=(r["model_ok_count"] if "model_ok_count" in r.keys() else 1) or 0,
            model_retry_count=(r["model_retry_count"] if "model_retry_count" in r.keys() else 0) or 0,
            model_fail_count=(r["model_fail_count"] if "model_fail_count" in r.keys() else 0) or 0,
            expert_tool_calls=(r["expert_tool_calls"] if "expert_tool_calls" in r.keys() else 0) or 0,
            error_type=r["error_type"] or "",
            created_at=r["created_at"] or 0,
        )

    @staticmethod
    def _tool_call_row(r: Any) -> ToolCallRow:
        return ToolCallRow(
            id=r["id"], chat_id=r["chat_id"], session_uuid=r["session_uuid"] or "",
            tool_name=r["tool_name"] or "",
            status=r["status"] or "ok",
            duration_ms=r["duration_ms"] or 0,
            result_size=r["result_size"] or 0,
            error_type=r["error_type"] or "",
            created_at=r["created_at"] or 0,
        )

    async def add_usage(
        self,
        chat_id: int,
        usage: Usage,
        cost_usd: float = 0.0,
        *,
        commit: bool = True,
        last_usage: Usage | None = None,
        last_cost_usd: float | None = None,
        connect_ms: int = 0,
        first_token_ms: int = 0,
        total_time_ms: int = 0,
        run_total_time_ms: int = 0,
        run_model_calls: int = 0,
        run_tool_calls: int = 0,
        model: str = "",
        protocol: str = "",
        think_level: str = "",
    ) -> None:
        """记录一轮用量。

        usage      — 本轮跨多次 API 调用的累加值，只用于累计花费/账单。
        last_usage — 最后一次 API 调用的快照；prompt(input+cache) = 模型实际看到的
                     整个上下文体积，存进 last_* 供「最近模型调用」显示与上下文占用。
                     为 None 时回退用 usage（单轮无工具场景两者相等）。
        cost_usd   — 最近一次 Agent run 总费用。
        last_cost_usd — 最近一次模型 API 调用费用；为 None 时回退 cost_usd。
        """
        last = last_usage if last_usage is not None else usage
        last_call_cost = cost_usd if last_cost_usd is None else last_cost_usd
        await self.ensure_session(chat_id, commit=commit)
        ts = now_ts()
        # last_* 是「最近一次成功模型调用」快照，status 的「上下文」「最近一轮」都读它。
        # 失败 / 空 usage 轮（如上游 400 在首调就拒收，last 的 prompt 体积为 0）不能
        # 用这组 0 覆盖上一次成功快照，否则上下文会被显示成 0/最近一轮全 0。
        # 累计用量(usage_*)照常累加（失败轮本就是 0，加 0 无副作用）。
        last_prompt = last.input_tokens + last.cache_read_tokens + last.cache_write_tokens
        if last_prompt > 0:
            await self._conn.execute(
                """
                UPDATE sessions SET
                  usage_input_tokens = COALESCE(usage_input_tokens, 0) + ?,
                  usage_output_tokens = COALESCE(usage_output_tokens, 0) + ?,
                  usage_cache_read_tokens = COALESCE(usage_cache_read_tokens, 0) + ?,
                  usage_cache_write_tokens = COALESCE(usage_cache_write_tokens, 0) + ?,
                  usage_cost_usd = COALESCE(usage_cost_usd, 0) + ?,
                  last_input_tokens = ?,
                  last_output_tokens = ?,
                  last_cache_read_tokens = ?,
                  last_cache_write_tokens = ?,
                  last_cost_usd = ?,
                  last_connect_ms = ?,
                  last_first_token_ms = ?,
                  last_total_time_ms = ?,
                  last_run_cost_usd = ?,
                  last_run_total_time_ms = ?,
                  last_run_model_calls = ?,
                  last_run_tool_calls = ?,
                  last_model = ?,
                  last_protocol = ?,
                  last_think_level = ?,
                  last_created_at = ?,
                  updated_at = ?
                WHERE chat_id = ?
                """,
                (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens,
                 usage.cache_write_tokens, cost_usd, last.input_tokens, last.output_tokens,
                 last.cache_read_tokens, last.cache_write_tokens, last_call_cost, connect_ms,
                 first_token_ms, total_time_ms, cost_usd, run_total_time_ms, run_model_calls,
                 run_tool_calls, model, protocol, think_level, ts, ts, chat_id))
        else:
            # 失败 / 空轮：只累加累计用量，保留上一次成功的 last_* 快照。
            await self._conn.execute(
                """
                UPDATE sessions SET
                  usage_input_tokens = COALESCE(usage_input_tokens, 0) + ?,
                  usage_output_tokens = COALESCE(usage_output_tokens, 0) + ?,
                  usage_cache_read_tokens = COALESCE(usage_cache_read_tokens, 0) + ?,
                  usage_cache_write_tokens = COALESCE(usage_cache_write_tokens, 0) + ?,
                  usage_cost_usd = COALESCE(usage_cost_usd, 0) + ?,
                  updated_at = ?
                WHERE chat_id = ?
                """,
                (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens,
                 usage.cache_write_tokens, cost_usd, ts, chat_id))
        if commit:
            await self._conn.commit()

    async def bump_user_turn(self, chat_id: int) -> None:
        """用户发来一条消息：user_turns+1；若本轮还没开始时间则记为现在。"""
        await self.ensure_session(chat_id)
        ts = now_ts()
        await self._conn.execute(
            """
            UPDATE sessions SET
              stat_user_turns = COALESCE(stat_user_turns, 0) + 1,
              turn_started_at = CASE WHEN COALESCE(turn_started_at, 0) = 0 THEN ?
                                     ELSE turn_started_at END,
              updated_at = ?
            WHERE chat_id = ?
            """,
            (ts, ts, chat_id))
        await self._conn.commit()

    async def add_turn_stats(
        self, chat_id: int, *, commit: bool = True, tool_calls: int = 0, model_calls: int = 0,
        model_ok: int = 0, model_retry: int = 0, model_fail: int = 0,
        connect_ms_sum: int = 0, first_token_ms_sum: int = 0,
        total_time_ms_sum: int = 0, output_tokens_sum: int = 0,
    ) -> None:
        """累加一次 Agent.run 的统计到本轮计数。"""
        await self.ensure_session(chat_id, commit=commit)
        await self._conn.execute(
            """
            UPDATE sessions SET
              stat_tool_calls = COALESCE(stat_tool_calls, 0) + ?,
              stat_model_calls = COALESCE(stat_model_calls, 0) + ?,
              stat_model_ok = COALESCE(stat_model_ok, 0) + ?,
              stat_model_retry = COALESCE(stat_model_retry, 0) + ?,
              stat_model_fail = COALESCE(stat_model_fail, 0) + ?,
              stat_connect_ms_sum = COALESCE(stat_connect_ms_sum, 0) + ?,
              stat_first_token_ms_sum = COALESCE(stat_first_token_ms_sum, 0) + ?,
              stat_total_time_ms_sum = COALESCE(stat_total_time_ms_sum, 0) + ?,
              stat_output_tokens_sum = COALESCE(stat_output_tokens_sum, 0) + ?,
              updated_at = ?
            WHERE chat_id = ?
            """,
            (tool_calls, model_calls, model_ok, model_retry, model_fail,
             connect_ms_sum, first_token_ms_sum, total_time_ms_sum, output_tokens_sum,
             now_ts(), chat_id))
        if commit:
            await self._conn.commit()

    async def reset_turn_stats(self, chat_id: int) -> None:
        """清零本轮统计 + 累计用量 + 最近一轮快照（一并开新账）。

        last_* 也必须清——「上下文」「最近一轮」两个展示块读的就是这组字段，
        不清的话新会话仍会显示上一轮的 prompt 体积和耗时。
        """
        await self.ensure_session(chat_id)
        await self._conn.execute(
            """
            UPDATE sessions SET
              turn_started_at = 0, stat_user_turns = 0, stat_tool_calls = 0,
              stat_model_calls = 0, stat_model_ok = 0, stat_model_retry = 0,
              stat_model_fail = 0, stat_connect_ms_sum = 0, stat_first_token_ms_sum = 0,
              stat_total_time_ms_sum = 0, stat_output_tokens_sum = 0,
              usage_input_tokens = 0, usage_output_tokens = 0,
              usage_cache_read_tokens = 0, usage_cache_write_tokens = 0, usage_cost_usd = 0,
              last_input_tokens = 0, last_output_tokens = 0, last_cache_read_tokens = 0,
              last_cache_write_tokens = 0, last_cost_usd = 0, last_connect_ms = 0,
              last_first_token_ms = 0, last_total_time_ms = 0,
              last_run_cost_usd = 0, last_run_total_time_ms = 0,
              last_run_model_calls = 0, last_run_tool_calls = 0,
              last_model = '',
              last_protocol = '', last_think_level = '', last_created_at = 0,
              session_uuid = '', system_snapshot = '',
              updated_at = ?
            WHERE chat_id = ?
            """,
            (now_ts(), chat_id))
        await self._conn.execute(
            "DELETE FROM controller_model_contexts WHERE chat_id=?", (chat_id,)
        )
        await self._conn.execute(
            "DELETE FROM web_controller_context_snapshots WHERE chat_id=?", (chat_id,)
        )
        await self._conn.execute(
            "DELETE FROM web_memory_reminders WHERE chat_id=?", (chat_id,)
        )
        await self._conn.commit()

    async def usage_totals(self, chat_id: int) -> UsageTotals:
        await self.ensure_session(chat_id)
        cur = await self._conn.execute(
            """
            SELECT usage_input_tokens, usage_output_tokens,
                   usage_cache_read_tokens, usage_cache_write_tokens, usage_cost_usd,
                   last_input_tokens, last_output_tokens, last_cache_read_tokens,
                   last_cache_write_tokens, last_cost_usd, last_connect_ms,
                   last_first_token_ms, last_total_time_ms,
                   last_run_cost_usd, last_run_total_time_ms,
                   last_run_model_calls, last_run_tool_calls,
                   last_model, last_protocol,
                   last_think_level, last_created_at,
                   turn_started_at, stat_user_turns, stat_tool_calls, stat_model_calls,
                   stat_model_ok, stat_model_retry, stat_model_fail,
                   stat_connect_ms_sum, stat_first_token_ms_sum, stat_total_time_ms_sum,
                   stat_output_tokens_sum
            FROM sessions WHERE chat_id=?
            """,
            (chat_id,))
        row = await cur.fetchone()
        if not row:
            return UsageTotals()
        return UsageTotals(
            input_tokens=row["usage_input_tokens"] or 0,
            output_tokens=row["usage_output_tokens"] or 0,
            cache_read_tokens=row["usage_cache_read_tokens"] or 0,
            cache_write_tokens=row["usage_cache_write_tokens"] or 0,
            cost_usd=row["usage_cost_usd"] or 0.0,
            last_input_tokens=row["last_input_tokens"] or 0,
            last_output_tokens=row["last_output_tokens"] or 0,
            last_cache_read_tokens=row["last_cache_read_tokens"] or 0,
            last_cache_write_tokens=row["last_cache_write_tokens"] or 0,
            last_cost_usd=row["last_cost_usd"] or 0.0,
            last_connect_ms=row["last_connect_ms"] or 0,
            last_first_token_ms=row["last_first_token_ms"] or 0,
            last_total_time_ms=row["last_total_time_ms"] or 0,
            last_run_cost_usd=row["last_run_cost_usd"] or 0.0,
            last_run_total_time_ms=row["last_run_total_time_ms"] or 0,
            last_run_model_calls=row["last_run_model_calls"] or 0,
            last_run_tool_calls=row["last_run_tool_calls"] or 0,
            last_model=row["last_model"] or "",
            last_protocol=row["last_protocol"] or "",
            last_think_level=row["last_think_level"] or "",
            last_created_at=row["last_created_at"] or 0,
            turn_started_at=row["turn_started_at"] or 0,
            stat_user_turns=row["stat_user_turns"] or 0,
            stat_tool_calls=row["stat_tool_calls"] or 0,
            stat_model_calls=row["stat_model_calls"] or 0,
            stat_model_ok=row["stat_model_ok"] or 0,
            stat_model_retry=row["stat_model_retry"] or 0,
            stat_model_fail=row["stat_model_fail"] or 0,
            stat_connect_ms_sum=row["stat_connect_ms_sum"] or 0,
            stat_first_token_ms_sum=row["stat_first_token_ms_sum"] or 0,
            stat_total_time_ms_sum=row["stat_total_time_ms_sum"] or 0,
            stat_output_tokens_sum=row["stat_output_tokens_sum"] or 0,
        )

    async def add(self, chat_id: int, role: str, content: str = "", *,
                  reasoning: str = "", signature: str = "",
                  tool_calls: list[ToolCall] | None = None,
                  tool_call_id: str = "", name: str = "", tokens: int = 0,
                  conversation_uuid: str = "",
                  turn_uuid: str = "",
                  parent_turn_uuid: str = "",
                  run_root_turn_uuid: str = "",
                  task_uuid: str = "",
                  agent_session_uuid: str = "",
                  commit: bool = True) -> int:
        tc_json = None
        if tool_calls:
            tc_json = json.dumps([{"id": t.id, "name": t.name, "arguments": t.arguments}
                                  for t in tool_calls], ensure_ascii=False)
        turn = str(turn_uuid or "").strip()
        root_turn = str(run_root_turn_uuid or turn or "").strip()
        cur = await self._conn.execute(
            "INSERT INTO messages (chat_id, role, content, reasoning, signature, "
            "tool_calls_json, tool_call_id, name, tokens, compacted, created_at, "
            "conversation_uuid, turn_uuid, parent_turn_uuid, run_root_turn_uuid, "
            "task_uuid, agent_session_uuid) "
            "VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?)",
            (chat_id, role, content, reasoning, signature, tc_json,
             tool_call_id, name, tokens, now_ts(),
             str(conversation_uuid or "").strip(),
             turn,
             str(parent_turn_uuid or "").strip(),
             root_turn,
             str(task_uuid or "").strip(),
             str(agent_session_uuid or "").strip()))
        if commit:
            await self._conn.commit()
        return cur.lastrowid or 0

    async def repair_dangling_tool_calls(self, chat_id: int) -> int:
        """补齐最近一条 assistant 的「光杆 tool_call」(落 isError 占位 tool 结果)。

        被停止/新消息打断时,工具串行执行,后续 tool_call 来不及产生结果,但 assistant
        那条(含全部 tool_calls)已落库 → DB 留下无结果的调用。这里在中止收尾时从源头补齐,
        避免脏历史污染后续请求(build_history 的 repair_tool_pairing 是第二道兜底)。

        幂等:已有配对结果的 call 不重复补。返回补了几条。
        """
        cur = await self._conn.execute(
            "SELECT id, tool_calls_json FROM messages "
            "WHERE chat_id=? AND role='assistant' AND tool_calls_json IS NOT NULL "
            "AND compacted=0 ORDER BY id DESC LIMIT 1", (chat_id,))
        row = await cur.fetchone()
        if not row or not row["tool_calls_json"]:
            return 0
        try:
            calls = json.loads(row["tool_calls_json"])
        except (json.JSONDecodeError, TypeError):
            return 0
        if not isinstance(calls, list) or not calls:
            return 0
        asst_id = row["id"]
        # 该 assistant 之后已存在的 tool 结果 id 集合(只看本轮之后,避免跨轮误判)
        cur2 = await self._conn.execute(
            "SELECT tool_call_id FROM messages "
            "WHERE chat_id=? AND role='tool' AND id>?", (chat_id, asst_id))
        have = {r["tool_call_id"] for r in await cur2.fetchall()}
        added = 0
        for c in calls:
            if not isinstance(c, dict):
                continue
            cid = c.get("id")
            if cid and cid not in have:
                await self.add(chat_id, "tool",
                               "[openbear] 工具调用被中止,未返回结果。",
                               tool_call_id=cid, name=c.get("name", ""))
                added += 1
        return added

    async def recent(self, chat_id: int, limit: int | None = None) -> list[MsgRow]:
        """取未压缩消息（按 id 升序）。

        默认读取全部未压缩消息，不能静默丢弃旧消息：系统提示词、工具调用配对、
        Anthropic thinking signature 等关键上下文都依赖完整历史。真正的上下文收敛
        交给 safeguard 压缩，而不是在这里按条数截断。
        """
        if limit is None:
            cur = await self._conn.execute(
                "SELECT * FROM messages WHERE chat_id=? AND compacted=0 ORDER BY id ASC",
                (chat_id,))
        else:
            cur = await self._conn.execute(
                "SELECT * FROM messages WHERE chat_id=? AND compacted=0 ORDER BY id ASC LIMIT ?",
                (chat_id, limit))
        rows = await cur.fetchall()
        return [self._row(r) for r in rows]

    async def recent_visible_history(self, chat_id: int, limit: int = 100) -> list[MsgRow]:
        """Return recent user-visible dialogue rows, including compacted source rows.

        A root-context summary keeps execution internals in the summary only. Its
        recent-history tail is deliberately reconstructed from the visible transcript:
        user messages and assistant replies without tool calls. Tool outputs,
        assistant tool-call envelopes, reasoning, and runtime state never cross this
        boundary even when their source rows are still retained for audit.
        """
        try:
            take = max(0, int(limit or 0))
        except (TypeError, ValueError):
            take = 100
        if take <= 0:
            return []
        cur = await self._conn.execute(
            """
            SELECT * FROM (
              SELECT * FROM messages
              WHERE chat_id=?
                AND COALESCE(content, '') <> ''
                AND (
                  role='user'
                  OR (role='assistant' AND tool_calls_json IS NULL)
                )
              ORDER BY id DESC
              LIMIT ?
            )
            ORDER BY id ASC
            """,
            (chat_id, take),
        )
        rows = await cur.fetchall()
        return [self._row(r) for r in rows]

    async def clear(self, chat_id: int) -> None:
        await self._conn.execute(
            "DELETE FROM web_operation_messages WHERE message_id IN (SELECT id FROM messages WHERE chat_id=?)",
            (chat_id,),
        )
        await self._conn.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
        await self._conn.execute("DELETE FROM summaries WHERE chat_id=?", (chat_id,))
        await self._conn.execute("DELETE FROM controller_model_contexts WHERE chat_id=?", (chat_id,))
        await self._conn.execute("DELETE FROM web_controller_context_snapshots WHERE chat_id=?", (chat_id,))
        await self._conn.execute("DELETE FROM web_memory_reminders WHERE chat_id=?", (chat_id,))
        # model_calls / tool_calls 是全时间统计明细，不随新会话清理；新会话只换 session_uuid。
        await self._conn.commit()

    async def delete_from_message_id(self, chat_id: int, first_message_id: int) -> dict[str, int]:
        """Delete one transcript suffix and invalidate any summary that covered it.

        The compacted source rows remain in ``messages`` until this transaction,
        so surviving rows before the cutoff can be made visible again whenever a
        cumulative summary becomes invalid. This is the safe primitive used by
        future "delete this turn and everything after it" APIs.
        """
        cutoff = int(first_message_id or 0)
        if cutoff <= 0:
            return {"messages": 0, "summaries": 0, "links": 0, "remainingSummaryUpTo": 0}
        async with self._conn.transaction(label="delete-transcript-suffix") as conn:
            cur = await conn.execute(
                "SELECT 1 FROM messages WHERE chat_id=? AND id=? LIMIT 1",
                (chat_id, cutoff),
            )
            if await cur.fetchone() is None:
                return {"messages": 0, "summaries": 0, "links": 0, "remainingSummaryUpTo": 0}
            await conn.execute(
                "DELETE FROM controller_model_contexts WHERE chat_id=?", (chat_id,)
            )
            await conn.execute(
                "DELETE FROM web_controller_context_snapshots WHERE chat_id=?", (chat_id,)
            )
            await conn.execute(
                "DELETE FROM web_memory_reminders WHERE chat_id=?", (chat_id,)
            )
            cur = await conn.execute(
                """
                DELETE FROM web_operation_messages
                WHERE message_id IN (SELECT id FROM messages WHERE chat_id=? AND id>=?)
                """,
                (chat_id, cutoff),
            )
            deleted_links = int(cur.rowcount or 0)
            cur = await conn.execute(
                "DELETE FROM messages WHERE chat_id=? AND id>=?",
                (chat_id, cutoff),
            )
            deleted_messages = int(cur.rowcount or 0)
            cur = await conn.execute(
                "DELETE FROM summaries WHERE chat_id=? AND up_to_message_id>=?",
                (chat_id, cutoff),
            )
            deleted_summaries = int(cur.rowcount or 0)
            cur = await conn.execute(
                """SELECT COALESCE(MAX(id), 0) AS summary_id,
                          COALESCE(MAX(up_to_message_id), 0) AS up_to
                   FROM summaries WHERE chat_id=?""",
                (chat_id,),
            )
            row = await cur.fetchone()
            remaining_summary_id = int((row["summary_id"] if row else 0) or 0)
            remaining_up_to = int((row["up_to"] if row else 0) or 0)
            cur = await conn.execute(
                "SELECT COALESCE(session_uuid,'') AS session_uuid FROM sessions WHERE chat_id=?",
                (chat_id,),
            )
            session_row = await cur.fetchone()
            session_uuid = str((session_row["session_uuid"] if session_row else "") or "")
            await MessageDAO(self._db, connection=conn).set_controller_context_usage(
                chat_id,
                session_uuid=session_uuid,
                tokens=None,
                summary_id=remaining_summary_id,
                commit=False,
            )
            await conn.execute(
                """
                UPDATE messages
                SET compacted=CASE WHEN id<=? THEN 1 ELSE 0 END
                WHERE chat_id=?
                """,
                (remaining_up_to, chat_id),
            )
        return {
            "messages": deleted_messages,
            "summaries": deleted_summaries,
            "links": deleted_links,
            "remainingSummaryUpTo": remaining_up_to,
        }

    async def has_history(self, chat_id: int) -> bool:
        """会话是否已有内容（未压缩消息或已压缩摘要任一存在）。

        用于模型切换判定：空会话（刚开新会话、还没发消息）可随意切换，包括跨家族。
        """
        cur = await self._conn.execute(
            "SELECT 1 FROM messages WHERE chat_id=? LIMIT 1", (chat_id,))
        if await cur.fetchone():
            return True
        cur = await self._conn.execute(
            "SELECT 1 FROM summaries WHERE chat_id=? LIMIT 1", (chat_id,))
        return bool(await cur.fetchone())

    async def mark_compacted(self, chat_id: int, up_to_id: int) -> None:
        await self._conn.execute(
            "UPDATE messages SET compacted=1 WHERE chat_id=? AND id<=?", (chat_id, up_to_id))
        await self._conn.commit()

    @staticmethod
    def _row(r: Any) -> MsgRow:
        tcs: list[ToolCall] = []
        if r["tool_calls_json"]:
            for t in json.loads(r["tool_calls_json"]):
                tcs.append(ToolCall(id=t.get("id", ""), name=t.get("name", ""),
                                    arguments=t.get("arguments", "")))
        keys = set(r.keys()) if hasattr(r, "keys") else set()
        return MsgRow(
            id=r["id"], chat_id=r["chat_id"], role=r["role"],
            content=r["content"] or "", reasoning=r["reasoning"] or "",
            signature=r["signature"] or "", tool_calls=tcs,
            tool_call_id=r["tool_call_id"] or "", name=r["name"] or "",
            tokens=r["tokens"] or 0, created_at=r["created_at"] or 0,
            conversation_uuid=(r["conversation_uuid"] if "conversation_uuid" in keys else "") or "",
            turn_uuid=(r["turn_uuid"] if "turn_uuid" in keys else "") or "",
            parent_turn_uuid=(r["parent_turn_uuid"] if "parent_turn_uuid" in keys else "") or "",
            run_root_turn_uuid=(r["run_root_turn_uuid"] if "run_root_turn_uuid" in keys else "") or "",
            task_uuid=(r["task_uuid"] if "task_uuid" in keys else "") or "",
            agent_session_uuid=(r["agent_session_uuid"] if "agent_session_uuid" in keys else "") or "",
        )


class SummaryDAO:
    def __init__(self, db: DB) -> None:
        self._db = db

    async def latest(self, chat_id: int) -> dict[str, Any] | None:
        cur = await self._db.conn.execute(
            "SELECT * FROM summaries WHERE chat_id=? ORDER BY id DESC LIMIT 1", (chat_id,))
        r = await cur.fetchone()
        return dict(r) if r else None

    async def get(self, chat_id: int, summary_id: int) -> dict[str, Any] | None:
        """Return one summary only within its internal conversation/chat owner."""
        cur = await self._db.conn.execute(
            "SELECT * FROM summaries WHERE chat_id=? AND id=? LIMIT 1",
            (int(chat_id), int(summary_id)),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_with_anchors(self, chat_id: int) -> list[dict[str, Any]]:
        """List summaries with the durable message boundary used for projection."""
        cur = await self._db.conn.execute(
            """
            SELECT summary.*, message.conversation_uuid AS anchor_conversation_uuid,
                   message.turn_uuid AS anchor_turn_uuid,
                   message.parent_turn_uuid AS anchor_parent_turn_uuid,
                   message.run_root_turn_uuid AS anchor_run_root_turn_uuid,
                   message.created_at AS anchor_created_at
            FROM summaries AS summary
            LEFT JOIN messages AS message
              ON message.id=summary.up_to_message_id AND message.chat_id=summary.chat_id
            WHERE summary.chat_id=?
            ORDER BY summary.id ASC
            """,
            (int(chat_id),),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def add(self, chat_id: int, summary: str, up_to_id: int, tokens: int) -> int:
        cur = await self._db.conn.execute(
            "INSERT INTO summaries (chat_id, summary, up_to_message_id, tokens, created_at) "
            "VALUES (?,?,?,?,?)", (chat_id, summary, up_to_id, tokens, now_ts()))
        await self._db.conn.commit()
        return cur.lastrowid or 0
