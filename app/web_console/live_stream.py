# ruff: noqa: F401,F403,F405
from __future__ import annotations

import inspect

from app.agent.native_continuation import serialize_messages, validate_model_context
from app.task_memory import without_task_memory_runtime_messages
from app.web_console.core import *


class _WebLiveStream:
    """In-process live event bus for one Web conversation.

    A model run must outlive any single browser connection.  The renderer writes
    here; every WebSocket subscriber receives the same event stream, and a newly
    opened tab can rebuild the visible in-flight draft from the latest snapshot.
    """

    def __init__(
        self,
        conversation_uuid: str,
        internal_chat_id: int,
        *,
        event_sink: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self.conversation_uuid = conversation_uuid
        self.internal_chat_id = internal_chat_id
        self._event_sink = event_sink
        self.current_turn_uuid = ""
        # Lifecycle identity of the current controller execution.  It may differ
        # from current_turn_uuid when the same visible root turn is resumed.
        self.current_run_uuid = ""
        self.status = "idle"
        self.current_status = "就绪"
        self.draft_text = ""
        self.draft_reasoning = ""
        self.footer = ""
        self.live_tools: list[str] = []
        self.last_stats: dict[str, Any] | None = None
        self.last_error = ""
        self.active_retry: dict[str, Any] = {}
        self.started_at_ms = 0
        self.status_started_at_ms = 0
        self.updated_at_ms = int(time.time() * 1000)
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        # Persisting an event and broadcasting its authoritative frames is one
        # ordered transport operation.  Detached Agent progress, controller
        # stats, and foreground output can call publish concurrently; without
        # this lock a later DB frame can reach the browser before an earlier one.
        self._publish_lock = asyncio.Lock()
        self._assistant_segment = 0
        self._after_tool_boundary = False
        self._tool_turns: dict[str, str] = {}
        self._agent_turn_uuid = ""
        self._latest_user_turn_uuid = ""
        self._hidden_turn_uuids: set[str] = set()
        # Live stats repaint every 500 ms, but duration-only ticks are ephemeral
        # UI updates rather than durable timeline facts.  Persist only when a
        # non-timer field changes; every tick is still broadcast to subscribers.
        self._persisted_stats_signature = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.status == "running",
            "status": self.status,
            "currentStatus": self.current_status,
            "draftText": self.draft_text,
            "draftReasoning": self.draft_reasoning,
            "footer": self.footer,
            "liveTools": list(self.live_tools),
            "events": [],
            "lastStats": self.last_stats,
            "lastError": self.last_error,
            "activeRetry": dict(self.active_retry),
            "startedAtMs": self.started_at_ms,
            "statusStartedAtMs": self.status_started_at_ms,
            "updatedAtMs": self.updated_at_ms,
        }

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(q)

    def _overflow_subscriber(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        """Wake a lagging writer so it closes and reconnects from its frame cursor."""
        self.unsubscribe(q)
        # A full queue cannot accept a sentinel.  Discarding buffered live events
        # is safe because every Operation Frame was committed before broadcast;
        # reconnect state/frames are the authoritative recovery path.
        while True:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                break
        q.put_nowait({
            "_webLiveStreamControl": "overflow",
            "conversationUuid": self.conversation_uuid,
        })
        log.warning(
            "WebSocket subscriber queue overflow; forcing cursor reconnect",
            会话=self.conversation_uuid,
            队列上限=q.maxsize,
        )

    @staticmethod
    def _short_hash(value: Any) -> str:
        try:
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            raw = str(value)
        return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:16]

    @classmethod
    def _stats_persistence_signature(cls, payload: dict[str, Any]) -> str:
        stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
        durable_stats = {key: value for key, value in stats.items() if key != "durationMs"}
        return cls._short_hash(durable_stats)

    def _event_key(self, payload: dict[str, Any]) -> str:
        typ = str(payload.get("type") or "event")
        tool_call_id = str(payload.get("toolCallId") or payload.get("tool_call_id") or "").strip()
        name = str(payload.get("name") or "").strip()
        line = str(payload.get("line") or "").strip()
        if tool_call_id:
            return f"tool:{tool_call_id}"
        if typ.startswith("tool"):
            for marker in ("AgentMessage", "AgentStop", "Agent"):
                if marker in line or marker == name:
                    return f"tool:{marker}"
            return f"tool-line:{self._short_hash({'name': name, 'line': line})}"
        if typ in {"delta", "final", "cut"}:
            # `cut` closes the exact assistant segment that was just streamed.
            # Falling back to a generic cut key makes the operation mapper close
            # segment 0 and leaves later same-turn segments active until `done`.
            return f"assistant:draft:{self._assistant_segment}"
        if typ == "status":
            return "status:current"
        return f"{typ}:{payload.get('eventUuid') or payload.get('eventId') or self._short_hash(payload)}"

    @staticmethod
    def _tool_call_id(payload: dict[str, Any]) -> str:
        return str(payload.get("toolCallId") or payload.get("tool_call_id") or "").strip()

    @staticmethod
    def _is_detached_agent_progress(payload: dict[str, Any]) -> bool:
        """Return true for background Agent progress that must not split foreground text.

        Detached Agent progress keeps updating the Agent card while the parent
        model may already be streaming its normal assistant answer. Treating
        those background frames as a fresh tool boundary makes the next
        full-text delta start a new assistant segment, which renders as repeated
        prefixes in the Web UI.
        """
        nested = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        task = nested.get("task") if isinstance(nested.get("task"), dict) else {}
        tool_name = str(payload.get("name") or nested.get("toolName") or nested.get("name") or "").strip()
        if tool_name not in {"Agent", "AgentMessage", "AgentStop"}:
            return False
        return bool(nested.get("detached") is True or task.get("detached") is True)

    @staticmethod
    def _turn_uuid(payload: dict[str, Any]) -> str:
        return str(payload.get("turnUuid") or payload.get("turn_uuid") or "").strip()

    def _pin_tool_turn(self, payload: dict[str, Any], typ: str) -> None:
        """Keep all events for one tool call attached to its original turn.

        A queued user message arrives while the original run is still active and
        intentionally moves `current_turn_uuid` to the queued turn for the user
        bubble itself. Tool progress/result events that belong to a call that
        already started must not inherit that newer turn. The durable timeline
        uses `turnUuid` as the grouping key, so pinning by toolCallId before the
        event is persisted is the only safe source of truth.
        """
        if typ not in {"tool_start", "tool_progress", "tool_update", "tool_result"}:
            return
        tool_call_id = self._tool_call_id(payload)
        if not tool_call_id:
            return
        turn_uuid = self._turn_uuid(payload)
        mapped_turn = self._tool_turns.get(tool_call_id)
        if typ == "tool_start":
            if mapped_turn:
                payload["turnUuid"] = mapped_turn
            elif turn_uuid:
                self._tool_turns[tool_call_id] = turn_uuid
            return
        if mapped_turn:
            payload["turnUuid"] = mapped_turn
            return
        if turn_uuid:
            self._tool_turns.setdefault(tool_call_id, turn_uuid)

    def pin_tool_batch_turn(self, tool_call_ids: list[str]) -> None:
        """Attach every tool call in one model response to the active agent turn.

        Running-time user steering publishes a new user/queued turn while the
        current model response may still be executing its tool batch.  Tool
        calls that have not started yet therefore cannot rely on the mutable
        latest user turn; they must inherit the assistant response turn captured
        before the batch begins.
        """
        turn_uuid = self._agent_turn_uuid or self.current_turn_uuid
        if not turn_uuid:
            return
        for raw in tool_call_ids or []:
            tool_call_id = str(raw or "").strip()
            if tool_call_id:
                self._tool_turns.setdefault(tool_call_id, turn_uuid)

    def activate_latest_user_turn(self) -> None:
        """Move subsequent agent events to the latest queued user turn.

        The queued user bubble is persisted immediately, but the active agent
        response should only switch to that turn after the loop actually drains
        steering and injects it into the model context.
        """
        if not self._latest_user_turn_uuid:
            return
        previous_agent_turn = self._agent_turn_uuid or self.current_turn_uuid
        same_turn = bool(self._latest_user_turn_uuid and previous_agent_turn and self._latest_user_turn_uuid == previous_agent_turn)
        self._agent_turn_uuid = self._latest_user_turn_uuid
        self.current_turn_uuid = self._latest_user_turn_uuid
        if same_turn:
            self._assistant_segment += 1
        else:
            self._assistant_segment = 0
        self._after_tool_boundary = False
        self._tool_turns = {}

    async def publish(self, event: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
        async with self._publish_lock:
            return await self._publish_serialized(event, persist=persist)

    async def _publish_serialized(self, event: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
        payload = dict(event)
        payload.setdefault("conversationUuid", self.conversation_uuid)
        payload.setdefault("chatId", self.internal_chat_id)
        payload.setdefault("ts", int(time.time() * 1000))
        typ = str(payload.get("type") or "")
        # Propagate the execution identity to every event in this controller run.
        # The operation mapper uses it only for lifecycle records; visible content
        # remains grouped by the stable root turn UUID.
        if self.current_run_uuid and typ != "accepted":
            payload.setdefault("runUuid", self.current_run_uuid)
        default_turn_uuid = self.current_turn_uuid if typ in {"user", "queued"} else (self._agent_turn_uuid or self.current_turn_uuid)
        if default_turn_uuid and typ not in {"accepted"}:
            payload.setdefault("turnUuid", default_turn_uuid)
        turn_uuid_for_internal = self._turn_uuid(payload)
        if turn_uuid_for_internal and turn_uuid_for_internal in self._hidden_turn_uuids:
            payload.setdefault("internal", True)
            payload.setdefault("hidden", True)
        self._pin_tool_turn(payload, typ)
        if typ in {"tool_start", "tool_update", "tool_progress", "tool_result"}:
            # A tool boundary closes the reasoning segment that immediately
            # preceded this exact model response.  Persist the segment identity
            # before `_apply()` advances it; otherwise every later tool tries to
            # close reasoning segment 0 and leaves the real active row spinning.
            payload.setdefault("assistantSegment", self._assistant_segment)
        if typ in {"delta", "final"} and self._after_tool_boundary:
            self._assistant_segment += 1
            self._after_tool_boundary = False
        payload.setdefault("eventKey", self._event_key(payload))
        payload.setdefault("contentHash", self._short_hash({k: v for k, v in payload.items() if k not in {"ts", "eventId", "eventUuid"}}))
        if typ == "accepted":
            self._persisted_stats_signature = ""
        stats_signature = self._stats_persistence_signature(payload) if typ == "stats" else ""
        persist_event = persist and (
            not stats_signature or stats_signature != self._persisted_stats_signature
        )
        if self._event_sink is not None and persist_event:
            payload = await self._event_sink(payload)
            if stats_signature:
                self._persisted_stats_signature = stats_signature
        payload.setdefault("eventUuid", str(payload.get("eventUuid") or payload.get("eventId") or uuid.uuid4()))
        payload.setdefault("eventId", str(payload.get("eventUuid") or payload.get("eventId") or ""))
        self._apply(payload)
        _log_web_frontend_event({
            "stage": "live.publish",
            "conversationUuid": self.conversation_uuid,
            "chatId": self.internal_chat_id,
            "eventType": typ,
            "turnUuid": str(payload.get("turnUuid") or payload.get("turn_uuid") or ""),
            "eventUuid": str(payload.get("eventUuid") or payload.get("event_uuid") or ""),
            "subscriberCount": len(self._subscribers),
            "liveStatus": self.status,
            "liveCurrentStatus": self.current_status,
            "payload": payload,
        })
        stale: list[asyncio.Queue[dict[str, Any]]] = []
        for q in self._subscribers:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                stale.append(q)
        for q in stale:
            self._overflow_subscriber(q)
        return payload

    def _apply(self, event: dict[str, Any]) -> None:
        typ = str(event.get("type") or "")
        event_ts = int(event.get("ts") or int(time.time() * 1000))
        self.updated_at_ms = event_ts
        if typ == "accepted":
            self.current_turn_uuid = str(event.get("turnUuid") or event.get("turn_uuid") or self.current_turn_uuid or uuid.uuid4())
            self.current_run_uuid = str(
                event.get("runUuid")
                or event.get("run_uuid")
                or event.get("executionRunUuid")
                or event.get("execution_run_uuid")
                or self.current_turn_uuid
            )
            if event.get("taskNotificationSilent") or event.get("hidden"):
                self._hidden_turn_uuids.add(self.current_turn_uuid)
            self._agent_turn_uuid = self.current_turn_uuid
            self._latest_user_turn_uuid = self.current_turn_uuid
            self._assistant_segment = 0
            self._after_tool_boundary = False
            self._tool_turns = {}
            self.status = "running"
            self.current_status = "已发送"
            self.active_retry = {}
            self.draft_text = ""
            self.draft_reasoning = ""
            self.footer = ""
            self.live_tools = []
            self.last_stats = None
            self.last_error = ""
            self.started_at_ms = self.updated_at_ms
            self.status_started_at_ms = 0
        elif typ == "user":
            next_turn_uuid = str(event.get("turnUuid") or event.get("turn_uuid") or uuid.uuid4())
            previous_agent_turn = self._agent_turn_uuid or self.current_turn_uuid
            same_turn = bool(next_turn_uuid and previous_agent_turn and next_turn_uuid == previous_agent_turn)
            self.current_turn_uuid = next_turn_uuid
            self._latest_user_turn_uuid = self.current_turn_uuid
            if not same_turn:
                self._assistant_segment = 0
                self._after_tool_boundary = False
            self.current_status = "已发送"
        elif typ == "queued":
            self.current_status = str(event.get("status") or "已追加到当前运行")
            self.status_started_at_ms = event_ts
        elif typ == "task_notification":
            self.current_status = "Agent 结果已回传"
            self.status_started_at_ms = event_ts
        elif typ == "agent_supervision":
            self.current_status = str(event.get("statusText") or "等待 Agent")
            self.status_started_at_ms = event_ts
        elif typ == "retry_wait":
            retry = event.get("retry") if isinstance(event.get("retry"), dict) else {}
            self.active_retry = dict(retry) if retry.get("active") else {}
            if retry.get("active"):
                self.current_status = "模型调用失败，等待重试"
                self.status_started_at_ms = event_ts
        elif typ == "status":
            if self.status != "error":
                self.current_status = str(event.get("status") or "运行中")
                self.status_started_at_ms = event_ts
        elif typ in {"tool", "tool_start"}:
            tool_call_id = self._tool_call_id(event)
            turn_uuid = self._turn_uuid(event)
            if typ == "tool_start" and tool_call_id and turn_uuid:
                self._tool_turns[tool_call_id] = turn_uuid
            # Text streamed before a later tool call is a pre-tool draft, not the
            # final user-facing answer.  Clear it from the reconnect snapshot so
            # a browser refresh cannot resurrect duplicate answer text around
            # the tool timeline.  Reasoning is kept for the collapsible audit row.
            self._after_tool_boundary = True
            self.draft_text = ""
            line = str(event.get("line") or event.get("name") or "工具调用")
            self.live_tools.append(line)
            self.current_status = "工具调用中"
            self.status_started_at_ms = event_ts
        elif typ == "tool_update":
            if not self._is_detached_agent_progress(event):
                self._after_tool_boundary = True
            self.draft_text = ""
            line = str(event.get("line") or "工具更新")
            if self.live_tools:
                self.live_tools[-1] = line
            else:
                self.live_tools.append(line)
            if not self._is_detached_agent_progress(event):
                self.current_status = "工具更新"
        elif typ == "tool_progress":
            if not self._is_detached_agent_progress(event):
                self._after_tool_boundary = True
            self.draft_text = ""
            if not self._is_detached_agent_progress(event):
                self.current_status = "工具执行中"
                self.status_started_at_ms = event_ts
        elif typ == "tool_result":
            self._after_tool_boundary = True
            self.current_status = "工具已完成"
        elif typ == "cut":
            # `cut` is a real assistant response boundary even when the tool
            # that caused it is intentionally hidden (notably AgentWait).
            # The next delta must get a fresh operation identity.  Clear both
            # visible channels so a waiting controller cannot keep projecting
            # reasoning from the segment that was just closed.
            self._after_tool_boundary = True
            self.draft_text = ""
            self.draft_reasoning = ""
        elif typ in {"delta", "final"}:
            self.draft_text = str(event.get("text") or "")
            reasoning = str(event.get("reasoning") or "")
            if reasoning:
                self.draft_reasoning = reasoning
            self.footer = str(event.get("footer") or self.footer or "")
            if typ == "final":
                self.current_status = "收尾中"
        elif typ == "notice":
            self.draft_text = str(event.get("text") or "")
            self.footer = str(event.get("footer") or self.footer or "")
            self.current_status = self.draft_text or "已停止"
        elif typ == "stats":
            stats = event.get("stats") if isinstance(event.get("stats"), dict) else None
            self.last_stats = stats
            if self.status != "error" and not (stats or {}).get("live"):
                self.current_status = "已完成"
        elif typ == "error":
            self.status = "error"
            self.current_status = "出错"
            self.active_retry = {}
            self.last_error = str(event.get("error") or "")
            self.draft_text = ""
            self.draft_reasoning = ""
            self.footer = ""
            self.live_tools = []
            self.started_at_ms = 0
            self.status_started_at_ms = 0
            self.current_turn_uuid = ""
            self.current_run_uuid = ""
            self._agent_turn_uuid = ""
            self._latest_user_turn_uuid = ""
        elif typ == "stopped":
            self.status = "idle"
            self.current_status = "已停止"
            self.active_retry = {}
            self.draft_text = ""
            self.draft_reasoning = ""
            self.footer = ""
            self.live_tools = []
            self.started_at_ms = 0
            self.status_started_at_ms = 0
            self.current_turn_uuid = ""
            self.current_run_uuid = ""
            self._agent_turn_uuid = ""
            self._latest_user_turn_uuid = ""
        elif typ == "done":
            self.active_retry = {}
            if self.status == "running":
                self.status = "idle"
            if self.status == "error":
                self.current_status = "出错"
            elif self.current_status not in {"出错", "已停止"}:
                self.current_status = "就绪"
            self.draft_text = ""
            self.draft_reasoning = ""
            self.footer = ""
            self.live_tools = []
            self.started_at_ms = 0
            self.status_started_at_ms = 0
            self.current_turn_uuid = ""
            self.current_run_uuid = ""
            self._agent_turn_uuid = ""
            self._latest_user_turn_uuid = ""


class _WebStreamRenderer:
    """Browser renderer for the Agent loop.

    WebSocket frames are the only live browser transport; renderer events are
    mirrored into _WebLiveStream, persisted as operation frames, then fanned out.
    """

    def __init__(
        self,
        live: _WebLiveStream | None = None,
        *,
        artifact_rewriter: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self.live = live
        self._artifact_rewriter = artifact_rewriter
        self._footer = ""
        self._closed = False
        self._terminal_emitted = False
        self._pending_delta: dict[str, Any] | None = None
        self._last_delta_emit_ms = 0
        self._last_delta_size = 0
        self._last_delta_persist_ms = 0
        self._tool_turns: dict[str, str] = {}

    @staticmethod
    def _delta_size(event: dict[str, Any]) -> int:
        return len(str(event.get("text") or "")) + len(str(event.get("reasoning") or ""))

    def _mark_delta_emitted(self, event: dict[str, Any]) -> None:
        self._last_delta_emit_ms = int(time.monotonic() * 1000)
        self._last_delta_size = self._delta_size(event)

    async def _emit_now(self, event: dict[str, Any], *, persist: bool = True) -> None:
        if self._closed:
            return
        if self.live is not None:
            await self.live.publish(event, persist=persist)

    async def _rewrite_artifact_text(self, event: dict[str, Any]) -> dict[str, Any]:
        """Rewrite assistant-local artifact refs before they reach the browser.

        The model is instructed to use workspace-relative Markdown links such as
        ``![label](workspace/artifacts/x.png)`` as an internal handoff.  The Web UI
        must receive the private ``/api/conversations/.../artifacts/...`` URL, not
        the local path.  Do this only on terminal snapshots to avoid rewriting
        partially streamed Markdown links.
        """
        if self._artifact_rewriter is None:
            return event
        typ = str(event.get("type") or "")
        if typ not in {"final", "notice"}:
            return event
        text = str(event.get("text") or "")
        if not text:
            return event
        rewritten = await self._artifact_rewriter(text)
        if rewritten == text:
            return event
        event = dict(event)
        event["text"] = rewritten
        return event

    async def _flush_delta(self, *, force_persist: bool = False) -> None:
        if not self._pending_delta:
            return
        event = self._pending_delta
        now_ms = int(time.monotonic() * 1000)
        persist = (
            force_persist
            or not self._last_delta_persist_ms
            or now_ms - self._last_delta_persist_ms >= 250
        )
        await self._emit_now(event, persist=persist)
        if persist:
            # A broadcast-only repaint is not a durable flush. Keep its latest
            # snapshot pending so the next tool/status boundary can force it
            # into the operation log before the live draft is cleared.
            if self._pending_delta is event:
                self._pending_delta = None
            self._last_delta_persist_ms = now_ms
        self._mark_delta_emitted(event)

    async def emit(self, event: dict[str, Any]) -> None:
        if self._closed:
            return
        event = await self._rewrite_artifact_text(event)
        typ = str(event.get("type") or "")
        if typ in {"stopped", "error"}:
            self._terminal_emitted = True
        if typ == "final":
            # final already carries the complete text/reasoning snapshot, so a
            # queued intermediate delta would only make the browser repaint the
            # same answer twice right before completion.
            self._pending_delta = None
        elif typ != "delta":
            # Tool/status boundaries must persist the latest complete stream
            # snapshot even when it falls inside the normal 250 ms DB cadence.
            await self._flush_delta(force_persist=True)
        await self._emit_now(event)

    async def close(self) -> None:
        if self._closed:
            return
        await self._flush_delta(force_persist=True)
        self._closed = True
        if self._terminal_emitted:
            return
        if self.live is not None:
            live_status = str(getattr(self.live, "status", "") or "")
            live_current_status = str(getattr(self.live, "current_status", "") or "")
            # A web stop request is published through _stop_web_conversation(),
            # outside this renderer.  That terminal event clears the live turn
            # ids, so an unconditional renderer close would append a second
            # bare done event with no turnUuid.  Treat an already-stopped/error
            # live stream as terminal and leave the durable timeline clean.
            if live_status == "error" or (live_status == "idle" and live_current_status == "已停止"):
                return
        event = {"type": "done"}
        if self.live is not None:
            await self.live.publish(event)

    async def pin_tool_batch_turn(self, tool_call_ids: list[str]) -> None:
        if self.live is not None:
            self.live.pin_tool_batch_turn(tool_call_ids)

    @staticmethod
    def _steer_item_text(item: Any) -> str:
        if isinstance(item, dict):
            return str(item.get("text") or item.get("content") or "").strip()
        return str(item or "").strip()

    async def on_steers_injected(self, steers: list[Any] | None = None, *, injected_texts: list[str] | None = None, cut: bool = False) -> None:
        if self.live is None:
            return
        items = [dict(item) for item in (steers or []) if isinstance(item, dict)]
        injected = [str(text or "").strip() for text in (injected_texts or []) if str(text or "").strip()]
        if items:
            await self.live.publish({
                "type": "pending_steering",
                "action": "drain",
                "items": items,
                "itemIds": [str(item.get("id") or "") for item in items if item.get("id")],
            })
        first = items[0] if items else {}
        turn_uuid = str(first.get("turnUuid") or first.get("turn_uuid") or "").strip()
        message_uuid = str(first.get("messageUuid") or first.get("message_uuid") or "").strip()
        text = "\n\n".join(injected) if injected else "\n\n".join([self._steer_item_text(item) for item in (steers or []) if self._steer_item_text(item)])
        if turn_uuid and text:
            await self.live.publish({
                "type": "user",
                "turnUuid": turn_uuid,
                "messageUuid": message_uuid or str(uuid.uuid4()),
                "text": text,
                "steeringInjected": True,
            })
        self.live.activate_latest_user_turn()

    async def on_status(self, status: str) -> None:
        await self.emit({"type": "status", "status": status})

    async def on_retry_state(self, state: dict[str, Any]) -> None:
        await self.emit({"type": "retry_wait", "retry": dict(state)})

    async def on_tool(self, tool_line: str) -> None:
        await self.emit({"type": "tool", "line": tool_line})

    def _remember_tool_turn(self, tool_call_id: str, event: dict[str, Any] | None = None) -> None:
        if not tool_call_id:
            return
        turn_uuid = ""
        if event is not None:
            turn_uuid = str(event.get("turnUuid") or event.get("turn_uuid") or "").strip()
        if not turn_uuid and self.live is not None:
            turn_uuid = str(getattr(self.live, "_agent_turn_uuid", "") or getattr(self.live, "current_turn_uuid", "") or "").strip()
        if turn_uuid:
            self._tool_turns.setdefault(tool_call_id, turn_uuid)

    def _attach_remembered_tool_turn(self, event: dict[str, Any]) -> None:
        tool_call_id = str(event.get("toolCallId") or event.get("tool_call_id") or "").strip()
        if not tool_call_id or event.get("turnUuid") or event.get("turn_uuid"):
            return
        turn_uuid = self._tool_turns.get(tool_call_id)
        if turn_uuid:
            event["turnUuid"] = turn_uuid

    async def on_tool_start(self, tool_call_id: str, name: str, arguments: str, tool_line: str = "") -> None:
        # AgentWait has a dedicated supervision card instead of a generic tool
        # card, but it is still a hard model-response boundary.  Omitting this
        # cut previously let the post-wait response reuse and overwrite an
        # already completed assistant operation.
        if name == "AgentWait":
            self._remember_tool_turn(tool_call_id)
            await self.cut()
            return
        event = {
            "type": "tool_start",
            "toolCallId": tool_call_id,
            "name": name,
            "arguments": arguments,
            "line": tool_line,
        }
        self._remember_tool_turn(tool_call_id, event)
        await self.emit(event)

    async def on_tool_update(self, tool_line: str, *, tool_call_id: str = "", name: str = "", arguments: str = "") -> None:
        event = {"type": "tool_update", "line": tool_line}
        if tool_call_id:
            event["toolCallId"] = tool_call_id
        if name:
            event["name"] = name
        if arguments:
            event["arguments"] = arguments
        await self.emit(event)

    async def on_tool_progress(self, tool_call_id: str, name: str, arguments: str, payload: dict[str, Any]) -> None:
        event = {
            "type": "tool_progress",
            "toolCallId": tool_call_id,
            "name": name,
            "arguments": arguments,
            "payload": payload if isinstance(payload, dict) else {"value": payload},
        }
        self._attach_remembered_tool_turn(event)
        self._remember_tool_turn(tool_call_id, event)
        if self._closed and self.live is not None:
            await self.live.publish(event)
            return
        await self.emit(event)

    async def on_tool_result(self, tool_call_id: str, name: str, arguments: str, result: str, duration_ms: int) -> None:
        if name == "AgentWait":
            return
        result_text = str(result or "")
        result_size = len(result_text.encode("utf-8"))
        display_result = result_text
        if name not in {"Agent", "AgentMessage", "AgentStop"} and len(display_result) > 12_000:
            display_result = (
                display_result[:12_000]
                + f"\n\n…[工具结果 {result_size} bytes，实时事件仅展示预览；完整结果保存在消息记录]"
            )
        event = {
            "type": "tool_result",
            "toolCallId": tool_call_id,
            "name": name,
            "arguments": arguments,
            "result": display_result,
            "resultSize": result_size,
            "resultTruncated": display_result != result_text,
            "durationMs": duration_ms,
        }
        self._attach_remembered_tool_turn(event)
        self._remember_tool_turn(tool_call_id, event)
        if self._closed and self.live is not None:
            await self.live.publish(event)
            return
        await self.emit(event)

    async def on_delta(self, full_text: str, reasoning: str = "") -> None:
        if self._closed:
            return
        event = {"type": "delta", "text": full_text, "reasoning": reasoning}
        self._pending_delta = event
        now_ms = int(time.monotonic() * 1000)
        size = self._delta_size(event)
        # The model stream can be much faster than a browser can markdown-render
        # complete snapshots.  Emit the latest snapshot at UI cadence instead of
        # forwarding every token; large jumps still flush immediately so very
        # fast completions do not look artificially throttled.
        if (
            not self._last_delta_emit_ms
            or now_ms - self._last_delta_emit_ms >= 50
            or abs(size - self._last_delta_size) >= 800
        ):
            await self._flush_delta()

    async def finalize(self, full_text: str, reasoning: str = "") -> None:
        await self.emit({"type": "final", "text": full_text, "reasoning": reasoning, "footer": self._footer})

    async def finalize_notice(self, note: str) -> None:
        await self.emit({"type": "notice", "text": note, "footer": self._footer})

    async def fail(self, error_text: str) -> None:
        await self.emit({"type": "error", "error": error_text})

    def set_footer(self, footer_html: str) -> None:
        self._footer = footer_html or ""

    async def cut(self) -> None:
        await self.emit({"type": "cut"})


class _WebDBPersister:
    def __init__(
        self,
        messages: MessageDAO,
        chat_id: int,
        session_uuid: str = "",
        *,
        conversation_uuid: str = "",
        protocol: str = "",
        model: str = "",
        model_label: str = "",
        turn_uuid: str = "",
        parent_turn_uuid: str = "",
        run_root_turn_uuid: str = "",
        task_uuid: str = "",
        agent_session_uuid: str = "",
        message_writer: Callable[[str, str, dict[str, Any], dict[str, Any]], Awaitable[int]] | None = None,
        on_message_saved: Callable[[int, str, dict[str, Any]], Awaitable[None] | None] | Callable[[int, str, dict[str, Any]], None] | None = None,
        artifact_rewriter: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self._m = messages
        self._chat_id = chat_id
        self._session_uuid = session_uuid
        self._conversation_uuid = str(conversation_uuid or "").strip()
        self._protocol = str(protocol or "").strip().lower()
        self._model = str(model or "").strip()
        self._model_label = str(model_label or "").strip()
        self._turn_uuid = str(turn_uuid or "").strip()
        self._parent_turn_uuid = str(parent_turn_uuid or "").strip()
        self._run_root_turn_uuid = str(run_root_turn_uuid or turn_uuid or "").strip()
        self._task_uuid = str(task_uuid or "").strip()
        self._agent_session_uuid = str(agent_session_uuid or "").strip()
        self._message_writer = message_writer
        self._on_message_saved = on_message_saved
        self._artifact_rewriter = artifact_rewriter
        self.saved_assistant = False
        self.saved_message_ids: list[int] = []
        self._pending_tool_assistant: dict[str, Any] | None = None

    def _ownership_kwargs(self) -> dict[str, str]:
        return {
            "conversation_uuid": self._conversation_uuid,
            "turn_uuid": self._turn_uuid,
            "parent_turn_uuid": self._parent_turn_uuid,
            "run_root_turn_uuid": self._run_root_turn_uuid or self._turn_uuid,
            "task_uuid": self._task_uuid,
            "agent_session_uuid": self._agent_session_uuid,
        }

    async def _notify_saved(self, message_id: int, role: str, *, extra: dict[str, Any] | None = None) -> None:
        if not message_id:
            return
        self.saved_message_ids.append(int(message_id))
        if self._on_message_saved is None:
            return
        payload = {
            "messageId": int(message_id),
            "role": role,
            "chatId": self._chat_id,
            **self._ownership_kwargs(),
            **(extra or {}),
        }
        maybe = self._on_message_saved(int(message_id), role, payload)
        if inspect.isawaitable(maybe):
            await maybe

    async def _store_message(
        self,
        role: str,
        content: str,
        *,
        extra: dict[str, Any] | None = None,
        **message_kwargs: Any,
    ) -> int:
        binding = dict(extra or {})
        if self._message_writer is not None:
            message_id = await self._message_writer(role, content, dict(message_kwargs), binding)
        else:
            message_id = await self._m.add(
                self._chat_id,
                role,
                content,
                **message_kwargs,
                **self._ownership_kwargs(),
            )
        await self._notify_saved(message_id, role, extra=binding)
        return int(message_id or 0)

    async def _rewrite_assistant_content(self, content: str) -> str:
        if not content or self._artifact_rewriter is None:
            return content
        return await self._artifact_rewriter(content)

    async def _flush_pending_tool_assistant(self) -> None:
        pending = self._pending_tool_assistant
        if not pending:
            return
        self._pending_tool_assistant = None
        content = await self._rewrite_assistant_content(str(pending.get("content") or ""))
        tool_calls = pending.get("tool_calls") or []
        tool_call_ids = [str(getattr(item, "id", "") or "") for item in tool_calls]
        tool_call_ids = [item for item in tool_call_ids if item]
        await self._store_message(
            "assistant", content,
            reasoning=str(pending.get("reasoning") or ""),
            signature=str(pending.get("signature") or ""),
            tool_calls=tool_calls or None,
            tokens=estimate_tokens(content) if content else 0,
            extra={"hasToolCalls": True, "toolCallIds": tool_call_ids},
        )
        self.saved_assistant = True

    async def save_assistant(self, *, content: str, reasoning: str, signature: str,
                             tool_calls: list[Any],
                             native_output_items: list[dict[str, Any]] | None = None) -> None:
        # Opaque items must never enter messages or any Web binding metadata.
        del native_output_items
        if not content and not reasoning and not tool_calls:
            return
        content = await self._rewrite_assistant_content(content)
        if tool_calls:
            # Do not persist a bare assistant tool_call before the long-running
            # Web tool result exists.  During refresh, the browser already has
            # live tool progress; an early DB row makes the same Agent
            # render twice (persisted "running" card + live progress card).
            # Flush it immediately before the first tool result so the stored
            # transcript still remains a valid assistant/tool pair.
            self._pending_tool_assistant = {
                "content": content,
                "reasoning": reasoning,
                "signature": signature,
                "tool_calls": tool_calls,
            }
            return
        await self._flush_pending_tool_assistant()
        await self._store_message(
            "assistant", content,
            reasoning=reasoning, signature=signature,
            tool_calls=None,
            tokens=estimate_tokens(content) if content else 0,
        )
        self.saved_assistant = True

    async def save_tool_result(self, *, tool_call_id: str, name: str, content: str,
                               duration_ms: int = 0) -> None:
        await self._flush_pending_tool_assistant()
        await self._store_message(
            "tool", content,
            tool_call_id=tool_call_id, name=name,
            tokens=estimate_tokens(content),
            extra={"toolCallId": tool_call_id, "toolName": name, "durationMs": duration_ms},
        )
        await self._m.add_tool_call(
            self._chat_id,
            session_uuid=self._session_uuid,
            tool_name=name,
            status="error" if (str(content or "").startswith("[错误]") or str(content or "").startswith("error:")) else "ok",
            duration_ms=duration_ms,
            result_size=len((content or "").encode("utf-8")),
            error_type=str(content or "").split("\n", 1)[0][:120]
            if (str(content or "").startswith("[错误]") or str(content or "").startswith("error:")) else "",
        )

    async def save_user(self, *, content: str, metadata: dict[str, Any] | None = None) -> None:
        await self._store_message(
            "user",
            content,
            tokens=estimate_tokens(content),
            extra=dict(metadata or {}),
        )
        await self._m.bump_user_turn(self._chat_id)

    async def save_native_context(self, *, messages: list[Message]) -> None:
        """Persist the complete private model context outside transcript rows."""
        # Trusted Task Memory runtime messages intentionally live only here. Unknown
        # neutral metadata is JSON-safe and provider serializers ignore it; public
        # message rows and summaries remain clean without delimiter scanning.
        payloads = serialize_messages(messages)
        if not validate_model_context(payloads):
            # Do not repair or partially edit opaque state. A later request safely
            # falls back to the readable DB transcript and starts a fresh chain.
            await self._m.clear_controller_model_context(self._chat_id)
            return
        await self._m.save_controller_model_context(
            self._chat_id,
            conversation_uuid=self._conversation_uuid,
            session_id=self._session_uuid,
            protocol=self._protocol,
            model=self._model,
            model_label=self._model_label,
            state={"version": 1, "messages": payloads},
        )


def _merge_runtime_convo_tail(rebuilt: list, current_convo: list | None, kept_message_count: int) -> list:
    """Preserve rich visible tail while opening a clean TaskMemory cache epoch.

    DB history is authoritative for the new summary prefix. Attachments and other
    rich visible payloads may be restored from the in-memory tail, but trusted
    TaskMemory state belongs to the old cache epoch and is excluded before counts
    are aligned. Provider-native items are also stripped because they were produced
    against the old prefix.
    """
    clean_rebuilt = without_task_memory_runtime_messages(list(rebuilt or []))
    clean_current = without_task_memory_runtime_messages(list(current_convo or []))
    if not clean_rebuilt or not clean_current or kept_message_count <= 0:
        return clean_rebuilt
    if len(clean_current) < kept_message_count or len(clean_rebuilt) < kept_message_count:
        return clean_rebuilt
    runtime_tail: list[Any] = []
    for message in clean_current[-kept_message_count:]:
        if not isinstance(message, dict):
            runtime_tail.append(message)
            continue
        item = dict(message)
        item.pop("native_output_items", None)
        runtime_tail.append(item)
    merged = list(clean_rebuilt[:-kept_message_count]) + runtime_tail
    return repair_tool_pairing(merged)


class _WebContextCompactionGate:
    """Web normal compaction bridge for Agent.run safe-boundary gates."""

    def __init__(
        self,
        owner: Any,
        chat_id: int,
        *,
        model_label: str = "",
        system: str = "",
        renderer: Any = None,
        on_compacted: Callable[[Any, int], Awaitable[None]] | None = None,
        request_refresher: Callable[[list], Awaitable[list]] | None = None,
        on_cache_epoch_reset: Callable[[], Any] | None = None,
        should_defer: Callable[[int], bool] | None = None,
    ) -> None:
        self._owner = owner
        self._chat_id = chat_id
        self._model_label = model_label
        self._system = system
        self._renderer = renderer
        self._on_compacted = on_compacted
        self._request_refresher = request_refresher
        self._on_cache_epoch_reset = on_cache_epoch_reset
        self._should_defer = should_defer

    async def maybe_compact_and_rebuild(self, *, source: str, prompt_tokens: int | None = None, convo: list | None = None):
        tokens = max(0, int(prompt_tokens or 0))
        estimate_convo = convo
        if convo is not None and self._request_refresher is not None:
            estimate_convo = await self._request_refresher(list(convo))
        if estimate_convo is not None:
            try:
                # Explicit Agent output usage catches tokenizer under-counting;
                # the assembled outbound estimate includes the latest runtime-only
                # catalog while canonical ``convo`` remains unmodified.
                tokens = max(tokens, int(self._owner._estimate_prompt_tokens(system=self._system, convo=estimate_convo)))
            except Exception:
                pass
        # A pending memory checkpoint may defer one normal compaction long enough
        # for the next safe provider request to preserve state. The callback sees
        # the full gate estimate; emergency overflow compaction is never deferred.
        if self._should_defer is not None:
            try:
                if self._should_defer(tokens):
                    return None
            except Exception:
                log.exception("检查压缩前记忆提醒状态失败，继续正常压缩", 会话=self._chat_id)
        try:
            compactor = self._owner._make_web_compactor(self._chat_id, model_label=self._model_label)
            outcome = await compactor.maybe_compact_detail(self._chat_id, prompt_tokens=tokens, source=source)
        except CompactionAccountingError:
            raise
        except Exception:
            log.exception("Web 对话同步压缩 gate 异常", 会话=self._chat_id, 来源=source)
            return None
        if not outcome.did:
            return None
        if self._on_cache_epoch_reset is not None:
            maybe_reset = self._on_cache_epoch_reset()
            if inspect.isawaitable(maybe_reset):
                await maybe_reset
        clear_read_file_state(chat_id=self._chat_id)
        rebuilt = await self._owner._build_history(self._chat_id)
        rebuilt = _merge_runtime_convo_tail(rebuilt, convo, int(outcome.kept_message_count or 0))
        after_tokens = 0
        refreshed_rebuilt = (
            await self._request_refresher(list(rebuilt))
            if self._request_refresher is not None else rebuilt
        )
        try:
            after_tokens = int(self._owner._estimate_prompt_tokens(system=self._system, convo=refreshed_rebuilt))
        except Exception:
            after_tokens = 0
        outcome.after_tokens = after_tokens or int(getattr(outcome, "after_tokens", 0) or 0)
        if self._renderer is not None:
            await self._owner._emit_context_compaction_event(self._renderer, outcome, source=source)
        if self._on_compacted is not None:
            await self._on_compacted(outcome, after_tokens)
        return rebuilt


class _WebEmergencyCompactor:
    def __init__(
        self,
        owner: Any,
        chat_id: int,
        *,
        model_label: str = "",
        renderer: Any = None,
        system: str = "",
        on_compacted: Callable[[Any, int], Awaitable[None]] | None = None,
        request_refresher: Callable[[list], Awaitable[list]] | None = None,
        on_cache_epoch_reset: Callable[[], Any] | None = None,
    ) -> None:
        self._owner = owner
        self._chat_id = chat_id
        self._model_label = model_label
        self._renderer = renderer
        self._system = system
        self._on_compacted = on_compacted
        self._request_refresher = request_refresher
        self._on_cache_epoch_reset = on_cache_epoch_reset

    async def compact_and_rebuild(self, convo: list | None = None):
        try:
            compactor = self._owner._make_web_compactor(self._chat_id, model_label=self._model_label)
            outcome = await compactor.force_compact_detail(self._chat_id, source="emergency")
        except CompactionAccountingError:
            raise
        except Exception:
            log.exception("Web 对话应急压缩异常", 会话=self._chat_id)
            return None
        if not outcome.did:
            return None
        if self._on_cache_epoch_reset is not None:
            maybe_reset = self._on_cache_epoch_reset()
            if inspect.isawaitable(maybe_reset):
                await maybe_reset
        clear_read_file_state(chat_id=self._chat_id)
        rebuilt = await self._owner._build_history(self._chat_id)
        rebuilt = _merge_runtime_convo_tail(rebuilt, convo, int(outcome.kept_message_count or 0))
        after_tokens = 0
        refreshed_rebuilt = (
            await self._request_refresher(list(rebuilt))
            if self._request_refresher is not None else rebuilt
        )
        try:
            after_tokens = int(self._owner._estimate_prompt_tokens(system=self._system, convo=refreshed_rebuilt))
        except Exception:
            after_tokens = 0
        outcome.after_tokens = after_tokens or int(getattr(outcome, "after_tokens", 0) or 0)
        if self._renderer is not None:
            await self._owner._emit_context_compaction_event(self._renderer, outcome, source="emergency")
        if self._on_compacted is not None:
            await self._on_compacted(outcome, after_tokens)
        return rebuilt

__all__ = [name for name in globals() if not name.startswith("__")]
