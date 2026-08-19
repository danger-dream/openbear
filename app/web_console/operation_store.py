# ruff: noqa: F401,F403,F405
from __future__ import annotations

from app.web_console.core import *
from app.web_console.live_stream import *


class WebAdminOperationsMixin:
    async def _prune_web_event_frames(
        self,
        conversation_uuid: str = "",
        *,
        keep_recent: int = 5000,
        max_age_days: int = 7,
    ) -> int:
        """Bound the append-only transport log without harming operation recovery.

        ``web_operations`` remains the full snapshot source of truth. The newest
        frame for every operation is retained, together with a reconnect window
        per conversation; only older superseded frames are removed.
        """
        cutoff_ms = int((time.time() - max(1, int(max_age_days)) * 86_400) * 1000)
        where_conversation = ""
        params: list[Any] = [cutoff_ms, max(100, int(keep_recent))]
        if conversation_uuid:
            where_conversation = "AND f.conversation_uuid=?"
            params.append(str(conversation_uuid))
        cur = await self.db.conn.execute(
            f"""
            DELETE FROM web_event_frames AS f
            WHERE (
                f.updated_at_ms < ?
                OR f.frame_seq <= (
                  SELECT COALESCE(MAX(recent.frame_seq), 0) - ?
                  FROM web_event_frames AS recent
                  WHERE recent.conversation_uuid=f.conversation_uuid
                )
              )
              {where_conversation}
              AND f.id NOT IN (
                SELECT MAX(latest.id)
                FROM web_event_frames AS latest
                GROUP BY latest.conversation_uuid, latest.op_id
              )
            """,
            tuple(params),
        )
        await self.db.conn.commit()
        return max(0, int(cur.rowcount or 0))

    async def _prune_web_task_notification_history(self, *, max_age_days: int = 30) -> int:
        cutoff = int(time.time()) - max(1, int(max_age_days)) * 86_400
        cur = await self.db.conn.execute(
            """
            DELETE FROM web_task_notifications
            WHERE state IN ('delivered','suppressed') AND updated_at < ?
            """,
            (cutoff,),
        )
        await self.db.conn.commit()
        return max(0, int(cur.rowcount or 0))

    async def _publish_operation(
        self,
        conversation_uuid: str,
        *,
        internal_chat_id: int = 0,
        owner_chat_id: int = 0,
        op_id: str,
        op_type: str,
        action: str,
        turn_uuid: str = "",
        parent_turn_uuid: str = "",
        run_root_turn_uuid: str = "",
        run_id: str = "",
        payload: dict[str, Any] | None = None,
        status: str = "",
        lifecycle: str = "",
        source: str = "",
        internal: bool = False,
        debug: dict[str, Any] | None = None,
        skip_if_missing: bool = False,
        conn: Any | None = None,
    ) -> dict[str, Any] | None:
        """Append one v2 frame and upsert its operation snapshot.

        For one opId, only revision/updatedAtMs advance. The caller is
        responsible for holding the per-conversation operation lock so
        frameSeq/displaySeq allocation stays consistent.
        """
        conv_uuid = str(conversation_uuid or "").strip()
        op_id = str(op_id or "").strip()
        op_type = str(op_type or "").strip()
        action = str(action or "patch").strip()
        if not conv_uuid or not op_id or not op_type:
            return None
        if conn is None:
            async with self.db.web_operation_transaction() as transaction_conn:
                return await self._publish_operation(
                    conv_uuid,
                    internal_chat_id=internal_chat_id,
                    owner_chat_id=owner_chat_id,
                    op_id=op_id,
                    op_type=op_type,
                    action=action,
                    turn_uuid=turn_uuid,
                    parent_turn_uuid=parent_turn_uuid,
                    run_root_turn_uuid=run_root_turn_uuid,
                    run_id=run_id,
                    payload=payload,
                    status=status,
                    lifecycle=lifecycle,
                    source=source,
                    internal=internal,
                    debug=debug,
                    skip_if_missing=skip_if_missing,
                    conn=transaction_conn,
                )

        cur = await conn.execute(
            "SELECT * FROM web_operations WHERE conversation_uuid=? AND op_id=? LIMIT 1",
            (conv_uuid, op_id),
        )
        existing = await cur.fetchone()
        if existing is None and skip_if_missing:
            return None
        patch_payload = dict(payload or {})
        if existing is not None and op_type == "agent":
            existing_payload = operation_json_loads_dict(str(existing["payload_json"] or "{}"))
            for field in ("rootToolCallId", "rootToolName", "rootArguments"):
                if existing_payload.get(field):
                    patch_payload.pop(field, None)
        if (
            existing is not None
            and op_type == "run"
            and action == "start"
            and str(existing["lifecycle"] or "") == "terminal"
        ):
            raise RuntimeError(f"terminal Web run cannot be restarted: {op_id}")
        if (
            existing is not None
            and op_type == "agent"
            and str(existing["lifecycle"] or "") == "terminal"
        ):
            existing_payload = operation_json_loads_dict(str(existing["payload_json"] or "{}"))
            if bool(existing_payload.get("merged")):
                # A tool-call placeholder is a one-way redirect to the first
                # durable task card that claimed it. Late progress snapshots
                # must not reopen it or retarget it to another concurrent task.
                return None
            existing_status = str(existing["status"] or "")
            incoming_status = str(patch_payload.get("status") or status or "")
            if action in {"start", "append", "patch"} or (
                incoming_status and existing_status and incoming_status != existing_status
            ):
                log.error(
                    "拒绝修改已终结的 Web Agent operation",
                    会话=conv_uuid,
                    operation=op_id,
                    action=action,
                    旧状态=existing_status,
                    新状态=incoming_status,
                )
                return None
        if (
            existing is not None
            and op_type == "agent_control"
            and str(existing["lifecycle"] or "") == "terminal"
        ):
            existing_payload = operation_json_loads_dict(str(existing["payload_json"] or "{}"))
            first_transcript_result = (
                action == "end"
                and bool(patch_payload.get("transcriptResult"))
                and not bool(existing_payload.get("transcriptResult"))
            )
            # A terminal task-progress snapshot can race immediately ahead of its
            # own tool_result; allow that one result to enrich the command
            # transcript. After the result boundary, detached child progress is
            # task telemetry and must never reopen the completed command or keep
            # the whole conversation active.
            if not first_transcript_result:
                return None
        if (
            existing is not None
            and op_type in {"assistant_message", "reasoning"}
            and str(existing["lifecycle"] or "") == "terminal"
        ):
            # Completed model responses are immutable facts.  This includes a
            # repeated `end`: AgentWait may cut an already-closed segment when
            # no new assistant delta was emitted between two reviews.  Advancing
            # revision/updatedAtMs for that idempotent boundary rewrites the
            # visible timestamp of the old reasoning row.
            if action != "end":
                log.error(
                    "忽略已完成的 Web assistant operation 后续事件",
                    会话=conv_uuid,
                    operation=op_id,
                    action=action,
                )
            return None

        old_payload = operation_json_loads_dict(str(existing["payload_json"] or "{}")) if existing is not None else {}
        now_ms_value = int(time.time() * 1000)
        created_at_ms = int(existing["created_at_ms"] or now_ms_value) if existing is not None else now_ms_value
        revision = int(existing["revision"] or 0) + 1 if existing is not None else 1
        display_seq = int(existing["display_seq"] or 0) if existing is not None else 0
        if display_seq <= 0:
            cur = await conn.execute(
                "SELECT COALESCE(MAX(display_seq), 0) + 10 AS next_display_seq FROM web_operations WHERE conversation_uuid=?",
                (conv_uuid,),
            )
            row = await cur.fetchone()
            display_seq = int((row["next_display_seq"] if row else 10) or 10)
        cur = await conn.execute(
            "SELECT COALESCE(MAX(frame_seq), 0) + 1 AS next_frame_seq FROM web_event_frames WHERE conversation_uuid=?",
            (conv_uuid,),
        )
        frame_row = await cur.fetchone()
        frame_seq = int((frame_row["next_frame_seq"] if frame_row else 1) or 1)

        snapshot_payload = reduce_operation_payload(
            old_payload,
            op_type=op_type,
            action=action,
            patch=patch_payload,
        )
        frame_payload = frame_payload_for_action(
            old_payload=old_payload,
            op_type=op_type,
            action=action,
            patch=patch_payload,
            snapshot_payload=snapshot_payload,
        )
        if existing is not None and op_type == "agent" and str(existing["turn_uuid"] or ""):
            # One Rath task is one visual Agent card. Later AgentMessage,
            # AgentStop, and task-notification patches may originate from
            # internal controller turns; they must update the card in-place, not
            # move it under the later notification execution.
            turn_uuid = str(existing["turn_uuid"] or "")
            run_root_turn_uuid = str(existing["run_root_turn_uuid"] or run_root_turn_uuid or "")
            run_id = str(existing["run_id"] or run_id or "")
        status_value = str(status or snapshot_payload.get("status") or "").strip()
        lifecycle_value = str(lifecycle or status_lifecycle(op_type, status_value, snapshot_payload) or "")
        if lifecycle_value in {"terminal", "waiting_control"}:
            # updated_at_ms remains useful as the latest transport revision, but
            # elapsed time must stop at the first terminal transition.  Persist
            # that boundary in the snapshot so frame retention cannot erase it.
            terminal_at_ms = int(old_payload.get("terminalAtMs") or now_ms_value)
            snapshot_payload["terminalAtMs"] = terminal_at_ms
            frame_payload["terminalAtMs"] = terminal_at_ms
        internal_value = 1 if (internal or bool(snapshot_payload.get("internal"))) else 0
        if existing is not None and op_type == "agent" and int(existing["internal"] or 0) == 0:
            internal_value = 0
        source_value = str(source or "")
        target_fields = _operation_target_fields(
            {
                "op_type": op_type,
                "turn_uuid": turn_uuid,
                "run_root_turn_uuid": run_root_turn_uuid,
                "target_type": str(existing["target_type"] or "") if existing is not None else "",
                "target_id": str(existing["target_id"] or "") if existing is not None else "",
                "task_uuid": str(existing["task_uuid"] or "") if existing is not None else "",
                "run_id": str(existing["run_id"] or "") if existing is not None else "",
            },
            snapshot_payload,
        )
        target_type = str(target_fields.get("targetType") or "")
        target_id = str(target_fields.get("targetId") or "")
        task_uuid = str(target_fields.get("taskUuid") or "")
        run_id = str(run_id or target_fields.get("runId") or "")
        if run_id and target_type == "run":
            target_id = run_id
        existing_transcript_ids: list[Any] = []
        if existing is not None:
            existing_transcript_ids = operation_json_loads_list(str(existing["transcript_message_ids_json"] or "[]"))
            if not isinstance(existing_transcript_ids, list):
                existing_transcript_ids = []
        incoming_transcript_ids = snapshot_payload.get("transcriptMessageIds")
        linked_cur = await conn.execute(
            """
            SELECT message_id FROM web_operation_messages
            WHERE conversation_uuid=? AND op_id=?
            ORDER BY id ASC
            """,
            (conv_uuid, op_id),
        )
        linked_transcript_ids = [int(row["message_id"]) for row in await linked_cur.fetchall()]
        if isinstance(incoming_transcript_ids, list):
            merged_transcript: list[Any] = []
            seen_transcript: set[str] = set()
            for item in [*existing_transcript_ids, *incoming_transcript_ids, *linked_transcript_ids]:
                key = str(item)
                if not key or key in seen_transcript:
                    continue
                seen_transcript.add(key)
                if isinstance(item, int | float) or str(item).isdigit():
                    merged_transcript.append(int(item))
                else:
                    merged_transcript.append(item)
            transcript_ids = merged_transcript
        elif existing is not None:
            transcript_ids = [*existing_transcript_ids]
            seen_existing = {str(item) for item in transcript_ids}
            transcript_ids.extend(item for item in linked_transcript_ids if str(item) not in seen_existing)
        else:
            transcript_ids = linked_transcript_ids
        snapshot_payload["transcriptMessageIds"] = transcript_ids if isinstance(transcript_ids, list) else []
        transcript_json = operation_json_dumps(snapshot_payload.get("transcriptMessageIds") or [])

        if existing is None:
            await conn.execute(
                """
                INSERT INTO web_operations (
                  conversation_uuid, internal_chat_id, op_id, op_type, turn_uuid,
                  parent_turn_uuid, run_root_turn_uuid, target_type, target_id, task_uuid, run_id,
                  display_seq, status, lifecycle, internal, source, transcript_message_ids_json,
                  revision, payload_json, created_at_ms, updated_at_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    conv_uuid,
                    int(internal_chat_id or 0),
                    op_id,
                    op_type,
                    turn_uuid,
                    parent_turn_uuid,
                    run_root_turn_uuid,
                    target_type,
                    target_id,
                    task_uuid,
                    run_id,
                    display_seq,
                    status_value,
                    lifecycle_value,
                    internal_value,
                    source_value,
                    transcript_json,
                    revision,
                    operation_json_dumps(snapshot_payload),
                    created_at_ms,
                    now_ms_value,
                ),
            )
        else:
            await conn.execute(
                """
                UPDATE web_operations
                SET internal_chat_id=?, op_type=?, turn_uuid=?, parent_turn_uuid=?,
                    run_root_turn_uuid=?, target_type=?, target_id=?, task_uuid=?, run_id=?,
                    display_seq=?, status=?, lifecycle=?, internal=?, source=?,
                    transcript_message_ids_json=?, revision=?, payload_json=?, updated_at_ms=?
                WHERE conversation_uuid=? AND op_id=?
                """,
                (
                    int(internal_chat_id or existing["internal_chat_id"] or 0),
                    op_type,
                    turn_uuid or str(existing["turn_uuid"] or ""),
                    parent_turn_uuid or str(existing["parent_turn_uuid"] or ""),
                    run_root_turn_uuid or str(existing["run_root_turn_uuid"] or ""),
                    target_type or str(existing["target_type"] or ""),
                    target_id or str(existing["target_id"] or ""),
                    task_uuid or str(existing["task_uuid"] or ""),
                    run_id or str(existing["run_id"] or ""),
                    display_seq,
                    status_value or str(existing["status"] or ""),
                    lifecycle_value or str(existing["lifecycle"] or ""),
                    internal_value,
                    source_value or str(existing["source"] or ""),
                    transcript_json,
                    revision,
                    operation_json_dumps(snapshot_payload),
                    now_ms_value,
                    conv_uuid,
                    op_id,
                ),
            )
        await conn.execute(
            """
            INSERT INTO web_event_frames (
              conversation_uuid, internal_chat_id, owner_chat_id, frame_seq,
              op_id, op_type, action, turn_uuid, parent_turn_uuid, run_root_turn_uuid,
              target_type, target_id, task_uuid, run_id, revision, display_seq,
              payload_json, debug_json, created_at_ms, updated_at_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                conv_uuid,
                int(internal_chat_id or 0),
                int(owner_chat_id or 0),
                frame_seq,
                op_id,
                op_type,
                action,
                turn_uuid,
                parent_turn_uuid,
                run_root_turn_uuid,
                target_type,
                target_id,
                task_uuid,
                run_id,
                revision,
                display_seq,
                operation_json_dumps(frame_payload),
                operation_json_dumps(debug or {}),
                now_ms_value,
                now_ms_value,
            ),
        )
        return frame_public({
            "conversation_uuid": conv_uuid,
            "internal_chat_id": int(internal_chat_id or 0),
            "owner_chat_id": int(owner_chat_id or 0),
            "frame_seq": frame_seq,
            "op_id": op_id,
            "op_type": op_type,
            "action": action,
            "turn_uuid": turn_uuid,
            "parent_turn_uuid": parent_turn_uuid,
            "run_root_turn_uuid": run_root_turn_uuid,
            "target_type": target_type,
            "target_id": target_id,
            "task_uuid": task_uuid,
            "run_id": run_id,
            "revision": revision,
            "display_seq": display_seq,
            "payload": frame_payload,
            "debug": debug or {},
            "created_at_ms": now_ms_value,
            "updated_at_ms": now_ms_value,
        })

    async def _cancel_active_operations_for_stop(self, conversation_uuid: str, *, internal_chat_id: int, reason: str, debug: dict[str, Any], conn: Any) -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        cur = await conn.execute(
            """
            SELECT * FROM web_operations
            WHERE conversation_uuid=?
              AND op_type IN ('run','tool','user_interaction','agent','agent_supervision','assistant_message','reasoning','status')
              AND COALESCE(lifecycle,'') IN ('active','paused','waiting_control')
            ORDER BY display_seq ASC, id ASC
            """,
            (conversation_uuid,),
        )
        for row in await cur.fetchall():
            op_type = str(row["op_type"] or "")
            payload = operation_json_loads_dict(str(row["payload_json"] or "{}"))
            status = "interrupted" if op_type == "agent" else "cancelled"
            patch = {"status": status, "reason": reason}
            if op_type in {"assistant_message", "reasoning"}:
                patch = {"complete": True, "cancelled": True, "reason": reason}
            elif op_type in {"status", "agent_supervision"}:
                patch = {"active": False, "statusText": reason or "已停止", "status": status}
            elif op_type == "agent":
                patch = self._agent_operation_terminal_patch(payload, status, reason=reason)
            elif op_type == "user_interaction":
                patch = {"status": "cancelled", "interactionStatus": "cancelled", "reason": reason}
            frame = await self._publish_operation(
                conversation_uuid,
                internal_chat_id=internal_chat_id,
                op_id=str(row["op_id"] or ""),
                op_type=op_type,
                action="cancel",
                turn_uuid=str(row["turn_uuid"] or ""),
                payload=patch,
                status=status,
                lifecycle="terminal",
                debug={**debug, "source": "stop_active_ops"},
                conn=conn,
            )
            if frame:
                frames.append(frame)
        return frames

    async def _complete_active_operations_for_done(self, conversation_uuid: str, *, internal_chat_id: int, turn_uuid: str, debug: dict[str, Any], conn: Any) -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        conv_uuid = str(conversation_uuid or "").strip()
        if not conv_uuid:
            return frames
        # A running-time steering message can switch the live/current run before
        # the final done event is emitted. Close every active foreground run
        # operation in the conversation, not only payload.turnUuid. Keep
        # agent/tool operations out of this sweep; detached/background agents
        # have their own lifecycle.
        cur = await conn.execute(
            """
            SELECT * FROM web_operations
            WHERE conversation_uuid=?
              AND op_type IN ('run','user_interaction','status','agent_supervision','assistant_message','reasoning')
              AND COALESCE(lifecycle,'') IN ('active','paused','waiting_control')
            ORDER BY display_seq ASC, id ASC
            """,
            (conv_uuid,),
        )
        now = int(time.time() * 1000)
        for row in await cur.fetchall():
            op_type = str(row["op_type"] or "")
            row_turn = str(row["turn_uuid"] or "")
            patch: dict[str, Any]
            if op_type in {"status", "agent_supervision"}:
                patch = {"active": False, "statusText": "就绪", "status": "completed"}
            elif op_type == "run":
                patch = {"runId": str(row["run_id"] or row["target_id"] or row_turn or ""), "status": "completed", "completedAtMs": now}
            elif op_type in {"assistant_message", "reasoning"}:
                patch = {"complete": True, "status": "completed"}
            elif op_type == "user_interaction":
                patch = {"status": "completed", "interactionStatus": "timeout"}
            else:
                patch = {"status": "completed"}
            frame = await self._publish_operation(
                conv_uuid,
                internal_chat_id=internal_chat_id,
                op_id=str(row["op_id"] or ""),
                op_type=op_type,
                action="end",
                turn_uuid=row_turn,
                payload=patch,
                status="completed",
                lifecycle="terminal",
                debug={**debug, "source": "done_active_ops", "doneTurnUuid": str(turn_uuid or "")},
                conn=conn,
            )
            if frame:
                frames.append(frame)
        return frames

    async def _fail_active_operations_for_error(self, conversation_uuid: str, *, internal_chat_id: int, turn_uuid: str, error: str, debug: dict[str, Any], conn: Any) -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        conv_uuid = str(conversation_uuid or "").strip()
        if not conv_uuid:
            return frames
        cur = await conn.execute(
            """
            SELECT * FROM web_operations
            WHERE conversation_uuid=?
              AND op_type IN ('run','user_interaction','status','agent_supervision','assistant_message','reasoning')
              AND COALESCE(lifecycle,'') IN ('active','paused','waiting_control')
            ORDER BY display_seq ASC, id ASC
            """,
            (conv_uuid,),
        )
        now = int(time.time() * 1000)
        for row in await cur.fetchall():
            op_type = str(row["op_type"] or "")
            row_turn = str(row["turn_uuid"] or "")
            patch: dict[str, Any]
            if op_type in {"status", "agent_supervision"}:
                patch = {"active": False, "statusText": "出错", "status": "failed", "error": error}
            elif op_type == "run":
                patch = {"runId": str(row["run_id"] or row["target_id"] or row_turn or ""), "status": "failed", "failedAtMs": now, "error": error}
            elif op_type in {"assistant_message", "reasoning"}:
                patch = {"complete": True, "status": "failed", "error": error}
            elif op_type == "user_interaction":
                patch = {"status": "failed", "interactionStatus": "error", "error": error}
            else:
                patch = {"status": "failed", "error": error}
            frame = await self._publish_operation(
                conv_uuid,
                internal_chat_id=internal_chat_id,
                op_id=str(row["op_id"] or ""),
                op_type=op_type,
                action="error",
                turn_uuid=row_turn,
                payload=patch,
                status="failed",
                lifecycle="terminal",
                debug={**debug, "source": "error_active_ops", "errorTurnUuid": str(turn_uuid or "")},
                conn=conn,
            )
            if frame:
                frames.append(frame)
        return frames

    async def _publish_native_operations(
        self,
        payload: dict[str, Any],
        *,
        internal_chat_id: int = 0,
        owner_chat_id: int = 0,
        operation_specs: list[dict[str, Any]] | None = None,
        debug_source: str = "source_native_operation_specs",
        conn: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Append v2 frames and reduce operation snapshots from native specs.

        Operation freshness and state mutation happen only through opId +
        revision. frame_seq is the transport cursor for reconnect/resync.
        """
        conv_uuid = str(payload.get("conversationUuid") or payload.get("conversation_uuid") or "").strip()
        if not conv_uuid:
            return []
        if conn is None:
            async with self.db.web_operation_transaction() as transaction_conn:
                return await self._publish_native_operations(
                    payload,
                    internal_chat_id=internal_chat_id,
                    owner_chat_id=owner_chat_id,
                    operation_specs=operation_specs,
                    debug_source=debug_source,
                    conn=transaction_conn,
                )
        debug_base = {
            "eventType": str(payload.get("type") or payload.get("kind") or ""),
            "eventUuid": str(payload.get("eventUuid") or payload.get("event_uuid") or ""),
            "operationEventKey": _web_operation_event_key(payload),
            "source": debug_source,
        }
        frames: list[dict[str, Any]] = []
        specs = operation_specs if operation_specs is not None else web_event_operation_specs(payload)
        for spec in specs:
            frame = await self._publish_operation(
                conv_uuid,
                internal_chat_id=internal_chat_id or int(payload.get("chatId") or payload.get("chat_id") or 0),
                owner_chat_id=owner_chat_id,
                op_id=str(spec.get("op_id") or ""),
                op_type=str(spec.get("op_type") or ""),
                action=str(spec.get("action") or "patch"),
                turn_uuid=str(spec.get("turn_uuid") or ""),
                parent_turn_uuid=str(spec.get("parent_turn_uuid") or ""),
                run_root_turn_uuid=str(spec.get("run_root_turn_uuid") or ""),
                run_id=str(spec.get("run_id") or ""),
                payload=spec.get("payload") if isinstance(spec.get("payload"), dict) else {},
                status=str(spec.get("status") or ""),
                lifecycle=str(spec.get("lifecycle") or ""),
                source=str(spec.get("source") or ""),
                internal=bool(spec.get("internal")),
                skip_if_missing=bool(spec.get("skip_if_missing")),
                debug=debug_base,
                conn=conn,
            )
            if frame:
                frames.append(frame)
        event_type = str(payload.get("type") or "")
        if event_type == "stopped":
            frames.extend(await self._cancel_active_operations_for_stop(
                conv_uuid,
                internal_chat_id=internal_chat_id or int(payload.get("chatId") or payload.get("chat_id") or 0),
                reason=str(payload.get("reason") or "已停止"),
                debug=debug_base,
                conn=conn,
            ))
        elif event_type == "done":
            frames.extend(await self._complete_active_operations_for_done(
                conv_uuid,
                internal_chat_id=internal_chat_id or int(payload.get("chatId") or payload.get("chat_id") or 0),
                turn_uuid=str(payload.get("turnUuid") or payload.get("turn_uuid") or ""),
                debug=debug_base,
                conn=conn,
            ))
        elif event_type == "error":
            frames.extend(await self._fail_active_operations_for_error(
                conv_uuid,
                internal_chat_id=internal_chat_id or int(payload.get("chatId") or payload.get("chat_id") or 0),
                turn_uuid=str(payload.get("turnUuid") or payload.get("turn_uuid") or ""),
                error=str(payload.get("error") or payload.get("message") or payload.get("text") or "Web 对话出错"),
                debug=debug_base,
                conn=conn,
            ))
        return frames

    async def _resolve_transcript_op_ids(
        self,
        conversation_uuid: str,
        *,
        turn_uuid: str = "",
        role: str = "",
        tool_call_id: str = "",
        task_uuid: str = "",
        has_tool_calls: bool = False,
    ) -> list[str]:
        """Pick the durable UI operation ids that should own one messages row."""
        conv_uuid = str(conversation_uuid or "").strip()
        turn = str(turn_uuid or "").strip()
        role_name = str(role or "").strip().lower()
        tool_id = str(tool_call_id or "").strip()
        agent_task = str(task_uuid or "").strip()
        if not conv_uuid:
            return []
        op_ids: list[str] = []
        if role_name == "user" and turn:
            cur = await self.db.conn.execute(
                """
                SELECT op_id FROM web_operations
                WHERE conversation_uuid=? AND turn_uuid=? AND op_type='user_message'
                ORDER BY display_seq DESC, id DESC
                LIMIT 1
                """,
                (conv_uuid, turn),
            )
            row = await cur.fetchone()
            if row and row["op_id"]:
                op_ids.append(str(row["op_id"]))
        elif role_name == "assistant" and turn and not has_tool_calls:
            cur = await self.db.conn.execute(
                """
                SELECT op_id FROM web_operations
                WHERE conversation_uuid=? AND turn_uuid=? AND op_type='assistant_message'
                ORDER BY display_seq DESC, id DESC
                LIMIT 1
                """,
                (conv_uuid, turn),
            )
            row = await cur.fetchone()
            if row and row["op_id"]:
                op_ids.append(str(row["op_id"]))
        elif role_name in {"tool", "assistant"} and (tool_id or agent_task):
            candidates = []
            if agent_task:
                candidates.append(f"agent:{agent_task}")
            if tool_id:
                candidates.extend([
                    f"tool:{tool_id}",
                    f"agent:{tool_id}",
                    f"agent_control:{tool_id}",
                ])
            if candidates:
                placeholders = ",".join("?" for _ in candidates)
                cur = await self.db.conn.execute(
                    f"""
                    SELECT op_id FROM web_operations
                    WHERE conversation_uuid=? AND op_id IN ({placeholders})
                    ORDER BY display_seq ASC, id ASC
                    """,
                    (conv_uuid, *candidates),
                )
                op_ids.extend(str(row["op_id"]) for row in await cur.fetchall() if row and row["op_id"])
            if not op_ids and turn and tool_id:
                cur = await self.db.conn.execute(
                    """
                    SELECT op_id FROM web_operations
                    WHERE conversation_uuid=? AND turn_uuid=?
                      AND op_type IN ('tool','user_interaction','agent','agent_control')
                      AND (
                        op_id=? OR op_id=? OR op_id=?
                        OR instr(COALESCE(payload_json,''), ?) > 0
                      )
                    ORDER BY display_seq DESC, id DESC
                    LIMIT 3
                    """,
                    (
                        conv_uuid,
                        turn,
                        f"tool:{tool_id}",
                        f"agent:{tool_id}",
                        f"agent_control:{tool_id}",
                        tool_id,
                    ),
                )
                op_ids.extend(str(row["op_id"]) for row in await cur.fetchall() if row and row["op_id"])
        # Always keep the run op as a coarse owner for the whole turn when present.
        if turn:
            cur = await self.db.conn.execute(
                """
                SELECT op_id FROM web_operations
                WHERE conversation_uuid=? AND turn_uuid=? AND op_type='run'
                ORDER BY display_seq DESC, id DESC
                LIMIT 1
                """,
                (conv_uuid, turn),
            )
            row = await cur.fetchone()
            if row and row["op_id"]:
                op_ids.append(str(row["op_id"]))
        # Preserve order while de-duplicating.
        out: list[str] = []
        seen: set[str] = set()
        for op_id in op_ids:
            key = str(op_id or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(key)
        return out

    async def _transcript_target_op_ids(
        self,
        conversation_uuid: str,
        *,
        role: str = "",
        turn_uuid: str = "",
        tool_call_id: str = "",
        task_uuid: str = "",
        has_tool_calls: bool = False,
        op_ids: list[str] | None = None,
    ) -> list[str]:
        """Resolve exact owners while keeping the turn run as a coarse owner."""
        conv_uuid = str(conversation_uuid or "").strip()
        turn = str(turn_uuid or "").strip()
        targets = [str(item or "").strip() for item in (op_ids or []) if str(item or "").strip()]
        # Explicit ids are authoritative for user/assistant rows. Tool rows may
        # additionally resolve an Agent redirect that only exists after dispatch.
        if not targets or str(role or "").lower() == "tool" or task_uuid:
            targets.extend(await self._resolve_transcript_op_ids(
                conv_uuid,
                turn_uuid=turn,
                role=role,
                tool_call_id=tool_call_id,
                task_uuid=task_uuid,
                has_tool_calls=has_tool_calls,
            ))
        if turn:
            targets.append(f"run:{turn}")
        out: list[str] = []
        seen: set[str] = set()
        for op_id in targets:
            key = str(op_id or "").strip()
            if key and key not in seen:
                seen.add(key)
                out.append(key)
        return out

    async def _attach_transcript_message_ids_tx(
        self,
        conn: Any,
        conversation_uuid: str,
        *,
        message_id: int,
        target_op_ids: list[str],
    ) -> list[str]:
        """Insert normalized links and refresh operation snapshots in one transaction."""
        conv_uuid = str(conversation_uuid or "").strip()
        mid = int(message_id or 0)
        if not conv_uuid or mid <= 0:
            return []
        now_ms = int(time.time() * 1000)
        attached: list[str] = []
        for op_id in target_op_ids:
            key = str(op_id or "").strip()
            if not key:
                continue
            await conn.execute(
                """
                INSERT OR IGNORE INTO web_operation_messages (
                  conversation_uuid, op_id, message_id, relation_kind, created_at_ms
                ) VALUES (?,?,?,?,?)
                """,
                (conv_uuid, key, mid, "transcript", now_ms),
            )
            cur = await conn.execute(
                "SELECT * FROM web_operations WHERE conversation_uuid=? AND op_id=? LIMIT 1",
                (conv_uuid, key),
            )
            row = await cur.fetchone()
            if row is None:
                # The normalized link intentionally survives until a later
                # operation start; _upsert_web_operation hydrates its JSON cache.
                continue
            existing_ids = operation_json_loads_list(str(row["transcript_message_ids_json"] or "[]"))
            normalized: list[int] = []
            seen_ids: set[int] = set()
            for item in existing_ids if isinstance(existing_ids, list) else []:
                try:
                    value = int(item)
                except Exception:
                    continue
                if value > 0 and value not in seen_ids:
                    seen_ids.add(value)
                    normalized.append(value)
            if mid not in seen_ids:
                normalized.append(mid)
            payload = operation_json_loads_dict(str(row["payload_json"] or "{}"))
            payload["transcriptMessageIds"] = normalized
            await conn.execute(
                """
                UPDATE web_operations
                SET transcript_message_ids_json=?, payload_json=?, updated_at_ms=?
                WHERE conversation_uuid=? AND op_id=?
                """,
                (operation_json_dumps(normalized), operation_json_dumps(payload), now_ms, conv_uuid, key),
            )
            attached.append(key)
        return attached

    async def _persist_web_transcript_message(
        self,
        messages: MessageDAO,
        chat_id: int,
        role: str,
        content: str = "",
        *,
        conversation_uuid: str,
        turn_uuid: str = "",
        parent_turn_uuid: str = "",
        run_root_turn_uuid: str = "",
        task_uuid: str = "",
        agent_session_uuid: str = "",
        op_ids: list[str] | None = None,
        binding_meta: dict[str, Any] | None = None,
        **message_kwargs: Any,
    ) -> int:
        """Atomically persist one model transcript row and all exact UI links."""
        conv_uuid = str(conversation_uuid or "").strip()
        turn = str(turn_uuid or "").strip()
        meta = dict(binding_meta or {})
        targets = await self._transcript_target_op_ids(
            conv_uuid,
            role=role,
            turn_uuid=turn,
            tool_call_id=str(meta.get("toolCallId") or ""),
            task_uuid=str(meta.get("taskUuid") or task_uuid or ""),
            has_tool_calls=bool(meta.get("hasToolCalls")),
            op_ids=op_ids,
        )
        async with self._web_operation_lock(conv_uuid):
            async with self.db.conn.transaction(label="persist-web-transcript") as conn:
                message_id = await messages.add(
                    chat_id,
                    role,
                    content,
                    conversation_uuid=conv_uuid,
                    turn_uuid=turn,
                    parent_turn_uuid=str(parent_turn_uuid or "").strip(),
                    run_root_turn_uuid=str(run_root_turn_uuid or turn or "").strip(),
                    task_uuid=str(task_uuid or "").strip(),
                    agent_session_uuid=str(agent_session_uuid or "").strip(),
                    commit=False,
                    **message_kwargs,
                )
                await self._attach_transcript_message_ids_tx(
                    conn, conv_uuid, message_id=message_id, target_op_ids=targets,
                )
                return int(message_id or 0)

    async def _web_message_operation_ids(self, conversation_uuid: str, message_id: int) -> list[str]:
        """Exact reverse lookup used by deletion/audit code."""
        cur = await self.db.conn.execute(
            """
            SELECT op_id FROM web_operation_messages
            WHERE conversation_uuid=? AND message_id=?
            ORDER BY id ASC
            """,
            (str(conversation_uuid or ""), int(message_id or 0)),
        )
        return [str(row["op_id"] or "") for row in await cur.fetchall() if row["op_id"]]

    async def _web_operations(
        self,
        conversation_uuid: str,
        *,
        limit: int = 10000,
        include_tool_details: bool = True,
    ) -> list[dict[str, Any]]:
        """Return durable operations, optionally with bounded tool-card payloads."""
        conv_uuid = str(conversation_uuid or "").strip()
        if not conv_uuid:
            return []
        cur = await self.db.conn.execute(
            """
            WITH terminal_times AS (
              SELECT op_id, MIN(created_at_ms) AS terminal_at_ms
              FROM web_event_frames
              WHERE conversation_uuid=? AND action IN ('end', 'error', 'cancel', 'stop')
              GROUP BY op_id
            )
            SELECT operations.*, terminal_times.terminal_at_ms
            FROM web_operations AS operations
            LEFT JOIN terminal_times ON terminal_times.op_id=operations.op_id
            WHERE operations.conversation_uuid=?
            ORDER BY operations.display_seq ASC, operations.id ASC
            LIMIT ?
            """,
            (conv_uuid, conv_uuid, int(limit or 10000)),
        )
        return [
            operation_public(dict(row), include_tool_details=include_tool_details)
            for row in await cur.fetchall()
        ]

    async def _web_operations_page(
        self,
        conversation_uuid: str,
        *,
        limit: int,
        before_display_seq: int | None = None,
        include_tool_details: bool = True,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Return a bounded tail page without splitting visible turns.

        The index seed still targets ``limit`` operations. Its lower boundary is
        expanded until every represented visible turn is complete; rows pulled
        in by that expansion are also closed over, so the displaySeq cursor can
        never skip an intervening row or split an equal-displaySeq tie. Every
        non-terminal operation remains an extra, together with its complete
        visible turn. Full payload rows are materialized only after that bounded
        metadata closure and retain the canonical ``display_seq, id`` order.
        """
        conv_uuid = str(conversation_uuid or "").strip()
        page_limit = max(1, int(limit or 1))
        before = int(before_display_seq or 0)
        empty_page = {
            "hasMoreBefore": False,
            "nextBeforeDisplaySeq": None,
            "timelineLimit": page_limit,
            "beforeDisplaySeq": before or None,
            "hasAnyOperations": False,
            "pageMinDisplaySeq": None,
            "pageRowCount": 0,
            "selectedTurnKeys": [],
        }
        if not conv_uuid:
            return [], empty_page

        def turn_key_sql(alias: str) -> str:
            return (
                "COALESCE("
                f"NULLIF({alias}.run_root_turn_uuid, ''), "
                f"NULLIF({alias}.turn_uuid, ''), "
                f"CASE WHEN COALESCE({alias}.target_type, '')='run' THEN "
                f"COALESCE(NULLIF({alias}.run_id, ''), NULLIF({alias}.target_id, '')) END, "
                "'')"
            )

        seed_params: list[Any] = [conv_uuid]
        seed_before_clause = ""
        if before > 0:
            seed_before_clause = "AND seed.display_seq < ?"
            seed_params.append(before)
        seed_params.append(page_limit)
        seed_cur = await self.db.conn.execute(
            f"""
            SELECT seed.id, seed.display_seq, {turn_key_sql('seed')} AS turn_key
            FROM web_operations AS seed
            WHERE seed.conversation_uuid=? {seed_before_clause}
            ORDER BY seed.display_seq DESC, seed.id DESC
            LIMIT ?
            """,
            tuple(seed_params),
        )
        seed_rows = [dict(row) for row in await seed_cur.fetchall()]
        page_boundary = min((int(row.get("display_seq") or 0) for row in seed_rows), default=0)
        page_turn_keys: set[str] = set()

        # Expanding one turn can lower the boundary past other rows. Include that
        # intervening range too, then complete any newly represented turns. The
        # boundary decreases monotonically, so this converges after at most the
        # number of represented turns and normally needs one iteration.
        while page_boundary > 0:
            range_params: list[Any] = [conv_uuid, page_boundary]
            range_before_clause = ""
            if before > 0:
                range_before_clause = "AND candidate.display_seq < ?"
                range_params.append(before)
            range_cur = await self.db.conn.execute(
                f"""
                SELECT DISTINCT {turn_key_sql('candidate')} AS turn_key
                FROM web_operations AS candidate
                WHERE candidate.conversation_uuid=?
                  AND candidate.display_seq>=? {range_before_clause}
                """,
                tuple(range_params),
            )
            page_turn_keys.update(
                str(row["turn_key"] or "")
                for row in await range_cur.fetchall()
                if str(row["turn_key"] or "")
            )
            if not page_turn_keys:
                break
            placeholders = ",".join("?" for _ in page_turn_keys)
            min_cur = await self.db.conn.execute(
                f"""
                SELECT MIN(candidate.display_seq) AS min_display_seq
                FROM web_operations AS candidate
                WHERE candidate.conversation_uuid=?
                  AND {turn_key_sql('candidate')} IN ({placeholders})
                """,
                (conv_uuid, *sorted(page_turn_keys)),
            )
            min_row = await min_cur.fetchone()
            expanded_boundary = int((min_row["min_display_seq"] if min_row else 0) or page_boundary)
            if expanded_boundary >= page_boundary:
                break
            page_boundary = expanded_boundary

        active_cur = await self.db.conn.execute(
            f"""
            SELECT DISTINCT {turn_key_sql('active')} AS turn_key
            FROM web_operations AS active
            WHERE active.conversation_uuid=?
              AND COALESCE(active.lifecycle, '') NOT IN ('terminal', 'informational')
            """,
            (conv_uuid,),
        )
        active_turn_keys = {
            str(row["turn_key"] or "")
            for row in await active_cur.fetchall()
            if str(row["turn_key"] or "")
        }

        selected_conditions: list[str] = []
        selected_params: list[Any] = [conv_uuid]
        if page_boundary > 0:
            page_parts = ["(operations.display_seq>=?" + (" AND operations.display_seq<?" if before > 0 else "") + ")"]
            selected_params.append(page_boundary)
            if before > 0:
                selected_params.append(before)
            if page_turn_keys:
                placeholders = ",".join("?" for _ in page_turn_keys)
                page_parts.append(f"{turn_key_sql('operations')} IN ({placeholders})")
                selected_params.extend(sorted(page_turn_keys))
            selected_conditions.append("(" + " OR ".join(page_parts) + ")")
        active_parts = ["COALESCE(operations.lifecycle, '') NOT IN ('terminal', 'informational')"]
        if active_turn_keys:
            placeholders = ",".join("?" for _ in active_turn_keys)
            active_parts.append(f"{turn_key_sql('operations')} IN ({placeholders})")
            selected_params.extend(sorted(active_turn_keys))
        selected_conditions.append("(" + " OR ".join(active_parts) + ")")

        cur = await self.db.conn.execute(
            f"""
            SELECT operations.*, (
              SELECT MIN(frame.created_at_ms)
              FROM web_event_frames AS frame
              WHERE frame.conversation_uuid=operations.conversation_uuid
                AND frame.op_id=operations.op_id
                AND frame.action IN ('end', 'error', 'cancel', 'stop')
            ) AS terminal_at_ms
            FROM web_operations AS operations
            WHERE operations.conversation_uuid=?
              AND ({' OR '.join(selected_conditions)})
            ORDER BY operations.display_seq ASC, operations.id ASC
            """,
            tuple(selected_params),
        )
        rows = [dict(row) for row in await cur.fetchall()]

        def row_turn_key(row: dict[str, Any]) -> str:
            if row.get("run_root_turn_uuid"):
                return str(row["run_root_turn_uuid"])
            if row.get("turn_uuid"):
                return str(row["turn_uuid"])
            if str(row.get("target_type") or "") == "run":
                return str(row.get("run_id") or row.get("target_id") or "")
            return ""

        def is_page_member(row: dict[str, Any]) -> bool:
            if page_boundary <= 0:
                return False
            display_seq = int(row.get("display_seq") or 0)
            in_range = display_seq >= page_boundary and (before <= 0 or display_seq < before)
            return in_range or row_turn_key(row) in page_turn_keys

        page_display_seqs = [
            int(row.get("display_seq") or 0)
            for row in rows
            if is_page_member(row)
        ]
        has_more = False
        if page_boundary > 0:
            more_cur = await self.db.conn.execute(
                "SELECT EXISTS(SELECT 1 FROM web_operations "
                "WHERE conversation_uuid=? AND display_seq<? LIMIT 1) AS present",
                (conv_uuid, page_boundary),
            )
            more_row = await more_cur.fetchone()
            has_more = bool(more_row and int(more_row["present"] or 0))
        exists_cur = await self.db.conn.execute(
            "SELECT EXISTS(SELECT 1 FROM web_operations WHERE conversation_uuid=? LIMIT 1) AS present",
            (conv_uuid,),
        )
        exists_row = await exists_cur.fetchone()
        has_any_operations = bool(exists_row and int(exists_row["present"] or 0))
        operations = []
        for row in rows:
            public = operation_public(row, include_tool_details=include_tool_details)
            # displaySeq is the visible placement key; the durable row id is the
            # canonical tie breaker needed when independently fetched pages are
            # merged in the browser. It is transport metadata only.
            public["operationOrder"] = int(row.get("id") or 0)
            operations.append(public)
        return operations, {
            "hasMoreBefore": has_more,
            "nextBeforeDisplaySeq": page_boundary if has_more else None,
            "timelineLimit": page_limit,
            "beforeDisplaySeq": before or None,
            "hasAnyOperations": has_any_operations,
            "pageMinDisplaySeq": page_boundary or None,
            "pageRowCount": len(page_display_seqs),
            "selectedTurnKeys": sorted(page_turn_keys | active_turn_keys),
        }

    async def _web_frames(
        self,
        conversation_uuid: str,
        *,
        after_frame_seq: int = 0,
        up_to_frame_seq: int | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        conv_uuid = str(conversation_uuid or "").strip()
        if not conv_uuid:
            return []
        upper = int(up_to_frame_seq or 0)
        upper_clause = "AND frame_seq<=?" if upper > 0 else ""
        params: list[Any] = [conv_uuid, int(after_frame_seq or 0)]
        if upper > 0:
            params.append(upper)
        params.append(int(limit or 1000))
        cur = await self.db.conn.execute(
            f"""
            SELECT * FROM web_event_frames
            WHERE conversation_uuid=? AND frame_seq>? {upper_clause}
            ORDER BY frame_seq ASC
            LIMIT ?
            """,
            tuple(params),
        )
        return [frame_public(dict(row)) for row in await cur.fetchall()]

    async def _publish_web_operation_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Persist a Web live event as operation frames and return DB-authoritative payload.

        High-frequency event identity is stabilized so streaming snapshots
        update the same v2 operation instead of creating duplicate UI cards.
        """
        payload = dict(event)
        explicit_operation_specs_raw = payload.pop("_webOperationSpecs", None)
        explicit_operation_specs = explicit_operation_specs_raw if isinstance(explicit_operation_specs_raw, list) else None
        conv_uuid = str(payload.get("conversationUuid") or payload.get("conversation_uuid") or "").strip()
        if not conv_uuid:
            return payload
        cur = await self.db.conn.execute(
            "SELECT owner_chat_id, internal_chat_id FROM web_conversations WHERE conversation_uuid=? LIMIT 1",
            (conv_uuid,),
        )
        conv_row = await cur.fetchone()
        owner_chat_id = int(conv_row["owner_chat_id"] or 0) if conv_row else 0
        internal_chat_id = int(conv_row["internal_chat_id"] or payload.get("chatId") or payload.get("chat_id") or 0) if conv_row else int(payload.get("chatId") or payload.get("chat_id") or 0)
        kind = str(payload.get("type") or payload.get("kind") or "event")
        if kind in {"final", "notice"} and hasattr(self, "_rewrite_web_artifact_links"):
            old_text = str(payload.get("text") or "")
            rewritten_text = await self._rewrite_web_artifact_links(
                old_text,
                conversation=dict(conv_row) if conv_row else None,
                turn_uuid=str(payload.get("turnUuid") or payload.get("turn_uuid") or ""),
                op_id=str(payload.get("opId") or ""),
            )
            if rewritten_text != old_text:
                payload["text"] = rewritten_text
                explicit_operation_specs = None

        stable_key = _web_operation_event_key(payload, kind)
        event_uuid = str(payload.get("eventUuid") or payload.get("event_uuid") or "").strip()
        if not event_uuid and stable_key:
            event_uuid = _web_operation_event_uuid(conv_uuid, stable_key)
        if not event_uuid:
            event_uuid = str(uuid.uuid4())
        payload["eventUuid"] = event_uuid
        payload["eventId"] = event_uuid
        payload.setdefault("conversationUuid", conv_uuid)
        payload.setdefault("ts", int(time.time() * 1000))
        message_uuid = str(payload.get("messageUuid") or payload.get("message_uuid") or "").strip()
        if kind in {"user", "delta", "final", "notice", "error"} and not message_uuid:
            payload["messageUuid"] = f"{event_uuid}:message"

        async with self._web_operation_lock(conv_uuid):
            frames = await self._publish_native_operations(
                payload,
                internal_chat_id=internal_chat_id,
                owner_chat_id=owner_chat_id,
                operation_specs=explicit_operation_specs,
                debug_source="source_native_operation_specs",
            )
        if frames:
            payload["_webFrames"] = frames
            payload["frameSeq"] = frames[-1].get("frameSeq")
            payload["opId"] = frames[-1].get("opId")
            payload["opType"] = frames[-1].get("opType")
            payload["revision"] = frames[-1].get("revision")
            payload["displaySeq"] = frames[-1].get("displaySeq")
        notifier = getattr(self, "web_task_telegram", None)
        if notifier is not None:
            try:
                await notifier.observe(
                    payload,
                    owner_chat_id=owner_chat_id,
                    internal_chat_id=internal_chat_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # External notification bookkeeping must never fail the Agent run
                # whose durable operation has already been committed above.
                log.exception("记录 Web 长任务 Telegram 通知事件失败", 会话=conv_uuid, 事件=kind)
        return payload

__all__ = [name for name in globals() if not name.startswith("__")]
