from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from aiohttp import web

from app.references import (
    ReferenceError,
    ReferenceMaterials,
    parse_references,
    reference_display_text,
    reference_occurrences,
)
from app.tools.history import _group_turns, _visible_history_items
from app.reference_policy import CONVERSATION_CONTENT_LIMIT
from app.web_console.core import _WEB_SESSION_KEY


class WebAdminReferenceMixin:
    def _reference_store(self):
        if not hasattr(self, "_reference_material_store"):
            self._reference_material_store = ReferenceMaterials(self.db)
        return self._reference_material_store

    def _reference_budget(self, conversation=None):
        model = str((conversation or {}).get("model") or self.config.models.primary or "")
        resolved = self.config.models.resolve(model)
        if not resolved:
            return 60000
        definition = resolved[1]
        window = int(definition.context_window or 128000)
        reserve = max(2048, int(definition.max_tokens or 8192))
        return max(1000, min(200000, window - reserve - 4096))

    async def _prepare_reference_bundle(self, row, text, op_id, *, existing_keys=None):
        if not parse_references(text):
            return "", []
        resolved = await self._reference_store().resolve(
            text,
            owner=int(row["owner_chat_id"]),
            conversation_uuid=str(row["conversation_uuid"]),
            budget=self._reference_budget(row),
            existing_keys=existing_keys,
        )
        bundle_id = await self._reference_store().save(
            resolved, conversation_uuid=str(row["conversation_uuid"]), op_id=op_id
        )
        return bundle_id, [{**item, "bundleId": bundle_id} for item in resolved["bindings"]]

    async def handle_api_reference_preview(self, request):
        owner = int(request[_WEB_SESSION_KEY].chat_id)
        body = await self._json_body(request)
        conversation_uuid = str(body.get("conversationUuid") or "")
        row = None
        if conversation_uuid and conversation_uuid != "local:new":
            row = await self._conversation_row(owner, conversation_uuid)
            if not row:
                return web.json_response(
                    {"ok": False, "error": "conversation_not_found"}, status=404
                )
        try:
            result = await self._reference_store().resolve(
                str(body.get("text") or ""),
                owner=owner,
                conversation_uuid=str((row or {}).get("conversation_uuid") or ""),
                budget=self._reference_budget(row),
                existing_keys=[key for key in (body.get("existingKeys") or []) if isinstance(key, str)],
            )
            # A preview is metadata only; opening an @ picker must never pull
            # full credential values into the browser, logs or localStorage.
            return web.json_response(
                {
                    "ok": True,
                    "items": result["manifest"],
                    "bindings": result["bindings"],
                    "conversationContentLimit": CONVERSATION_CONTENT_LIMIT,
                    "bodyChars": result["bodyChars"],
                    "estimatedTokens": result["estimatedTokens"],
                },
                headers={"Cache-Control": "no-store"},
            )
        except ReferenceError as exc:
            return web.json_response(
                {"ok": False, "error": exc.code, "referenceError": exc.public()},
                status=422,
                headers={"Cache-Control": "no-store"},
            )

    async def handle_api_reference_inspect(self, request):
        owner = int(request[_WEB_SESSION_KEY].chat_id)
        body = await self._json_body(request)
        text = str(body.get("text") or "")
        try:
            refs = parse_references(text)
        except ReferenceError as exc:
            return web.json_response({"ok": False, "error": exc.code}, status=422)
        if len(refs) != 1:
            return web.json_response({"ok": False, "error": "reference_unavailable"}, status=400)
        try:
            bundle_id = str(body.get("bundleId") or "")
            mention = refs[0].get("mode") == "mention"
            if bundle_id:
                columns = "b.manifest_json" if mention else "b.manifest_json,b.material_json"
                cursor = await self.db.conn.execute(
                    f"SELECT {columns} FROM web_reference_bundles b JOIN web_conversations c ON c.conversation_uuid=b.conversation_uuid WHERE b.bundle_uuid=? AND c.owner_chat_id=?",
                    (bundle_id, owner),
                )
                row = await cursor.fetchone()
                manifest = json.loads(row["manifest_json"]) if row else []
                item = next((item for item in manifest if item.get("key") == refs[0]["key"]), None)
                if item is None:
                    item = next((item for item in manifest if item.get("modeReason") and item.get("key") == refs[0]["key"] + ":mention"), None)
                if item is None:
                    raise ReferenceError("reference_unavailable", refs[0]["label"])
                mention = item.get("mode") == "mention"
                material = next(
                    (
                        m
                        for m in ([] if mention else json.loads(row["material_json"]))
                        if m["reference"]["key"] == item["key"]
                    ),
                    {},
                )
                content = str(material.get("content") or "")
            else:
                result = await self._reference_store().resolve(text, owner=owner, budget=200000)
                item = result["manifest"][0]
                mention = item.get("mode") == "mention"
                content = "" if mention or item["sensitive"] else result["materials"][0]["content"]
            return web.json_response(
                {
                    "ok": True,
                    "item": item,
                    "content": content[:60000],
                    "previewTruncated": len(content) > 60000,
                    "frozen": bool(bundle_id),
                    "conversationContentLimit": CONVERSATION_CONTENT_LIMIT,
                },
                headers={"Cache-Control": "no-store"},
            )
        except ReferenceError as exc:
            return web.json_response(
                {"ok": False, "error": exc.code, "referenceError": exc.public()},
                status=422,
                headers={"Cache-Control": "no-store"},
            )

    async def handle_api_reference_history(self, request):
        owner = int(request[_WEB_SESSION_KEY].chat_id)
        conv_uuid = str(request.match_info["conversation_uuid"])
        if not await self._conversation_row(owner, conv_uuid):
            return web.json_response({"ok": False, "error": "conversation_not_found"}, status=404)
        try:
            offset = max(0, int(request.query.get("offset", "0")))
        except ValueError:
            return web.json_response({"ok": False, "error": "invalid_offset"}, status=400)
        turns_only = request.query.get("view") == "turns"

        def read():
            with sqlite3.connect(
                Path(self.db.path).resolve().as_uri() + "?mode=ro", uri=True
            ) as conn:
                conn.row_factory = sqlite3.Row
                visible = _visible_history_items(conn, conv_uuid)
                turns = _group_turns(visible)
                items = []
                for turn in reversed(turns):
                    label = next(
                        (item.text for item in turn.items if item.role == "user"), "本轮问答"
                    )
                    items.append(
                        {
                            "kind": "turn",
                            "id": conv_uuid,
                            "itemId": turn.turn_uuid,
                            "key": f"turn:{conv_uuid}:{turn.turn_uuid}",
                            "label": reference_display_text(label)[:100],
                            "group": "整轮问答",
                            "bodyChars": sum(len(item.text) for item in turn.items),
                        }
                    )
                    if turns_only:
                        continue
                    for message in reversed(turn.items):
                        items.append(
                            {
                                "kind": "message",
                                "id": conv_uuid,
                                "itemId": message.op_id,
                                "key": f"message:{conv_uuid}:{message.op_id}",
                                "label": reference_display_text(message.text)[:100],
                                "bodyChars": len(message.text),
                                "group": "用户消息" if message.role == "user" else "助手回答",
                            }
                        )
                return {
                    "ok": True,
                    "items": items[offset : offset + 50],
                    "hasMore": len(items) > offset + 50,
                    "nextOffset": offset + 50,
                    "conversationContentLimit": CONVERSATION_CONTENT_LIMIT,
                }

        return web.json_response(
            await asyncio.to_thread(read), headers={"Cache-Control": "no-store"}
        )
