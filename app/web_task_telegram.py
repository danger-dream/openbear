"""Restart-safe Telegram notifications for long-running Web Agent turns."""
from __future__ import annotations

import asyncio
import contextlib
import html
import json
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from app.config import Config, WebTaskNotificationsConfig
from app.db.engine import DB
from app.logging import get_logger
from app.telegram_ui import edit_rich, send_rich

log = get_logger("web_task_telegram")

_TERMINAL_STATUSES = {"completed", "failed", "interrupted", "short"}
_AGENT_TERMINAL = {"completed", "failed", "cancelled", "interrupted", "needs_openbear_control", "partial"}
_RESULT_STEP_CHARS = 1200
_RESULT_EDIT_INTERVAL_S = 0.8
_MAX_DELIVERY_ATTEMPTS = 5


def _now() -> int:
    return int(time.time())


def _json_loads(value: Any, default: Any) -> Any:
    try:
        parsed = json.loads(str(value or ""))
    except Exception:
        return default
    return parsed


def _safe_text(value: Any, limit: int = 160) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _duration(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}小时{minutes:02d}分{secs:02d}秒"
    if minutes:
        return f"{minutes}分{secs:02d}秒"
    return f"{secs}秒"


def _safe_link(url: str) -> str:
    value = str(url or "").strip()
    parsed = urlparse(value)
    return value if parsed.scheme.lower() in {"http", "https"} and parsed.netloc else ""


def _inline_markdown(value: str) -> str:
    """Convert a conservative Markdown subset after escaping all model text."""
    source = str(value or "")
    codes: list[str] = []

    def stash_code(match: re.Match[str]) -> str:
        codes.append(f"<code>{html.escape(match.group(1), quote=False)}</code>")
        return f"\x00CODE{len(codes) - 1}\x00"

    source = re.sub(r"`([^`\n]+)`", stash_code, source)
    escaped = html.escape(source, quote=False)

    def link(match: re.Match[str]) -> str:
        label = match.group(1)
        url = html.unescape(match.group(2))
        safe = _safe_link(url)
        return f'<a href="{html.escape(safe, quote=True)}">{label}</a>' if safe else label

    escaped = re.sub(r"\[([^\]\n]+)\]\(([^)\s]+)\)", link, escaped)
    escaped = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"__([^_\n]+)__", r"<b>\1</b>", escaped)
    escaped = re.sub(r"~~([^~\n]+)~~", r"<s>\1</s>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", escaped)
    for index, code in enumerate(codes):
        escaped = escaped.replace(f"\x00CODE{index}\x00", code)
    return escaped


