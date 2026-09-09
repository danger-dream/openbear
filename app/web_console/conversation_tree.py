"""Lazy, arbitrarily-deep Web conversation organization tree.

The tree is intentionally separate from runtime chat ids and timeline payloads.  Its
list/status endpoints only return organization metadata; folder property bodies are
read only by the dedicated properties endpoint.
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Any

from aiohttp import web

from app.db.engine import now_ts
from app.tools import processes
from app.web_console.core import _WEB_SESSION_KEY, WebSession

_TREE_PAGE_SIZE = 50
_TREE_ORDER_STEP = 1024.0


class WebAdminConversationTreeMixin:
    async def _tree_folder_owned(self, owner_chat_id: int, folder_uuid: str) -> bool:
        folder_id = str(folder_uuid or "").strip()
        if not folder_id:
            return True
        cur = await self.db.conn.execute(
            "SELECT 1 FROM web_conversation_folders WHERE owner_chat_id=? AND folder_uuid=? LIMIT 1",
            (int(owner_chat_id), folder_id),
        )
        return await cur.fetchone() is not None

    async def _tree_folders(
        self,
        owner_chat_id: int,
        *,
        include_properties: bool = False,
    ) -> dict[str, dict[str, Any]]:
        columns = "*" if include_properties else """
            id, folder_uuid, owner_chat_id, parent_uuid, name, pinned_at,
            display_order, created_at, updated_at,
            CASE WHEN TRIM(COALESCE(workspace_dir,''))<>'' THEN 1 ELSE 0 END AS has_local_workspace,
            CASE WHEN TRIM(COALESCE(prompt_markdown,''))<>'' THEN 1 ELSE 0 END AS has_local_prompt
        """
        cur = await self.db.conn.execute(
            f"SELECT {columns} FROM web_conversation_folders WHERE owner_chat_id=?",
            (int(owner_chat_id),),
        )
        return {str(row["folder_uuid"]): dict(row) for row in await cur.fetchall()}

    @staticmethod
    def _tree_folder_path(folder_uuid: str, folders: dict[str, dict[str, Any]]) -> list[str]:
        path: list[str] = []
        current = str(folder_uuid or "")
        seen: set[str] = set()
        while current:
            if current in seen or current not in folders:
                break
            seen.add(current)
            path.append(current)
            current = str(folders[current].get("parent_uuid") or "")
        path.reverse()
        return path

    @classmethod
    def _tree_folder_path_text(cls, folder_uuid: str, folders: dict[str, dict[str, Any]]) -> str:
        return " / ".join(str(folders[item].get("name") or "") for item in cls._tree_folder_path(folder_uuid, folders))

    @classmethod
    def _tree_effective_from_map(
        cls,
        folder_uuid: str,
        folders: dict[str, dict[str, Any]],
        default_workspace: str,
    ) -> tuple[str, str, str, str]:
        workspace = ""
        prompt = ""
        workspace_source = ""
        prompt_source = ""
        current = str(folder_uuid or "")
        seen: set[str] = set()
        while current:
            if current in seen or current not in folders:
                break
            seen.add(current)
            row = folders[current]
            if not workspace and str(row.get("workspace_dir") or "").strip():
                workspace = str(row.get("workspace_dir") or "").strip()
                workspace_source = current
            if not prompt and str(row.get("prompt_markdown") or "").strip():
                prompt = str(row.get("prompt_markdown") or "")
                prompt_source = current
            if workspace and prompt:
                break
            current = str(row.get("parent_uuid") or "")
        return workspace or str(default_workspace or ""), prompt, workspace_source, prompt_source

    async def _tree_effective_folder_values(self, owner_chat_id: int, folder_uuid: str) -> tuple[str, str]:
        folders = await self._tree_folders(owner_chat_id, include_properties=True)
        workspace, prompt, _workspace_source, _prompt_source = self._tree_effective_from_map(
            folder_uuid,
            folders,
            str(getattr(self, "workspace_dir", "") or ""),
        )
        return workspace, prompt

    @staticmethod
    def _tree_descendants(folder_uuid: str, folders: dict[str, dict[str, Any]]) -> set[str]:
        wanted = {str(folder_uuid or "")}
        changed = True
        while changed:
            changed = False
            for item, row in folders.items():
                if item not in wanted and str(row.get("parent_uuid") or "") in wanted:
                    wanted.add(item)
                    changed = True
        wanted.discard("")
        return wanted

    async def _tree_running_state(self, owner_chat_id: int) -> dict[str, Any]:
        """Return complete unarchived runtime truth without loading timelines or attributes."""
        cur = await self.db.conn.execute(
            """
            SELECT conversation_uuid, internal_chat_id, folder_uuid, title, pinned_at,
                   display_order, created_at, updated_at, status, current_status, last_error
            FROM web_conversations
            WHERE owner_chat_id=? AND COALESCE(archived_at,0)=0
            """,
            (int(owner_chat_id),),
        )
        rows = [dict(row) for row in await cur.fetchall()]
        uuids = [str(row.get("conversation_uuid") or "") for row in rows]
        if uuids:
            placeholders = ",".join("?" for _ in uuids)
            cur = await self.db.conn.execute(
                f"""SELECT DISTINCT conversation_uuid FROM web_operations
                    WHERE conversation_uuid IN ({placeholders})
                      AND op_type!='notice'
                      AND COALESCE(lifecycle,'') IN ('active','paused','waiting_control')""",
                tuple(uuids),
            )
            operation_candidates = {str(row["conversation_uuid"] or "") for row in await cur.fetchall()}
            for row in rows:
                conv_uuid = str(row.get("conversation_uuid") or "")
                if str(row.get("status") or "idle") in {"running", "stopping", "error"} or conv_uuid in operation_candidates:
                    await self._reconcile_inactive_web_conversation_operations(
                        row, source="conversation_tree_status_reconcile"
                    )
        operation_facts = await self._web_operation_facts_for_conversations(uuids)

        rath_counts: dict[int, int] = {}
        if rows:
            chat_ids = [int(row.get("internal_chat_id") or 0) for row in rows]
            placeholders = ",".join("?" for _ in chat_ids)
            cur = await self.db.conn.execute(
                f"""
                SELECT chat_id, COUNT(*) AS active_count FROM rath_tasks
                WHERE chat_id IN ({placeholders})
                  AND COALESCE(status,'') IN ('queued','running','pausing','paused','resuming','stopping','needs_openbear_control')
                GROUP BY chat_id
                """,
                tuple(chat_ids),
            )
            rath_counts = {int(row["chat_id"]): int(row["active_count"] or 0) for row in await cur.fetchall()}
        process_chat_ids = {
            int(getattr(proc, "chat_id", 0) or 0)
            for proc in processes.active()
        }
        running_items: list[dict[str, Any]] = []
        folder_counts: dict[str, int] = {}
        direct_conversation_counts: dict[str, int] = {}
        folders = await self._tree_folders(owner_chat_id)
        for row in rows:
            folder_id = str(row.get("folder_uuid") or "")
            direct_conversation_counts[folder_id] = direct_conversation_counts.get(folder_id, 0) + 1
            conv_uuid = str(row.get("conversation_uuid") or "")
            chat_id = int(row.get("internal_chat_id") or 0)
            facts = operation_facts.get(conv_uuid) or {}
            live = self._web_live_streams.get(conv_uuid)
            live_snapshot = live.snapshot() if live is not None else {}
            running = bool(
                self._web_starting_turns.get(conv_uuid)
                or live_snapshot.get("running")
                or (self.runs is not None and self.runs.is_running(chat_id))
                or chat_id in process_chat_ids
                or int(facts.get("activeCount") or 0) > 0
                or rath_counts.get(chat_id, 0) > 0
            )
            if not running:
                continue
            current = str(live_snapshot.get("currentStatus") or row.get("current_status") or "运行中")
            if rath_counts.get(chat_id, 0) and not live_snapshot.get("running"):
                current = "Agent 后台执行中"
            running_items.append({
                "kind": "conversation",
                "id": conv_uuid,
                "conversationUuid": conv_uuid,
                "folderId": str(row.get("folder_uuid") or ""),
                "parentId": str(row.get("folder_uuid") or ""),
                "title": str(row.get("title") or "新会话"),
                "pinned": int(row.get("pinned_at") or 0) > 0,
                "pinnedAt": int(row.get("pinned_at") or 0),
                "displayOrder": float(row["display_order"]) if row.get("display_order") is not None else None,
                "createdAt": int(row.get("created_at") or 0),
                "archived": False,
                "running": True,
                "currentStatus": current,
            })
            for ancestor in self._tree_folder_path(str(row.get("folder_uuid") or ""), folders):
                folder_counts[ancestor] = folder_counts.get(ancestor, 0) + 1
        return {
            "items": running_items,
            "folderRunningCounts": folder_counts,
            "folderConversationCounts": self._tree_subtree_counts(direct_conversation_counts, folders),
            "revision": int(time.time() * 1000),
        }

    @classmethod
    def _tree_subtree_counts(cls, direct_counts: dict[str, int], folders: dict[str, dict[str, Any]]) -> dict[str, int]:
        """Count each conversation once in its folder and every ancestor, including unloaded folders."""
        totals = {folder_id: int(direct_counts.get(folder_id, 0)) for folder_id in folders}
        # The empty key remains the temporary node, never the sum of all roots.
        totals[""] = int(direct_counts.get("", 0))
        for folder_id, count in direct_counts.items():
            if not count:
                continue
            for ancestor in cls._tree_folder_path(folder_id, folders)[:-1]:
                totals[ancestor] += count
        return totals

    async def _tree_counts(self, owner_chat_id: int, folders: dict[str, dict[str, Any]]) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
        direct_folders: dict[str, int] = {}
        for row in folders.values():
            parent = str(row.get("parent_uuid") or "")
            direct_folders[parent] = direct_folders.get(parent, 0) + 1
        cur = await self.db.conn.execute(
            """
            SELECT folder_uuid,
                   SUM(CASE WHEN COALESCE(archived_at,0)=0 THEN 1 ELSE 0 END) AS active_count,
                   SUM(CASE WHEN COALESCE(archived_at,0)>0 THEN 1 ELSE 0 END) AS archived_count
            FROM web_conversations WHERE owner_chat_id=? GROUP BY folder_uuid
            """,
            (int(owner_chat_id),),
        )
        active: dict[str, int] = {}
        archived: dict[str, int] = {}
        for row in await cur.fetchall():
            folder_id = str(row["folder_uuid"] or "")
            active[folder_id] = int(row["active_count"] or 0)
            archived[folder_id] = int(row["archived_count"] or 0)
        return direct_folders, self._tree_subtree_counts(active, folders), archived

    def _tree_folder_json(
        self,
        row: dict[str, Any],
        folders: dict[str, dict[str, Any]],
        direct_folders: dict[str, int],
        active_counts: dict[str, int],
        archived_counts: dict[str, int],
        running_counts: dict[str, int],
    ) -> dict[str, Any]:
        folder_id = str(row.get("folder_uuid") or "")
        return {
            "kind": "folder",
            "id": folder_id,
            "folderId": folder_id,
            "parentId": str(row.get("parent_uuid") or ""),
            "name": str(row.get("name") or "未命名目录"),
            "path": self._tree_folder_path_text(folder_id, folders),
            "pinned": int(row.get("pinned_at") or 0) > 0,
            "pinnedAt": int(row.get("pinned_at") or 0),
            "displayOrder": float(row["display_order"]) if row.get("display_order") is not None else None,
            "createdAt": int(row.get("created_at") or 0),
            "updatedAt": int(row.get("updated_at") or 0),
            "childFolderCount": int(direct_folders.get(folder_id, 0)),
            "conversationCount": int(active_counts.get(folder_id, 0)),
            "archivedConversationCount": int(archived_counts.get(folder_id, 0)),
            "runningDescendantCount": int(running_counts.get(folder_id, 0)),
            "hasLocalWorkspace": bool(int(row.get("has_local_workspace") or 0)) if "has_local_workspace" in row else bool(str(row.get("workspace_dir") or "").strip()),
            "hasLocalPrompt": bool(int(row.get("has_local_prompt") or 0)) if "has_local_prompt" in row else bool(str(row.get("prompt_markdown") or "").strip()),
        }

    def _tree_conversation_json(
        self,
        row: dict[str, Any],
        folders: dict[str, dict[str, Any]],
        running_lookup: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        conv_uuid = str(row.get("conversation_uuid") or "")
        running = running_lookup.get(conv_uuid) or {}
        return {
            "kind": "conversation",
            "id": conv_uuid,
            "conversationUuid": conv_uuid,
            "folderId": str(row.get("folder_uuid") or ""),
            "parentId": str(row.get("folder_uuid") or ""),
            "title": str(row.get("title") or "新会话"),
            "pinned": int(row.get("pinned_at") or 0) > 0,
            "pinnedAt": int(row.get("pinned_at") or 0),
            "displayOrder": float(row["display_order"]) if row.get("display_order") is not None else None,
            "archived": int(row.get("archived_at") or 0) > 0,
            "archivedAt": int(row.get("archived_at") or 0),
            "createdAt": int(row.get("created_at") or 0),
            "lastConversationAt": int(row.get("last_conversation_at") or 0),
            "running": bool(running),
            "status": "running" if running else "idle",
            "currentStatus": str(running.get("currentStatus") or row.get("current_status") or "就绪"),
            "path": self._tree_folder_path_text(str(row.get("folder_uuid") or ""), folders) or "临时会话",
        }

    async def _tree_direct_folder_nodes(self, owner_chat_id: int, parent_uuid: str = "") -> list[dict[str, Any]]:
        folders = await self._tree_folders(owner_chat_id)
        direct, active, archived = await self._tree_counts(owner_chat_id, folders)
        running_state = await self._tree_running_state(owner_chat_id)
        running_counts = dict(running_state["folderRunningCounts"])
        rows = [row for row in folders.values() if str(row.get("parent_uuid") or "") == str(parent_uuid or "")]
        rows.sort(key=lambda row: (
            0 if int(row.get("pinned_at") or 0) > 0 else 1,
            1 if row.get("display_order") is None else 0,
            float(row.get("display_order") or 0),
            -int(row.get("created_at") or 0),
            -int(row.get("id") or 0),
        ))
        return [self._tree_folder_json(row, folders, direct, active, archived, running_counts) for row in rows]

    async def _tree_folder_items_for_path(
        self,
        owner_chat_id: int,
        folder_path: list[str],
        *,
        folders: dict[str, dict[str, Any]] | None = None,
        status: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if not folder_path:
            return []
        folder_map = folders if folders is not None else await self._tree_folders(owner_chat_id)
        running_state = status if status is not None else await self._tree_running_state(owner_chat_id)
        direct, active, archived = await self._tree_counts(owner_chat_id, folder_map)
        running_counts = dict(running_state.get("folderRunningCounts") or {})
        return [
            self._tree_folder_json(folder_map[folder_id], folder_map, direct, active, archived, running_counts)
            for folder_id in folder_path
            if folder_id in folder_map
        ]

    async def _tree_locate_conversation(
        self,
        owner_chat_id: int,
        conversation_uuid: str,
        *,
        folders: dict[str, dict[str, Any]] | None = None,
        status: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        conv_uuid = str(conversation_uuid or "").strip()
        if not conv_uuid:
            return None
        cur = await self.db.conn.execute(
            """
            SELECT wc.*,
                   COALESCE((SELECT MAX(m.created_at) FROM messages m
                             WHERE m.chat_id=wc.internal_chat_id
                               AND COALESCE(m.task_uuid,'')=''
                               AND (m.role='user' OR (m.role='assistant' AND TRIM(COALESCE(m.content,''))<>''))),0) AS last_conversation_at
            FROM web_conversations wc
            WHERE wc.owner_chat_id=? AND wc.conversation_uuid=? LIMIT 1
            """,
            (int(owner_chat_id), conv_uuid),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        folder_map = folders if folders is not None else await self._tree_folders(owner_chat_id)
        running_state = status
        if running_state is None:
            # Archived conversations themselves never run, but their folder path can
            # still contain active descendants whose aggregate marker must stay true.
            running_state = await self._tree_running_state(owner_chat_id)
        lookup = {str(item["conversationUuid"]): item for item in running_state.get("items") or []}
        item = self._tree_conversation_json(dict(row), folder_map, lookup)
        folder_path = self._tree_folder_path(str(row["folder_uuid"] or ""), folder_map)
        folder_items = await self._tree_folder_items_for_path(
            owner_chat_id, folder_path, folders=folder_map, status=running_state
        )
        return {"item": item, "folderPath": folder_path, "folderItems": folder_items}

    async def handle_api_conversation_tree_bootstrap(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        owner = int(session.chat_id)
        explicit_uuid = str(request.query.get("conversationUuid") or "").strip()
        folders = await self._tree_folders(owner)
        roots = await self._tree_direct_folder_nodes(owner, "")
        status = await self._tree_running_state(owner)
        selected = await self._tree_locate_conversation(
            owner, explicit_uuid, folders=folders, status=status
        ) if explicit_uuid else None
        if selected is None and not explicit_uuid:
            cur = await self.db.conn.execute(
                """
                SELECT wc.conversation_uuid
                FROM web_conversations wc
                WHERE wc.owner_chat_id=? AND COALESCE(wc.archived_at,0)=0
                ORDER BY COALESCE((SELECT MAX(m.created_at) FROM messages m
                                   WHERE m.chat_id=wc.internal_chat_id
                                     AND COALESCE(m.task_uuid,'')=''
                                     AND (m.role='user' OR (m.role='assistant' AND TRIM(COALESCE(m.content,''))<>''))),0) DESC,
                         COALESCE(wc.created_at,0) DESC, wc.id DESC
                LIMIT 1
                """,
                (owner,),
            )
            row = await cur.fetchone()
            if row is not None:
                selected = await self._tree_locate_conversation(
                    owner, str(row["conversation_uuid"] or ""), folders=folders, status=status
                )
        cur = await self.db.conn.execute(
            """SELECT
                 SUM(CASE WHEN COALESCE(archived_at,0)=0 THEN 1 ELSE 0 END) AS active_count,
                 SUM(CASE WHEN COALESCE(archived_at,0)>0 THEN 1 ELSE 0 END) AS archived_count
               FROM web_conversations WHERE owner_chat_id=?""",
            (owner,),
        )
        raw_count_row = await cur.fetchone()
        count_row = dict(raw_count_row) if raw_count_row is not None else {}
        running_paths: list[list[str]] = []
        for item in status["items"]:
            running_paths.append(self._tree_folder_path(str(item.get("folderId") or ""), folders))
        selected_path = list((selected or {}).get("folderPath") or [])
        initial_paths = running_paths + ([selected_path] if selected is not None else []) if status["items"] else ([selected_path] if selected is not None else [])
        located_folder_ids: list[str] = []
        seen_folder_ids: set[str] = set()
        for path in initial_paths:
            for folder_id in path:
                if folder_id not in seen_folder_ids:
                    seen_folder_ids.add(folder_id)
                    located_folder_ids.append(folder_id)
        located_folders = await self._tree_folder_items_for_path(
            owner, located_folder_ids, folders=folders, status=status
        )
        return web.json_response({
            "ok": True,
            "rootFolders": roots,
            "selected": selected,
            "running": status,
            "initialExpandedPaths": initial_paths,
            "locatedFolders": located_folders,
            "activeCount": int((count_row or {}).get("active_count") or 0),
            "archivedCount": int((count_row or {}).get("archived_count") or 0),
            "temporaryCount": int(await self._tree_scalar(
                "SELECT COUNT(*) FROM web_conversations WHERE owner_chat_id=? AND COALESCE(archived_at,0)=0 AND COALESCE(folder_uuid,'')=''",
                (owner,),
            )),
        })

    async def _tree_scalar(self, sql: str, params: tuple[Any, ...]) -> int:
        cur = await self.db.conn.execute(sql, params)
        row = await cur.fetchone()
        return int(row[0] or 0) if row else 0

    async def handle_api_conversation_tree_children(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        owner = int(session.chat_id)
        parent = str(request.query.get("parentId") or "").strip()
        system_node = str(request.query.get("systemNode") or "").strip()
        include_folder_id = str(request.query.get("includeFolderId") or "").strip()
        tracked_folder_ids = [
            item.strip()
            for item in str(request.query.get("includeFolderIds") or "").split(",")
            if item.strip()
        ][:100]
        if include_folder_id and include_folder_id not in tracked_folder_ids:
            tracked_folder_ids.insert(0, include_folder_id)
        if parent and not await self._tree_folder_owned(owner, parent):
            return web.json_response({"ok": False, "error": "folder_not_found"}, status=404)
        if system_node not in {"", "temporary", "archive"}:
            return web.json_response({"ok": False, "error": "invalid_system_node"}, status=400)
        try:
            limit = max(1, min(100, int(request.query.get("limit") or _TREE_PAGE_SIZE)))
            offset = max(0, int(request.query.get("cursor") or 0))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "invalid_cursor"}, status=400)

        folders = await self._tree_folders(owner)
        status = await self._tree_running_state(owner) if system_node != "archive" else {"items": [], "folderRunningCounts": {}}
        running_lookup = {str(item["conversationUuid"]): item for item in status.get("items") or []}
        nodes: list[dict[str, Any]] = []
        if system_node == "":
            nodes.extend(await self._tree_direct_folder_nodes(owner, parent))
        archive_clause = "COALESCE(wc.archived_at,0)>0" if system_node == "archive" else "COALESCE(wc.archived_at,0)=0"
        folder_clause = "" if system_node == "archive" else "AND COALESCE(wc.folder_uuid,'')=?"
        params: tuple[Any, ...] = (owner,) if system_node == "archive" else (owner, parent)
        cur = await self.db.conn.execute(
            f"""
            SELECT wc.*,
                   COALESCE((SELECT MAX(m.created_at) FROM messages m
                             WHERE m.chat_id=wc.internal_chat_id
                               AND COALESCE(m.task_uuid,'')=''
                               AND (m.role='user' OR (m.role='assistant' AND TRIM(COALESCE(m.content,''))<>''))),0) AS last_conversation_at
            FROM web_conversations wc
            WHERE wc.owner_chat_id=? AND {archive_clause} {folder_clause}
            ORDER BY CASE WHEN COALESCE(wc.pinned_at,0)>0 THEN 0 ELSE 1 END,
                     CASE WHEN wc.display_order IS NULL THEN 1 ELSE 0 END,
                     wc.display_order ASC, COALESCE(wc.created_at,0) DESC, wc.id DESC
            """,
            params,
        )
        nodes.extend(self._tree_conversation_json(dict(row), folders, running_lookup) for row in await cur.fetchall())
        page = nodes[offset:offset + limit]
        included_folder_ids: list[str] = []
        if tracked_folder_ids and system_node == "":
            folder_nodes = {
                str(item.get("folderId") or ""): item
                for item in nodes
                if item.get("kind") == "folder"
            }
            if include_folder_id and include_folder_id not in folder_nodes:
                return web.json_response({"ok": False, "error": "included_folder_not_direct_child"}, status=400)
            page_folder_ids = {
                str(item.get("folderId") or "") for item in page if item.get("kind") == "folder"
            }
            for folder_id in tracked_folder_ids:
                included_folder = folder_nodes.get(folder_id)
                if included_folder is None:
                    continue
                included_folder_ids.append(folder_id)
                if folder_id not in page_folder_ids:
                    page.append(included_folder)
                    page_folder_ids.add(folder_id)
        next_cursor = str(offset + limit) if offset + limit < len(nodes) else ""
        return web.json_response({
            "ok": True,
            "parentId": parent,
            "systemNode": system_node,
            "items": page,
            "includedFolderId": include_folder_id if include_folder_id in included_folder_ids else "",
            "includedFolderIds": included_folder_ids,
            "nextCursor": next_cursor,
            "hasMore": bool(next_cursor),
        })

    async def handle_api_conversation_tree_search(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        owner = int(session.chat_id)
        query = str(request.query.get("q") or "").strip()
        archive_unlocked = str(request.query.get("archiveUnlocked") or "").lower() in {"1", "true", "yes"}
        try:
            limit = max(1, min(100, int(request.query.get("limit") or _TREE_PAGE_SIZE)))
            offset = max(0, int(request.query.get("cursor") or 0))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "invalid_cursor"}, status=400)
        if not query:
            return web.json_response({"ok": True, "items": [], "nextCursor": "", "hasMore": False})
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        folders = await self._tree_folders(owner)
        status = await self._tree_running_state(owner)
        running_lookup = {str(item["conversationUuid"]): item for item in status.get("items") or []}
        results: list[dict[str, Any]] = []
        for row in folders.values():
            if query.casefold() in str(row.get("name") or "").casefold():
                result = {
                    "kind": "folder",
                    "id": str(row.get("folder_uuid") or ""),
                    "folderId": str(row.get("folder_uuid") or ""),
                    "parentId": str(row.get("parent_uuid") or ""),
                    "name": str(row.get("name") or ""),
                    "path": self._tree_folder_path_text(str(row.get("folder_uuid") or ""), folders),
                    "pinned": int(row.get("pinned_at") or 0) > 0,
                    "conversationCount": int(status.get("folderConversationCounts", {}).get(str(row.get("folder_uuid") or ""), 0)),
                    "runningDescendantCount": int(status.get("folderRunningCounts", {}).get(str(row.get("folder_uuid") or ""), 0)),
                }
                results.append(result)
        archive_clause = "" if archive_unlocked else "AND COALESCE(wc.archived_at,0)=0"
        cur = await self.db.conn.execute(
            f"""
            SELECT wc.*,
                   COALESCE((SELECT MAX(m.created_at) FROM messages m
                             WHERE m.chat_id=wc.internal_chat_id
                               AND COALESCE(m.task_uuid,'')=''
                               AND (m.role='user' OR (m.role='assistant' AND TRIM(COALESCE(m.content,''))<>''))),0) AS last_conversation_at
            FROM web_conversations wc
            WHERE wc.owner_chat_id=? {archive_clause} AND wc.title LIKE ? ESCAPE '\\'
            ORDER BY COALESCE((SELECT MAX(m.created_at) FROM messages m
                               WHERE m.chat_id=wc.internal_chat_id
                               AND COALESCE(m.task_uuid,'')=''
                               AND (m.role='user' OR (m.role='assistant' AND TRIM(COALESCE(m.content,''))<>''))),0) DESC,
                     wc.id DESC
            """,
            (owner, pattern),
        )
        results.extend(self._tree_conversation_json(dict(row), folders, running_lookup) for row in await cur.fetchall())
        results.sort(key=lambda item: (0 if item.get("kind") == "folder" else 1, str(item.get("path") or ""), str(item.get("name") or item.get("title") or "")))
        page = results[offset:offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(results) else ""
        return web.json_response({"ok": True, "items": page, "nextCursor": next_cursor, "hasMore": bool(next_cursor)})

    async def handle_api_conversation_tree_status(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        return web.json_response({"ok": True, **await self._tree_running_state(int(session.chat_id))})

    async def handle_api_conversation_tree_locate(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        located = await self._tree_locate_conversation(
            int(session.chat_id), str(request.match_info.get("conversation_uuid") or "")
        )
        if located is None:
            return web.json_response({"ok": False, "error": "conversation_not_found"}, status=404)
        return web.json_response({"ok": True, **located})

    async def handle_api_conversation_tree_folder_locate(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        owner = int(session.chat_id)
        folder_id = str(request.match_info.get("folder_uuid") or "").strip()
        folders = await self._tree_folders(owner)
        if folder_id not in folders:
            return web.json_response({"ok": False, "error": "folder_not_found"}, status=404)
        folder_path = self._tree_folder_path(folder_id, folders)
        status = await self._tree_running_state(owner)
        return web.json_response({
            "ok": True,
            "folderPath": folder_path,
            "folderItems": await self._tree_folder_items_for_path(
                owner, folder_path, folders=folders, status=status
            ),
        })

    async def handle_api_conversation_tree_folders(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        query = str(request.query.get("q") or "").strip().casefold()
        folders = await self._tree_folders(int(session.chat_id))
        items = []
        for folder_id, row in folders.items():
            path = self._tree_folder_path_text(folder_id, folders)
            if query and query not in path.casefold():
                continue
            items.append({
                "folderId": folder_id,
                "parentId": str(row.get("parent_uuid") or ""),
                "name": str(row.get("name") or ""),
                "path": path,
            })
        items.sort(key=lambda item: (item["path"].casefold(), item["folderId"]))
        return web.json_response({"ok": True, "items": items})

    async def handle_api_conversation_folder_create(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        owner = int(session.chat_id)
        body = await self._json_body(request)
        name = str(body.get("name") or "").strip()[:120]
        parent = str(body.get("parentId") or "").strip()
        if not name:
            return web.json_response({"ok": False, "error": "folder_name_required"}, status=400)
        if parent and not await self._tree_folder_owned(owner, parent):
            return web.json_response({"ok": False, "error": "folder_not_found"}, status=404)
        async with self._conversation_tree_lock:
            cur = await self.db.conn.execute(
                "SELECT MIN(display_order) FROM web_conversation_folders WHERE owner_chat_id=? AND parent_uuid=? AND COALESCE(pinned_at,0)=0",
                (owner, parent),
            )
            row = await cur.fetchone()
            minimum = row[0] if row else None
            order = _TREE_ORDER_STEP if minimum is None else float(minimum) - _TREE_ORDER_STEP
            folder_id = str(uuid.uuid4())
            ts = now_ts()
            await self.db.conn.execute(
                """INSERT INTO web_conversation_folders
                   (folder_uuid,owner_chat_id,parent_uuid,name,display_order,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (folder_id, owner, parent, name, order, ts, ts),
            )
            await self.db.conn.commit()
        folders = await self._tree_folders(owner)
        direct, active, archived = await self._tree_counts(owner, folders)
        data = self._tree_folder_json(folders[folder_id], folders, direct, active, archived, {})
        await self.audit("web.conversation_folder.create", actor="web", chat_id=owner, ip=request.remote or "", detail={"folderId": folder_id, "parentId": parent})
        return web.json_response({"ok": True, "folder": data})

    async def handle_api_conversation_folder_patch(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        owner = int(session.chat_id)
        folder_id = str(request.match_info.get("folder_uuid") or "").strip()
        if not await self._tree_folder_owned(owner, folder_id):
            return web.json_response({"ok": False, "error": "folder_not_found"}, status=404)
        body = await self._json_body(request)
        assignments: list[str] = []
        params: list[Any] = []
        if "name" in body:
            name = str(body.get("name") or "").strip()[:120]
            if not name:
                return web.json_response({"ok": False, "error": "folder_name_required"}, status=400)
            assignments.append("name=?")
            params.append(name)
        if "pinned" in body:
            if not isinstance(body.get("pinned"), bool):
                return web.json_response({"ok": False, "error": "invalid_pinned_type"}, status=400)
            assignments.append("pinned_at=?")
            params.append(now_ts() if body["pinned"] else 0)
        if not assignments:
            return web.json_response({"ok": False, "error": "nothing_to_update"}, status=400)
        assignments.append("updated_at=?")
        params.append(now_ts())
        params.extend([owner, folder_id])
        async with self._conversation_tree_lock:
            await self.db.conn.execute(
                f"UPDATE web_conversation_folders SET {', '.join(assignments)} WHERE owner_chat_id=? AND folder_uuid=?",
                tuple(params),
            )
            await self.db.conn.commit()
        nodes = await self._tree_direct_folder_nodes(owner, str((await self._tree_folders(owner))[folder_id].get("parent_uuid") or ""))
        return web.json_response({"ok": True, "folder": next(item for item in nodes if item["folderId"] == folder_id)})

    async def handle_api_conversation_folder_properties(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        owner = int(session.chat_id)
        folder_id = str(request.match_info.get("folder_uuid") or "").strip()
        folders = await self._tree_folders(owner, include_properties=True)
        row = folders.get(folder_id)
        if row is None:
            return web.json_response({"ok": False, "error": "folder_not_found"}, status=404)
        workspace, prompt, workspace_source, prompt_source = self._tree_effective_from_map(
            folder_id, folders, str(getattr(self, "workspace_dir", "") or "")
        )
        return web.json_response({
            "ok": True,
            "folderId": folder_id,
            "name": str(row.get("name") or ""),
            "path": self._tree_folder_path_text(folder_id, folders),
            "workspace": {
                "local": str(row.get("workspace_dir") or ""),
                "effective": workspace,
                "sourceFolderId": workspace_source,
                "sourcePath": self._tree_folder_path_text(workspace_source, folders) if workspace_source else "默认 workspace",
            },
            "prompt": {
                "local": str(row.get("prompt_markdown") or ""),
                "effective": prompt,
                "sourceFolderId": prompt_source,
                "sourcePath": self._tree_folder_path_text(prompt_source, folders) if prompt_source else "未设置",
            },
        })

    async def _tree_conversation_rows(self, owner_chat_id: int) -> list[dict[str, Any]]:
        cur = await self.db.conn.execute(
            """SELECT wc.*, COALESCE(s.system_snapshot,'') AS system_snapshot
               FROM web_conversations wc LEFT JOIN sessions s ON s.chat_id=wc.internal_chat_id
               WHERE wc.owner_chat_id=?""",
            (int(owner_chat_id),),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def _tree_change_impact(
        self,
        owner_chat_id: int,
        *,
        kind: str,
        item_id: str,
        target_folder_id: str = "",
        workspace_dir: str | None = None,
        prompt_markdown: str | None = None,
        force_snapshot: bool = False,
    ) -> dict[str, Any]:
        folders_before = await self._tree_folders(owner_chat_id, include_properties=True)
        folders_after = {key: dict(value) for key, value in folders_before.items()}
        candidates: set[str] = set()
        if kind == "properties":
            if item_id not in folders_after:
                raise web.HTTPNotFound(text="folder_not_found")
            if workspace_dir is not None:
                folders_after[item_id]["workspace_dir"] = workspace_dir
            if prompt_markdown is not None:
                folders_after[item_id]["prompt_markdown"] = prompt_markdown
            candidates = self._tree_descendants(item_id, folders_before)
        elif kind == "folder":
            if item_id not in folders_after:
                raise web.HTTPNotFound(text="folder_not_found")
            folders_after[item_id]["parent_uuid"] = target_folder_id
            candidates = self._tree_descendants(item_id, folders_before)
        elif kind != "conversation":
            raise web.HTTPBadRequest(text="invalid_tree_item_kind")

        changed: list[dict[str, Any]] = []
        archived = 0
        running = 0
        for row in await self._tree_conversation_rows(owner_chat_id):
            conv_uuid = str(row.get("conversation_uuid") or "")
            folder_id = str(row.get("folder_uuid") or "")
            if kind == "conversation" and conv_uuid != item_id:
                continue
            if kind in {"properties", "folder"} and folder_id not in candidates:
                continue
            old_values = self._tree_effective_from_map(folder_id, folders_before, str(getattr(self, "workspace_dir", "") or ""))[:2]
            new_folder = target_folder_id if kind == "conversation" else folder_id
            new_values = self._tree_effective_from_map(new_folder, folders_after, str(getattr(self, "workspace_dir", "") or ""))[:2]
            if old_values == new_values and not (force_snapshot and kind == "conversation"):
                continue
            item = {**row, "old_values": old_values, "new_values": new_values}
            is_running = await self._web_conversation_has_active_runtime(row)
            item["running_now"] = is_running
            changed.append(item)
            archived += 1 if int(row.get("archived_at") or 0) > 0 else 0
            running += 1 if is_running else 0
        return {
            "rows": changed,
            "affectedCount": len(changed),
            "archivedCount": archived,
            "runningCount": running,
            "updatableCount": max(0, len(changed) - running),
            "cacheInvalidated": bool(changed),
        }

    @staticmethod
    def _tree_public_impact(impact: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in impact.items() if key != "rows"}

    @staticmethod
    def _tree_property_values(body: dict[str, Any]) -> tuple[str, str]:
        workspace = str(body.get("workspaceDir") or "").strip()
        prompt = str(body.get("promptMarkdown") or "")
        if len(workspace) > 4096:
            raise web.HTTPBadRequest(text="workspace_dir_too_long")
        if len(prompt) > 200_000:
            raise web.HTTPBadRequest(text="folder_prompt_too_long")
        return workspace, prompt

    async def handle_api_conversation_folder_properties_impact(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        body = await self._json_body(request)
        workspace, prompt = self._tree_property_values(body)
        impact = await self._tree_change_impact(
            int(session.chat_id), kind="properties",
            item_id=str(request.match_info.get("folder_uuid") or ""),
            workspace_dir=workspace, prompt_markdown=prompt,
        )
        return web.json_response({"ok": True, **self._tree_public_impact(impact)})

    async def _tree_apply_snapshot_updates_locked(
        self,
        impact: dict[str, Any],
        *,
        update_snapshots: bool,
        mutate: Callable[[Any], Awaitable[None]],
    ) -> dict[str, Any]:
        if not update_snapshots or not impact["rows"]:
            async with self.db.conn.transaction(label="conversation-tree-change") as conn:
                await mutate(conn)
            return {"updatedCount": 0, "skippedRunningCount": 0}

        update_rows: list[dict[str, Any]] = []
        rendered: dict[str, str] = {}
        skipped = 0
        async with AsyncExitStack() as stack:
            for row in sorted(impact["rows"], key=lambda item: int(item.get("internal_chat_id") or 0)):
                chat_id = int(row.get("internal_chat_id") or 0)
                acquired = await stack.enter_async_context(
                    self.operation_locks.try_chat(chat_id, "conversation_tree_snapshot_update")
                )
                if not acquired:
                    skipped += 1
                    continue
                fresh = await self._conversation_row(
                    int(row.get("owner_chat_id") or 0), str(row.get("conversation_uuid") or ""), require=True
                )
                if await self._web_conversation_has_active_runtime(fresh):
                    skipped += 1
                    continue
                conv_uuid = str(row.get("conversation_uuid") or "")
                rendered[conv_uuid] = await self._build_system_prompt_for_chat(
                    conv_uuid,
                    folder_values=tuple(row["new_values"]),
                    strict=True,
                )
                update_rows.append(row)
            async with self.db.conn.transaction(label="conversation-tree-change-and-snapshots") as conn:
                await mutate(conn)
                ts = now_ts()
                for row in update_rows:
                    await conn.execute(
                        "UPDATE sessions SET system_snapshot=?, updated_at=? WHERE chat_id=?",
                        (rendered[str(row.get("conversation_uuid") or "")], ts, int(row.get("internal_chat_id") or 0)),
                    )
                    # Provider continuation caches contain the previous system input.
                    # Removing only this sidecar forces a clean next request; transcript,
                    # summaries, files and TaskMemory remain untouched.
                    await conn.execute(
                        "DELETE FROM controller_model_contexts WHERE chat_id=?",
                        (int(row.get("internal_chat_id") or 0),),
                    )
        return {"updatedCount": len(update_rows), "skippedRunningCount": skipped}

    async def handle_api_conversation_folder_properties_update(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        owner = int(session.chat_id)
        folder_id = str(request.match_info.get("folder_uuid") or "").strip()
        body = await self._json_body(request)
        workspace, prompt = self._tree_property_values(body)
        update_snapshots = body.get("updateSnapshots") is True
        async with self._conversation_tree_lock:
            impact = await self._tree_change_impact(
                owner, kind="properties", item_id=folder_id,
                workspace_dir=workspace, prompt_markdown=prompt,
            )

            async def mutate(conn: Any) -> None:
                await conn.execute(
                    """UPDATE web_conversation_folders
                       SET workspace_dir=?, prompt_markdown=?, updated_at=?
                       WHERE owner_chat_id=? AND folder_uuid=?""",
                    (workspace, prompt, now_ts(), owner, folder_id),
                )

            result = await self._tree_apply_snapshot_updates_locked(
                impact, update_snapshots=update_snapshots, mutate=mutate
            )
        await self.audit("web.conversation_folder.properties", actor="web", chat_id=owner, ip=request.remote or "", detail={"folderId": folder_id, **result})
        return web.json_response({"ok": True, **self._tree_public_impact(impact), **result})

    async def _tree_validate_move_target(
        self,
        owner: int,
        kind: str,
        item_id: str,
        target_folder: str,
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        folders = await self._tree_folders(owner)
        if target_folder and target_folder not in folders:
            raise web.HTTPNotFound(text="folder_not_found")
        if kind == "folder":
            row = folders.get(item_id)
            if row is None:
                raise web.HTTPNotFound(text="folder_not_found")
            if target_folder == item_id or target_folder in self._tree_descendants(item_id, folders):
                raise web.HTTPConflict(text="folder_cycle")
            return row, folders
        if kind == "conversation":
            row = await self._conversation_row(owner, item_id, require=True)
            return row, folders  # type: ignore[return-value]
        raise web.HTTPBadRequest(text="invalid_tree_item_kind")

    async def handle_api_conversation_tree_move_impact(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        owner = int(session.chat_id)
        body = await self._json_body(request)
        kind = str(body.get("kind") or "")
        item_id = str(body.get("id") or "").strip()
        target = str(body.get("targetFolderId") or "").strip()
        await self._tree_validate_move_target(owner, kind, item_id, target)
        impact = await self._tree_change_impact(owner, kind=kind, item_id=item_id, target_folder_id=target)
        return web.json_response({"ok": True, **self._tree_public_impact(impact)})

    async def _tree_archive_successor(self, owner: int, item_id: str) -> dict[str, Any] | None:
        """Find the next visible sibling (or previous at the end), across pages."""
        current = await self._conversation_row(owner, item_id, require=True)
        parent = str(current.get("folder_uuid") or "")
        cur = await self.db.conn.execute(
            """SELECT conversation_uuid, title, folder_uuid
               FROM web_conversations
               WHERE owner_chat_id=? AND COALESCE(folder_uuid,'')=?
                 AND (COALESCE(archived_at,0)=0 OR conversation_uuid=?)
               ORDER BY CASE WHEN COALESCE(pinned_at,0)>0 THEN 0 ELSE 1 END,
                        CASE WHEN display_order IS NULL THEN 1 ELSE 0 END,
                        display_order ASC, COALESCE(created_at,0) DESC, id DESC""",
            (owner, parent, item_id),
        )
        rows = [dict(row) for row in await cur.fetchall()]
        index = next((i for i, row in enumerate(rows) if row["conversation_uuid"] == item_id), -1)
        if index < 0 or len(rows) <= 1:
            return None
        sibling = rows[index + 1] if index + 1 < len(rows) else rows[index - 1]
        return {"conversationUuid": sibling["conversation_uuid"], "title": sibling["title"], "folderId": parent}

    async def _tree_reorder_entity(
        self,
        conn: Any,
        *,
        owner: int,
        kind: str,
        item_id: str,
        target_folder: str,
        before_id: str,
        after_id: str,
        force_reinsert: bool = False,
    ) -> None:
        if kind == "folder":
            table, id_col, parent_col = "web_conversation_folders", "folder_uuid", "parent_uuid"
        else:
            table, id_col, parent_col = "web_conversations", "conversation_uuid", "folder_uuid"
        cur = await conn.execute(
            f"SELECT * FROM {table} WHERE owner_chat_id=? AND {id_col}=? LIMIT 1",
            (owner, item_id),
        )
        moving = await cur.fetchone()
        if moving is None:
            raise web.HTTPNotFound(text=f"{kind}_not_found")
        moving = dict(moving)
        current_parent = str(moving.get(parent_col) or "")
        # A same-parent drop without a new destination, including a self-neighbor
        # sent by an older UI, is an idempotent no-op. Never change timestamps.
        if not force_reinsert and current_parent == target_folder and (
            not before_id and not after_id or before_id == item_id or after_id == item_id
        ):
            return
        if before_id == item_id or after_id == item_id:
            raise web.HTTPConflict(text="move_neighbor_stale")
        pinned = int(moving["pinned_at"] or 0) > 0
        archive_clause = ""
        if kind == "conversation":
            archive_clause = "AND COALESCE(archived_at,0)>0" if int(moving.get("archived_at") or 0)>0 else "AND COALESCE(archived_at,0)=0"
        pin_clause = "COALESCE(pinned_at,0)>0" if pinned else "COALESCE(pinned_at,0)=0"
        cur = await conn.execute(
            f"""SELECT id, {id_col} AS item_id, created_at FROM {table}
                WHERE owner_chat_id=? AND COALESCE({parent_col},'')=? AND {pin_clause} {archive_clause} AND {id_col}<>?
                ORDER BY CASE WHEN display_order IS NULL THEN 1 ELSE 0 END,
                         display_order ASC, COALESCE(created_at,0) DESC, id DESC""",
            (owner, target_folder, item_id),
        )
        rows = [dict(row) for row in await cur.fetchall()]
        positions = {str(row["item_id"]): index for index, row in enumerate(rows)}
        if before_id and after_id and (
            before_id not in positions or after_id not in positions or positions[before_id] >= positions[after_id]
        ):
            raise web.HTTPConflict(text="move_neighbor_stale")
        if before_id:
            if before_id not in positions:
                raise web.HTTPConflict(text="move_neighbor_stale")
            insert_at = positions[before_id] + 1
        elif after_id:
            if after_id not in positions:
                raise web.HTTPConflict(text="move_neighbor_stale")
            insert_at = positions[after_id]
        else:
            # Dropping on a folder has no explicit manual anchor. Insert by the
            # original creation rank; retain every other sibling's manual order.
            created_key = (int(moving.get("created_at") or 0), int(moving["id"]))
            insert_at = sum((int(row.get("created_at") or 0), int(row["id"])) > created_key for row in rows)
        rows.insert(insert_at, {"item_id": item_id})
        # Update by the stable public id; numeric table ids never leave the server.
        await conn.execute(
            f"UPDATE {table} SET {parent_col}=? WHERE owner_chat_id=? AND {id_col}=?",
            (target_folder, owner, item_id),
        )
        for index, row in enumerate(rows, start=1):
            await conn.execute(
                f"UPDATE {table} SET display_order=?, updated_at=? WHERE owner_chat_id=? AND {id_col}=?",
                (float(index * _TREE_ORDER_STEP), now_ts(), owner, str(row["item_id"])),
            )

    async def handle_api_conversation_tree_move(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        owner = int(session.chat_id)
        body = await self._json_body(request)
        kind = str(body.get("kind") or "")
        item_id = str(body.get("id") or "").strip()
        target = str(body.get("targetFolderId") or "").strip()
        before_id = str(body.get("beforeId") or "").strip()
        after_id = str(body.get("afterId") or "").strip()
        if before_id and after_id and before_id == after_id:
            return web.json_response({"ok": False, "error": "invalid_move_neighbors"}, status=400)
        async with self._conversation_tree_lock:
            row, _folders = await self._tree_validate_move_target(owner, kind, item_id, target)
            current_parent = str(row.get("parent_uuid" if kind == "folder" else "folder_uuid") or "")
            archived_conversation = kind == "conversation" and int(row.get("archived_at") or 0) > 0
            unarchive = archived_conversation and body.get("unarchive") is True
            refresh_snapshot = archived_conversation and body.get("updateSnapshots") is True
            impact = await self._tree_change_impact(
                owner, kind=kind, item_id=item_id, target_folder_id=target, force_snapshot=refresh_snapshot,
            ) if current_parent != target or refresh_snapshot else {
                "rows": [], "affectedCount": 0, "archivedCount": 0, "runningCount": 0, "updatableCount": 0, "cacheInvalidated": False,
            }

            async def mutate(conn: Any) -> None:
                if unarchive:
                    # Visibility, destination, order and optional snapshot change
                    # commit together. Reorder against active, not archived peers.
                    await conn.execute(
                        "UPDATE web_conversations SET archived_at=0 WHERE owner_chat_id=? AND conversation_uuid=?",
                        (owner, item_id),
                    )
                await self._tree_reorder_entity(
                    conn, owner=owner, kind=kind, item_id=item_id,
                    target_folder=target, before_id=before_id, after_id=after_id, force_reinsert=unarchive,
                )

            result = await self._tree_apply_snapshot_updates_locked(
                impact,
                update_snapshots=body.get("updateSnapshots") is True,
                mutate=mutate,
            )
        result["unarchived"] = unarchive
        await self.audit("web.conversation_tree.move", actor="web", chat_id=owner, ip=request.remote or "", detail={"kind": kind, "id": item_id, "targetFolderId": target, **result})
        return web.json_response({"ok": True, **self._tree_public_impact(impact), **result})

    async def handle_api_conversation_folder_delete(self, request: web.Request) -> web.Response:
        """Delete only the classification node; content is always migrated, never deleted."""
        session: WebSession = request[_WEB_SESSION_KEY]
        owner = int(session.chat_id)
        folder_id = str(request.match_info.get("folder_uuid") or "").strip()
        body = await self._json_body(request)
        target = str(body.get("targetFolderId") or "").strip()
        row, _folder_metadata = await self._tree_validate_move_target(owner, "folder", folder_id, target)
        folders = await self._tree_folders(owner, include_properties=True)
        if target == folder_id or target in self._tree_descendants(folder_id, folders):
            return web.json_response({"ok": False, "error": "folder_cycle"}, status=409)
        direct_children = [key for key, value in folders.items() if str(value.get("parent_uuid") or "") == folder_id]
        direct_conversations = await self._tree_scalar(
            "SELECT COUNT(*) FROM web_conversations WHERE owner_chat_id=? AND COALESCE(folder_uuid,'')=?",
            (owner, folder_id),
        )
        if (direct_children or direct_conversations) and "targetFolderId" not in body:
            return web.json_response({"ok": False, "error": "folder_not_empty", "childFolderCount": len(direct_children), "conversationCount": direct_conversations}, status=409)
        # Reparenting children makes every conversation formerly below this node a
        # candidate for inherited-value changes.
        impacted_rows: list[dict[str, Any]] = []
        folders_after = {key: dict(value) for key, value in folders.items()}
        for child in direct_children:
            folders_after[child]["parent_uuid"] = target
        for conv in await self._tree_conversation_rows(owner):
            conv_folder = str(conv.get("folder_uuid") or "")
            if conv_folder == folder_id:
                new_folder = target
            elif folder_id in self._tree_folder_path(conv_folder, folders):
                new_folder = conv_folder
            else:
                continue
            old_values = self._tree_effective_from_map(conv_folder, folders, str(getattr(self, "workspace_dir", "") or ""))[:2]
            new_values = self._tree_effective_from_map(new_folder, folders_after, str(getattr(self, "workspace_dir", "") or ""))[:2]
            if old_values != new_values:
                conv.update(old_values=old_values, new_values=new_values)
                impacted_rows.append(conv)
        running_count = 0
        for conv in impacted_rows:
            conv["running_now"] = await self._web_conversation_has_active_runtime(conv)
            running_count += int(bool(conv["running_now"]))
        impact = {
            "rows": impacted_rows,
            "affectedCount": len(impacted_rows),
            "archivedCount": sum(1 for conv in impacted_rows if int(conv.get("archived_at") or 0) > 0),
            "runningCount": running_count,
            "updatableCount": len(impacted_rows) - running_count,
            "cacheInvalidated": bool(impacted_rows),
        }
        if body.get("impactOnly") is True:
            return web.json_response({"ok": True, **self._tree_public_impact(impact), "nonEmpty": bool(direct_children or direct_conversations)})
        async with self._conversation_tree_lock:
            async def mutate(conn: Any) -> None:
                await conn.execute(
                    "UPDATE web_conversation_folders SET parent_uuid=?, updated_at=? WHERE owner_chat_id=? AND parent_uuid=?",
                    (target, now_ts(), owner, folder_id),
                )
                await conn.execute(
                    "UPDATE web_conversations SET folder_uuid=? WHERE owner_chat_id=? AND folder_uuid=?",
                    (target, owner, folder_id),
                )
                await conn.execute(
                    "DELETE FROM web_conversation_folders WHERE owner_chat_id=? AND folder_uuid=?",
                    (owner, folder_id),
                )
            result = await self._tree_apply_snapshot_updates_locked(
                impact, update_snapshots=body.get("updateSnapshots") is True, mutate=mutate
            )
        await self.audit("web.conversation_folder.delete", actor="web", chat_id=owner, ip=request.remote or "", detail={"folderId": folder_id, "targetFolderId": target, **result})
        return web.json_response({"ok": True, **self._tree_public_impact(impact), **result})
