"""Private window persistence and incremental execution history.

No public Web projection reads these tables. Accessors require an owner derived
from the running Controller/Agent, never an owner supplied by a model tool call.
Old provider checkpoints and summary IDs do not define window validity.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from app.agent.native_continuation import (
    deserialize_messages,
    serialize_messages,
    transcript_message_fingerprint,
    validate_model_context,
)
from app.context.window import CONTEXT_META, mark_source, neutral_context, source_of
from app.db.engine import DB, now_ts
from app.llm.base import Message
from app.logging import get_logger

log = get_logger("context.store")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


class StaleWindow(RuntimeError):
    def __init__(self, reason: str, **metadata: Any) -> None:
        super().__init__(reason)
        self.metadata = metadata


class ControllerMessagesAppended(StaleWindow):
    """Only a verified append may retry the context stage, never the Agent run."""


@dataclass
class ControllerBoundary:
    high_water: int
    prefix_count: int
    window_stamp: tuple[Any, ...]
    session: str | None
    rows: dict[int, str]
    phase: str


def merge_controller_additions(messages: list[Message], additions: list[Message]) -> list[Message]:
    """Backfill older originals without rewriting existing units or tool groups.

    Rows older than an already adopted user belong before that user's request,
    not after its cancellation/changed instructions. Truly new inputs still go
    after the live assistant/tool batch, even if they were persisted mid-batch.
    The caller validates the merged batch before advancing its coverage.
    """
    def row_id(message: Message) -> int:
        meta = source_of(message)
        value = int(meta.get("message_id") or 0)
        return value if meta.get("id") == f"message:{value}" else 0

    newest_user = max((row_id(m) for m in messages if m.get("role") == "user"), default=0)
    ordered = sorted(additions, key=row_id)
    backfill = [m for m in ordered if row_id(m) < newest_user]
    new_inputs = [m for m in ordered if row_id(m) >= newest_user]
    merged: list[Message] = []
    offset = 0
    for message in messages:
        # Never insert a user (or another assistant) inside a declared tool batch.
        if message.get("role") != "tool":
            anchor = row_id(message)
            while offset < len(backfill) and row_id(backfill[offset]) < anchor:
                merged.append(backfill[offset])
                offset += 1
        merged.append(message)
    return [*merged, *backfill[offset:], *new_inputs]


class ContextHistoryUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ContextOwner:
    kind: str
    key: str
    conversation_uuid: str = ""
    session_uuid: str = ""
    agent_session_uuid: str = ""
    task_uuid: str = ""
    chat_id: int = 0

    @classmethod
    def controller(cls, *, chat_id: int, session_uuid: str, conversation_uuid: str = "") -> ContextOwner:
        if not session_uuid:
            raise ValueError("controller_context_requires_session")
        return cls("controller", f"controller:{chat_id}:{session_uuid}", conversation_uuid, session_uuid, chat_id=chat_id)

    @classmethod
    def agent(cls, *, task_uuid: str, agent_session_uuid: str = "", conversation_uuid: str = "", session_uuid: str = "", chat_id: int = 0) -> ContextOwner:
        if not task_uuid:
            raise ValueError("agent_context_requires_task")
        key = f"agent:{agent_session_uuid}" if agent_session_uuid else f"legacy-task:{task_uuid}"
        return cls("agent", key, conversation_uuid, session_uuid, agent_session_uuid, task_uuid, chat_id)


@dataclass(frozen=True)
class RequestTicket:
    owner_key: str
    window_version: int
    route: str
    sequence: int
    request_id: str
    estimate_tokens: int = 0


class WindowStore:
    def __init__(self, db: DB, owner: ContextOwner) -> None:
        self.db = db
        self.owner = owner

    async def _ensure(self, conn: Any) -> None:
        owner, ts = self.owner, now_ts()
        await conn.execute(
            """INSERT OR IGNORE INTO context_windows
               (owner_key,owner_kind,conversation_uuid,session_uuid,agent_session_uuid,task_uuid,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (owner.key, owner.kind, owner.conversation_uuid, owner.session_uuid, owner.agent_session_uuid, owner.task_uuid, ts, ts),
        )

    async def load(self, *, fresh: bool = False) -> dict[str, Any] | None:
        if fresh:
            # A live cursor on the shared reader can pin an old WAL snapshot.
            # Context CAS inputs must come from the same authoritative writer.
            async with self.db.conn.transaction(label="context-state-read"):
                return await self.load()
        cur = await self.db.conn.execute("SELECT * FROM context_windows WHERE owner_key=?", (self.owner.key,))
        row = await cur.fetchone()
        if row is None:
            return None
        data = dict(row)
        data["state"] = json.loads(data.pop("state_json"))
        return data

    async def _window_stamp(self, conn: Any) -> tuple[Any, ...]:
        cur = await conn.execute(
            """SELECT revision,source_revision,source_high_water,window_version,route_fingerprint,
                      owner_kind,session_uuid,agent_session_uuid,task_uuid,request_sequence
               FROM context_windows WHERE owner_key=?""", (self.owner.key,))
        row = await cur.fetchone()
        return tuple(row) if row is not None else ()

    async def _controller_session(self, conn: Any) -> str | None:
        cur = await conn.execute("SELECT session_uuid FROM sessions WHERE chat_id=?", (self.owner.chat_id,))
        row = await cur.fetchone()
        return str(row[0] or "") if row is not None else None

    async def _row_fingerprints(self, conn: Any, ids: Any) -> dict[int, str]:
        # Only active source rows are read, never reload the lifetime transcript.
        # Historical edits use the existing lineage invalidation protocol. Counts
        # additionally catch prefix deletion, even outside the selected tail.
        ids = list(ids)
        fingerprints = {}
        for offset in range(0, len(ids), 400):
            chunk = ids[offset:offset + 400]
            cur = await conn.execute(
                f"SELECT * FROM messages WHERE chat_id=? AND id IN ({','.join('?' for _ in chunk)})",
                (self.owner.chat_id, *chunk))
            for row in await cur.fetchall():
                fingerprints[int(row["id"])] = _hash({key: row[key] for key in row.keys() if key not in {"tokens", "compacted"}})
        return fingerprints

    def _boundary_conflict(self, reason: str, boundary: ControllerBoundary, actual: int,
                           stamp: tuple[Any, ...], *, append: bool = False) -> StaleWindow:
        metadata = {"owner": self.owner.key, "phase": boundary.phase,
                    "expectedHighWater": boundary.high_water, "actualHighWater": actual,
                    "expectedRevision": boundary.window_stamp[0] if boundary.window_stamp else None,
                    "actualRevision": stamp[0] if stamp else None}
        log.warning("context.window_conflict", reason=reason, **metadata)
        return (ControllerMessagesAppended if append else StaleWindow)(reason, **metadata)

    async def _check_controller_boundary(self, conn: Any, boundary: ControllerBoundary, *, allow_append: bool = False) -> int:
        cur = await conn.execute("SELECT COALESCE(MAX(id),0) FROM messages WHERE chat_id=?", (self.owner.chat_id,))
        actual = int((await cur.fetchone())[0])
        stamp = await self._window_stamp(conn)
        if stamp != boundary.window_stamp or await self._controller_session(conn) != boundary.session:
            raise self._boundary_conflict("context_source_or_window_changed", boundary, actual, stamp)
        cur = await conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id=? AND id<=?", (self.owner.chat_id, boundary.high_water))
        if (int((await cur.fetchone())[0]) != boundary.prefix_count
                or await self._row_fingerprints(conn, boundary.rows) != boundary.rows):
            raise self._boundary_conflict("controller_history_changed", boundary, actual, stamp)
        if actual < boundary.high_water:
            raise self._boundary_conflict("controller_history_changed", boundary, actual, stamp)
        if actual > boundary.high_water and not allow_append:
            raise self._boundary_conflict("new_controller_message_arrived", boundary, actual, stamp, append=True)
        return actual

    async def controller_boundary(
        self, messages: list[Message], *, since: int | None, phase: str,
        expected_revision: int = 0, previous: ControllerBoundary | None = None,
        restored_anchor: dict[str, Any] | None = None,
    ) -> tuple[ControllerBoundary, list[Message]]:
        """Take/refresh one authoritative boundary, preserving the original guard.

        A refresh checks the OLD prefix and window before accepting any new IDs.
        Earlier execution may be restored only as a closed batch, never as user
        steering. Other-owner/concurrent execution stays fatal. Returned originals
        are already durable: merge them with merge_controller_additions, without
        replaying persistence or tool side effects.
        """
        from app.db.dao import MessageDAO
        from app.rath.controller_projection import project_history_message_for_controller

        if self.owner.kind != "controller":
            raise ValueError("controller_boundary_requires_controller")
        ids = {int(source_of(m).get("message_id") or 0) for m in messages
               if source_of(m).get("id") == f"message:{int(source_of(m).get('message_id') or 0)}"}
        async with self.db.conn.transaction(label="controller-context-boundary") as conn:
            if previous is not None:
                high_water = await self._check_controller_boundary(conn, previous, allow_append=True)
                floor = previous.high_water
                ids.update(previous.rows)
                stamp, session = previous.window_stamp, previous.session
            else:
                stamp = await self._window_stamp(conn)
                session = await self._controller_session(conn)
                cur = await conn.execute("SELECT COALESCE(MAX(id),0) FROM messages WHERE chat_id=?", (self.owner.chat_id,))
                high_water = int((await cur.fetchone())[0])
                provisional = ControllerBoundary(high_water, 0, (expected_revision,), session, {}, phase)
                if (expected_revision and (not stamp or stamp[0] != expected_revision)) or (
                    session and session != self.owner.session_uuid
                ):
                    raise self._boundary_conflict("context_source_or_window_changed", provisional, high_water, stamp)
                restored_floor = 0
                if restored_anchor is not None and since is None:
                    # The Web loader supplies this ONLY when it actually adopted
                    # an identity/anchor-validated private checkpoint. A legacy
                    # private projection need not map 1:1 to public message IDs.
                    expected_window = (restored_anchor["windowRevision"], restored_anchor["windowVersion"])
                    if expected_window != ((stamp[0], stamp[3]) if stamp else (0, 0)):
                        raise self._boundary_conflict("restored_controller_window_changed", provisional, high_water, stamp)
                    if not stamp:
                        # Recheck the exact legacy covered prefix on the writer;
                        # newer rows remain outside this proof and are reconciled
                        # below. Never derive consumption from the current MAX.
                        restored_floor = int(restored_anchor["sourceHighWater"])
                        cur = await conn.execute("SELECT * FROM messages WHERE chat_id=? AND id<=? ORDER BY id",
                                                 (self.owner.chat_id, restored_floor))
                        covered_rows = await cur.fetchall()
                        proof = [{"id": int(row["id"]), "fingerprint": transcript_message_fingerprint(MessageDAO._row(row).to_message())}
                                 for row in covered_rows]
                        if (proof != restored_anchor["messages"]
                                or max((item["id"] for item in proof), default=0) != restored_floor):
                            raise self._boundary_conflict("restored_controller_history_changed", provisional, high_water, stamp)
                        ids.update(item["id"] for item in proof)
                await self._ensure(conn)
                stamp = await self._window_stamp(conn)
                state = await self.load()
                floor = since if since is not None else max(restored_floor, int((state or {}).get("state", {}).get("sourceMessageHighWater") or 0))
                # Legacy summaries cover a prefix without individual row sources.
                if since is None:
                    floor = max(floor, max((int(source_of(m).get("message_id") or 0) for m in messages
                                            if source_of(m).get("kind") == "summary"), default=0))
            cur = await conn.execute("SELECT id FROM messages WHERE chat_id=? AND id>? ORDER BY id", (self.owner.chat_id, floor))
            missing = [int(row[0]) for row in await cur.fetchall() if int(row[0]) not in ids]
            # Only execution before an ALREADY adopted user can be historical.
            # A newly discovered user must not camouflage a concurrent writer.
            # Earlier does not mean discardable: retain its real evidence and
            # prove the complete merged protocol below before advancing coverage.
            newest_user_id = max((int(source_of(m).get("message_id") or 0) for m in messages
                                  if m.get("role") == "user"), default=0)
            additions = []
            for message_id in missing:
                cur = await conn.execute("SELECT * FROM messages WHERE chat_id=? AND id=?", (self.owner.chat_id, message_id))
                row = MessageDAO._row(await cur.fetchone())
                if row.task_uuid or row.agent_session_uuid:
                    guard = previous or ControllerBoundary(floor, 0, stamp, session, {}, phase)
                    raise self._boundary_conflict("controller_unrecognized_execution_append", guard, high_water, stamp)
                historical_execution = row.role in ("assistant", "tool") and row.id < newest_user_id
                if row.role == "user" or historical_execution:
                    original = row.to_message()
                    message = project_history_message_for_controller(original)
                    mark_source(message, kind="human" if row.role == "user" else "execution", source_id=f"message:{row.id}",
                                message_id=row.id, reference_only=message == original, turn_uuid=row.turn_uuid,
                                run_root_turn_uuid=row.run_root_turn_uuid or row.turn_uuid)
                    # Use the same durable bundle links as history restoration. Only
                    # IDs are bound here; the request overlay expands frozen content.
                    cur = await conn.execute(
                        """SELECT b.bundle_uuid FROM web_operation_messages l JOIN web_reference_bundles b
                           ON b.conversation_uuid=l.conversation_uuid AND b.op_id=l.op_id
                           WHERE l.message_id=? ORDER BY b.created_at,b.bundle_uuid""", (row.id,))
                    bundles = [str(item[0]) for item in await cur.fetchall()]
                    if bundles:
                        message["openbear_reference_bundle"] = bundles
                        message[CONTEXT_META]["reference_only"] = False
                    additions.append(message)
                else:
                    guard = previous or ControllerBoundary(floor, 0, stamp, session, {}, phase)
                    raise self._boundary_conflict("controller_unrecognized_execution_append", guard, high_water, stamp)
            if additions and not validate_model_context(merge_controller_additions(messages, additions)):
                guard = previous or ControllerBoundary(floor, 0, stamp, session, {}, phase)
                raise self._boundary_conflict("controller_recovery_requires_closed_tool_batch", guard, high_water, stamp)
            ids.update(missing)
            fingerprints = await self._row_fingerprints(conn, ids)
            if set(fingerprints) != ids:
                guard = previous or ControllerBoundary(floor, 0, stamp, session, {}, phase)
                raise self._boundary_conflict("controller_history_changed", guard, high_water, stamp)
            cur = await conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id=?", (self.owner.chat_id,))
            count = int((await cur.fetchone())[0])
            return ControllerBoundary(high_water, count, stamp, session, fingerprints, phase), additions

    async def archive(self, messages: list[Message], *, controller_boundary: ControllerBoundary | None = None) -> dict[str, int]:
        """Append previously unseen semantic units before any window can evict them.

        Agent payloads are private originals, not 1000-character audit previews.
        A source message ID can point to the Controller's existing transcript.
        Runtime snapshots are regenerated, not accumulated as user instructions.
        """
        payloads: list[tuple[str, str, str, str, int | None, str, str]] = []
        empty_tool_alternates: dict[str, str] = {}
        for message in messages:
            meta = source_of(message)
            if meta.get("kind") == "runtime":
                continue
            if not meta.get("id"):
                mark_source(message, kind=str(meta.get("kind") or "execution"))
                meta = source_of(message)
            item = serialize_messages(neutral_context([message]))[0]
            item.pop(CONTEXT_META, None)
            # A pure tool-call assistant has no text in either representation.
            # Legacy live checkpoints used null, SQLite rows used an empty string.
            # Compare only this exact alternate; never relax source identity/CAS
            # for changed text, tool arguments, media or non-tool messages.
            if item.get("role") == "assistant" and item.get("tool_calls") and item.get("content") in (None, ""):
                alternate = {**item, "content": "" if item.get("content") is None else None}
                empty_tool_alternates[str(meta["id"])] = _hash(alternate)
            message_id = int(meta.get("message_id") or 0) or None
            # Reference-only records are opt-in: rich current media or overlays
            # must not be replaced by their plaintext public transcript.
            body = {} if message_id and meta.get("reference_only") else item
            payloads.append((str(meta["id"]), str(meta.get("kind") or "execution"),
                             str(meta.get("task_uuid") or self.owner.task_uuid),
                             str(meta.get("turn_uuid") or ""), message_id, _json(body), _hash(item)))
        async with self.db.conn.transaction(label="context-history-append") as conn:
            if controller_boundary is not None:
                await self._check_controller_boundary(conn, controller_boundary)
            await self._ensure(conn)
            added = 0
            for event_id, kind, task_uuid, turn_uuid, message_id, body, fingerprint in payloads:
                cur = await conn.execute(
                    "SELECT fingerprint FROM context_execution_events WHERE owner_key=? AND event_id=?",
                    (self.owner.key, event_id),
                )
                row = await cur.fetchone()
                if row is not None:
                    if row["fingerprint"] not in (fingerprint, empty_tool_alternates.get(event_id)):
                        raise StaleWindow("source_event_changed; invalidate the edited lineage before continuing")
                    continue
                await conn.execute(
                    """INSERT INTO context_execution_events
                       (owner_key,event_id,kind,task_uuid,turn_uuid,message_id,payload_json,fingerprint,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (self.owner.key, event_id, kind, task_uuid, turn_uuid, message_id, body, fingerprint, now_ts()),
                )
                added += 1
            cur = await conn.execute("SELECT COALESCE(MAX(seq),0) AS high_water FROM context_execution_events WHERE owner_key=?", (self.owner.key,))
            high_water = int((await cur.fetchone())["high_water"])
            if added:
                await conn.execute(
                    "UPDATE context_windows SET source_revision=source_revision+?,source_high_water=?,updated_at=? WHERE owner_key=?",
                    (added, high_water, now_ts(), self.owner.key),
                )
            cur = await conn.execute("SELECT revision,source_revision FROM context_windows WHERE owner_key=?", (self.owner.key,))
            row = await cur.fetchone()
            result = {"revision": int(row["revision"]), "sourceRevision": int(row["source_revision"]), "highWater": high_water, "added": added}
            if controller_boundary is not None:
                stamp = await self._window_stamp(conn)
        if controller_boundary is not None:
            # Only OUR committed archive additions advance this expectation.
            controller_boundary.window_stamp = stamp
        return result

    async def save(
        self, messages: list[Message], *, expected_revision: int,
        expected_source_revision: int, route: str, rotated: bool = False,
        reason: str = "checkpoint", detail: dict[str, Any] | None = None,
        expected_message_high_water: int | None = None,
        controller_boundary: ControllerBoundary | None = None,
        source_message_high_water: int | None = None,
        extra_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # The independent fallback contains neutral, protocol-closed content.
        # Provider opaque items live solely in their existing private checkpoint.
        clean = neutral_context(messages)
        if not validate_model_context(clean):
            raise ValueError("context_window_has_incomplete_tool_batch")
        state = {**(extra_state or {}), "version": 1, "messages": serialize_messages(clean),
                 "sourceMessageHighWater": max((int(source_of(message).get("message_id") or 0) for message in clean), default=0)}
        async with self.db.conn.transaction(label="context-window-save") as conn:
            if controller_boundary is not None:
                await self._check_controller_boundary(conn, controller_boundary)
            await self._ensure(conn)
            cur = await conn.execute("SELECT * FROM context_windows WHERE owner_key=?", (self.owner.key,))
            old = dict(await cur.fetchone())
            if old["revision"] != expected_revision or old["source_revision"] != expected_source_revision:
                raise StaleWindow("context_source_or_window_changed")
            if expected_message_high_water is not None:
                cur = await conn.execute("SELECT COALESCE(MAX(id),0) AS high_water FROM messages WHERE chat_id=?", (self.owner.chat_id,))
                actual = int((await cur.fetchone())["high_water"])
                if actual != expected_message_high_water:
                    metadata = {"owner": self.owner.key, "phase": reason,
                                "expectedHighWater": expected_message_high_water, "actualHighWater": actual,
                                "expectedRevision": expected_revision, "actualRevision": old["revision"]}
                    log.warning("context.window_conflict", reason="new_controller_message_arrived", **metadata)
                    raise StaleWindow("new_controller_message_arrived", **metadata)
            for message in clean:
                meta = source_of(message)
                if meta.get("kind") == "runtime":
                    continue
                if not meta.get("id"):
                    raise ContextHistoryUnavailable("unarchived_context_message")
                cur = await conn.execute(
                    "SELECT 1 FROM context_execution_events WHERE owner_key=? AND event_id=?",
                    (self.owner.key, str(meta["id"])),
                )
                if await cur.fetchone() is None:
                    raise ContextHistoryUnavailable("context_source_not_in_owner_history")
            cur = await conn.execute(
                "SELECT COALESCE(MAX(message_id),0) AS high_water FROM context_execution_events WHERE owner_key=?",
                (self.owner.key,))
            archived_high_water = int((await cur.fetchone())["high_water"])
            old_state = json.loads(old["state_json"])
            # Coverage is independent from the selected/archived tail after a
            # suffix rollback. Copy may explicitly rebind that trusted boundary;
            # newly copied raw rows beyond it have NOT necessarily been consumed.
            # Within an existing lineage, saving a smaller selection cannot undo
            # previously committed coverage (rollback replaces the window first).
            covered = archived_high_water if source_message_high_water is None else int(source_message_high_water)
            if covered < 0:
                raise ValueError("invalid_source_message_high_water")
            state["sourceMessageHighWater"] = max(
                int(old_state.get("sourceMessageHighWater") or 0), covered,
            )
            for key in ("calibration", "strategy"):
                if key in old_state:
                    state.setdefault(key, old_state[key])
            changed_route = old["route_fingerprint"] != route
            version = int(old["window_version"]) + int(rotated or changed_route)
            invalidate = rotated or changed_route
            await conn.execute(
                """UPDATE context_windows SET revision=revision+1,window_version=?,route_fingerprint=?,state_json=?,
                   task_uuid=?,usage_known=CASE WHEN ? THEN 0 ELSE usage_known END,
                   usage_tokens=CASE WHEN ? THEN 0 ELSE usage_tokens END,updated_at=? WHERE owner_key=?""",
                (version, route, _json(state), self.owner.task_uuid, invalidate, invalidate, now_ts(), self.owner.key),
            )
            if rotated:
                if self.owner.kind == "controller":
                    await conn.execute("DELETE FROM controller_model_contexts WHERE chat_id=?", (self.owner.chat_id,))
                if self.owner.kind == "controller" and detail and detail.get("strategy") == "model_summary":
                    summary_cur = await conn.execute(
                        "INSERT INTO summaries(chat_id,summary,up_to_message_id,tokens,created_at) VALUES(?,?,?,?,?)",
                        (self.owner.chat_id, detail["summary"], int(detail.get("upToMessageId") or 0),
                         int(detail.get("summaryTokens") or 0), now_ts()),
                    )
                    detail["summaryId"] = int(summary_cur.lastrowid)
                    detail["summaryRef"] = f"/api/conversations/{self.owner.conversation_uuid}/compactions/{summary_cur.lastrowid}"
                await conn.execute(
                    "INSERT INTO context_window_rotations(rotation_id,owner_key,window_version,reason,detail_json,created_at) VALUES(?,?,?,?,?,?)",
                    (str(uuid.uuid4()), self.owner.key, version, reason, _json(detail or {}), now_ts()),
                )
        return {"windowVersion": version, "revision": expected_revision + 1, "sourceRevision": expected_source_revision, "usageInvalidated": invalidate}

    async def begin_request(self, *, route: str) -> RequestTicket:
        async with self.db.conn.transaction(label="context-request-begin") as conn:
            cur = await conn.execute("SELECT window_version,route_fingerprint,request_sequence,state_json FROM context_windows WHERE owner_key=?", (self.owner.key,))
            row = await cur.fetchone()
            if row is None or row["route_fingerprint"] != route:
                raise StaleWindow("request_context_route_changed")
            sequence = int(row["request_sequence"]) + 1
            await conn.execute("UPDATE context_windows SET request_sequence=? WHERE owner_key=?", (sequence, self.owner.key))
            state = json.loads(row["state_json"])
            estimate = int(state.get("rawEstimatedInputTokens") or 0) if not state.get("mediaTokensUnknown") else 0
            return RequestTicket(self.owner.key, int(row["window_version"]), route, sequence, str(uuid.uuid4()), estimate)

    async def observe_usage(self, ticket: RequestTicket, *, tokens: int | None) -> bool:
        if ticket.owner_key != self.owner.key:
            return False
        async with self.db.conn.transaction(label="context-request-usage") as conn:
            cur = await conn.execute(
                """UPDATE context_windows SET usage_known=?,usage_tokens=?,usage_request_id=?,usage_request_sequence=?,updated_at=?,
                   state_json=json_set(state_json,'$.calibration',json(?))
                   WHERE owner_key=? AND window_version=? AND route_fingerprint=?
                     AND usage_request_sequence<? AND request_sequence=?""",
                (tokens is not None, max(0, int(tokens or 0)), ticket.request_id, ticket.sequence, now_ts(),
                 _json({"tokens": tokens, "estimate": ticket.estimate_tokens, "route": ticket.route, "windowVersion": ticket.window_version}),
                 self.owner.key, ticket.window_version, ticket.route, ticket.sequence, ticket.sequence),
            )
            return cur.rowcount == 1

    async def restore_messages(self) -> list[Message] | None:
        saved = await self.load()
        if saved is None:
            return None
        if saved["state"].get("incompleteBatch"):
            raise ContextHistoryUnavailable("incomplete_tool_batch_requires_reliable_execution_checkpoint")
        payloads = saved["state"].get("messages")
        if not isinstance(payloads, list):
            return None
        messages = deserialize_messages(payloads)
        if not validate_model_context(messages):
            raise ContextHistoryUnavailable("invalid_saved_window")
        return messages

    async def event_payload(self, event_id: str) -> dict[str, Any]:
        cur = await self.db.conn.execute("SELECT * FROM context_execution_events WHERE owner_key=? AND event_id=?", (self.owner.key, event_id))
        row = await cur.fetchone()
        if row is None:
            raise ContextHistoryUnavailable("event_not_found_in_own_history")
        item = dict(row)
        payload = json.loads(item.pop("payload_json"))
        if not payload and item["message_id"]:
            # A deleted original stays deleted. Never resurrect it from a stale
            # public preview or from another conversation's identical text.
            from app.db.dao import MessageDAO
            cur = await self.db.conn.execute("SELECT * FROM messages WHERE id=? AND chat_id=?", (item["message_id"], self.owner.chat_id))
            source = await cur.fetchone()
            if source is None:
                raise ContextHistoryUnavailable("original_message_deleted")
            payload = serialize_messages(neutral_context([MessageDAO(self.db)._row(source).to_message()]))[0]
            if _hash(payload) != item["fingerprint"]:
                raise ContextHistoryUnavailable("original_message_changed")
        for key in (CONTEXT_META, "native_output_items", "reasoning", "signature"):
            payload.pop(key, None)
        item["payload"] = payload
        return item

    async def index(self, *, after: int = 0, high_water: int | None = None, limit: int = 30, query: str = "") -> dict[str, Any]:
        saved = await self.load()
        bound = int((saved or {}).get("source_high_water") or 0)
        if high_water is not None:
            if high_water < 0 or high_water > bound:
                raise StaleWindow("invalid_history_high_water")
            bound = high_water
        count = min(100, max(1, limit))
        # Index metadata is intentionally body-free. Search reads only this
        # owner's archived semantic payload, never private provider reasoning.
        where = "owner_key=? AND seq>? AND seq<=?"
        params: list[Any] = [self.owner.key, max(0, after), bound]
        if query:
            where += """ AND (instr(payload_json,?)>0 OR instr(kind,?)>0
                OR instr(COALESCE(json_extract(payload_json,'$.content'),''),?)>0
                OR EXISTS (SELECT 1 FROM json_each(payload_json,'$.tool_calls') call
                           WHERE instr(json_extract(call.value,'$.arguments'),?)>0)"""
            params += [query, query, query, query]
            if self.owner.kind == "controller":
                where += " OR EXISTS (SELECT 1 FROM messages m WHERE m.id=context_execution_events.message_id AND m.chat_id=? AND (instr(m.content,?)>0 OR instr(m.tool_calls_json,?)>0))"
                params += [self.owner.chat_id, query, query]
            where += ")"
        cur = await self.db.conn.execute(
            f"""SELECT seq,event_id,kind,task_uuid,turn_uuid,message_id,
                       json_extract(payload_json,'$.role') AS role,
                       COALESCE(json_extract(payload_json,'$.name'),json_extract(payload_json,'$.tool_calls[0].name'),'') AS toolName,
                       COALESCE(json_extract(payload_json,'$.tool_call_id'),json_extract(payload_json,'$.tool_calls[0].id'),'') AS toolCallId,
                       length(payload_json) AS stored_chars,created_at
                FROM context_execution_events WHERE {where} ORDER BY seq LIMIT ?""",
            (*params, count + 1),
        )
        rows = [dict(row) for row in await cur.fetchall()]
        more = len(rows) > count
        rows = rows[:count]
        return {"events": rows, "highWater": bound, "hasMore": more, "nextAfter": int(rows[-1]["seq"]) if rows and more else None}
