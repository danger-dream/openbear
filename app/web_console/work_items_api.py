# ruff: noqa: F401,F403,F405
"""Task panel API: the conversation's work items, the user's edits, and opening their files."""
from __future__ import annotations

import asyncio
import contextlib
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.tools.file_state import normalize_path
from app.utils import estimate_tokens
from app.web_console.core import *
from app.work_items import (
    Actor,
    Source,
    WorkItemConflict,
    WorkItemError,
    WorkItemNotFound,
    WorkItemsDAO,
    WorkItemsRuntime,
    changed_event,
    now_ms,
    walk,
)

_FILE_KINDS = {"file", "report"}
_DIFF_LIMIT = 400_000
_GIT_TIMEOUT_S = 15


class WebAdminWorkItemsMixin:
    def _work_items(self) -> WorkItemsDAO:
        return WorkItemsDAO(self.db)

    @staticmethod
    def _work_item_error(exc: Exception) -> web.Response:
        if isinstance(exc, WorkItemNotFound):
            return web.json_response({"ok": False, "error": exc.code, "message": exc.message}, status=404)
        if isinstance(exc, WorkItemConflict):
            return web.json_response({"ok": False, "error": exc.code, "message": exc.message}, status=409)
        if isinstance(exc, WorkItemError):
            return web.json_response({"ok": False, "error": exc.code, "message": exc.message}, status=400)
        raise exc

    async def _work_items_publish(self, row: dict[str, Any], numbers: list[str], action: str) -> None:
        with contextlib.suppress(Exception):
            await self._live_for(row).publish(changed_event(str(row["conversation_uuid"]), numbers, action), persist=False)

    async def _work_items_active_root(self, row: dict[str, Any]) -> str:
        if self.runs is None or not self.runs.is_running(int(row.get("internal_chat_id") or 0)):
            return ""
        info = await self._web_active_round_info(str(row["conversation_uuid"]), int(row.get("internal_chat_id") or 0))
        return str(info.get("rootTurnUuid") or "")

    async def handle_api_work_items(self, request: web.Request) -> web.Response:
        row = await self._conversation_from_request(request)
        conv = str(row["conversation_uuid"])
        dao = self._work_items()
        tree = await dao.tree(conv)
        active_root = await self._work_items_active_root(row)
        focus = await dao.focus(conv, active_root) if active_root else None
        if focus:
            current = focus.get("current") or ""
            node = next((n for n in walk(tree) if n["number"] == current), None)
            focus["current"] = current if node is not None and node["status"] == "doing" else ""
            focus["active"] = True
        runtime = WorkItemsRuntime(dao, conv, active_root)
        outline, _focus, _seq = await runtime.outline()
        return web.json_response({
            "ok": True,
            "tasks": tree,
            "focus": focus,
            "running": bool(active_root),
            "turnFocuses": await dao.turn_focuses(conv),
            "outline": {"text": outline, "tokens": estimate_tokens(outline) if outline else 0},
        })

    async def handle_api_work_item_detail(self, request: web.Request) -> web.Response:
        row = await self._conversation_from_request(request)
        try:
            detail = await self._work_items().detail(str(row["conversation_uuid"]), request.match_info["number"])
        except Exception as exc:
            return self._work_item_error(exc)
        for item in detail.get("evidence") or []:
            if item.get("diff"):
                item["hasDiff"] = True
                item.pop("diff")
        for event in detail.get("events") or []:
            for item in (event.get("data") or {}).get("evidence") or []:
                if item.get("diff"):
                    item["hasDiff"] = True
                    item.pop("diff")
        return web.json_response({"ok": True, "item": detail})

    def _panel_actor(self) -> Actor:
        return Actor("user")

    async def handle_api_work_item_add(self, request: web.Request) -> web.Response:
        row = await self._conversation_from_request(request)
        body = await self._json_body(request)
        title = str(body.get("title") or "").strip()
        try:
            created = await self._work_items().add(str(row["conversation_uuid"]), self._panel_actor(),
                                                   request.match_info["number"], [{"title": title}],
                                                   reason=f"加了子项：{title[:120]}")
        except Exception as exc:
            return self._work_item_error(exc)
        await self._work_items_publish(row, created, "add")
        return web.json_response({"ok": True, "created": created})

    async def handle_api_work_item_update(self, request: web.Request) -> web.Response:
        row = await self._conversation_from_request(request)
        body = await self._json_body(request)
        changes = {key: body[key] for key in ("title", "description", "criteria") if key in body}
        try:
            item = await self._work_items().update(str(row["conversation_uuid"]), self._panel_actor(),
                                                   request.match_info["number"], revision=int(body.get("revision") or 0),
                                                   changes=changes)
        except Exception as exc:
            return self._work_item_error(exc)
        await self._work_items_publish(row, [item["number"]], "update")
        return web.json_response({"ok": True, "item": item})

    async def _work_item_transition(self, request: web.Request, action: str) -> web.Response:
        row = await self._conversation_from_request(request)
        body = await self._json_body(request)
        reason = str(body.get("reason") or "").strip()
        source = Source("panel", True, "user", created_at_ms=now_ms())
        summary = {"drop": "标为不做", "reopen": "重新打开"}[action] + (f"：{reason}" if reason else "")
        try:
            item = await self._work_items().transition(
                str(row["conversation_uuid"]), self._panel_actor(), request.match_info["number"], action,
                summary=summary, reason=reason or ("用户在面板上重新打开" if action == "reopen" else ""), source=source,
                revision=int(body.get("revision") or 0))
        except Exception as exc:
            return self._work_item_error(exc)
        await self._work_items_publish(row, [item["number"]], action)
        return web.json_response({"ok": True, "item": item})

    async def handle_api_work_item_drop(self, request: web.Request) -> web.Response:
        return await self._work_item_transition(request, "drop")

    async def handle_api_work_item_reopen(self, request: web.Request) -> web.Response:
        return await self._work_item_transition(request, "reopen")

    async def handle_api_work_item_delete(self, request: web.Request) -> web.Response:
        row = await self._conversation_from_request(request)
        body = await self._json_body(request)
        reason = str(body.get("reason") or request.query.get("reason") or "用户在面板上删除（建错的项）").strip()
        try:
            removed = await self._work_items().delete(str(row["conversation_uuid"]), self._panel_actor(),
                                                      request.match_info["number"], reason=reason)
        except Exception as exc:
            return self._work_item_error(exc)
        await self._work_items_publish(row, removed, "delete")
        return web.json_response({"ok": True, "deleted": removed})

    async def _work_item_file_entry(self, request: web.Request) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """The evidence entry named by ref; the path comes only from the stored record."""
        row = await self._conversation_from_request(request)
        conv = str(row["conversation_uuid"])
        dao = self._work_items()
        try:
            detail = await dao.detail(conv, request.match_info["number"])
        except WorkItemError:
            raise web.HTTPNotFound(text="work item not found")
        ref = str(request.query.get("ref") or "")
        entries: list[dict[str, Any]] = []
        if ref.startswith("item:"):
            entries = detail.get("evidence") or []
            index = ref[5:]
        elif ref.startswith("event:"):
            _, event_uuid, index = (ref.split(":", 2) + ["", ""])[:3]
            event = next((e for e in detail.get("events") or [] if e["eventUuid"] == event_uuid), None)
            data = (event or {}).get("data") or {}
            entries = data.get("evidence") or data.get("files") or []
        else:
            raise web.HTTPBadRequest(text="invalid ref")
        try:
            entry = entries[int(index)]
        except (ValueError, IndexError):
            raise web.HTTPNotFound(text="file not found")
        return row, detail, entry

    @staticmethod
    def _work_item_path_allowed(path: Path) -> bool:
        # Any directory on this machine; only credential and database files stay closed.
        lowered = path.name.lower()
        return lowered not in {"openbear.json", ".env"} and not lowered.endswith((".db", ".sqlite", ".sqlite3", ".pem", ".key"))

    def _work_item_resolve(self, entry: dict[str, Any], row: dict[str, Any]) -> Path:
        if entry.get("kind") not in _FILE_KINDS or not entry.get("path"):
            raise web.HTTPNotFound(text="not a file")
        if entry.get("server"):
            raise web.HTTPNotFound(text=f"the file is on server {entry['server']}")
        raw = str(entry["path"]).strip()
        if raw.startswith("workspace/artifacts/"):
            raw = str(Path(str(getattr(self, "workspace_dir", "") or "workspace")).parent / raw)
        try:
            path = normalize_path(raw).resolve()
            found = path.is_file()
        except OSError:
            found = False
        if not found:
            raise web.HTTPNotFound(text="file not found")
        if not self._work_item_path_allowed(path):
            raise web.HTTPForbidden(text="this file cannot be opened from the panel")
        return path

    async def handle_api_work_item_file(self, request: web.Request) -> web.Response:
        row, detail, entry = await self._work_item_file_entry(request)
        conv = str(row["conversation_uuid"])
        ref = str(request.query.get("ref") or "")
        has_diff = bool(entry.get("diff")) or bool(_commit_of(detail, entry))
        try:
            path = self._work_item_resolve(entry, row)
        except web.HTTPNotFound:
            if not has_diff:
                raise
            return web.json_response({"ok": True, "artifact": {
                "artifactUuid": f"work-item:{detail['number']}:{ref}", "conversationUuid": conv,
                "source": "work-item-file", "fileName": Path(str(entry.get("path") or entry.get("commit") or "改动")).name,
                "mimeType": "text/plain", "sizeBytes": 0, "path": entry.get("path") or "", "missing": True, "hasDiff": True,
                "inlinePreview": True,
                "diffUrl": f"/api/conversations/{conv}/work-items/{detail['number']}/file/diff?ref={quote(ref, safe='')}",
            }})
        stat = path.stat()
        looks_text, sha256_value = await asyncio.to_thread(self._tool_file_probe, path, digest=False)
        mime_type = self._tool_file_mime(path.name, looks_text)
        base = f"/api/conversations/{conv}/work-items/{detail['number']}/file"
        query = f"?ref={quote(ref, safe='')}"
        project = not str(path).startswith(str(Path(str(getattr(self, "workspace_dir", "") or "")).resolve() / "artifacts"))
        return web.json_response({"ok": True, "artifact": {
            "artifactUuid": f"work-item:{detail['number']}:{ref}",
            "conversationUuid": conv,
            "source": "work-item-file",
            "fileName": path.name,
            "mimeType": mime_type,
            "sizeBytes": int(stat.st_size),
            "modifiedAt": int(stat.st_mtime * 1000),
            "path": str(path),
            "projectFile": project,
            "hasDiff": has_diff,
            "inlinePreview": self._web_artifact_inline_allowed(path.name, mime_type),
            "contentUrl": f"{base}/content{query}",
            "downloadUrl": f"{base}/content{query}&download=1",
            "diffUrl": f"{base}/diff{query}" if has_diff else "",
        }})

    async def handle_api_work_item_file_content(self, request: web.Request) -> web.StreamResponse:
        row, _detail, entry = await self._work_item_file_entry(request)
        path = self._work_item_resolve(entry, row)
        looks_text, _ = await asyncio.to_thread(self._tool_file_probe, path, digest=False)
        mime_type = self._tool_file_mime(path.name, looks_text)
        forced = str(request.query.get("download") or "").lower() in {"1", "true", "yes", "on"}
        inline = self._web_artifact_inline_allowed(path.name, mime_type) and not forced
        response_mime = f"{mime_type}; charset=utf-8" if _is_text_file(path.name, mime_type) else mime_type
        return web.FileResponse(path, headers={
            "Content-Type": response_mime,
            "Content-Disposition": self._web_artifact_content_disposition(path.name, inline=inline),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        })

    async def handle_api_work_item_file_diff(self, request: web.Request) -> web.Response:
        """What this item changed in the file: the recorded commit, else the snapshot taken at completion."""
        row, detail, entry = await self._work_item_file_entry(request)
        commit = _commit_of(detail, entry)
        if commit:
            paths = [entry["path"]] if entry.get("kind") in _FILE_KINDS and entry.get("path") else \
                [item["path"] for item in detail.get("evidence") or [] if item.get("kind") in _FILE_KINDS and item.get("path")]
            text, error = await self._work_item_git_show(commit, paths)
            if text:
                return web.json_response({"ok": True, "source": "commit", "commit": commit, "diff": text})
            if not entry.get("diff"):
                return web.json_response({"ok": False, "error": error or "diff_unavailable"}, status=404)
        if entry.get("diff"):
            return web.json_response({"ok": True, "source": "snapshot", "diff": str(entry["diff"])[:_DIFF_LIMIT]})
        return web.json_response({"ok": False, "error": "diff_unavailable"}, status=404)

    async def _work_item_git_show(self, commit: str, paths: list[str]) -> tuple[str, str]:
        if not re.fullmatch(r"[0-9a-fA-F]{6,40}", commit or ""):
            return "", "invalid_commit"
        resolved = []
        root = None
        for raw in paths:
            with contextlib.suppress(OSError):
                path = normalize_path(raw).resolve()
                for parent in [path.parent, *path.parents]:
                    if (parent / ".git").exists():
                        root = root or parent
                        if parent == root:
                            resolved.append(str(path.relative_to(root)))
                        break
        root = root or Path.cwd().resolve()
        if not (root / ".git").exists():
            return "", "not_a_repository"
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "show", "--no-color", "--format=commit %H%n%s%n", commit, "--", *resolved,
                cwd=str(root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=_GIT_TIMEOUT_S)
        except Exception:
            return "", "git_failed"
        if proc.returncode != 0:
            return "", "commit_not_found"
        return out.decode("utf-8", "replace")[:_DIFF_LIMIT], ""


def _commit_of(detail: dict[str, Any], entry: dict[str, Any]) -> str:
    if entry.get("kind") == "commit":
        return str(entry.get("commit") or "")
    return next((str(item.get("commit") or "") for item in detail.get("evidence") or [] if item.get("kind") == "commit"), "")
