"""Explicit, single-conversation refresh of a frozen main-controller prompt."""
from __future__ import annotations

import difflib
import hashlib
import re

from aiohttp import web

from app.db.engine import now_ts
from app.web_console.core import _WEB_SESSION_KEY, WebSession


def system_prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(str(prompt or "").encode("utf-8")).hexdigest()


def _prompt_response(payload: dict, *, status: int = 200) -> web.Response:
    # A diff can contain private folder instructions or expanded memory.
    return web.json_response(payload, status=status, headers={"Cache-Control": "no-store"})


class WebAdminConversationPromptMixin:
    async def handle_api_conversation_prompt_preview(self, request: web.Request) -> web.Response:
        return await self._conversation_prompt_refresh(request, apply=False)

    async def handle_api_conversation_prompt_update(self, request: web.Request) -> web.Response:
        return await self._conversation_prompt_refresh(request, apply=True)

    async def _conversation_prompt_refresh(self, request: web.Request, *, apply: bool) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        conv_uuid = str(request.match_info.get("conversation_uuid") or "")
        row = await self._conversation_row(session.chat_id, conv_uuid, require=True)
        chat_id = int(row["internal_chat_id"])
        body = await self._json_body(request) if apply else {}
        if apply and (
            body.get("confirmed") is not True
            or any(not isinstance(body.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", body[key])
                   for key in ("beforeHash", "afterHash"))
        ):
            return _prompt_response({"ok": False, "error": "prompt_confirmation_required"}, status=400)

        # Match the folder-update lock ordering. Neither a queued nor a running
        # conversation is ever refreshed later as a side effect of this request.
        async with self._conversation_tree_lock:
            async with self.operation_locks.try_chat(chat_id, "conversation_prompt_refresh") as acquired:
                if not acquired:
                    return _prompt_response({"ok": False, "error": "busy"}, status=409)
                row = await self._conversation_row(session.chat_id, conv_uuid, require=True)
                if self._web_starting_turns.get(conv_uuid) or await self._web_conversation_has_active_runtime(row):
                    return _prompt_response({"ok": False, "error": "busy"}, status=409)
                cur = await self.db.conn.execute("SELECT system_snapshot FROM sessions WHERE chat_id=?", (chat_id,))
                existing = await cur.fetchone()
                if existing is None:
                    return _prompt_response({"ok": False, "error": "session_not_found"}, status=409)
                before = str(existing["system_snapshot"] or "")
                before_hash = system_prompt_sha256(before)
                try:
                    candidate = await self._build_system_prompt_for_chat(conv_uuid, strict=True)
                    if not str(candidate or "").strip() or "[[ERROR:" in candidate:
                        raise ValueError("invalid rendered system prompt")
                except Exception:
                    # Do not expose raw render exceptions: they may contain
                    # private template input. Never install a fallback prompt.
                    return _prompt_response({"ok": False, "error": "prompt_render_failed"}, status=400)
                after_hash = system_prompt_sha256(candidate)
                metadata = {
                    "ok": True, "conversationUuid": conv_uuid,
                    "beforeHash": before_hash, "afterHash": after_hash,
                    "beforeChars": len(before), "afterChars": len(candidate),
                    "changed": before != candidate,
                }
                if not apply:
                    # splitlines normalizes the diff presentation only; exact
                    # hashes and equality bind the actual unmodified bytes.
                    delta = "\n".join(difflib.unified_diff(
                        before.splitlines(), candidate.splitlines(),
                        fromfile="当前冻结提示词", tofile="按当前配置重新组装", lineterm="",
                    ))
                    if not delta and before != candidate:
                        delta = "提示词正文相同，但末尾换行发生变化。"
                    return _prompt_response({**metadata, "diff": delta})
                # A changed template/memory/folder must not silently replace the
                # candidate the user just reviewed. Re-preview, never auto-retry.
                if after_hash != body["afterHash"]:
                    return _prompt_response({"ok": False, "error": "prompt_preview_changed"}, status=409)
                if before == candidate:
                    return _prompt_response({**metadata, "updated": False})
                if before_hash != body["beforeHash"]:
                    return _prompt_response({"ok": False, "error": "prompt_preview_changed"}, status=409)
                if self._web_starting_turns.get(conv_uuid) or await self._web_conversation_has_active_runtime(row):
                    return _prompt_response({"ok": False, "error": "busy"}, status=409)
                async with self.db.conn.transaction(label="conversation-prompt-refresh") as conn:
                    cur = await conn.execute(
                        "UPDATE sessions SET system_snapshot=?,updated_at=? WHERE chat_id=? AND COALESCE(system_snapshot,'')=?",
                        (candidate, now_ts(), chat_id, before),
                    )
                    if cur.rowcount != 1:
                        return _prompt_response({"ok": False, "error": "prompt_preview_changed"}, status=409)
                    # Opaque provider continuation can carry the old system. Do
                    # not touch transcript, summaries, files, or TaskMemory.
                    await conn.execute("DELETE FROM controller_model_contexts WHERE chat_id=?", (chat_id,))
                await self.audit(
                    "conversation.prompt.refresh", actor="web", chat_id=session.chat_id,
                    ip=request.remote or "", detail={
                        "conversationUuid": conv_uuid, "beforeHash": before_hash,
                        "afterHash": after_hash, "beforeChars": len(before), "afterChars": len(candidate),
                    },
                )
                return _prompt_response({**metadata, "updated": True})
