# ruff: noqa: F401,F403,F405
from __future__ import annotations

from app.task_memory import TaskMemoryDAO
from app.tools.agents import _render_agent_task_notification, _task_status_label
from app.web_console.core import *
from app.web_console.live_stream import *


class WebAdminConversationsMixin:
    _DUPLICATE_NOTIFICATION_TASK_BATCH_SIZE = 500
    _DISPLAY_ORDER_STEP = 1024.0

    async def _current_session_uuid(self, chat_id: int) -> str:
        return await MessageDAO(self.db).current_session_uuid(chat_id)

    def _web_conversation_json(self, row: dict[str, Any] | Any, *, live: _WebLiveStream | None = None, operation_facts: dict[str, Any] | None = None) -> dict[str, Any]:
        data = dict(row)
        conv_uuid = str(data.get("conversation_uuid") or "")
        startup_running = bool(self._web_starting_turns.get(conv_uuid))
        snapshot = live.snapshot() if live is not None else {}
        snapshot_running = bool(snapshot.get("running")) if snapshot else False
        op_facts = operation_facts if isinstance(operation_facts, dict) else {}
        has_operation_facts = bool(op_facts.get("hasOperations"))
        rath_active = int(op_facts.get("activeRathTaskCount") or 0) > 0
        op_active = int(op_facts.get("activeCount") or 0) > 0 or rath_active
        db_status = str(data.get("status") or "idle")
        db_current = str(data.get("current_status") or ("运行中" if db_status == "running" else "就绪"))
        # Only an active operation can claim that an otherwise-idle conversation
        # is newer than the conversation row. Informational snapshots (notably the
        # startup stats merge) must not make an older zombie operation look fresh.
        latest_active_op_sec = int(op_facts.get("latestActiveUpdatedAtMs") or 0) // 1000
        row_updated_at = int(data.get("updated_at") or 0)
        operations_are_stale_after_idle = bool(
            db_status == "idle"
            and not startup_running
            and not snapshot_running
            and latest_active_op_sec
            and row_updated_at
            and latest_active_op_sec <= row_updated_at
        )
        if startup_running:
            status_value, current_value, running_value = "running", "已发送", True
        elif snapshot_running:
            status_value = str(snapshot.get("status") or "running")
            current_value = str(snapshot.get("currentStatus") or db_current or "运行中")
            running_value = True
        elif has_operation_facts and not operations_are_stale_after_idle:
            if op_active:
                if int(op_facts.get("waitingControlCount") or 0) > 0:
                    status_value, current_value, running_value = "running", "等待主控裁决", True
                elif int(op_facts.get("pausedCount") or 0) > 0 and int(op_facts.get("activeForegroundCount") or 0) <= 0:
                    status_value, current_value, running_value = "running", "已暂停", True
                elif int(op_facts.get("activeForegroundCount") or 0) > 0:
                    status_value, current_value, running_value = "running", "运行中", True
                elif int(op_facts.get("activeAgentCount") or 0) > 0 or rath_active:
                    status_value, current_value, running_value = "running", "Agent 后台执行中", True
                else:
                    status_value, current_value, running_value = "running", "运行中", True
            else:
                # No durable runtime remains. Preserve the authoritative DB label
                # (for example “已中断（运行状态恢复）”) instead of flattening every
                # reconciled conversation back to the generic “就绪”.
                status_value, current_value, running_value = "idle", db_current, False
        elif rath_active:
            status_value, current_value, running_value = "running", "Agent 后台执行中", True
        else:
            status_value = str(snapshot.get("status") or "running") if snapshot_running else db_status
            current_value = str(snapshot.get("currentStatus") or "运行中") if snapshot_running else db_current
            running_value = snapshot_running if snapshot else bool(self.runs and self.runs.is_running(int(data.get("internal_chat_id") or 0)))
        base_cost_usd = float(data["cost_usd"] if data.get("cost_usd") is not None else data.get("costUsd", 0.0) or 0.0)
        return {
            "conversationUuid": str(data.get("conversation_uuid") or ""),
            "ownerChatId": int(data.get("owner_chat_id") or 0),
            "internalChatId": int(data.get("internal_chat_id") or 0),
            "title": str(data.get("title") or "新对话"),
            "model": str(data.get("model") or ""),
            "agentModel": str(data.get("agent_model") or ""),
            "agentThinkLevel": str(data.get("agent_think_level") or ""),
            "agentFastMode": (
                None
                if int(data.get("agent_fast_mode") if data.get("agent_fast_mode") is not None else -1) < 0
                else bool(int(data.get("agent_fast_mode") or 0))
            ),
            "agentFastModeRaw": int(data.get("agent_fast_mode") if data.get("agent_fast_mode") is not None else -1),
            # Once live.status is idle, DB conversation status is authoritative.
            # Otherwise a stale in-memory live snapshot can make a finished Web
            # conversation look like it is still stuck at the last tool_progress.
            "status": status_value,
            "currentStatus": current_value,
            "running": running_value,
            "lastError": str(snapshot.get("lastError") or data.get("last_error") or ""),
            "createdAt": int(data.get("created_at") or 0),
            "updatedAt": int(data.get("updated_at") or 0),
            "pinnedAt": int(data.get("pinned_at") or 0),
            "pinned": int(data.get("pinned_at") or 0) > 0,
            "displayOrder": float(data["display_order"]) if data.get("display_order") is not None else None,
            "archivedAt": int(data.get("archived_at") or 0),
            "archived": int(data.get("archived_at") or 0) > 0,
            "messageCount": int(data.get("message_count") or 0),
            # Agent requests are now committed to the same sessions/model_calls
            # ledger immediately after each upstream call. Rath task counters are
            # progress metadata over those same requests, so adding them here would
            # double-count. Live stats are also a view of already durable
            # calls and must not be added on top of the ledger.
            "costUsd": base_cost_usd,
        }

    async def _next_web_conversation_display_order(
        self,
        owner_chat_id: int,
        *,
        pinned: bool,
        conn: Any | None = None,
    ) -> float:
        """Place a newly persisted conversation at the head of its visible group."""
        db_conn = conn or self.db.conn
        pin_clause = "COALESCE(pinned_at, 0) > 0" if pinned else "COALESCE(pinned_at, 0) = 0"
        cur = await db_conn.execute(
            f"""
            SELECT MIN(display_order) AS min_display_order
            FROM web_conversations
            WHERE owner_chat_id=? AND {pin_clause} AND display_order IS NOT NULL
            """,
            (owner_chat_id,),
        )
        row = await cur.fetchone()
        minimum = row["min_display_order"] if row else None
        return self._DISPLAY_ORDER_STEP if minimum is None else float(minimum) - self._DISPLAY_ORDER_STEP

    async def _reorder_web_conversation_display_group(
        self,
        owner_chat_id: int,
        *,
        pinned: bool,
        moving_uuid: str,
        before_uuid: str = "",
        after_uuid: str = "",
        conn: Any,
    ) -> float | None:
        """Move one row inside its pin group and reindex that group atomically."""
        pin_clause = "COALESCE(pinned_at, 0) > 0" if pinned else "COALESCE(pinned_at, 0) = 0"
        cur = await conn.execute(
            f"""
            SELECT id, conversation_uuid
            FROM web_conversations
            WHERE owner_chat_id=? AND {pin_clause}
            ORDER BY CASE WHEN display_order IS NULL THEN 1 ELSE 0 END ASC,
                     display_order ASC,
                     COALESCE(created_at, 0) DESC,
                     id DESC
            """,
            (owner_chat_id,),
        )
        rows = [dict(row) for row in await cur.fetchall()]
        positions = {str(item["conversation_uuid"] or ""): index for index, item in enumerate(rows)}
        moving_index = positions.get(moving_uuid)
        if moving_index is None:
            return None
        moving = rows.pop(moving_index)
        positions = {str(item["conversation_uuid"] or ""): index for index, item in enumerate(rows)}
        before_index = positions.get(before_uuid) if before_uuid else None
        after_index = positions.get(after_uuid) if after_uuid else None
        if before_uuid and before_index is None:
            return None
        if after_uuid and after_index is None:
            return None
        if before_index is not None and after_index is not None and before_index >= after_index:
            return None
        if before_index is not None:
            insert_at = before_index + 1
        elif after_index is not None:
            insert_at = after_index
        else:
            return None
        rows.insert(insert_at, moving)
        await conn.executemany(
            "UPDATE web_conversations SET display_order=? WHERE id=?",
            [
                (float((index + 1) * self._DISPLAY_ORDER_STEP), int(item["id"]))
                for index, item in enumerate(rows)
            ],
        )
        return float((insert_at + 1) * self._DISPLAY_ORDER_STEP)

    async def _create_web_conversation(
        self,
        owner_chat_id: int,
        *,
        title: str = "新对话",
        model: str = "",
        internal_chat_id: int | None = None,
        conversation_uuid: str = "",
        run_config: dict[str, Any] | None = None,
        persist_defaults: bool = False,
        create_lock_held: bool = False,
    ) -> dict[str, Any]:
        conv_uuid = conversation_uuid or str(uuid.uuid4())
        config = run_config if isinstance(run_config, dict) else {}
        model_label = str(config.get("main_model") or model or getattr(self.model_selection, "current", "") or self.config.models.primary)
        main_thinking = str(config.get("main_thinking_level") or "")
        main_fast = 1 if bool(config.get("main_fast_mode")) else 0
        agent_model = str(config.get("agent_model") or "")
        agent_thinking = str(config.get("agent_think_level") or "")
        agent_fast = int(config.get("agent_fast_mode") if config.get("agent_fast_mode") is not None else -1)
        # Conversation row and its sessions row are committed in one SQLite
        # transaction, so POST never exposes a half-configured conversation.
        lock_context = contextlib.nullcontext() if create_lock_held else self._web_conversation_create_lock
        async with lock_context:
            attempts = 1 if internal_chat_id is not None else 8
            last_exc: Exception | None = None
            for _attempt in range(attempts):
                try:
                    async with self.db.conn.transaction(label="create-web-conversation") as conn:
                        if internal_chat_id is not None:
                            internal = int(internal_chat_id)
                        else:
                            cur = await conn.execute("SELECT MIN(internal_chat_id) AS min_id FROM web_conversations")
                            min_row = await cur.fetchone()
                            min_id = int(min_row["min_id"] or 0) if min_row else 0
                            internal = min_id - 1 if min_id < 0 else -1
                        ts = now_ts()
                        display_order = await self._next_web_conversation_display_order(
                            owner_chat_id,
                            pinned=False,
                            conn=conn,
                        )
                        await conn.execute(
                            """
                            INSERT INTO web_conversations (
                              conversation_uuid, owner_chat_id, internal_chat_id, title, model,
                              agent_model, agent_think_level, agent_fast_mode,
                              status, current_status, created_at, updated_at, display_order
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                conv_uuid, owner_chat_id, internal, title or "新对话", model_label,
                                agent_model, agent_thinking, agent_fast,
                                "idle", "就绪", ts, ts, display_order,
                            ),
                        )
                        await conn.execute(
                            "INSERT OR IGNORE INTO sessions (chat_id, created_at, updated_at) VALUES (?,?,?)",
                            (internal, ts, ts),
                        )
                        await conn.execute(
                            """
                            UPDATE sessions
                            SET session_uuid=?, thinking_level=?, fast_mode=?, updated_at=?
                            WHERE chat_id=?
                            """,
                            (conv_uuid, main_thinking, main_fast, ts, internal),
                        )
                        if persist_defaults:
                            await conn.execute(
                                """
                                INSERT INTO web_conversation_defaults (
                                  owner_chat_id, main_model, main_thinking_level, main_fast_mode,
                                  agent_model, agent_think_level, agent_fast_mode, revision, updated_at
                                ) VALUES (?,?,?,?,?,?,?,?,?)
                                ON CONFLICT(owner_chat_id) DO UPDATE SET
                                  main_model=excluded.main_model,
                                  main_thinking_level=excluded.main_thinking_level,
                                  main_fast_mode=excluded.main_fast_mode,
                                  agent_model=excluded.agent_model,
                                  agent_think_level=excluded.agent_think_level,
                                  agent_fast_mode=excluded.agent_fast_mode,
                                  revision=web_conversation_defaults.revision+1,
                                  updated_at=excluded.updated_at
                                """,
                                (
                                    owner_chat_id, model_label, main_thinking, main_fast,
                                    agent_model, agent_thinking, agent_fast, 1, ts,
                                ),
                            )
                    return await self._conversation_row(owner_chat_id, conv_uuid, require=True)
                except sqlite3.IntegrityError as exc:
                    last_exc = exc
                    if internal_chat_id is not None:
                        raise
            raise RuntimeError(f"failed to allocate unique web internal chat id: {last_exc}")

    async def _owned_chat_ids_for_web_session(self, owner_chat_id: int) -> set[int]:
        ids = {int(owner_chat_id)}
        cur = await self.db.conn.execute(
            "SELECT internal_chat_id FROM web_conversations WHERE owner_chat_id=?",
            (int(owner_chat_id),),
        )
        for row in await cur.fetchall():
            ids.add(int(row["internal_chat_id"] or 0))
        return ids

    async def _table_columns_for_copy(self, table: str) -> list[str]:
        cur = await self.db.conn.execute(f"PRAGMA table_info({table})")
        return [str(row["name"] if "name" in row.keys() else row[1]) for row in await cur.fetchall()]

    @staticmethod
    def _duplicate_pairs(*maps: dict[str, str], extra: list[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for old, new in extra or []:
            old_text, new_text = str(old or ""), str(new or "")
            if old_text and new_text and old_text != new_text:
                pairs.append((old_text, new_text))
        for mapping in maps:
            for old, new in mapping.items():
                old_text, new_text = str(old or ""), str(new or "")
                if old_text and new_text and old_text != new_text:
                    pairs.append((old_text, new_text))
        # Longest first avoids partial replacement if one generated id is a prefix
        # of another.  dict.fromkeys keeps the order while removing duplicates.
        return list(dict.fromkeys(sorted(pairs, key=lambda item: len(item[0]), reverse=True)))

    @classmethod
    def _duplicate_notification_task_batches(cls, task_uuids: Any) -> list[tuple[str, ...]]:
        unique = sorted({str(task_uuid).strip() for task_uuid in task_uuids or [] if str(task_uuid).strip()})
        size = cls._DUPLICATE_NOTIFICATION_TASK_BATCH_SIZE
        return [tuple(unique[offset:offset + size]) for offset in range(0, len(unique), size)]

    async def _suppress_duplicate_task_notifications(
        self,
        *,
        conversation_uuid: str,
        internal_chat_id: int,
        task_uuids: Any,
    ) -> None:
        batches = self._duplicate_notification_task_batches(task_uuids)
        if not batches:
            return
        suppressed_at = now_ts()
        for batch in batches:
            notification_placeholders = ",".join("?" for _ in batch)
            await self.db.conn.execute(
                f"""
                UPDATE web_task_notifications
                SET state='suppressed', claim_token='', claimed_at=0,
                    delivered_at=?, updated_at=?
                WHERE conversation_uuid=? AND internal_chat_id=?
                  AND task_uuid IN ({notification_placeholders})
                  AND state IN ('pending','processing')
                """,
                (suppressed_at, suppressed_at, conversation_uuid, internal_chat_id, *batch),
            )

    @staticmethod
    def _rewrite_duplicate_text_refs(value: Any, pairs: list[tuple[str, str]]) -> str:
        text = "" if value is None else str(value)
        for old, new in pairs:
            text = text.replace(old, new)
        return text

    @classmethod
    def _rewrite_duplicate_json_obj(
        cls,
        value: Any,
        pairs: list[tuple[str, str]],
        *,
        old_internal_chat_id: int,
        new_internal_chat_id: int,
        message_id_map: dict[int, int] | None = None,
        key_hint: str = "",
    ) -> Any:
        key_norm = re.sub(r"[^a-z0-9]", "", str(key_hint or "").lower())
        message_keys = {"messageid", "messageids", "transcriptmessageids", "transcriptmessageid"}
        if isinstance(value, dict):
            return {
                key: cls._rewrite_duplicate_json_obj(
                    item,
                    pairs,
                    old_internal_chat_id=old_internal_chat_id,
                    new_internal_chat_id=new_internal_chat_id,
                    message_id_map=message_id_map,
                    key_hint=str(key),
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                cls._rewrite_duplicate_json_obj(
                    item,
                    pairs,
                    old_internal_chat_id=old_internal_chat_id,
                    new_internal_chat_id=new_internal_chat_id,
                    message_id_map=message_id_map,
                    key_hint=key_hint,
                )
                for item in value
            ]
        if isinstance(value, int) and not isinstance(value, bool):
            if int(value) == int(old_internal_chat_id):
                return int(new_internal_chat_id)
            if key_norm in message_keys and message_id_map:
                return int(message_id_map.get(int(value), int(value)))
            return value
        if isinstance(value, str):
            text = cls._rewrite_duplicate_text_refs(value, pairs)
            if text == str(old_internal_chat_id):
                return str(new_internal_chat_id)
            if key_norm in message_keys and message_id_map and text.isdigit():
                return str(message_id_map.get(int(text), int(text)))
            return text
        return value

    @classmethod
    def _rewrite_duplicate_json_text(
        cls,
        value: Any,
        pairs: list[tuple[str, str]],
        *,
        old_internal_chat_id: int,
        new_internal_chat_id: int,
        message_id_map: dict[int, int] | None = None,
    ) -> str:
        text = "" if value is None else str(value)
        if not text:
            return text
        try:
            parsed = json.loads(text)
        except Exception:
            return cls._rewrite_duplicate_text_refs(text, pairs)
        rewritten = cls._rewrite_duplicate_json_obj(
            parsed,
            pairs,
            old_internal_chat_id=old_internal_chat_id,
            new_internal_chat_id=new_internal_chat_id,
            message_id_map=message_id_map,
        )
        return json.dumps(rewritten, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _map_message_id_json_list(raw: Any, message_id_map: dict[int, int]) -> str:
        try:
            items = json.loads(str(raw or "[]"))
        except Exception:
            items = []
        if not isinstance(items, list):
            items = []
        mapped = [message_id_map.get(int(item), int(item)) if isinstance(item, int | float) or str(item).isdigit() else item for item in items]
        return json.dumps(mapped, ensure_ascii=False, separators=(",", ":"))

    async def _copy_table_rows_for_duplicate(
        self,
        table: str,
        where_sql: str,
        params: tuple[Any, ...],
        transform: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> int:
        columns = [col for col in await self._table_columns_for_copy(table) if col != "id"]
        cur = await self.db.conn.execute(f"SELECT * FROM {table} WHERE {where_sql}", params)
        rows = [dict(row) for row in await cur.fetchall()]
        if not rows:
            return 0
        copied = 0
        for row in rows:
            row.pop("id", None)
            row.update(transform(dict(row)) or {})
            values = [row.get(col) for col in columns]
            placeholders = ",".join("?" for _ in columns)
            await self.db.conn.execute(
                f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
                tuple(values),
            )
            copied += 1
        return copied

    async def _copy_messages_for_duplicate(
        self,
        old_internal_chat_id: int,
        new_internal_chat_id: int,
        pairs: list[tuple[str, str]],
    ) -> dict[int, int]:
        columns = [col for col in await self._table_columns_for_copy("messages") if col != "id"]
        cur = await self.db.conn.execute(
            "SELECT * FROM messages WHERE chat_id=? ORDER BY id ASC",
            (old_internal_chat_id,),
        )
        message_id_map: dict[int, int] = {}
        for source in [dict(row) for row in await cur.fetchall()]:
            old_message_id = int(source.get("id") or 0)
            source.pop("id", None)
            source["chat_id"] = new_internal_chat_id
            for text_col in ("content", "reasoning"):
                if text_col in source:
                    source[text_col] = self._rewrite_duplicate_text_refs(source.get(text_col), pairs)
            if "tool_calls_json" in source:
                source["tool_calls_json"] = self._rewrite_duplicate_json_text(
                    source.get("tool_calls_json"),
                    pairs,
                    old_internal_chat_id=old_internal_chat_id,
                    new_internal_chat_id=new_internal_chat_id,
                )
            values = [source.get(col) for col in columns]
            placeholders = ",".join("?" for _ in columns)
            inserted = await self.db.conn.execute(
                f"INSERT INTO messages ({','.join(columns)}) VALUES ({placeholders})",
                tuple(values),
            )
            if old_message_id:
                message_id_map[old_message_id] = int(inserted.lastrowid or 0)
        return message_id_map

    async def _uuid_map_for_rows(self, table: str, column: str, where_sql: str, params: tuple[Any, ...]) -> dict[str, str]:
        cur = await self.db.conn.execute(f"SELECT {column} FROM {table} WHERE {where_sql}", params)
        out: dict[str, str] = {}
        for row in await cur.fetchall():
            old = str(row[column] or "").strip()
            if old:
                out[old] = str(uuid.uuid4())
        return out

    async def _duplicate_web_conversation_data(
        self,
        source: dict[str, Any],
        *,
        title: str = "",
    ) -> dict[str, Any]:
        old_uuid = str(source.get("conversation_uuid") or "")
        old_internal = int(source.get("internal_chat_id") or 0)
        owner_chat_id = int(source.get("owner_chat_id") or 0)
        source_title = str(source.get("title") or "新会话").strip() or "新会话"
        new_title = (str(title or "").strip() or f"{source_title} 副本")[:120]
        old_session_uuid = await MessageDAO(self.db).current_session_uuid(old_internal) or old_uuid
        new_row = await self._create_web_conversation(
            owner_chat_id,
            title=new_title,
            model=str(source.get("model") or ""),
        )
        new_uuid = str(new_row.get("conversation_uuid") or "")
        new_internal = int(new_row.get("internal_chat_id") or 0)
        new_session_uuid = new_uuid
        # Preserve conversation-level Agent defaults on duplicate.
        agent_fast_mode = int(source.get("agent_fast_mode") if source.get("agent_fast_mode") is not None else -1)
        await self.db.conn.execute(
            """
            UPDATE web_conversations
            SET agent_model=?, agent_think_level=?, agent_fast_mode=?, updated_at=?
            WHERE conversation_uuid=?
            """,
            (
                str(source.get("agent_model") or ""),
                str(source.get("agent_think_level") or ""),
                agent_fast_mode,
                now_ts(),
                new_uuid,
            ),
        )
        new_row["agent_model"] = str(source.get("agent_model") or "")
        new_row["agent_think_level"] = str(source.get("agent_think_level") or "")
        new_row["agent_fast_mode"] = agent_fast_mode

        try:
            old_task_uuids = list((await self._uuid_map_for_rows("rath_tasks", "task_uuid", "chat_id=?", (old_internal,))).keys())
            task_map = {old: str(uuid.uuid4()) for old in old_task_uuids}
            artifact_map = await self._uuid_map_for_rows("web_artifacts", "artifact_uuid", "conversation_uuid=?", (old_uuid,))
            agent_session_map = await self._uuid_map_for_rows("rath_agent_sessions", "session_uuid", "chat_id=?", (old_internal,))
            rath_artifact_map: dict[str, str] = {}
            control_map: dict[str, str] = {}
            if old_task_uuids:
                placeholders = ",".join("?" for _ in old_task_uuids)
                rath_artifact_map = await self._uuid_map_for_rows("rath_task_artifacts", "artifact_uuid", f"task_uuid IN ({placeholders})", tuple(old_task_uuids))
                control_map = await self._uuid_map_for_rows("rath_task_controls", "control_uuid", f"task_uuid IN ({placeholders})", tuple(old_task_uuids))

            pairs = self._duplicate_pairs(
                artifact_map,
                task_map,
                agent_session_map,
                rath_artifact_map,
                control_map,
                extra=[(old_uuid, new_uuid), (old_session_uuid, new_session_uuid)],
            )
            message_id_map = await self._copy_messages_for_duplicate(old_internal, new_internal, pairs)

            session_cur = await self.db.conn.execute("SELECT * FROM sessions WHERE chat_id=?", (old_internal,))
            if await session_cur.fetchone() is not None:
                await self.db.conn.execute("DELETE FROM sessions WHERE chat_id=?", (new_internal,))
                await self._copy_table_rows_for_duplicate(
                    "sessions",
                    "chat_id=?",
                    (old_internal,),
                    lambda row: {"chat_id": new_internal, "session_uuid": new_session_uuid},
                )

            await self._copy_table_rows_for_duplicate(
                "summaries",
                "chat_id=?",
                (old_internal,),
                lambda row: {
                    "chat_id": new_internal,
                    "summary": self._rewrite_duplicate_text_refs(row.get("summary"), pairs),
                    "up_to_message_id": message_id_map.get(int(row.get("up_to_message_id") or 0), int(row.get("up_to_message_id") or 0)),
                },
            )
            for table in ("model_calls", "tool_calls"):
                await self._copy_table_rows_for_duplicate(
                    table,
                    "chat_id=?",
                    (old_internal,),
                    lambda _row: {"chat_id": new_internal, "session_uuid": new_session_uuid},
                )
            await self._copy_table_rows_for_duplicate(
                "operations",
                "chat_id=?",
                (old_internal,),
                lambda row: {
                    "operation_uuid": str(uuid.uuid4()),
                    "chat_id": new_internal,
                    "detail_json": self._rewrite_duplicate_json_text(
                        row.get("detail_json"),
                        pairs,
                        old_internal_chat_id=old_internal,
                        new_internal_chat_id=new_internal,
                        message_id_map=message_id_map,
                    ),
                },
            )
            await self._copy_table_rows_for_duplicate(
                "web_artifacts",
                "conversation_uuid=?",
                (old_uuid,),
                lambda row: {
                    "artifact_uuid": artifact_map.get(str(row.get("artifact_uuid") or ""), str(uuid.uuid4())),
                    "conversation_uuid": new_uuid,
                    "owner_chat_id": owner_chat_id,
                    "internal_chat_id": new_internal,
                    "message_id": message_id_map.get(int(row.get("message_id") or 0), int(row.get("message_id") or 0)),
                },
            )
            await self._copy_table_rows_for_duplicate(
                "web_operations",
                "conversation_uuid=?",
                (old_uuid,),
                lambda row: {
                    "conversation_uuid": new_uuid,
                    "internal_chat_id": new_internal,
                    "op_id": self._rewrite_duplicate_text_refs(row.get("op_id"), pairs),
                    "turn_uuid": self._rewrite_duplicate_text_refs(row.get("turn_uuid"), pairs),
                    "parent_turn_uuid": self._rewrite_duplicate_text_refs(row.get("parent_turn_uuid"), pairs),
                    "run_root_turn_uuid": self._rewrite_duplicate_text_refs(row.get("run_root_turn_uuid"), pairs),
                    "target_id": self._rewrite_duplicate_text_refs(row.get("target_id"), pairs),
                    "task_uuid": self._rewrite_duplicate_text_refs(row.get("task_uuid"), pairs),
                    "run_id": self._rewrite_duplicate_text_refs(row.get("run_id"), pairs),
                    "transcript_message_ids_json": self._map_message_id_json_list(row.get("transcript_message_ids_json"), message_id_map),
                    "payload_json": self._rewrite_duplicate_json_text(
                        row.get("payload_json"),
                        pairs,
                        old_internal_chat_id=old_internal,
                        new_internal_chat_id=new_internal,
                        message_id_map=message_id_map,
                    ),
                },
            )
            await self._copy_table_rows_for_duplicate(
                "web_operation_messages",
                "conversation_uuid=?",
                (old_uuid,),
                lambda row: {
                    "conversation_uuid": new_uuid,
                    "op_id": self._rewrite_duplicate_text_refs(row.get("op_id"), pairs),
                    "message_id": message_id_map.get(int(row.get("message_id") or 0), int(row.get("message_id") or 0)),
                },
            )
            await self._copy_table_rows_for_duplicate(
                "web_event_frames",
                "conversation_uuid=?",
                (old_uuid,),
                lambda row: {
                    "conversation_uuid": new_uuid,
                    "owner_chat_id": owner_chat_id,
                    "internal_chat_id": new_internal,
                    "op_id": self._rewrite_duplicate_text_refs(row.get("op_id"), pairs),
                    "turn_uuid": self._rewrite_duplicate_text_refs(row.get("turn_uuid"), pairs),
                    "parent_turn_uuid": self._rewrite_duplicate_text_refs(row.get("parent_turn_uuid"), pairs),
                    "run_root_turn_uuid": self._rewrite_duplicate_text_refs(row.get("run_root_turn_uuid"), pairs),
                    "target_id": self._rewrite_duplicate_text_refs(row.get("target_id"), pairs),
                    "task_uuid": self._rewrite_duplicate_text_refs(row.get("task_uuid"), pairs),
                    "run_id": self._rewrite_duplicate_text_refs(row.get("run_id"), pairs),
                    "payload_json": self._rewrite_duplicate_json_text(
                        row.get("payload_json"),
                        pairs,
                        old_internal_chat_id=old_internal,
                        new_internal_chat_id=new_internal,
                        message_id_map=message_id_map,
                    ),
                    "debug_json": self._rewrite_duplicate_json_text(
                        row.get("debug_json"),
                        pairs,
                        old_internal_chat_id=old_internal,
                        new_internal_chat_id=new_internal,
                        message_id_map=message_id_map,
                    ),
                },
            )

            await self._copy_table_rows_for_duplicate(
                "rath_agent_sessions",
                "chat_id=?",
                (old_internal,),
                lambda row: {
                    "session_uuid": agent_session_map.get(str(row.get("session_uuid") or ""), str(uuid.uuid4())),
                    "openbear_session_uuid": new_session_uuid,
                    "chat_id": new_internal,
                    "last_task_uuid": task_map.get(str(row.get("last_task_uuid") or ""), str(row.get("last_task_uuid") or "")),
                    "metadata_json": self._rewrite_duplicate_json_text(
                        row.get("metadata_json"),
                        pairs,
                        old_internal_chat_id=old_internal,
                        new_internal_chat_id=new_internal,
                    ),
                },
            )
            await self._copy_table_rows_for_duplicate(
                "rath_tasks",
                "chat_id=?",
                (old_internal,),
                lambda row: {
                    "task_uuid": task_map.get(str(row.get("task_uuid") or ""), str(uuid.uuid4())),
                    "chat_id": new_internal,
                    "parent_session_uuid": new_session_uuid,
                    "agent_session_uuid": agent_session_map.get(str(row.get("agent_session_uuid") or ""), str(row.get("agent_session_uuid") or "")),
                    "caller_agent_session_uuid": agent_session_map.get(str(row.get("caller_agent_session_uuid") or ""), str(row.get("caller_agent_session_uuid") or "")),
                    "parent_task_uuid": task_map.get(str(row.get("parent_task_uuid") or ""), str(row.get("parent_task_uuid") or "")),
                    "input_json": self._rewrite_duplicate_json_text(
                        row.get("input_json"),
                        pairs,
                        old_internal_chat_id=old_internal,
                        new_internal_chat_id=new_internal,
                    ),
                    "output_json": self._rewrite_duplicate_json_text(
                        row.get("output_json"),
                        pairs,
                        old_internal_chat_id=old_internal,
                        new_internal_chat_id=new_internal,
                    ),
                },
            )
            await TaskMemoryDAO(self.db).duplicate_conversation(
                old_uuid,
                new_uuid,
                task_map,
                conn=self.db.conn,
            )
            await self._suppress_duplicate_task_notifications(
                conversation_uuid=new_uuid,
                internal_chat_id=new_internal,
                task_uuids=task_map.values(),
            )
            if old_task_uuids:
                placeholders = ",".join("?" for _ in old_task_uuids)
                await self._copy_table_rows_for_duplicate(
                    "rath_task_events",
                    f"task_uuid IN ({placeholders})",
                    tuple(old_task_uuids),
                    lambda row: {
                        "task_uuid": task_map.get(str(row.get("task_uuid") or ""), str(row.get("task_uuid") or "")),
                        "detail_json": self._rewrite_duplicate_json_text(
                            row.get("detail_json"),
                            pairs,
                            old_internal_chat_id=old_internal,
                            new_internal_chat_id=new_internal,
                        ),
                    },
                )
                await self._copy_table_rows_for_duplicate(
                    "rath_task_artifacts",
                    f"task_uuid IN ({placeholders})",
                    tuple(old_task_uuids),
                    lambda row: {
                        "artifact_uuid": rath_artifact_map.get(str(row.get("artifact_uuid") or ""), str(uuid.uuid4())),
                        "task_uuid": task_map.get(str(row.get("task_uuid") or ""), str(row.get("task_uuid") or "")),
                        "source_refs_json": self._rewrite_duplicate_json_text(
                            row.get("source_refs_json"),
                            pairs,
                            old_internal_chat_id=old_internal,
                            new_internal_chat_id=new_internal,
                        ),
                    },
                )
                await self._copy_table_rows_for_duplicate(
                    "rath_task_controls",
                    f"task_uuid IN ({placeholders})",
                    tuple(old_task_uuids),
                    lambda row: {
                        "control_uuid": control_map.get(str(row.get("control_uuid") or ""), str(uuid.uuid4())),
                        "task_uuid": task_map.get(str(row.get("task_uuid") or ""), str(row.get("task_uuid") or "")),
                    },
                )
            # Usage copied for billing/history is not a provider report for the
            # duplicated controller context. Keep its display/compact authority
            # explicitly unknown until the duplicate makes its own model request.
            await MessageDAO(self.db).set_controller_context_usage(
                new_internal,
                session_uuid=new_session_uuid,
                tokens=None,
                commit=False,
            )
            await self.db.conn.commit()
            return await self._conversation_row(owner_chat_id, new_uuid, require=True)  # type: ignore[return-value]
        except Exception:
            await self.db.conn.rollback()
            with contextlib.suppress(Exception):
                await self.db.conn.execute("DELETE FROM web_conversations WHERE conversation_uuid=?", (new_uuid,))
                await self.db.conn.execute("DELETE FROM sessions WHERE chat_id=?", (new_internal,))
                await self.db.conn.commit()
            raise

    async def _web_conversation_has_active_runtime(self, row: dict[str, Any]) -> bool:
        internal_chat_id = int(row.get("internal_chat_id") or 0)
        conv_uuid = str(row.get("conversation_uuid") or "")
        # web_conversations.status is a denormalized display cache.  It must not
        # veto reconciliation or duplication when every authoritative runtime fact
        # says the old process is gone.
        live = self._web_live_streams.get(conv_uuid)
        if live is not None and bool(live.snapshot().get("running")):
            return True
        if self.runs is not None and self.runs.is_running(internal_chat_id):
            return True
        if any(int(getattr(proc, "chat_id", 0) or 0) == internal_chat_id for proc in processes.active()):
            return True
        cur = await self.db.conn.execute(
            """
            SELECT 1 FROM web_operations
            WHERE conversation_uuid=? AND op_type!='notice'
              AND COALESCE(lifecycle,'') IN ('active','paused','waiting_control')
            LIMIT 1
            """,
            (conv_uuid,),
        )
        if await cur.fetchone() is not None:
            return True
        cur = await self.db.conn.execute(
            """
            SELECT 1 FROM rath_tasks
            WHERE chat_id=? AND COALESCE(status,'') IN ('queued','running','pausing','paused','resuming','stopping','needs_openbear_control')
            LIMIT 1
            """,
            (internal_chat_id,),
        )
        return await cur.fetchone() is not None

    async def _conversation_row(self, owner_chat_id: int, conversation_uuid: str, *, require: bool = False) -> dict[str, Any] | None:
        # Archiving controls sidebar visibility only. Direct access must keep working
        # for an open conversation, its WebSocket, and any active background task.
        cur = await self.db.conn.execute(
            """
            SELECT * FROM web_conversations
            WHERE owner_chat_id=? AND conversation_uuid=?
            LIMIT 1
            """,
            (owner_chat_id, conversation_uuid),
        )
        row = await cur.fetchone()
        if not row:
            if require:
                raise web.HTTPNotFound(text="conversation_not_found")
            return None
        return dict(row)

    async def _ensure_default_web_conversation(self, owner_chat_id: int) -> dict[str, Any]:
        cur = await self.db.conn.execute(
            """
            SELECT * FROM web_conversations
            WHERE owner_chat_id=?
            ORDER BY CASE WHEN COALESCE(archived_at, 0)=0 THEN 0 ELSE 1 END,
                     created_at DESC,
                     id DESC
            LIMIT 1
            """,
            (owner_chat_id,),
        )
        row = await cur.fetchone()
        if row:
            return dict(row)

        # 新 Web 时间线以独立 internal_chat_id 为准，不再把 旧主会话
        # 包装成 Web 默认会话；否则清理 Web 历史后又会把旧主会话带回前端。
        return await self._create_web_conversation(owner_chat_id)

    async def _web_operation_facts_for_conversations(self, conversation_uuids: list[str]) -> dict[str, dict[str, Any]]:
        ids = [str(x or "") for x in conversation_uuids if str(x or "")]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        cur = await self.db.conn.execute(
            f"""
            SELECT
              conversation_uuid,
              COUNT(*) AS operation_count,
              SUM(CASE WHEN op_type!='notice' AND COALESCE(lifecycle,'') IN ('active','paused','waiting_control') THEN 1 ELSE 0 END) AS active_count,
              SUM(CASE WHEN op_type='run' AND COALESCE(lifecycle,'') IN ('active','paused','waiting_control') THEN 1 ELSE 0 END) AS active_foreground_count,
              SUM(CASE WHEN op_type='agent' AND COALESCE(lifecycle,'') IN ('active','paused','waiting_control') THEN 1 ELSE 0 END) AS active_agent_count,
              SUM(CASE WHEN op_type!='notice' AND COALESCE(lifecycle,'')='paused' THEN 1 ELSE 0 END) AS paused_count,
              SUM(CASE WHEN op_type!='notice' AND COALESCE(lifecycle,'')='waiting_control' THEN 1 ELSE 0 END) AS waiting_control_count,
              MAX(updated_at_ms) AS latest_updated_at_ms,
              MAX(CASE WHEN op_type!='notice' AND COALESCE(lifecycle,'') IN ('active','paused','waiting_control') THEN updated_at_ms ELSE 0 END) AS latest_active_updated_at_ms
            FROM web_operations
            WHERE conversation_uuid IN ({placeholders})
            GROUP BY conversation_uuid
            """,
            tuple(ids),
        )
        out: dict[str, dict[str, Any]] = {}
        for row in await cur.fetchall():
            conv_uuid = str(row["conversation_uuid"] or "")
            out[conv_uuid] = {
                "hasOperations": int(row["operation_count"] or 0) > 0,
                "operationCount": int(row["operation_count"] or 0),
                "activeCount": int(row["active_count"] or 0),
                "activeForegroundCount": int(row["active_foreground_count"] or 0),
                "activeAgentCount": int(row["active_agent_count"] or 0),
                "pausedCount": int(row["paused_count"] or 0),
                "waitingControlCount": int(row["waiting_control_count"] or 0),
                "latestUpdatedAtMs": int(row["latest_updated_at_ms"] or 0),
                "latestActiveUpdatedAtMs": int(row["latest_active_updated_at_ms"] or 0),
            }
        return out

    async def _active_rath_task_uuids_for_chat(self, chat_id: int) -> set[str]:
        if self.rath is None:
            return set()
        try:
            tasks = await self.rath.all_active_tasks_for_chat(int(chat_id or 0))
        except Exception:
            return set()
        return {str(getattr(task, "task_uuid", "") or "") for task in tasks if str(getattr(task, "task_uuid", "") or "")}

    async def _terminal_status_for_agent_operation(self, payload: dict[str, Any], active_task_uuids: set[str]) -> str:
        task_uuids = self._agent_task_uuids_from_payload(payload)
        if task_uuids & active_task_uuids:
            return ""
        terminal_statuses: list[str] = []
        for task_uuid in task_uuids:
            task = None
            if self.rath_dao is not None:
                with contextlib.suppress(Exception):
                    task = await self.rath_dao.get_task(task_uuid)
            status = str(getattr(task, "status", "") or "").strip()
            if status in {"completed", "partial", "failed", "cancelled", "interrupted"}:
                terminal_statuses.append(status)
            elif status == "needs_openbear_control":
                return ""
        if terminal_statuses:
            if all(status == "completed" for status in terminal_statuses):
                return "completed"
            if any(status == "completed" for status in terminal_statuses):
                return "partial"
            if any(status == "cancelled" for status in terminal_statuses):
                return "cancelled"
            if any(status == "interrupted" for status in terminal_statuses):
                return "interrupted"
            return "failed"
        direct = str(payload.get("status") or "").strip()
        if direct in {"completed", "partial", "failed", "cancelled", "interrupted"}:
            return direct
        if not task_uuids and not active_task_uuids:
            return "failed" if direct in {"", "queued", "running", "resuming", "pausing", "stopping"} else "interrupted"
        return ""

    async def _reconcile_inactive_web_conversation_operations(self, row: dict[str, Any], *, source: str) -> list[dict[str, Any]]:
        conv_uuid = str(row.get("conversation_uuid") or "").strip()
        if not conv_uuid:
            return []
        async with self._web_operation_lock(conv_uuid):
            return await self._reconcile_inactive_web_conversation_operations_locked(row, source=source)

    async def _reconcile_terminal_web_agent_operations_locked(
        self,
        row: dict[str, Any],
        *,
        source: str,
        active_task_uuids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        conv_uuid = str(row.get("conversation_uuid") or "").strip()
        internal_chat_id = int(row.get("internal_chat_id") or 0)
        if not conv_uuid or not internal_chat_id:
            return []
        if active_task_uuids is None:
            active_task_uuids = await self._active_rath_task_uuids_for_chat(internal_chat_id)
        cur = await self.db.conn.execute(
            """
            SELECT * FROM web_operations
            WHERE conversation_uuid=?
              AND op_type='agent'
              AND COALESCE(lifecycle,'') IN ('active','paused','waiting_control')
            ORDER BY display_seq ASC, id ASC
            """,
            (conv_uuid,),
        )
        frames: list[dict[str, Any]] = []
        for op_row in await cur.fetchall():
            old_payload = operation_json_loads_dict(str(op_row["payload_json"] or "{}"))
            status = await self._terminal_status_for_agent_operation(old_payload, active_task_uuids)
            if not status:
                continue
            action = "error" if status == "failed" else "cancel" if status in {"cancelled", "interrupted"} else "end"
            frame = await self._publish_operation(
                conv_uuid,
                internal_chat_id=internal_chat_id,
                op_id=str(op_row["op_id"] or ""),
                op_type="agent",
                action=action,
                turn_uuid=str(op_row["turn_uuid"] or ""),
                payload={**self._agent_operation_terminal_patch(old_payload, status), "reconciled": True},
                status=status,
                lifecycle="terminal",
                debug={"source": source, "reason": "terminal_agent_operation_reconcile"},
            )
            if frame:
                frames.append(frame)
        if frames:
            await self.db.conn.commit()
        return frames

    async def _reconcile_inactive_web_conversation_operations_locked(self, row: dict[str, Any], *, source: str) -> list[dict[str, Any]]:
        conv_uuid = str(row.get("conversation_uuid") or "").strip()
        internal_chat_id = int(row.get("internal_chat_id") or 0)
        if not conv_uuid or not internal_chat_id:
            return []
        # `accepted` is durably published before live._apply() marks the stream
        # running and before the controller task reaches RunRegistry. A list/state
        # request can otherwise enter this reconciler in that narrow window and
        # terminalize the brand-new run as stale. The sender owns this guard until
        # all startup facts are established.
        if self._web_starting_turns.get(conv_uuid):
            return []
        if await self.operation_locks.current_operation(internal_chat_id) == "web_manual_compact":
            return []
        db_status = str(row.get("status") or "idle")
        live = self._web_live_streams.get(conv_uuid)
        if live is not None and bool(live.snapshot().get("running")):
            return []
        if self.runs is not None and self.runs.is_running(internal_chat_id):
            return []
        active_task_uuids = await self._active_rath_task_uuids_for_chat(internal_chat_id)
        agent_frames = await self._reconcile_terminal_web_agent_operations_locked(
            row,
            source=source,
            active_task_uuids=active_task_uuids,
        )
        active_processes = [
            proc for proc in processes.active()
            if int(getattr(proc, "chat_id", 0) or 0) == internal_chat_id
        ]
        # The coarse DB flag is not proof of a live runtime.  On restart it is
        # precisely the stale fact that needs reconciliation.  Only process-local
        # run/live state (checked above), an active Rath task, or a surviving
        # managed process may keep the conversation open.
        if db_status == "running" and (active_task_uuids or active_processes):
            return agent_frames
        cur = await self.db.conn.execute(
            """
            SELECT * FROM web_operations
            WHERE conversation_uuid=?
              AND op_type IN ('run','tool','user_interaction','agent','agent_control','agent_supervision','assistant_message','reasoning','status','notice','context_compaction')
              AND COALESCE(lifecycle,'') IN ('active','paused','waiting_control')
            ORDER BY display_seq ASC, id ASC
            """,
            (conv_uuid,),
        )
        rows = await cur.fetchall()
        if not rows:
            return []
        run_statuses: dict[str, str] = {}
        run_ids = sorted({str(r["run_id"] or r["target_id"] or r["turn_uuid"] or "") for r in rows if str(r["run_id"] or r["target_id"] or r["turn_uuid"] or "")})
        if run_ids:
            placeholders = ",".join("?" for _ in run_ids)
            cur = await self.db.conn.execute(
                f"""
                SELECT COALESCE(NULLIF(run_id,''), NULLIF(target_id,''), turn_uuid) AS run_key, status FROM web_operations
                WHERE conversation_uuid=? AND op_type='run' AND COALESCE(NULLIF(run_id,''), NULLIF(target_id,''), turn_uuid) IN ({placeholders})
                """,
                (conv_uuid, *run_ids),
            )
            for status_row in await cur.fetchall():
                run_statuses[str(status_row["run_key"] or "")] = str(status_row["status"] or "")
        frames: list[dict[str, Any]] = []
        now = int(time.time() * 1000)
        for op_row in rows:
            op_type = str(op_row["op_type"] or "")
            op_id = str(op_row["op_id"] or "")
            row_turn = str(op_row["turn_uuid"] or "")
            row_run = str(op_row["run_id"] or op_row["target_id"] or row_turn or "")
            old_payload = operation_json_loads_dict(str(op_row["payload_json"] or "{}"))
            run_status = run_statuses.get(row_run, "")
            stale_process_runtime = db_status in {"running", "stopping"} and not active_task_uuids and not active_processes
            status = (
                "interrupted" if stale_process_runtime
                else "failed" if db_status == "error" or run_status == "failed"
                else "cancelled" if run_status == "cancelled"
                else "interrupted" if run_status == "interrupted"
                else "completed"
            )
            if op_type == "agent":
                status = await self._terminal_status_for_agent_operation(old_payload, active_task_uuids)
                if not status:
                    continue
            action = "error" if status == "failed" else "cancel" if status in {"cancelled", "interrupted"} else "end"
            if op_type == "user_interaction":
                if status == "failed":
                    patch = {"status": "failed", "interactionStatus": "error"}
                elif status in {"cancelled", "interrupted"}:
                    status = "cancelled"
                    action = "cancel"
                    patch = {"status": "cancelled", "interactionStatus": "cancelled"}
                else:
                    status = "completed"
                    action = "end"
                    patch = {"status": "completed", "interactionStatus": "timeout"}
            elif op_type == "notice":
                # A notification is historical/informational even when it records
                # a needs_openbear_control boundary. It must never keep a finished
                # conversation active in the sidebar.
                status = str(old_payload.get("status") or op_row["status"] or "completed")
                action = "patch"
                patch = {"status": status}
            elif op_type in {"status", "agent_supervision"}:
                label = "出错" if status == "failed" else "已中断" if status == "interrupted" else "已停止" if status == "cancelled" else "就绪"
                patch = {"active": False, "statusText": label, "status": status}
            elif op_type == "run":
                patch = {"runId": row_run, "status": status, "completedAtMs": now}
            elif op_type in {"assistant_message", "reasoning"}:
                patch = {"complete": True, "status": status}
            elif op_type == "agent":
                patch = {**self._agent_operation_terminal_patch(old_payload, status), "reconciled": True}
            else:
                patch = {"status": status}
            frame = await self._publish_operation(
                conv_uuid,
                internal_chat_id=internal_chat_id,
                op_id=op_id,
                op_type=op_type,
                action=action,
                turn_uuid=row_turn,
                payload=patch,
                status=status,
                lifecycle="informational" if op_type == "notice" else "terminal",
                debug={"source": source, "reason": "inactive_conversation_operation_reconcile", "dbStatus": db_status},
            )
            if frame:
                frames.append(frame)
        if frames:
            await self.db.conn.commit()
        if db_status in {"running", "stopping"} and not active_task_uuids and not active_processes:
            await self._touch_web_conversation(
                conv_uuid,
                status="idle",
                current_status="已中断（运行状态恢复）",
                last_error="",
            )
            # Callers continue rendering this same row object after reconciliation;
            # keep it consistent with the just-committed durable state.
            row["status"] = "idle"
            row["current_status"] = "已中断（运行状态恢复）"
            row["last_error"] = ""
        return [*agent_frames, *frames]

    async def _list_web_conversations(self, owner_chat_id: int, *, include_archived: bool = False) -> list[dict[str, Any]]:
        await self._ensure_default_web_conversation(owner_chat_id)
        archive_clause = "" if include_archived else "AND COALESCE(wc.archived_at,0)=0"
        cur = await self.db.conn.execute(
            f"""
            SELECT wc.*,
                   COALESCE((
                     SELECT COUNT(*) FROM messages m
                     WHERE m.chat_id=wc.internal_chat_id AND m.role IN ('user','assistant')
                   ), 0) AS message_count,
                   COALESCE((
                     SELECT usage_cost_usd FROM sessions s
                     WHERE s.chat_id=wc.internal_chat_id
                   ), 0) AS cost_usd
            FROM web_conversations wc
            WHERE wc.owner_chat_id=? {archive_clause}
            ORDER BY COALESCE(wc.created_at,0) DESC, wc.id DESC
            LIMIT 100
            """,
            (owner_chat_id,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        for r in rows:
            await self._reconcile_inactive_web_conversation_operations(r, source="conversation_list_reconcile")
        operation_facts = await self._web_operation_facts_for_conversations([str(r.get("conversation_uuid") or "") for r in rows])
        if self.rath is not None:
            for r in rows:
                conv_uuid = str(r.get("conversation_uuid") or "")
                facts = dict(operation_facts.get(conv_uuid) or {})
                with contextlib.suppress(Exception):
                    active_tasks = await self.rath.all_active_tasks_for_chat(int(r.get("internal_chat_id") or 0))
                    facts["activeRathTaskCount"] = len(active_tasks)
                    facts["activeRathTaskUuids"] = [str(getattr(task, "task_uuid", "") or "") for task in active_tasks]
                if facts:
                    operation_facts[conv_uuid] = facts
        items = [
            self._web_conversation_json(
                r,
                live=self._web_live_streams.get(str(r.get("conversation_uuid") or "")),
                operation_facts=operation_facts.get(str(r.get("conversation_uuid") or "")),
            )
            for r in rows
        ]
        return items

    def _live_for(self, row: dict[str, Any]) -> _WebLiveStream:
        conv_uuid = str(row.get("conversation_uuid") or "")
        internal_chat_id = int(row.get("internal_chat_id") or 0)
        live = self._web_live_streams.get(conv_uuid)
        if live is None:
            live = _WebLiveStream(
                conv_uuid,
                internal_chat_id,
                event_sink=self._publish_web_operation_event,
            )
            if self.runs is not None and self.runs.is_running(internal_chat_id):
                live.status = "running"
                live.current_status = str(row.get("current_status") or "运行中")
            self._web_live_streams[conv_uuid] = live
        return live

    def _web_task_notification_lock(self, chat_id: int) -> asyncio.Lock:
        key = int(chat_id or 0)
        lock = self._web_task_notification_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._web_task_notification_locks[key] = lock
        return lock

    async def _latest_visible_root_turn_uuid(self, conversation_uuid: str) -> str:
        conv_uuid = str(conversation_uuid or "").strip()
        if not conv_uuid:
            return ""
        cur = await self.db.conn.execute(
            """
            SELECT turn_uuid FROM web_operations
            WHERE conversation_uuid=? AND op_type='user_message' AND COALESCE(internal,0)=0
              AND COALESCE(json_extract(payload_json, '$.queued'), 0)=0
            ORDER BY display_seq DESC, id DESC
            LIMIT 1
            """,
            (conv_uuid,),
        )
        row = await cur.fetchone()
        return str(row["turn_uuid"] or "") if row else ""

    async def _web_active_round_info(self, conversation_uuid: str, internal_chat_id: int) -> dict[str, Any]:
        """Backend authority for whether a Web user round is still open.

        A round starts at a visible user message and remains open while any
        foreground run, detached/waiting Agent, background Bash/process, pending
        notification, or queued steering belongs to the conversation.  UI code
        must not infer this independently; it only renders the rootTurnUuid and
        operation facts emitted here.
        """
        conv_uuid = str(conversation_uuid or "").strip()
        chat_id = int(internal_chat_id or 0)
        root_turn_uuid = ""
        active_reasons: list[str] = []

        if self.runs is not None and chat_id and self.runs.is_running(chat_id):
            active_reasons.append("run")
            live = self._web_live_streams.get(conv_uuid) if conv_uuid else None
            if live is not None:
                root_turn_uuid = str(getattr(live, "_agent_turn_uuid", "") or getattr(live, "current_turn_uuid", "") or "").strip()

        if conv_uuid:
            cur = await self.db.conn.execute(
                """
                SELECT turn_uuid, run_root_turn_uuid, run_id, op_type, status, lifecycle
                FROM web_operations
                WHERE conversation_uuid=?
                  AND op_type IN ('run','agent','tool','user_interaction','agent_supervision')
                  AND COALESCE(lifecycle,'') IN ('active','paused','waiting_control')
                ORDER BY display_seq ASC, id ASC
                """,
                (conv_uuid,),
            )
            for row in await cur.fetchall():
                candidate = str(row["run_root_turn_uuid"] or row["turn_uuid"] or row["run_id"] or "").strip()
                if candidate and not root_turn_uuid:
                    root_turn_uuid = candidate
                active_reasons.append(str(row["op_type"] or "operation"))

        active_tasks = []
        if self.rath_dao is not None and chat_id:
            with contextlib.suppress(Exception):
                active_tasks = await self.rath_dao.active_tasks_for_chat(chat_id, limit=100, controllable=True)
        active_tasks = [task for task in active_tasks if not conv_uuid or str(getattr(task, "parent_session_uuid", "") or "") == conv_uuid]
        if active_tasks:
            active_reasons.append("agent")
            if not root_turn_uuid:
                task_uuids = [str(getattr(task, "task_uuid", "") or "") for task in active_tasks if str(getattr(task, "task_uuid", "") or "")]
                if task_uuids:
                    placeholders = ",".join("?" for _ in task_uuids)
                    cur = await self.db.conn.execute(
                        f"""
                        SELECT turn_uuid, run_root_turn_uuid, run_id FROM web_operations
                        WHERE conversation_uuid=? AND op_type='agent' AND task_uuid IN ({placeholders})
                        ORDER BY display_seq ASC, id ASC
                        LIMIT 1
                        """,
                        (conv_uuid, *task_uuids),
                    )
                    row = await cur.fetchone()
                    if row:
                        root_turn_uuid = str(row["run_root_turn_uuid"] or row["turn_uuid"] or row["run_id"] or "").strip()

        active_processes = [
            info for info in processes.active_for_chat(chat_id)
            if not conv_uuid
            or not str(getattr(info, "session_uuid", "") or "")
            or str(getattr(info, "session_uuid", "") or "") == conv_uuid
        ]
        if active_processes:
            active_reasons.append("process")
            if not root_turn_uuid:
                for info in active_processes:
                    candidate = str(getattr(info, "run_root_turn_uuid", "") or getattr(info, "turn_uuid", "") or "").strip()
                    if candidate:
                        root_turn_uuid = candidate
                        break

        if conv_uuid and (self._web_task_notification_pending.get(conv_uuid) or self._web_task_notification_deferred.get(conv_uuid)):
            active_reasons.append("notification")
            if not root_turn_uuid:
                deferred_payload = self._web_task_notification_deferred.get(conv_uuid, [])[:1]
                if deferred_payload:
                    root_turn_uuid = str(
                        deferred_payload[0].get("runRootTurnUuid")
                        or deferred_payload[0].get("rootTurnUuid")
                        or deferred_payload[0].get("turnUuid")
                        or ""
                    ).strip()
        if chat_id and steering.pending_items(chat_id):
            active_reasons.append("steering")

        if not root_turn_uuid:
            root_turn_uuid = await self._latest_visible_root_turn_uuid(conv_uuid)
        return {
            "active": bool(active_reasons),
            "rootTurnUuid": root_turn_uuid,
            "activeReasons": sorted(set(active_reasons)),
            "activeTaskCount": len(active_tasks),
            "activeProcessCount": len(active_processes),
        }

    async def _next_assistant_segment_index(self, conversation_uuid: str, turn_uuid: str) -> int:
        conv_uuid = str(conversation_uuid or "").strip()
        turn = str(turn_uuid or "").strip()
        if not conv_uuid or not turn:
            return 0
        cur = await self.db.conn.execute(
            """
            SELECT op_id FROM web_operations
            WHERE conversation_uuid=? AND turn_uuid=? AND op_type='assistant_message'
            """,
            (conv_uuid, turn),
        )
        max_segment = -1
        prefix = f"assistant:{turn}:"
        for row in await cur.fetchall():
            op_id = str(row["op_id"] or "")
            if not op_id.startswith(prefix):
                continue
            tail = op_id[len(prefix):]
            if tail.isdigit():
                max_segment = max(max_segment, int(tail))
        return max_segment + 1

    async def _filter_tasks_for_root_turn(self, conversation_uuid: str, tasks: list[Any], root_turn_uuid: str) -> list[Any]:
        """Keep only active Agent tasks that belong to the same visible root turn.

        Task notifications should wait for sibling Agents from the same user round,
        not for every detached Agent in the conversation.  If a legacy task has no
        operation row yet, keep it as a conservative fallback; but when an operation
        clearly points to another root turn, exclude it.
        """
        conv_uuid = str(conversation_uuid or "").strip()
        root = str(root_turn_uuid or "").strip()
        ordered: list[tuple[str, Any]] = [
            (str(getattr(task, "task_uuid", "") or "").strip(), task)
            for task in (tasks or [])
        ]
        ordered = [(task_uuid, task) for task_uuid, task in ordered if task_uuid]
        if not conv_uuid or not root or not ordered:
            return [task for _, task in ordered]
        placeholders = ",".join("?" for _ in ordered)
        cur = await self.db.conn.execute(
            f"""
            SELECT task_uuid, turn_uuid, run_root_turn_uuid, run_id
            FROM web_operations
            WHERE conversation_uuid=? AND op_type='agent' AND task_uuid IN ({placeholders})
            """,
            (conv_uuid, *[task_uuid for task_uuid, _ in ordered]),
        )
        mapped: set[str] = set()
        matched: set[str] = set()
        for row in await cur.fetchall():
            task_uuid = str(row["task_uuid"] or "").strip()
            if not task_uuid:
                continue
            mapped.add(task_uuid)
            # Visible root ownership is authoritative. execution run IDs are
            # lifecycle identities and may be shared by malformed/legacy events;
            # use them only when the operation has no visible turn/root fields.
            visible_roots = {
                str(row["turn_uuid"] or "").strip(),
                str(row["run_root_turn_uuid"] or "").strip(),
            }
            visible_roots.discard("")
            roots = visible_roots or {str(row["run_id"] or "").strip()}
            if root in roots:
                matched.add(task_uuid)
        if not mapped:
            return [task for _, task in ordered]
        return [task for task_uuid, task in ordered if task_uuid in matched or task_uuid not in mapped]

    async def _root_turn_for_task_notification(self, conversation_uuid: str, payload: dict[str, Any]) -> str:
        conv_uuid = str(conversation_uuid or "").strip()
        if not conv_uuid:
            return ""
        direct = str(payload.get("runRootTurnUuid") or payload.get("rootTurnUuid") or payload.get("turnUuid") or "").strip()
        if direct:
            return direct
        task_uuids = sorted(self._agent_task_uuids_from_payload(payload))
        job_id = str(payload.get("jobId") or payload.get("sessionId") or "").strip()
        if job_id:
            task_uuids.append(job_id)
        task_uuids = [item for item in task_uuids if item]
        if task_uuids:
            placeholders = ",".join("?" for _ in task_uuids)
            cur = await self.db.conn.execute(
                f"""
                SELECT turn_uuid, run_root_turn_uuid, run_id FROM web_operations
                WHERE conversation_uuid=?
                  AND (task_uuid IN ({placeholders}) OR target_id IN ({placeholders}) OR op_id IN ({placeholders}))
                ORDER BY display_seq ASC, id ASC
                LIMIT 1
                """,
                (conv_uuid, *task_uuids, *task_uuids, *[f"tool:{item}" for item in task_uuids]),
            )
            row = await cur.fetchone()
            if row:
                found = str(row["run_root_turn_uuid"] or row["turn_uuid"] or row["run_id"] or "").strip()
                if found:
                    return found
        active = await self._web_active_round_info(conv_uuid, int(payload.get("chatId") or 0))
        found = str(active.get("rootTurnUuid") or "").strip()
        if found:
            return found
        return await self._latest_visible_root_turn_uuid(conv_uuid)

    async def _should_suppress_web_task_notification(self, conversation_uuid: str, payload: dict[str, Any]) -> bool:
        task_uuid = str(payload.get("taskUuid") or "").strip()
        status = str(payload.get("status") or "").strip()
        if status in {"cancelled", "interrupted"}:
            return True
        task = None
        if task_uuid and self.rath_dao is not None:
            with contextlib.suppress(Exception):
                task = await self.rath_dao.get_task(task_uuid)
        if task is not None:
            durable_status = str(task.status or "")
            if durable_status in {"cancelled", "interrupted"}:
                return True
            durable_final = {"completed", "failed", "cancelled", "interrupted", "needs_openbear_control"}
            if durable_status in durable_final and status and status != durable_status:
                return True
        conv_uuid = str(conversation_uuid or "")
        if self._web_stop_markers.get(conv_uuid):
            return True
        if task_uuid and task_uuid in self._web_stopped_task_uuids.get(conv_uuid, set()):
            return True
        return False

    @staticmethod
    def _web_task_notification_key(conversation_uuid: str, payload: dict[str, Any]) -> str:
        explicit = str(payload.get("notificationKey") or "").strip()
        if explicit:
            return hashlib.sha256(explicit.encode("utf-8")).hexdigest()
        recent = payload.get("recentEvents") if isinstance(payload.get("recentEvents"), list) else []
        last_event_seq = max(
            [int(item.get("seq") or 0) for item in recent if isinstance(item, dict)] or [0]
        )
        identity = {
            "conversationUuid": str(conversation_uuid or ""),
            "taskUuid": str(payload.get("taskUuid") or payload.get("jobId") or ""),
            "kind": str(payload.get("kind") or "task-notification"),
            "status": str(payload.get("status") or ""),
            "continued": bool(payload.get("continued")),
            "lastEventSeq": last_event_seq,
            "artifactUuid": str((payload.get("result") or {}).get("artifactUuid") or "") if isinstance(payload.get("result"), dict) else "",
            "summary": str(payload.get("summary") or ""),
        }
        raw = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def _persist_web_task_notification(
        self,
        conversation: dict[str, Any] | None,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        row = dict(conversation or {})
        conv_uuid = str(row.get("conversation_uuid") or payload.get("conversationUuid") or "").strip()
        internal_chat_id = int(row.get("internal_chat_id") or payload.get("chatId") or 0)
        owner_chat_id = int(row.get("owner_chat_id") or payload.get("ownerChatId") or 0)
        if not conv_uuid or not internal_chat_id:
            return None
        if await self._should_suppress_web_task_notification(conv_uuid, payload):
            return None
        notification_key = self._web_task_notification_key(conv_uuid, payload)
        notification_uuid = str(uuid.uuid4())
        ts = now_ts()
        durable_payload = dict(payload)
        durable_payload["conversationUuid"] = conv_uuid
        durable_payload["chatId"] = internal_chat_id
        await self.db.conn.execute(
            """
            INSERT OR IGNORE INTO web_task_notifications (
              notification_uuid, notification_key, conversation_uuid, internal_chat_id,
              owner_chat_id, task_uuid, kind, task_status, payload_json, state,
              attempts, claim_token, claimed_at, next_attempt_at, last_error,
              created_at, updated_at, delivered_at
            ) VALUES (?,?,?,?,?,?,?,?,?,'pending',0,'',0,0,'',?,?,0)
            """,
            (
                notification_uuid,
                notification_key,
                conv_uuid,
                internal_chat_id,
                owner_chat_id,
                str(payload.get("taskUuid") or payload.get("jobId") or ""),
                str(payload.get("kind") or "task-notification"),
                str(payload.get("status") or ""),
                json.dumps(durable_payload, ensure_ascii=False, separators=(",", ":"), default=str),
                ts,
                ts,
            ),
        )
        await self.db.conn.commit()
        cur = await self.db.conn.execute(
            "SELECT notification_uuid, state, payload_json FROM web_task_notifications WHERE notification_key=? LIMIT 1",
            (notification_key,),
        )
        stored = await cur.fetchone()
        if stored is None or str(stored["state"] or "") in {"delivered", "suppressed"}:
            return None
        try:
            queued = json.loads(str(stored["payload_json"] or "{}"))
        except Exception:
            queued = durable_payload
        if not isinstance(queued, dict):
            queued = durable_payload
        queued["_notificationUuid"] = str(stored["notification_uuid"] or "")
        queued["_notificationKey"] = notification_key
        task_uuid = str(payload.get("taskUuid") or payload.get("jobId") or "").strip()
        task_status = str(payload.get("status") or "").strip()
        if task_uuid and task_status:
            cur = await self.db.conn.execute(
                """
                SELECT notification_uuid FROM web_task_notifications
                WHERE conversation_uuid=? AND task_uuid=? AND task_status=? AND state='pending'
                ORDER BY id ASC
                """,
                (conv_uuid, task_uuid, task_status),
            )
            queued["_notificationUuids"] = [
                str(item["notification_uuid"] or "") for item in await cur.fetchall()
                if str(item["notification_uuid"] or "")
            ]
        return queued

    @staticmethod
    def _web_task_notification_ids(payload: dict[str, Any]) -> set[str]:
        ids = {
            str(item or "")
            for item in (payload.get("_notificationUuids") if isinstance(payload.get("_notificationUuids"), list) else [])
        }
        ids.add(str(payload.get("_notificationUuid") or ""))
        return ids - {""}

    async def _offer_web_task_notification_to_controller(
        self,
        conversation: dict[str, Any] | None,
        payload: dict[str, Any],
        *,
        root_turn_uuid: str = "",
        allow_wake: bool = True,
    ) -> bool:
        """Mirror one durable notification into its live same-root controller.

        The SQLite outbox remains authoritative. This helper only supplies the
        low-latency wake edge needed by ``AgentWait``; if that edge cannot be
        classified safely, it wakes conservatively and lets the controller's
        durable snapshot decide what to do. Returning ``False`` means there is
        no matching live controller and the normal post-turn worker owns delivery.
        """
        row = dict(conversation or {})
        conv_uuid = str(row.get("conversation_uuid") or payload.get("conversationUuid") or "").strip()
        chat_id = int(row.get("internal_chat_id") or payload.get("chatId") or 0)
        if not conv_uuid or not chat_id:
            return False
        wake = self._web_controller_wake_events.get(conv_uuid)
        if wake is None:
            return False

        target_root = str(
            payload.get("runRootTurnUuid")
            or payload.get("rootTurnUuid")
            or payload.get("turnUuid")
            or ""
        ).strip()
        if not target_root:
            with contextlib.suppress(Exception):
                target_root = await self._root_turn_for_task_notification(conv_uuid, payload)
        controller_root = str(root_turn_uuid or "").strip()
        if not controller_root:
            with contextlib.suppress(Exception):
                active_round = await self._web_active_round_info(conv_uuid, chat_id)
                controller_root = str(active_round.get("rootTurnUuid") or "").strip()
        if target_root and controller_root and target_root != controller_root:
            return False
        scoped_root = controller_root or target_root

        notifications = self._web_controller_notifications.setdefault(conv_uuid, [])
        payload_ids = self._web_task_notification_ids(payload)
        for index, existing in enumerate(notifications):
            if not isinstance(existing, dict):
                continue
            existing_ids = self._web_task_notification_ids(existing)
            if not payload_ids or not (payload_ids & existing_ids):
                continue
            merged = dict(payload)
            merged_ids = payload_ids | existing_ids
            merged["_notificationUuids"] = sorted(merged_ids)
            if str(merged.get("_notificationUuid") or "") not in merged_ids:
                merged["_notificationUuid"] = sorted(merged_ids)[0]
            notifications[index] = merged
            break
        else:
            notifications.append(dict(payload))

        if not allow_wake:
            return True

        status = str(payload.get("status") or "")
        urgent = bool(payload.get("requiresDecision")) or status in {
            "needs_openbear_control",
            "failed",
            "cancelled",
            "interrupted",
        }
        if not urgent and self.rath_dao is not None:
            try:
                active = await self.rath_dao.active_tasks_for_chat(chat_id, limit=100, controllable=True)
                active = [
                    task
                    for task in active
                    if str(getattr(task, "parent_session_uuid", "") or "") == conv_uuid
                ]
                if scoped_root:
                    active = await self._filter_tasks_for_root_turn(conv_uuid, active, scoped_root)
                urgent = not active
            except Exception:
                # A failed best-effort classification must never turn a durable
                # terminal result into an infinite event_only wait.
                log.exception(
                    "Agent 通知唤醒分类失败，改为保守唤醒",
                    会话=chat_id,
                    conversation_uuid=conv_uuid,
                )
                urgent = True
        if urgent and self._web_controller_wake_events.get(conv_uuid) is wake:
            wake.set()
        return True

    def _ensure_web_task_notification_worker(
        self,
        conversation_uuid: str,
        internal_chat_id: int,
        owner_chat_id: int = 0,
        *,
        task_name: str = "task",
    ) -> None:
        """Own one post-turn drain without yielding between check and claim."""
        conv_uuid = str(conversation_uuid or "").strip()
        if (
            not conv_uuid
            or not int(internal_chat_id or 0)
            or not self._web_task_notification_pending.get(conv_uuid)
            or conv_uuid in self._web_task_notification_workers
        ):
            return
        self._web_task_notification_workers.add(conv_uuid)
        asyncio.create_task(
            self._run_web_task_notification_when_idle(
                conversation_uuid=conv_uuid,
                internal_chat_id=int(internal_chat_id),
                owner_chat_id=int(owner_chat_id or 0),
                payload={},
            ),
            name=f"web-task-notification-{str(task_name or conv_uuid)[:8] or 'task'}",
        )

    def _enqueue_web_task_notification(
        self,
        conversation_uuid: str,
        internal_chat_id: int,
        owner_chat_id: int,
        payload: dict[str, Any],
    ) -> None:
        """Mirror one durable outbox fact into the level-triggered drain."""
        conv_uuid = str(conversation_uuid or "").strip()
        ids = self._web_task_notification_ids(payload)
        if not conv_uuid or not ids:
            return
        pending = self._web_task_notification_pending.setdefault(conv_uuid, [])
        for index, existing in enumerate(pending):
            if not isinstance(existing, dict):
                continue
            existing_ids = self._web_task_notification_ids(existing)
            if not existing_ids & ids:
                continue
            # SQL fallback may already be mirrored when the richer callback
            # arrives. Preserve every durable UUID while keeping richer fields.
            merged = dict(payload)
            merged_ids = existing_ids | ids
            merged["_notificationUuids"] = sorted(merged_ids)
            if str(merged.get("_notificationUuid") or "") not in merged_ids:
                merged["_notificationUuid"] = sorted(merged_ids)[0]
            pending[index] = merged
            break
        else:
            pending.append(dict(payload))
        self._ensure_web_task_notification_worker(
            conv_uuid,
            int(internal_chat_id or 0),
            int(owner_chat_id or 0),
            task_name=str(payload.get("taskUuid") or payload.get("jobId") or conv_uuid),
        )

    def _remove_web_task_notification_ids_from_pending(
        self,
        conversation_uuid: str,
        notification_uuids: set[str],
    ) -> None:
        """Remove a same-root claim from its post-turn in-memory mirror."""
        conv_uuid = str(conversation_uuid or "").strip()
        remove_ids = {str(item) for item in notification_uuids if str(item)}
        if not conv_uuid or not remove_ids:
            return
        remaining_payloads: list[dict[str, Any]] = []
        for item in self._web_task_notification_pending.get(conv_uuid, []):
            if not isinstance(item, dict):
                continue
            item_ids = self._web_task_notification_ids(item)
            if not item_ids:
                remaining_payloads.append(item)
                continue
            remaining_ids = item_ids - remove_ids
            if not remaining_ids:
                continue
            updated = dict(item)
            updated["_notificationUuids"] = sorted(remaining_ids)
            if str(updated.get("_notificationUuid") or "") not in remaining_ids:
                updated["_notificationUuid"] = sorted(remaining_ids)[0]
            remaining_payloads.append(updated)
        if remaining_payloads:
            self._web_task_notification_pending[conv_uuid] = remaining_payloads
        else:
            self._web_task_notification_pending.pop(conv_uuid, None)

    async def _mark_web_task_notifications_suppressed(self, payloads: list[dict[str, Any]]) -> None:
        ids = sorted(set().union(*(
            self._web_task_notification_ids(item) for item in payloads if isinstance(item, dict)
        )) if payloads else set())
        if not ids:
            return
        ts = now_ts()
        await self.db.conn.execute(
            f"UPDATE web_task_notifications SET state='suppressed', claim_token='', delivered_at=?, updated_at=? WHERE notification_uuid IN ({','.join('?' for _ in ids)}) AND state<>'delivered'",
            (ts, ts, *ids),
        )
        await self.db.conn.commit()

    async def _mark_web_task_notifications_delivered(self, notification_uuids: set[str] | list[str]) -> None:
        ids = sorted({str(item) for item in notification_uuids if str(item)})
        if not ids:
            return
        ts = now_ts()
        await self.db.conn.execute(
            f"UPDATE web_task_notifications SET state='delivered', claim_token='', delivered_at=?, updated_at=?, last_error='' WHERE notification_uuid IN ({','.join('?' for _ in ids)}) AND state<>'delivered'",
            (ts, ts, *ids),
        )
        await self.db.conn.commit()

    async def _partition_web_task_notification_ack(
        self,
        notification_uuids: set[str] | list[str],
    ) -> tuple[set[str], set[str]]:
        """Partition successful delivery by each notification's durable boundary."""
        ids = sorted({str(item) for item in notification_uuids if str(item)})
        if not ids:
            return set(), set()
        cur = await self.db.conn.execute(
            f"""
            SELECT n.notification_uuid, n.state, n.kind, n.task_status, n.task_uuid,
                   n.payload_json, t.status AS rath_task_status
            FROM web_task_notifications n
            LEFT JOIN rath_tasks t ON t.task_uuid=n.task_uuid
            WHERE n.notification_uuid IN ({','.join('?' for _ in ids)})
            """,
            tuple(ids),
        )
        resolved: set[str] = set()
        unresolved: set[str] = set(ids)
        suppress_ids: set[str] = set()
        terminal = {"completed", "failed", "cancelled", "interrupted"}
        for row in await cur.fetchall():
            notification_uuid = str(row["notification_uuid"] or "")
            if not notification_uuid:
                continue
            state = str(row["state"] or "")
            if state in {"delivered", "suppressed"}:
                resolved.add(notification_uuid)
                unresolved.discard(notification_uuid)
                continue
            try:
                stored_payload = json.loads(str(row["payload_json"] or "{}"))
            except Exception:
                stored_payload = {}
            if not isinstance(stored_payload, dict):
                stored_payload = {}
            kind = str(row["kind"] or stored_payload.get("kind") or "")
            notification_status = str(
                row["task_status"] or stored_payload.get("status") or stored_payload.get("taskStatus") or ""
            )
            task_uuid = str(row["task_uuid"] or stored_payload.get("taskUuid") or "")
            task_status = str(row["rath_task_status"] or "")
            if kind == "plan-approval-required":
                # AgentPlanDecision marks only the exact task/version row delivered.
                if task_status in terminal:
                    suppress_ids.add(notification_uuid)
                    resolved.add(notification_uuid)
                    unresolved.discard(notification_uuid)
                continue
            if notification_status == "needs_openbear_control":
                # Legacy fallback rows do not identify a control generation. If the
                # task is waiting again, conservatively retain the notice rather than
                # acknowledge a possibly newer boundary.
                if task_uuid and task_status and task_status != "needs_openbear_control":
                    resolved.add(notification_uuid)
                    unresolved.discard(notification_uuid)
                continue
            # Terminal results and ordinary tool/task notifications keep the existing
            # successful-delivery acknowledgement semantics.
            resolved.add(notification_uuid)
            unresolved.discard(notification_uuid)
        if suppress_ids:
            await self._mark_web_task_notifications_suppressed([
                {"_notificationUuid": notification_uuid} for notification_uuid in sorted(suppress_ids)
            ])
        return resolved, unresolved

    async def _plan_notification_turn_resolved(self, payloads: list[dict[str, Any]]) -> bool:
        ids = set().union(*(
            self._web_task_notification_ids(item)
            for item in payloads
            if isinstance(item, dict) and str(item.get("kind") or "") == "plan-approval-required"
        )) if payloads else set()
        if not ids:
            return True
        resolved, unresolved = await self._partition_web_task_notification_ack(ids)
        return not unresolved and resolved == ids

    async def _claim_web_task_notifications(self, payloads: list[dict[str, Any]]) -> tuple[str, set[str]]:
        ids = set().union(*(
            self._web_task_notification_ids(item) for item in payloads if isinstance(item, dict)
        )) if payloads else set()
        if not ids:
            return "", set()
        token = str(uuid.uuid4())
        ts = now_ts()
        await self.db.conn.execute(
            f"UPDATE web_task_notifications SET state='processing', claim_token=?, claimed_at=?, attempts=attempts+1, updated_at=? WHERE notification_uuid IN ({','.join('?' for _ in ids)}) AND state='pending'",
            (token, ts, ts, *sorted(ids)),
        )
        await self.db.conn.commit()
        cur = await self.db.conn.execute(
            f"SELECT notification_uuid FROM web_task_notifications WHERE notification_uuid IN ({','.join('?' for _ in ids)}) AND state='processing' AND claim_token=?",
            (*sorted(ids), token),
        )
        claimed = {str(row["notification_uuid"] or "") for row in await cur.fetchall()}
        return token, claimed

    async def _requeue_web_task_notifications(self, notification_uuids: set[str], error: str) -> None:
        ids = sorted({str(item) for item in notification_uuids if str(item)})
        if not ids:
            return
        ts = now_ts()
        await self.db.conn.execute(
            f"UPDATE web_task_notifications SET state='pending', claim_token='', claimed_at=0, next_attempt_at=?, last_error=?, updated_at=? WHERE notification_uuid IN ({','.join('?' for _ in ids)}) AND state='processing'",
            (ts + 1, str(error or "notification delivery failed")[:2000], ts, *ids),
        )
        await self.db.conn.commit()

    @staticmethod
    def _hydrate_recovered_agent_notification(payload: dict[str, Any], stored: Any) -> dict[str, Any]:
        """Restore canonical Agent payload data from the authoritative Rath row.

        The SQL terminal trigger atomically stores ``output_json`` in its durable
        fallback.  A process crash can happen before the richer Python callback
        is persisted, so recovery also restores real final-output usage and the
        standard controller instruction without creating another notification
        identity.
        """
        data = dict(payload or {})
        status = str(data.get("status") or data.get("taskStatus") or "").strip()
        try:
            actual_output_tokens = max(0, int(stored["task_last_output_tokens"] or 0))
        except (KeyError, TypeError, ValueError, IndexError):
            actual_output_tokens = 0
        if status == "completed":
            try:
                existing_tokens = max(0, int(data.get("resultOutputTokens") or 0))
            except (TypeError, ValueError):
                existing_tokens = 0
            if existing_tokens <= 0 and actual_output_tokens > 0:
                data["resultOutputTokens"] = actual_output_tokens
            try:
                existing_count = max(0, int(data.get("resultCount") or 0))
            except (TypeError, ValueError):
                existing_count = 0
            if existing_count <= 0:
                data["resultCount"] = 1
        if data.get("durableFallback"):
            raw_content = str(data.get("content") or "").strip()
            result: dict[str, Any] = {}
            if raw_content:
                try:
                    decoded = json.loads(raw_content)
                except Exception:
                    decoded = None
                if isinstance(decoded, dict):
                    result = decoded
            if result:
                data["result"] = result
            data["content"] = _render_agent_task_notification(data)
        return data

    async def _recover_web_task_notifications(
        self,
        conversation_uuid: str = "",
        *,
        reset_processing: bool = False,
    ) -> None:
        ts = now_ts()
        if not conversation_uuid:
            # Startup can reclaim every process-owned lease. During normal
            # operation only expired leases are reclaimed, so a transient DB
            # failure cannot strand a notification forever without stealing a
            # legitimately long model turn from its live worker.
            if reset_processing:
                claim_where = "state='processing'"
                claim_params: tuple[Any, ...] = (ts,)
            else:
                claim_where = "state='processing' AND claimed_at>0 AND claimed_at<=?"
                claim_params = (ts, ts - 900)
            await self.db.conn.execute(
                f"UPDATE web_task_notifications SET state='pending', claim_token='', claimed_at=0, next_attempt_at=0, updated_at=? WHERE {claim_where}",
                claim_params,
            )
            await self.db.conn.commit()
        where = "n.state='pending' AND COALESCE(n.next_attempt_at,0)<=?"
        params: list[Any] = [ts]
        if conversation_uuid:
            where += " AND n.conversation_uuid=?"
            params.append(str(conversation_uuid))
        cur = await self.db.conn.execute(
            f"""
            SELECT n.*, c.owner_chat_id AS conversation_owner_chat_id,
                   t.last_output_tokens AS task_last_output_tokens,
                   t.status AS rath_task_status
            FROM web_task_notifications n
            JOIN web_conversations c ON c.conversation_uuid=n.conversation_uuid
            LEFT JOIN rath_tasks t ON t.task_uuid=n.task_uuid
            WHERE {where}
            ORDER BY n.id ASC
            LIMIT 500
            """,
            tuple(params),
        )
        controller_offers: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for stored in await cur.fetchall():
            try:
                payload = json.loads(str(stored["payload_json"] or "{}"))
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            payload = self._hydrate_recovered_agent_notification(payload, stored)
            payload["_notificationUuid"] = str(stored["notification_uuid"] or "")
            payload["_notificationKey"] = str(stored["notification_key"] or "")
            if str(payload.get("kind") or "") == "plan-approval-required" and str(
                stored["rath_task_status"] or ""
            ) in {"completed", "failed", "cancelled", "interrupted"}:
                await self._mark_web_task_notifications_suppressed([payload])
                continue
            conv_uuid = str(stored["conversation_uuid"] or "")
            internal_chat_id = int(stored["internal_chat_id"] or 0)
            owner_chat_id = int(stored["conversation_owner_chat_id"] or stored["owner_chat_id"] or 0)
            self._enqueue_web_task_notification(
                conv_uuid,
                internal_chat_id,
                owner_chat_id,
                payload,
            )
            controller_offers.append((
                {
                    "conversation_uuid": conv_uuid,
                    "internal_chat_id": internal_chat_id,
                    "owner_chat_id": owner_chat_id,
                },
                payload,
            ))
        # Populate every same-root result before setting any wake edge. Otherwise
        # a recovered batch can wake on its first row and let AgentWait consume a
        # partial result set while later rows are still being hydrated.
        for offer_row, offer_payload in controller_offers:
            await self._offer_web_task_notification_to_controller(
                offer_row,
                offer_payload,
                allow_wake=False,
            )
        for offer_row, offer_payload in controller_offers:
            await self._offer_web_task_notification_to_controller(offer_row, offer_payload)

    async def _schedule_web_task_notification(self, conversation: dict[str, Any] | None, payload: dict[str, Any]) -> None:
        """Queue a Claude-Code-style task-notification for a Web conversation.

        The Rath child task is already detached from the main OpenBear run.  When
        it completes, we wait until the owning Web conversation is idle and then
        start a new OpenBear turn with the notification as an internal user-role
        reminder.  A per-chat lock gives us QueryGuard-like serialization: only
        one notification turn can run at a time, and user turns keep priority
        because this method waits for `runs.is_running` to go false.
        """
        if not isinstance(payload, dict):
            return
        # Bash is foreground-complete. Ignore legacy/in-flight Bash completion
        # callbacks so they cannot create a delayed synthetic user turn after the
        # owning controller turn has already finished.
        if str(payload.get("kind") or "").startswith("tool-notification") and str(payload.get("toolName") or "") == "Bash":
            return
        row = dict(conversation or {})
        conv_uuid = str(row.get("conversation_uuid") or payload.get("conversationUuid") or "").strip()
        internal_chat_id = int(row.get("internal_chat_id") or payload.get("chatId") or 0)
        owner_chat_id = int(row.get("owner_chat_id") or 0)
        if not conv_uuid or not internal_chat_id:
            return
        queued = await self._persist_web_task_notification(row, payload)
        if queued is None:
            return
        self._enqueue_web_task_notification(
            conv_uuid,
            internal_chat_id,
            owner_chat_id,
            queued,
        )
        await self._offer_web_task_notification_to_controller(row, queued)

    @staticmethod
    def _agent_task_uuids_from_payload(payload: dict[str, Any] | None) -> set[str]:
        out: set[str] = set()
        if not isinstance(payload, dict):
            return out
        for key in ("taskUuid", "task_uuid"):
            value = str(payload.get(key) or "").strip()
            if value:
                out.add(value)
        task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
        for key in ("taskUuid", "task_uuid"):
            value = str(task.get(key) or "").strip()
            if value:
                out.add(value)
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        result_task = result.get("task") if isinstance(result.get("task"), dict) else {}
        for source in (result, result_task):
            for key in ("taskUuid", "task_uuid"):
                value = str(source.get(key) or "").strip()
                if value:
                    out.add(value)
        if isinstance(payload.get("taskUuids"), list):
            out.update(str(x).strip() for x in payload.get("taskUuids") or [] if str(x).strip())
        for item in payload.get("results") if isinstance(payload.get("results"), list) else []:
            if isinstance(item, dict):
                out.update(WebAdminConversationsMixin._agent_task_uuids_from_payload(item))
        return out

    @staticmethod
    def _agent_terminal_label(status: str, reason: str = "") -> str:
        if reason and status in {"cancelled", "interrupted"}:
            return reason
        if status == "partial":
            return "部分完成"
        return _task_status_label(str(status or ""))

    @staticmethod
    def _agent_operation_terminal_patch(payload: dict[str, Any], status: str, *, reason: str = "") -> dict[str, Any]:
        terminal_keep = {"completed", "failed", "cancelled", "interrupted"}
        status = str(status or "").strip()
        label = WebAdminConversationsMixin._agent_terminal_label(status, reason)

        def update_item(item: dict[str, Any]) -> dict[str, Any]:
            current = str(item.get("status") or "").strip()
            if current in terminal_keep:
                return item
            next_item = {**item, "status": status}
            if label:
                next_item["currentStatus"] = label
            return next_item

        def update_task(task: dict[str, Any]) -> dict[str, Any]:
            current = str(task.get("status") or "").strip()
            if current in terminal_keep:
                return task
            next_task = {**task, "status": status}
            if label:
                next_task["currentStatus"] = label
            return next_task

        patch: dict[str, Any] = {"status": status}
        if reason:
            patch["reason"] = reason
            patch["error"] = reason
        if isinstance(payload.get("results"), list):
            next_results = []
            for item in payload.get("results") or []:
                if not isinstance(item, dict):
                    next_results.append(item)
                    continue
                next_item = update_item(item)
                task = item.get("task") if isinstance(item.get("task"), dict) else None
                if task is not None:
                    next_item = {**next_item, "task": update_task(task)}
                result = item.get("result") if isinstance(item.get("result"), dict) else None
                result_task = result.get("task") if isinstance(result, dict) and isinstance(result.get("task"), dict) else None
                if result_task is not None:
                    next_item = {**next_item, "result": {**result, "task": update_task(result_task)}}
                next_results.append(next_item)
            patch["results"] = next_results
        task = payload.get("task") if isinstance(payload.get("task"), dict) else None
        if task is not None:
            patch["task"] = update_task(task)
        result = payload.get("result") if isinstance(payload.get("result"), dict) else None
        result_task = result.get("task") if isinstance(result, dict) and isinstance(result.get("task"), dict) else None
        if result_task is not None:
            patch["result"] = {"task": update_task(result_task)}
        if payload.get("resultText"):
            with contextlib.suppress(Exception):
                result_data = json.loads(str(payload.get("resultText") or "{}"))
                if isinstance(result_data, dict):
                    result_data["status"] = status
                    result_data["ok"] = status == "completed"
                    if patch.get("error"):
                        result_data["error"] = patch["error"]
                    if "task" in patch:
                        result_data["task"] = patch["task"]
                    if "results" in patch:
                        result_data["results"] = patch["results"]
                    patch["resultText"] = json.dumps(result_data, ensure_ascii=False, indent=2, default=str)
        return patch

    @staticmethod
    def _terminal_status_from_agent_notification(payload: dict[str, Any]) -> str:
        status = str(payload.get("status") or "").strip()
        if status in {"completed", "failed", "cancelled", "interrupted", "needs_openbear_control"}:
            return status
        return status or "completed"

    async def _publish_agent_notification_completion(self, live: _WebLiveStream, payload: dict[str, Any]) -> None:
        task_uuids = self._agent_task_uuids_from_payload(payload)
        if not task_uuids:
            return
        cur = await self.db.conn.execute(
            """
            SELECT * FROM web_operations
            WHERE conversation_uuid=? AND op_type='agent'
            ORDER BY display_seq ASC, id ASC
            """,
            (live.conversation_uuid,),
        )
        rows = await cur.fetchall()
        status = self._terminal_status_from_agent_notification(payload)
        for row in rows:
            old_payload = operation_json_loads_dict(str(row["payload_json"] or "{}"))
            if old_payload.get("merged") is True:
                continue
            if not (self._agent_task_uuids_from_payload(old_payload) & task_uuids):
                continue
            tool_call_id = str(old_payload.get("toolCallId") or str(row["op_id"] or "").split(":", 1)[-1] or "").strip()
            tool_name = str(old_payload.get("rootToolName") or old_payload.get("toolName") or old_payload.get("name") or "Agent").strip()
            next_payload = dict(old_payload)
            for key in ("taskUuid", "continued", "agentSession", "task", "result", "recentEvents", "summary"):
                if key in payload:
                    next_payload[key] = payload[key]
            next_payload.update({
                "status": status,
                "detached": bool(next_payload.get("detached")),
                "completedByTaskNotification": True,
                "toolCallId": tool_call_id,
                "toolName": tool_name,
                "name": str(old_payload.get("name") or tool_name),
                "args": str(old_payload.get("args") or old_payload.get("arguments") or ""),
                "arguments": str(old_payload.get("arguments") or old_payload.get("args") or ""),
            })
            event = {
                "type": "tool_progress",
                "chatId": live.internal_chat_id,
                "turnUuid": str(row["turn_uuid"] or ""),
                "toolCallId": tool_call_id,
                "name": tool_name,
                "arguments": str(next_payload.get("arguments") or ""),
                "payload": next_payload,
                "_webOperationSpecs": [{
                    "op_id": str(row["op_id"] or ""),
                    "op_type": "agent",
                    "action": "end",
                    "turn_uuid": str(row["turn_uuid"] or ""),
                    "payload": next_payload,
                    "status": status,
                    "lifecycle": "terminal" if status != "needs_openbear_control" else "waiting_control",
                    "source": "task_notification",
                }],
            }
            # The outbox row is acknowledged only after this durable operation
            # update and the follow-up turn both succeed. Let publication errors
            # escape so the worker can requeue/reconcile instead of losing the
            # terminal Agent card update.
            await live.publish(event)

    @staticmethod
    def _task_notification_status(payload: dict[str, Any]) -> str:
        return str((payload or {}).get("status") or "completed").strip() or "completed"

    @staticmethod
    def _is_waiting_control_notification(payload: dict[str, Any]) -> bool:
        return WebAdminConversationsMixin._task_notification_status(payload) == "needs_openbear_control"

    @staticmethod
    def _dedupe_web_task_notifications(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep the newest durable task revision and retain every outbox id."""
        keyed: dict[str, dict[str, Any]] = {}
        versions: dict[str, tuple[int, int]] = {}
        order: list[str] = []
        out_unkeyed: list[dict[str, Any]] = []
        for index, item in enumerate(payloads):
            if not isinstance(item, dict):
                continue
            data = dict(item)
            key = str(data.get("taskUuid") or data.get("jobId") or "").strip()
            if not key:
                out_unkeyed.append(data)
                continue
            recent = data.get("recentEvents") if isinstance(data.get("recentEvents"), list) else []
            event_seq = max([int(event.get("seq") or 0) for event in recent if isinstance(event, dict)] or [0])
            candidate_version = (event_seq, index)
            if key not in keyed:
                order.append(key)
                keyed[key] = data
                versions[key] = candidate_version
                keyed[key]["_notificationUuids"] = sorted(
                    WebAdminConversationsMixin._web_task_notification_ids(data)
                )
                continue
            all_ids = (
                WebAdminConversationsMixin._web_task_notification_ids(keyed[key])
                | WebAdminConversationsMixin._web_task_notification_ids(data)
            )
            if candidate_version >= versions[key]:
                keyed[key] = data
                versions[key] = candidate_version
            keyed[key]["_notificationUuids"] = sorted(all_ids)
        return [keyed[key] for key in order if key in keyed] + out_unkeyed

    @staticmethod
    def _controller_task_notification(payload: dict[str, Any]) -> dict[str, Any]:
        """Project one durable UI notification into minimal controller context.

        Web operations and the Agent monitor retain the full payload. The main
        model receives only fields needed for a decision or final synthesis, so
        ``content`` cannot duplicate Plan/result/event structures already
        present in the same notification.
        """
        if not isinstance(payload, dict):
            return {}
        kind = str(payload.get("kind") or "task-notification")
        status = str(payload.get("status") or "")
        item: dict[str, Any] = {
            "kind": kind,
            "status": status,
            "taskUuid": str(payload.get("taskUuid") or payload.get("jobId") or ""),
            "summary": str(payload.get("summary") or ""),
        }
        notification_ids = sorted(WebAdminConversationsMixin._web_task_notification_ids(payload))
        if notification_ids:
            item["notificationUuids"] = notification_ids
        if kind == "plan-approval-required":
            for key in (
                "requiresDecision", "expectedPlanVersion", "planVersion", "planType",
                "approvalCycle", "revisionCount", "controllerGuidance", "plan",
                "completedHistory", "priorDecisions", "decisionTool",
            ):
                if key in payload:
                    item[key] = payload[key]
            task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
            item["task"] = {
                key: task[key]
                for key in ("taskUuid", "title", "status", "currentStatus")
                if key in task
            }
            return item
        result = payload.get("result")
        if result not in (None, "", {}, []):
            item["result"] = result
        else:
            content = str(payload.get("content") or "").strip()
            if content:
                item["content"] = content
        task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
        if task:
            item["task"] = {
                key: task[key]
                for key in ("taskUuid", "title", "status", "currentStatus", "output")
                if key in task
            }
        for key in ("reason", "continuable", "next", "controlUuid"):
            if key in payload:
                item[key] = payload[key]
        return item

    @staticmethod
    def _task_notification_result_budget(payload: dict[str, Any]) -> tuple[int, int]:
        """Return output tokens/count for newly delivered completed Agent results."""
        if not isinstance(payload, dict):
            return 0, 0
        try:
            explicit_tokens = max(0, int(payload.get("resultOutputTokens") or 0))
            explicit_count = max(0, int(payload.get("resultCount") or 0))
        except (TypeError, ValueError):
            explicit_tokens = 0
            explicit_count = 0
        if explicit_tokens > 0 or explicit_count > 0:
            return explicit_tokens, explicit_count
        if str(payload.get("status") or "") != "completed":
            return 0, 0
        task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
        last_usage = task.get("lastUsage") if isinstance(task.get("lastUsage"), dict) else {}
        try:
            fallback = max(0, int(last_usage.get("outputTokens") or 0))
        except (TypeError, ValueError):
            fallback = 0
        if fallback <= 0:
            candidate = payload.get("result")
            if not candidate:
                candidate = payload.get("content")
            if candidate:
                fallback = max(0, estimate_tokens(json.dumps(candidate, ensure_ascii=False, default=str)))
        return fallback, 1

    @staticmethod
    def _batch_task_notification_summary(statuses: list[str], total: int) -> str:
        completed = len([status for status in statuses if status == "completed"])
        failed = len([status for status in statuses if status == "failed"])
        waiting = len([status for status in statuses if status == "needs_openbear_control"])
        other = max(0, int(total or 0) - completed - failed - waiting)
        parts: list[str] = []
        if completed:
            parts.append(f"{completed} 个后台任务完成")
        if failed:
            parts.append(f"{failed} 个后台任务失败")
        if waiting:
            parts.append(f"{waiting} 个后台任务等待裁决")
        if other:
            parts.append(f"{other} 个后台任务有新进展")
        return "，".join(parts) if parts else f"{total} 个后台任务有新进展"

    @staticmethod
    def _coalesce_web_task_notifications(payloads: list[dict[str, Any]]) -> dict[str, Any]:
        items = WebAdminConversationsMixin._dedupe_web_task_notifications([dict(item) for item in payloads if isinstance(item, dict)])
        if len(items) <= 1:
            return items[0] if items else {}
        summaries: list[str] = []
        contents: list[str] = []
        statuses: list[str] = []
        task_uuids: list[str] = []
        kinds = {str(item.get("kind") or "task-notification") for item in items}
        for idx, item in enumerate(items, 1):
            kind = str(item.get("kind") or "task-notification")
            tool_name = str(item.get("toolName") or "").strip()
            task_uuid = str(item.get("taskUuid") or item.get("jobId") or "").strip()
            if task_uuid:
                task_uuids.append(task_uuid)
            status = WebAdminConversationsMixin._task_notification_status(item)
            statuses.append(status)
            summary = str(item.get("summary") or (f"{tool_name or '后台工具'} 任务完成" if kind == "tool-notification" else "Agent task completed")).strip()
            label = f"{idx}. {summary}"
            if task_uuid:
                label += f"（{task_uuid[:8]}）"
            summaries.append(label)
            content = str(item.get("content") or "").strip()
            if not content:
                content = json.dumps({k: v for k, v in item.items() if k != "content"}, ensure_ascii=False, indent=2, default=str)
            contents.append(f"<notification index=\"{idx}\" status=\"{status}\">\n{content}\n</notification>")
        all_completed = all(status == "completed" for status in statuses)
        waiting = any(status == "needs_openbear_control" for status in statuses)
        result_budgets = [WebAdminConversationsMixin._task_notification_result_budget(item) for item in items]
        return {
            "kind": "task-notification-batch" if kinds != {"tool-notification"} else "tool-notification-batch",
            "status": "needs_openbear_control" if waiting else ("completed" if all_completed else "partial"),
            "summary": WebAdminConversationsMixin._batch_task_notification_summary(statuses, len(items)),
            "content": "\n\n".join(contents),
            "results": items,
            "taskUuids": task_uuids,
            "batched": True,
            "batchCount": len(items),
            "batchSummaries": summaries,
            "resultOutputTokens": sum(tokens for tokens, _count in result_budgets),
            "resultCount": sum(count for _tokens, count in result_budgets),
        }

    async def _run_web_task_notification_when_idle(
        self,
        *,
        conversation_uuid: str,
        internal_chat_id: int,
        owner_chat_id: int = 0,
        payload: dict[str, Any],
    ) -> None:
        lock = self._web_task_notification_lock(internal_chat_id)
        inflight_notification_ids: set[str] = set()
        try:
            async with lock:
                while True:
                    # Match Claude Code's queue processor: task-notification is handled
                    # only once the active query is idle.  New notifications that arrive
                    # while waiting are batched into one internal follow-up turn.
                    while self.runs is not None and self.runs.is_running(internal_chat_id):
                        await asyncio.sleep(0.5)
                    # Small debounce so near-simultaneous Agent completions summarize once.
                    await asyncio.sleep(0.35)
                    pending = self._web_task_notification_pending.pop(conversation_uuid, [])
                    if payload:
                        pending.insert(0, dict(payload))
                        payload = {}
                    if not pending:
                        return

                    row = None
                    if owner_chat_id:
                        row = await self._conversation_row(owner_chat_id, conversation_uuid)
                    if row is None:
                        cur = await self.db.conn.execute(
                            "SELECT * FROM web_conversations WHERE conversation_uuid=?",
                            (conversation_uuid,),
                        )
                        fetched = await cur.fetchone()
                        row = dict(fetched) if fetched else None
                    if row is None:
                        await self._mark_web_task_notifications_suppressed(pending)
                        return

                    filtered_current: list[dict[str, Any]] = []
                    suppressed: list[dict[str, Any]] = []
                    for item in pending:
                        if await self._should_suppress_web_task_notification(conversation_uuid, item):
                            suppressed.append(item)
                        else:
                            filtered_current.append(item)
                    deferred_existing: list[dict[str, Any]] = []
                    for item in self._web_task_notification_deferred.pop(conversation_uuid, []):
                        if await self._should_suppress_web_task_notification(conversation_uuid, item):
                            suppressed.append(item)
                        else:
                            deferred_existing.append(item)
                    await self._mark_web_task_notifications_suppressed(suppressed)
                    if not filtered_current and not deferred_existing:
                        continue
                    all_notifications = self._dedupe_web_task_notifications([*deferred_existing, *filtered_current])
                    waiting_control_notifications = [item for item in all_notifications if self._is_waiting_control_notification(item)]
                    result_notifications = [item for item in all_notifications if not self._is_waiting_control_notification(item)]
                    # Budget pauses / OpenBear-control waits are not task completion.
                    # Notify the controller so it may continue/stop that Agent, but
                    # keep already completed sibling results deferred for the real
                    # final synthesis after all siblings are terminal.
                    if waiting_control_notifications:
                        if result_notifications:
                            self._web_task_notification_deferred[conversation_uuid] = result_notifications
                        combined_notifications = waiting_control_notifications
                    else:
                        combined_notifications = result_notifications
                    if not combined_notifications:
                        continue
                    batch_payload = self._coalesce_web_task_notifications(combined_notifications)
                    root_turn_uuid = await self._root_turn_for_task_notification(conversation_uuid, batch_payload)
                    batch_task_uuids = self._agent_task_uuids_from_payload(batch_payload)
                    remaining_related_tasks = []
                    # A control-boundary notice must wake the main controller
                    # immediately.  Waiting for any sibling (running *or* also
                    # waiting for control) creates a circular dependency: only
                    # that controller turn can decide whether the paused task
                    # should continue or stop.  Ordinary completion results still
                    # wait for their related siblings so final synthesis remains
                    # consolidated.
                    if not waiting_control_notifications and self.rath_dao is not None:
                        with contextlib.suppress(Exception):
                            remaining_related_tasks = await self.rath_dao.active_tasks_for_chat(internal_chat_id, limit=100, controllable=True)
                        remaining_related_tasks = [
                            task_row for task_row in remaining_related_tasks
                            if str(getattr(task_row, "parent_session_uuid", "") or "") == conversation_uuid
                            and str(getattr(task_row, "task_uuid", "") or "") not in batch_task_uuids
                        ]
                        remaining_related_tasks = await self._filter_tasks_for_root_turn(conversation_uuid, remaining_related_tasks, root_turn_uuid)
                    if remaining_related_tasks:
                        live = self._live_for(row)
                        current_payload = self._coalesce_web_task_notifications(filtered_current) if filtered_current else batch_payload
                        current_task_uuids = self._agent_task_uuids_from_payload(current_payload)
                        notification_text = str(current_payload.get("summary") or "后台任务已有部分结果，等待其它相关任务完成后统一汇总").strip()
                        await live.publish({
                            "type": "task_notification",
                            "turnUuid": root_turn_uuid,
                            "rootTurnUuid": root_turn_uuid,
                            "taskUuid": str(next(iter(current_task_uuids or batch_task_uuids), "")),
                            "taskUuids": sorted(current_task_uuids or batch_task_uuids),
                            "status": str(current_payload.get("status") or ""),
                            "summary": notification_text,
                            "text": notification_text,
                            "hidden": True,
                            "internal": True,
                            "deferredUntilRelatedTasksTerminal": True,
                            "remainingTaskUuids": [str(getattr(task_row, "task_uuid", "") or "") for task_row in remaining_related_tasks],
                        })
                        for item in filtered_current:
                            await self._publish_agent_notification_completion(live, item)
                        # Expose the deferred fact only after its durable hidden
                        # notification/completion frames are committed.  Callers
                        # that observe this in-memory marker can then safely read
                        # the corresponding frame log without racing publication.
                        self._web_task_notification_deferred[conversation_uuid] = combined_notifications
                        await self._touch_web_conversation(
                            conversation_uuid,
                            status="running",
                            current_status="等待其它后台任务完成",
                            last_error="",
                        )
                        continue
                    filtered = combined_notifications
                    _claim_token, claimed_notification_ids = await self._claim_web_task_notifications(filtered)
                    inflight_notification_ids = set(claimed_notification_ids)
                    if any(item.get("_notificationUuid") for item in filtered) and not claimed_notification_ids:
                        continue
                    task: asyncio.Task[Any] | None = None
                    while task is None:
                        async with self.operation_locks.chat(internal_chat_id, "web_task_notification"):
                            if self.runs is not None and self.runs.is_running(internal_chat_id):
                                pass
                            else:
                                live = self._live_for(row)
                                turn_uuid = root_turn_uuid or str(uuid.uuid4())
                                notification_text = str(batch_payload.get("content") or "").strip()
                                if not notification_text:
                                    notification_text = json.dumps({k: v for k, v in batch_payload.items() if k != "content"}, ensure_ascii=False, indent=2, default=str)
                                kind = str(batch_payload.get("kind") or "task-notification").strip()
                                tool_name = str(batch_payload.get("toolName") or "").strip()
                                default_summary = f"{tool_name or '后台工具'} 任务完成" if kind == "tool-notification" else "Agent task completed"
                                status = str(batch_payload.get("status") or "").strip()
                                if str(batch_payload.get("batched") or ""):
                                    default_summary = self._batch_task_notification_summary(
                                        [self._task_notification_status(item) for item in filtered],
                                        int(batch_payload.get("batchCount") or len(filtered)),
                                    )
                                waiting_control = status == "needs_openbear_control" or any(self._is_waiting_control_notification(item) for item in filtered)
                                current_status = (
                                    "Agent 等待 OpenBear 裁决"
                                    if waiting_control else (
                                        f"后台 {tool_name} 结果汇总中" if kind == "tool-notification" and tool_name else ("后台任务结果汇总中" if kind.startswith("tool-notification") else "Agent 结果汇总中")
                                    )
                                )
                                summary = str(batch_payload.get("summary") or default_summary).strip()
                                task_info = batch_payload.get("task") if isinstance(batch_payload.get("task"), dict) else {}
                                title = str(batch_payload.get("title") or task_info.get("title") or "").strip()
                                task_uuid = str(batch_payload.get("taskUuid") or batch_payload.get("jobId") or "").strip()
                                if not task_uuid:
                                    task_uuids = batch_payload.get("taskUuids") if isinstance(batch_payload.get("taskUuids"), list) else []
                                    task_uuid = str(task_uuids[0] if len(task_uuids) == 1 else "").strip()
                                execution_run_uuid = str(uuid.uuid4())
                                await live.publish({
                                    "type": "accepted",
                                    "chatId": internal_chat_id,
                                    "turnUuid": turn_uuid,
                                    "rootTurnUuid": turn_uuid,
                                    "runUuid": execution_run_uuid,
                                    "executionRunUuid": execution_run_uuid,
                                    "taskNotification": True,
                                    "taskNotificationSilent": False,
                                    "hidden": False,
                                })
                                await live.publish({
                                    "type": "task_notification",
                                    "turnUuid": turn_uuid,
                                    "rootTurnUuid": turn_uuid,
                                    "taskUuid": task_uuid,
                                    "taskUuids": batch_payload.get("taskUuids") if isinstance(batch_payload.get("taskUuids"), list) else ([task_uuid] if task_uuid else []),
                                    "status": status,
                                    "summary": summary,
                                    "title": title,
                                    "batchCount": int(batch_payload.get("batchCount") or 0),
                                    "text": summary,
                                    "hidden": False,
                                })
                                for item in filtered:
                                    await self._publish_agent_notification_completion(live, item)
                                await self._touch_web_conversation(
                                    conversation_uuid,
                                    status="running",
                                    current_status=current_status,
                                    last_error="",
                                )
                                segment_index = await self._next_assistant_segment_index(conversation_uuid, turn_uuid)
                                live._assistant_segment = max(int(getattr(live, "_assistant_segment", 0) or 0), segment_index)
                                renderer = _WebStreamRenderer(
                                    live=live,
                                    artifact_rewriter=self._web_assistant_artifact_rewriter(row, turn_uuid=turn_uuid),
                                )
                                task_uuid_for_name = str(task_uuid or "batch").strip()
                                task = asyncio.create_task(
                                    self._run_web_turn(
                                        internal_chat_id,
                                        notification_text,
                                        renderer,
                                        media=[],
                                        conversation=row,
                                        task_notification=True,
                                        task_notification_payload=batch_payload,
                                        root_turn_uuid=turn_uuid,
                                    ),
                                    name=f"web-task-notification-turn-{task_uuid_for_name[:8] or internal_chat_id}",
                                )
                                if self.runs is not None:
                                    self.runs.register(internal_chat_id, task)
                        if task is None:
                            await asyncio.sleep(0.5)
                    try:
                        delivery_result = await task
                        # Compatibility fakes/legacy handlers returned None on
                        # successful completion; production now returns False
                        # explicitly for failed/cancelled notification turns.
                        delivered = delivery_result is not False
                    except asyncio.CancelledError:
                        await asyncio.shield(self._requeue_web_task_notifications(claimed_notification_ids, "notification turn cancelled"))
                        raise
                    except Exception as exc:
                        await self._requeue_web_task_notifications(
                            claimed_notification_ids,
                            f"{type(exc).__name__}: {exc}",
                        )
                        inflight_notification_ids.clear()
                        await asyncio.sleep(1.0)
                        await self._recover_web_task_notifications(conversation_uuid)
                    else:
                        if delivered:
                            resolved_ids, unresolved_ids = await self._partition_web_task_notification_ack(
                                claimed_notification_ids
                            )
                            if resolved_ids:
                                await self._mark_web_task_notifications_delivered(resolved_ids)
                            if unresolved_ids:
                                await self._requeue_web_task_notifications(
                                    unresolved_ids,
                                    "notification boundary remains unresolved after controller turn",
                                )
                            inflight_notification_ids.clear()
                            if unresolved_ids:
                                # Do not immediately reclaim the same unresolved
                                # boundary. A new producer event or durable recovery
                                # scan will trigger the next reasonable review.
                                return
                        else:
                            await self._requeue_web_task_notifications(
                                claimed_notification_ids,
                                "notification turn did not produce a successful final response",
                            )
                            inflight_notification_ids.clear()
                            await asyncio.sleep(1.0)
                            await self._recover_web_task_notifications(conversation_uuid)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Web task notification worker failed", 会话=conversation_uuid)
            if inflight_notification_ids:
                await asyncio.shield(self._requeue_web_task_notifications(
                    inflight_notification_ids,
                    f"{type(exc).__name__}: {exc}",
                ))
                inflight_notification_ids.clear()
            await asyncio.sleep(1.0)
            await self._recover_web_task_notifications(conversation_uuid)
        finally:
            if inflight_notification_ids:
                with contextlib.suppress(Exception):
                    await asyncio.shield(self._requeue_web_task_notifications(
                        inflight_notification_ids,
                        "notification worker stopped before acknowledgement",
                    ))
            # Release ownership and recheck pending without an await in
            # between. Producers use the same no-await enqueue/owner sequence, so
            # one side always observes and owns newly arrived work.
            self._web_task_notification_workers.discard(conversation_uuid)
            self._ensure_web_task_notification_worker(
                conversation_uuid,
                internal_chat_id,
                owner_chat_id,
                task_name=conversation_uuid,
            )

    def _web_operation_lock(self, conversation_uuid: str) -> asyncio.Lock:
        key = str(conversation_uuid or "")
        lock = self._web_operation_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._web_operation_locks[key] = lock
        return lock


    async def _touch_web_conversation(
        self,
        conversation_uuid: str,
        *,
        status: str | None = None,
        current_status: str | None = None,
        last_error: str | None = None,
    ) -> None:
        parts = ["updated_at=?"]
        params: list[Any] = [now_ts()]
        if status is not None:
            parts.append("status=?")
            params.append(status)
        if current_status is not None:
            parts.append("current_status=?")
            params.append(current_status)
        if last_error is not None:
            parts.append("last_error=?")
            params.append(last_error)
        params.append(conversation_uuid)
        await self.db.conn.execute(
            f"UPDATE web_conversations SET {', '.join(parts)} WHERE conversation_uuid=?",
            tuple(params),
        )
        await self.db.conn.commit()

    async def _maybe_title_web_conversation(self, row: dict[str, Any], text: str) -> None:
        title = str(row.get("title") or "").strip()
        if title and title not in {"新对话", "当前对话"}:
            return
        clean = re.sub(r"\s+", " ", str(text or "")).strip()
        if not clean:
            return
        new_title = clean[:36] + ("…" if len(clean) > 36 else "")
        await self.db.conn.execute(
            "UPDATE web_conversations SET title=?, updated_at=? WHERE conversation_uuid=?",
            (new_title, now_ts(), str(row.get("conversation_uuid") or "")),
        )
        await self.db.conn.commit()
        row["title"] = new_title

__all__ = [name for name in globals() if not name.startswith("__")]