def markdown_to_telegram_blocks(markdown: str) -> list[str]:
    """Return individually valid Telegram HTML blocks.

    Raw HTML is always escaped. Fenced code and table-like rows are rendered as
    preformatted text; all other formatting uses the Telegram-safe subset.
    """
    lines = str(markdown or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[str] = []
    paragraph: list[str] = []
    i = 0

    def flush_paragraph() -> None:
        if not paragraph:
            return
        text = "\n".join(paragraph).strip()
        paragraph.clear()
        if not text:
            return
        # Keep each block small enough for progressive Rich Message edits.
        while len(text) > 1800:
            cut = text.rfind("。", 0, 1800)
            if cut < 600:
                cut = text.rfind("\n", 0, 1800)
            if cut < 600:
                cut = 1800
            else:
                cut += 1
            blocks.append(_inline_markdown(text[:cut]))
            text = text[cut:].lstrip()
        if text:
            blocks.append(_inline_markdown(text))

    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("```"):
            flush_paragraph()
            language = line.lstrip()[3:].strip()
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            code = "\n".join(code_lines) or " "
            for start in range(0, len(code), 5000):
                piece = html.escape(code[start:start + 5000], quote=False)
                cls = f' class="language-{html.escape(language, quote=True)}"' if language and start == 0 else ""
                blocks.append(f"<pre><code{cls}>{piece}</code></pre>")
            i += 1
            continue
        if not line.strip():
            flush_paragraph()
            i += 1
            continue
        if line.startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i + 1]):
            flush_paragraph()
            table = [line, lines[i + 1]]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                table.append(lines[i])
                i += 1
            table_text = "\n".join(table)
            for start in range(0, len(table_text), 5000):
                blocks.append(f"<pre>{html.escape(table_text[start:start + 5000], quote=False)}</pre>")
            continue
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.+)$", line)
        if heading:
            flush_paragraph()
            blocks.append(f"<b>{_inline_markdown(heading.group(1))}</b>")
            i += 1
            continue
        if line.lstrip().startswith(">"):
            flush_paragraph()
            quote_lines: list[str] = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                quote_lines.append(lines[i].lstrip()[1:].lstrip())
                i += 1
            blocks.append(f"<blockquote>{_inline_markdown(chr(10).join(quote_lines))}</blockquote>")
            continue
        item = re.match(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(.+)$", line)
        if item:
            flush_paragraph()
            marker = "•" if not re.match(r"^\s*\d+", line) else re.match(r"^\s*(\d+)", line).group(1) + "."  # type: ignore[union-attr]
            blocks.append(f"{marker} {_inline_markdown(item.group(1))}")
            i += 1
            continue
        paragraph.append(line)
        i += 1
    flush_paragraph()
    return blocks or ["…"]


def _rich_pages(blocks: list[str], limit: int = 28_000) -> list[list[str]]:
    pages: list[list[str]] = []
    current: list[str] = []
    size = 0
    for block in blocks:
        extra = len(block) + (2 if current else 0)
        if current and size + extra > limit:
            pages.append(current)
            current, size = [], 0
        current.append(block)
        size += len(block) + (2 if len(current) > 1 else 0)
    if current:
        pages.append(current)
    return pages or [["…"]]


@dataclass(slots=True)
class Delivery:
    id: int
    root_turn_uuid: str
    event_type: str
    payload: dict[str, Any]
    attempts: int
    message_ids: tuple[int, ...]


class WebTaskTelegramNotifier:
    def __init__(self, config: Config, db: DB, bot: Bot) -> None:
        self.config = config
        self.db = db
        self.bot = bot
        self._worker: asyncio.Task[Any] | None = None
        self._wake = asyncio.Event()

    def apply_config(self, config: Config) -> None:
        self.config = config
        self._wake.set()

    async def start(self) -> None:
        now = _now()
        await self.db.conn.execute(
            "UPDATE web_tg_notification_outbox SET state='pending', updated_at=? WHERE state='processing'",
            (now,),
        )
        await self.db.conn.commit()
        cur = await self.db.conn.execute(
            "SELECT root_turn_uuid FROM web_tg_notification_runs WHERE status='running' ORDER BY started_at"
        )
        stale = [str(row["root_turn_uuid"] or "") for row in await cur.fetchall()]
        for root_turn_uuid in stale:
            if root_turn_uuid:
                await self.db.conn.execute(
                    "UPDATE web_tg_notification_outbox SET state='cancelled', updated_at=? WHERE root_turn_uuid=? AND state IN ('pending','processing')",
                    (now, root_turn_uuid),
                )
                await self.db.conn.commit()
                await self.finish(root_turn_uuid, "interrupted")
        self._worker = asyncio.create_task(self._worker_loop(), name="web-tg-notification-worker")
        self._wake.set()

    async def prune(self, *, keep_days: int = 90) -> tuple[int, int]:
        cutoff = _now() - max(7, int(keep_days)) * 86_400
        outbox_result = await self.db.conn.execute(
            """
            DELETE FROM web_tg_notification_outbox
            WHERE state IN ('sent','cancelled','failed') AND updated_at<?
            """,
            (cutoff,),
        )
        runs_result = await self.db.conn.execute(
            """
            DELETE FROM web_tg_notification_runs
            WHERE status IN ('completed','failed','interrupted','short')
              AND updated_at<?
              AND NOT EXISTS (
                SELECT 1 FROM web_tg_notification_outbox AS outbox
                WHERE outbox.root_turn_uuid=web_tg_notification_runs.root_turn_uuid
              )
            """,
            (cutoff,),
        )
        await self.db.conn.commit()
        return int(runs_result.rowcount or 0), int(outbox_result.rowcount or 0)

    async def send_test(self, owner_chat_id: int) -> int:
        owner = int(owner_chat_id or 0)
        whitelist = {int(value) for value in self.config.telegram.whitelist_ids}
        if owner <= 0 or owner not in whitelist:
            raise ValueError("owner_not_whitelisted")
        cfg = self.config.web.task_notifications
        events = "、".join(cfg.events) if cfg.events else "未选择事件"
        message = await send_rich(
            self.bot,
            owner,
            "<b>✅ OpenBear 长任务通知测试成功</b>\n\n"
            f"时长阈值：{int(cfg.threshold_minutes)} 分钟\n"
            f"发送最终回答：{'开启' if cfg.include_result else '关闭'}\n"
            f"通知事件：{html.escape(events)}",
        )
        return int(message.message_id)

    async def stop(self) -> None:
        task = self._worker
        self._worker = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def register(self, event: dict[str, Any], *, owner_chat_id: int, internal_chat_id: int, title: str, model: str) -> None:
        cfg = self.config.web.task_notifications
        if not cfg.enabled or event.get("taskNotificationSilent") or event.get("hidden") or event.get("internal"):
            return
        root = str(event.get("runUuid") or event.get("turnUuid") or "").strip()
        owner = int(owner_chat_id or 0)
        whitelist = {int(value) for value in self.config.telegram.whitelist_ids}
        if not root or owner <= 0 or owner not in whitelist:
            return
        now = _now()
        event_ts_ms = int(event.get("ts") or 0)
        started_at = event_ts_ms // 1000 if event_ts_ms > 0 else now
        threshold_at = started_at + int(cfg.threshold_minutes) * 60
        snapshot = cfg.model_dump(by_alias=True)
        await self.db.conn.execute(
            """
            INSERT OR IGNORE INTO web_tg_notification_runs (
              root_turn_uuid, conversation_uuid, owner_chat_id, internal_chat_id,
              title, model, status, config_json, result_text, started_at,
              threshold_at, completed_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, '', ?, ?, 0, ?, ?)
            """,
            (
                root,
                str(event.get("conversationUuid") or ""),
                owner,
                int(internal_chat_id),
                _safe_text(title, 200),
                _safe_text(model, 120),
                json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
                started_at,
                threshold_at,
                now,
                now,
            ),
        )
        await self.db.conn.commit()
        if "task_started" in cfg.events:
            await self._queue_event(root, "task_started", "task_started", {}, deliver_after=threshold_at)

    async def observe(self, event: dict[str, Any], *, owner_chat_id: int, internal_chat_id: int) -> None:
        kind = str(event.get("type") or "").strip()
        if kind == "accepted":
            if (
                not self.config.web.task_notifications.enabled
                or event.get("taskNotificationSilent")
                or event.get("hidden")
                or event.get("internal")
            ):
                return
            cur = await self.db.conn.execute(
                "SELECT title, model FROM web_conversations WHERE conversation_uuid=? LIMIT 1",
                (str(event.get("conversationUuid") or ""),),
            )
            row = await cur.fetchone()
            await self.register(
                event,
                owner_chat_id=owner_chat_id,
                internal_chat_id=internal_chat_id,
                title=str(row["title"] or "新对话") if row else "新对话",
                model=str(row["model"] or "") if row else "",
            )
            return
        root = str(event.get("runUuid") or event.get("rootTurnUuid") or event.get("turnUuid") or "").strip()
        if not root:
            return
        run = await self._run(root)
        if not run or str(run.get("status") or "") != "running":
            return
        if kind == "final" and not event.get("internal") and not event.get("hidden"):
            text = str(event.get("text") or "")
            if text:
                await self.db.conn.execute(
                    "UPDATE web_tg_notification_runs SET result_text=?, updated_at=? WHERE root_turn_uuid=? AND status='running'",
                    (text, _now(), root),
                )
                await self.db.conn.commit()
            return
        if kind == "retry_wait":
            retry = event.get("retry") if isinstance(event.get("retry"), dict) else {}
            if retry.get("active"):
                attempt = int(retry.get("attempt") or retry.get("retry") or retry.get("retryCount") or 0)
                await self._queue_selected(
                    run,
                    "retrying",
                    f"retrying:{attempt or event.get('eventUuid')}",
                    {"attempt": attempt, "delaySeconds": float(retry.get("delaySeconds") or retry.get("delay") or 0)},
                )
            return
        if kind in {"tool_progress", "tool_result", "task_notification"}:
            nested = event.get("payload") if isinstance(event.get("payload"), dict) else event
            if kind == "tool_result" and str(event.get("name") or "") in {"Agent", "AgentMessage"}:
                parsed_result = _json_loads(event.get("result"), {})
                if isinstance(parsed_result, dict):
                    nested = parsed_result
            task = nested.get("task") if isinstance(nested.get("task"), dict) else {}
            tool_name = str(event.get("name") or nested.get("toolName") or "")
            task_uuid = str(task.get("taskUuid") or nested.get("taskUuid") or event.get("taskUuid") or "").strip()
            status = str(task.get("status") or nested.get("status") or event.get("status") or "").strip()
            if (tool_name in {"Agent", "AgentMessage"} or kind == "task_notification") and task_uuid:
                payload = {
                    "taskUuid": task_uuid,
                    "title": _safe_text(task.get("title") or task.get("displayName") or nested.get("title") or "", 120),
                    "status": status,
                }
                if status == "running":
                    await self._queue_selected(run, "agent_started", f"agent_started:{task_uuid}", payload)
                elif status in _AGENT_TERMINAL:
                    await self._queue_selected(run, "agent_finished", f"agent_finished:{task_uuid}:{status}", payload)
            return
        if kind == "error":
            await self.finish(root, "failed")
        elif kind == "stopped":
            await self.finish(root, "interrupted")
        elif kind == "done":
            await self.finish(root, "completed")

    async def finish(self, root_turn_uuid: str, status: str) -> None:
        run = await self._run(root_turn_uuid)
        if not run or str(run.get("status") or "") in _TERMINAL_STATUSES:
            return
        now = _now()
        threshold_at = int(run.get("threshold_at") or 0)
        config = self._run_config(run)
        if not config.enabled or now < threshold_at:
            await self.db.conn.execute(
                "UPDATE web_tg_notification_runs SET status='short', completed_at=?, updated_at=? WHERE root_turn_uuid=? AND status='running'",
                (now, now, root_turn_uuid),
            )
            await self.db.conn.execute(
                "UPDATE web_tg_notification_outbox SET state='cancelled', updated_at=? WHERE root_turn_uuid=? AND state IN ('pending','processing')",
                (now, root_turn_uuid),
            )
            await self.db.conn.commit()
            return
        normalized = status if status in {"completed", "failed", "interrupted"} else "failed"
        await self.db.conn.execute(
            "UPDATE web_tg_notification_runs SET status=?, completed_at=?, updated_at=? WHERE root_turn_uuid=? AND status='running'",
            (normalized, now, now, root_turn_uuid),
        )
        await self.db.conn.commit()
        event_type = {"completed": "task_completed", "failed": "task_failed", "interrupted": "task_interrupted"}[normalized]
        queued_terminal = False
        if event_type in config.events:
            await self._queue_event(root_turn_uuid, event_type, event_type, {}, deliver_after=now)
            queued_terminal = True
        result = str(run.get("result_text") or "")
        if normalized == "completed" and config.include_result and result:
            if not queued_terminal:
                await self._queue_event(root_turn_uuid, "result_ready", "result_ready", {}, deliver_after=now)
            await self._queue_event(root_turn_uuid, "result", "result", {"text": result}, deliver_after=now)
        self._wake.set()

    async def _run(self, root_turn_uuid: str) -> dict[str, Any] | None:
        cur = await self.db.conn.execute(
            "SELECT * FROM web_tg_notification_runs WHERE root_turn_uuid=? LIMIT 1",
            (root_turn_uuid,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    @staticmethod
    def _run_config(run: dict[str, Any]) -> WebTaskNotificationsConfig:
        raw = _json_loads(run.get("config_json"), {})
        try:
            return WebTaskNotificationsConfig.model_validate(raw)
        except Exception:
            return WebTaskNotificationsConfig(enabled=False, include_result=False, events=[])

    async def _queue_selected(self, run: dict[str, Any], event_type: str, key: str, payload: dict[str, Any]) -> None:
        cfg = self._run_config(run)
        if not cfg.enabled or event_type not in cfg.events:
            return
        now = _now()
        await self._queue_event(
            str(run.get("root_turn_uuid") or ""),
            event_type,
            key,
            payload,
            deliver_after=max(now, int(run.get("threshold_at") or now)),
        )

    async def _queue_event(self, root: str, event_type: str, key: str, payload: dict[str, Any], *, deliver_after: int) -> None:
        if not root:
            return
        now = _now()
        await self.db.conn.execute(
            """
            INSERT OR IGNORE INTO web_tg_notification_outbox (
              delivery_key, root_turn_uuid, event_type, payload_json, state,
              deliver_after, attempts, telegram_message_ids_json, last_error,
              created_at, updated_at, delivered_at
            ) VALUES (?, ?, ?, ?, 'pending', ?, 0, '[]', '', ?, ?, 0)
            """,
            (
                f"{root}:{key}",
                root,
                event_type,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                int(deliver_after),
                now,
                now,
            ),
        )
        await self.db.conn.commit()
        self._wake.set()

    async def _worker_loop(self) -> None:
        while True:
            try:
                delivery = await self._claim_due()
                if delivery is None:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=2.0)
                    except TimeoutError:
                        pass
                    continue
                await self._deliver(delivery)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Web 长任务 Telegram 通知 worker 异常")
                await asyncio.sleep(1)

    async def _coalesce_due_buffered(self, now: int) -> None:
        cur = await self.db.conn.execute(
            """
            SELECT outbox.id, outbox.root_turn_uuid, outbox.event_type,
                   outbox.payload_json, outbox.created_at, runs.started_at
            FROM web_tg_notification_outbox AS outbox
            JOIN web_tg_notification_runs AS runs
              ON runs.root_turn_uuid=outbox.root_turn_uuid
            WHERE outbox.state='pending'
              AND outbox.deliver_after<=?
              AND outbox.created_at<runs.threshold_at
              AND outbox.event_type IN ('task_started','agent_started','agent_finished','retrying')
            ORDER BY outbox.root_turn_uuid, outbox.id
            """,
            (now,),
        )
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in await cur.fetchall():
            grouped.setdefault(str(row["root_turn_uuid"] or ""), []).append(dict(row))
        changed = False
        for rows in grouped.values():
            if len(rows) < 2:
                continue
            first, rest = rows[0], rows[1:]
            events = [
                {
                    "eventType": str(row.get("event_type") or ""),
                    "payload": _json_loads(row.get("payload_json"), {}),
                    "createdAt": int(row.get("created_at") or 0),
                    "elapsedSeconds": max(0, int(row.get("created_at") or 0) - int(row.get("started_at") or 0)),
                }
                for row in rows
            ]
            await self.db.conn.execute(
                "UPDATE web_tg_notification_outbox SET event_type='threshold_summary', payload_json=?, updated_at=? WHERE id=? AND state='pending'",
                (json.dumps({"events": events}, ensure_ascii=False, separators=(",", ":")), now, int(first["id"])),
            )
            placeholders = ",".join("?" for _ in rest)
            await self.db.conn.execute(
                f"UPDATE web_tg_notification_outbox SET state='cancelled', updated_at=? WHERE id IN ({placeholders}) AND state='pending'",
                (now, *(int(row["id"]) for row in rest)),
            )
            changed = True
        if changed:
            await self.db.conn.commit()

    async def _claim_due(self) -> Delivery | None:
        now = _now()
        await self._coalesce_due_buffered(now)
        cur = await self.db.conn.execute(
            """
            SELECT outbox.* FROM web_tg_notification_outbox AS outbox
            WHERE outbox.state='pending' AND outbox.deliver_after<=?
              AND NOT EXISTS (
                SELECT 1 FROM web_tg_notification_outbox AS earlier
                WHERE earlier.root_turn_uuid=outbox.root_turn_uuid
                  AND earlier.id<outbox.id
                  AND earlier.state IN ('pending','processing')
              )
            ORDER BY outbox.id LIMIT 1
            """,
            (now,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        delivery_id = int(row["id"])
        result = await self.db.conn.execute(
            "UPDATE web_tg_notification_outbox SET state='processing', attempts=attempts+1, updated_at=? WHERE id=? AND state='pending'",
            (now, delivery_id),
        )
        await self.db.conn.commit()
        if int(result.rowcount or 0) != 1:
            return None
        return Delivery(
            id=delivery_id,
            root_turn_uuid=str(row["root_turn_uuid"] or ""),
            event_type=str(row["event_type"] or ""),
            payload=_json_loads(row["payload_json"], {}),
            attempts=int(row["attempts"] or 0) + 1,
            message_ids=tuple(
                int(item)
                for item in _json_loads(row["telegram_message_ids_json"], [])
                if str(item).isdigit() and int(item) > 0
            ),
        )

    async def _deliver(self, delivery: Delivery) -> None:
        run = await self._run(delivery.root_turn_uuid)
        if not run:
            await self._mark(delivery, "cancelled", "run_not_found")
            return
        owner = int(run.get("owner_chat_id") or 0)
        whitelist = {int(value) for value in self.config.telegram.whitelist_ids}
        if owner <= 0 or owner not in whitelist:
            await self._mark(delivery, "failed", "owner_not_whitelisted")
            return
        try:
            if delivery.event_type == "result":
                message_ids = await self._stream_result(delivery, owner, str(delivery.payload.get("text") or ""))
            else:
                message = await send_rich(self.bot, owner, await self._event_html(run, delivery))
                message_ids = [int(message.message_id)] if getattr(message, "message_id", None) else []
        except asyncio.CancelledError:
            raise
        except TelegramRetryAfter as exc:
            await self._retry(delivery, f"rate_limited:{int(exc.retry_after)}", delay=max(1, int(exc.retry_after)))
            return
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            await self._mark(delivery, "failed", type(exc).__name__)
            return
        except Exception as exc:
            if delivery.attempts >= _MAX_DELIVERY_ATTEMPTS:
                await self._mark(delivery, "failed", type(exc).__name__)
            else:
                await self._retry(delivery, type(exc).__name__, delay=min(300, 2 ** delivery.attempts))
            return
        now = _now()
        await self.db.conn.execute(
            "UPDATE web_tg_notification_outbox SET state='sent', telegram_message_ids_json=?, last_error='', delivered_at=?, updated_at=? WHERE id=? AND state='processing'",
            (json.dumps(message_ids), now, now, delivery.id),
        )
        await self.db.conn.commit()

    async def _retry(self, delivery: Delivery, error: str, *, delay: int) -> None:
        now = _now()
        await self.db.conn.execute(
            "UPDATE web_tg_notification_outbox SET state='pending', deliver_after=?, last_error=?, updated_at=? WHERE id=? AND state='processing'",
            (now + max(1, int(delay)), _safe_text(error), now, delivery.id),
        )
        await self.db.conn.commit()
        self._wake.set()

    async def _mark(self, delivery: Delivery, state: str, error: str) -> None:
        await self.db.conn.execute(
            "UPDATE web_tg_notification_outbox SET state=?, last_error=?, updated_at=? WHERE id=? AND state='processing'",
            (state, _safe_text(error), _now(), delivery.id),
        )
        await self.db.conn.commit()

    async def _event_html(self, run: dict[str, Any], delivery: Delivery) -> str:
        event = delivery.event_type
        labels = {
            "task_started": ("⏳", "OpenBear 长任务仍在运行"),
            "threshold_summary": ("⏳", "OpenBear 长任务仍在运行"),
            "agent_started": ("🚀", "Agent 已启动"),
            "agent_finished": ("✅", "Agent 执行结束"),
            "retrying": ("⚠️", "模型调用失败，正在重试"),
            "task_completed": ("✅", "OpenBear 任务已完成"),
            "result_ready": ("✅", "OpenBear 任务已完成"),
            "task_failed": ("❌", "OpenBear 任务失败"),
            "task_interrupted": ("⏹", "OpenBear 任务已中断"),
        }
        icon, title = labels.get(event, ("ℹ️", "OpenBear 任务通知"))
        latest_title = str(run.get("title") or "新对话")
        latest_model = str(run.get("model") or "")
        with contextlib.suppress(Exception):
            cur = await self.db.conn.execute(
                "SELECT title, model FROM web_conversations WHERE conversation_uuid=? LIMIT 1",
                (str(run.get("conversation_uuid") or ""),),
            )
            row = await cur.fetchone()
            if row:
                latest_title = str(row["title"] or latest_title)
                latest_model = str(row["model"] or latest_model)
        elapsed = _duration((_now() if not run.get("completed_at") else int(run.get("completed_at") or 0)) - int(run.get("started_at") or 0))
        lines = [f"<b>{icon} {html.escape(title)}</b>", "", f"会话：{html.escape(_safe_text(latest_title, 120))}"]
        if latest_model:
            lines.append(f"模型：<code>{html.escape(_safe_text(latest_model, 120))}</code>")
        lines.append(f"耗时：{elapsed}")
        payload = delivery.payload
        if event.startswith("agent_"):
            agent_name = _safe_text(payload.get("title") or payload.get("taskUuid") or "Agent", 100)
            lines.append(f"Agent：{html.escape(agent_name)}")
            if payload.get("status"):
                lines.append(f"状态：<code>{html.escape(_safe_text(payload.get('status'), 40))}</code>")
        elif event == "retrying":
            if int(payload.get("attempt") or 0) > 0:
                lines.append(f"重试：第 {int(payload['attempt'])} 次")
            if float(payload.get("delaySeconds") or 0) > 0:
                lines.append(f"等待：{float(payload['delaySeconds']):g} 秒")
        elif event == "threshold_summary":
            event_labels = {
                "task_started": "任务启动",
                "agent_started": "Agent 启动",
                "agent_finished": "Agent 执行结束",
                "retrying": "模型出错重试",
            }
            lines.extend(["", "此前事件："])
            for item in payload.get("events") if isinstance(payload.get("events"), list) else []:
                item_payload = item.get("payload") if isinstance(item, dict) and isinstance(item.get("payload"), dict) else {}
                label = event_labels.get(str((item or {}).get("eventType") or ""), "任务更新")
                suffix = ""
                if item_payload.get("title") or item_payload.get("taskUuid"):
                    suffix = " · " + _safe_text(item_payload.get("title") or item_payload.get("taskUuid"), 80)
                elif int(item_payload.get("attempt") or 0) > 0:
                    suffix = f" · 第 {int(item_payload['attempt'])} 次"
                lines.append(f"• +{_duration(int((item or {}).get('elapsedSeconds') or 0))} {html.escape(label + suffix)}")
        if event == "result_ready":
            lines.extend(["", "最终回答将在下一条消息中显示。"])
        return "\n".join(lines)

    async def _checkpoint_message_ids(self, delivery_id: int, message_ids: list[int]) -> None:
        await self.db.conn.execute(
            "UPDATE web_tg_notification_outbox SET telegram_message_ids_json=?, updated_at=? WHERE id=? AND state='processing'",
            (json.dumps(message_ids, separators=(",", ":")), _now(), delivery_id),
        )
        await self.db.conn.commit()

    async def _stream_result(self, delivery: Delivery, owner: int, text: str) -> list[int]:
        blocks = markdown_to_telegram_blocks(text)
        message_ids = list(delivery.message_ids)
        for page_index, page in enumerate(_rich_pages(blocks), start=1):
            prefix = "<b>📄 最终回答</b>\n\n" if page_index == 1 else f"<b>📄 最终回答 · 第 {page_index} 页</b>\n\n"
            final_body = prefix + "\n\n".join(page)
            if page_index <= len(message_ids):
                await edit_rich(self.bot, owner, message_ids[page_index - 1], final_body)
                continue
            cumulative: list[str] = []
            message_id = 0
            last_size = 0
            for index, block in enumerate(page):
                cumulative.append(block)
                body = prefix + "\n\n".join(cumulative)
                is_last = index == len(page) - 1
                if not is_last and len(body) - last_size < _RESULT_STEP_CHARS:
                    continue
                if not message_id:
                    message = await send_rich(self.bot, owner, body)
                    message_id = int(message.message_id)
                    message_ids.append(message_id)
                    await self._checkpoint_message_ids(delivery.id, message_ids)
                else:
                    await asyncio.sleep(_RESULT_EDIT_INTERVAL_S)
                    await edit_rich(self.bot, owner, message_id, body)
                last_size = len(body)
            if not message_id:
                message = await send_rich(self.bot, owner, final_body)
                message_ids.append(int(message.message_id))
                await self._checkpoint_message_ids(delivery.id, message_ids)
        return message_ids
