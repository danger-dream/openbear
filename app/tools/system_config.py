"""SystemConfig: the model explains and changes OpenBear settings, prompt templates and models."""
from __future__ import annotations

import json
from typing import Any

from aiohttp import web

from app.admin import channels as channel_admin
from app.admin import settings as settings_admin
from app.assistant import changes
from app.db.engine import now_ts
from app.runtime.tool_result import ToolOutcome
from app.settings.specs import GROUPS, SPECS, WEB_DOMAINS
from app.tools.base import ToolRegistry, current_tool_context

RESOURCES = ("setting", "template", "model")
ROLES = {"main": "is_active", "agent": "is_agent_active", "assistant": "is_assistant_active"}
ROLE_LABELS = {"main": "主提示词", "agent": "Agent 提示词", "assistant": "页面助手提示词", "persona": "人设"}

DESCRIPTION = (
    "OpenBear's own configuration; resource selects the area.\n"
    "setting (设置 page): list gives the setting groups with each setting's path and title, or with query only the settings whose path, "
    "title, description or group contain every word. get gives settings by path (paths): title, description, type, allowed values or range, when a change "
    "takes effect, and the current value (sensitive values masked). set changes one setting (path, value as text: numbers, 开/关 for "
    "switches, comma-separated lists).\n"
    "template (提示词模板 page): list gives each template's id, name, roles (main, agent, assistant, persona) and size. get gives one "
    "template's full text by id, or the active one for role. create adds an inactive template (name, content). update changes name and/or "
    "content: the whole content, or find/replace for one exact, unique passage. activate makes the template the active one for role "
    "(main, agent, assistant), or with role=persona turns its persona use on or off (on). Content is checked by rendering before it is "
    "saved; changes reach new conversations, not the prompts existing conversations already saved.\n"
    "model (模型渠道 page): list gives the providers, their models and the primary model. add adds a model to a provider (provider, model: "
    "id plus optional name, contextWindow, maxTokens, thinkingLevels, defaultThinkingLevel, supportsFast, input, cost). update changes a "
    "model's fields (provider, id, model). sync_metadata fills a model's limits and prices from the models.dev catalog (provider, id)."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "resource": {"type": "string", "enum": list(RESOURCES)},
        "action": {"type": "string", "description": "setting: list|get|set; template: list|get|create|update|activate; model: list|add|update|sync_metadata"},
        "query": {"type": "string", "description": "setting list: words to look for"},
        "paths": {"type": "array", "items": {"type": "string"}, "description": "setting get"},
        "path": {"type": "string", "description": "setting set"},
        "value": {"type": "string", "description": "setting set: the new value as text"},
        "id": {"type": "string", "description": "template id, or model id"},
        "role": {"type": "string", "enum": ["main", "agent", "assistant", "persona"]},
        "on": {"type": "boolean", "description": "template activate with role=persona"},
        "name": {"type": "string", "description": "template name"},
        "content": {"type": "string", "description": "template content"},
        "find": {"type": "string"},
        "replace": {"type": "string"},
        "provider": {"type": "string", "description": "model: provider name"},
        "model": {"type": "object", "description": "model add/update: model fields"},
    },
    "required": ["resource", "action"],
    "additionalProperties": False,
}


def _ok(**data: Any) -> str:
    return json.dumps({"ok": True, **data}, ensure_ascii=False)


def _failed(error: str, message: str = "", **extra: Any) -> ToolOutcome:
    return ToolOutcome(json.dumps({"ok": False, "error": error, "message": message or error, **extra}, ensure_ascii=False),
                       "failed", "not_started")


class SystemConfig:
    def __init__(self, admin: Any) -> None:
        self.admin = admin

    async def _audit(self, kind: str, detail: dict[str, Any]) -> None:
        await self.admin.audit(kind, actor="model", chat_id=int(current_tool_context().chat_id or 0), detail=detail)

    async def run(self, args: dict[str, Any]) -> Any:
        resource, action = str(args.get("resource") or ""), str(args.get("action") or "")
        handler = getattr(self, f"_{resource}_{action}", None) if resource in RESOURCES else None
        if handler is None:
            return _failed("unknown_action", f"unknown {resource or 'resource'} action: {action}")
        try:
            return await handler(args)
        except ValueError as exc:
            return _failed("invalid", str(exc))

    # Settings

    def _setting_view(self, path: str, values: dict[str, Any]) -> dict[str, Any]:
        spec = settings_admin.serialize_spec(SPECS[path])
        view = {k: v for k, v in spec.items() if not (v is None or v is False or v == "" or v == [])}
        view.pop("variables", None)
        if len(str(view.get("defaultValue") or "")) > 400:
            view["defaultValue"] = str(view["defaultValue"])[:400] + "…"
        value = values.get(path)
        view["value"] = settings_admin.mask_secret(value) if spec["sensitive"] and value else value
        return view

    async def _setting_list(self, args: dict[str, Any]) -> Any:
        query = [w.lower() for w in str(args.get("query") or "").split() if w]
        if query:
            found = [
                {"path": p, "title": s.title, "desc": s.desc, "group": GROUPS.get(s.group, (s.group,))[0]}
                for p, s in SPECS.items()
                if all(w in f"{p} {s.title} {s.desc} {GROUPS.get(s.group, (s.group,))[0]}".lower() for w in query)
            ]
            return _ok(items=found[:60])
        domains = []
        for _key, (title, desc, sections) in WEB_DOMAINS.items():
            groups = [{"title": GROUPS[g][0], "settings": [{"path": p, "title": SPECS[p].title} for p in GROUPS[g][1] if p in SPECS]}
                      for g in sections if g in GROUPS]
            domains.append({"title": title, "desc": desc, "groups": groups})
        return _ok(domains=domains)

    async def _setting_get(self, args: dict[str, Any]) -> Any:
        paths = [str(p) for p in args.get("paths") or ([args["path"]] if args.get("path") else [])]
        unknown = [p for p in paths if p not in SPECS]
        if not paths or unknown:
            return _failed("unknown_setting", "use paths from setting list", unknown=unknown)
        values = settings_admin.safe_settings_payload(self.admin.config.model_dump(by_alias=True))["values"]
        return _ok(items=[self._setting_view(p, values) for p in paths])

    async def _setting_set(self, args: dict[str, Any]) -> Any:
        path = str(args.get("path") or "")
        if path not in SPECS:
            return _failed("unknown_setting", "use a path from setting list")
        if "value" not in args:
            return _failed("value_required", "set needs value")
        if not self.admin._config_writer_available():
            return _failed("config_store_unavailable", "settings cannot be saved in this process")
        stored = await self.admin._store_setting(path, args["value"])
        await self._audit("settings.update", {"path": path, "sensitive": settings_admin.is_sensitive_path(path), "noop": not stored})
        await changes.publish("setting", path)
        values = settings_admin.safe_settings_payload(self.admin.config.model_dump(by_alias=True))["values"]
        return _ok(setting=self._setting_view(path, values), effect=SPECS[path].effect)

    # Templates

    @staticmethod
    def _roles(row: Any) -> list[str]:
        roles = [role for role, column in ROLES.items() if row[column]]
        if row["is_persona"] and not row["is_agent_active"] and not row["is_assistant_active"]:
            roles.append("persona")
        return roles

    async def _template_row(self, template_id: Any) -> Any:
        if not str(template_id or "").isdigit():
            return None
        cur = await self.admin.db.conn.execute("SELECT * FROM memory_templates WHERE id=?", (int(template_id),))
        return await cur.fetchone()

    async def _template_list(self, args: dict[str, Any]) -> Any:
        cur = await self.admin.db.conn.execute(
            "SELECT id, name, is_active, is_agent_active, is_persona, is_assistant_active, length(content) AS chars, updated_at FROM memory_templates ORDER BY id")
        return _ok(items=[{"id": r["id"], "name": r["name"], "roles": self._roles(r), "chars": r["chars"], "updatedAt": r["updated_at"]}
                          for r in await cur.fetchall()], roles=ROLE_LABELS)

    async def _template_get(self, args: dict[str, Any]) -> Any:
        role = str(args.get("role") or "")
        if role in ROLES and not args.get("id"):
            cur = await self.admin.db.conn.execute(f"SELECT * FROM memory_templates WHERE {ROLES[role]}=1 ORDER BY id DESC LIMIT 1")
            row = await cur.fetchone()
        else:
            row = await self._template_row(args.get("id"))
        if row is None:
            return _failed("template_not_found", "use an id from template list")
        return _ok(template={"id": row["id"], "name": row["name"], "roles": self._roles(row), "content": row["content"]})

    async def _validated(self, content: str, name: str, *, agent: bool) -> str:
        return await self.admin._validate_memory_template(content, name, agent_active=agent)

    async def _check_memory_writable(self) -> ToolOutcome | None:
        guard = getattr(self.admin, "_require_memory_write_allowed", None)
        if guard is None:
            return None
        try:
            await guard()
        except web.HTTPConflict as exc:
            return _failed("memory_busy", exc.text or "memory operation is running")
        return None

    async def _template_create(self, args: dict[str, Any]) -> Any:
        if blocked := await self._check_memory_writable():
            return blocked
        name, content = str(args.get("name") or "新模板"), str(args.get("content") or "")
        if not content.strip():
            return _failed("content_required", "create needs content")
        error = await self._validated(content, name, agent=False)
        if error:
            return _failed("template_invalid", error)
        cur = await self.admin.db.conn.execute(
            "INSERT INTO memory_templates (name, content, is_active, is_agent_active, is_persona, is_assistant_active, updated_at) VALUES (?,?,0,0,0,0,?)",
            (name, content, now_ts()))
        await self.admin.db.conn.commit()
        template_id = int(cur.lastrowid or 0)
        await self._audit("memory_template.save", {"id": template_id, "name": name, "contentLength": len(content), "source": "model"})
        await changes.publish("template", template_id, "create")
        return _ok(template={"id": template_id, "name": name, "roles": [], "chars": len(content)})

    async def _template_update(self, args: dict[str, Any]) -> Any:
        if blocked := await self._check_memory_writable():
            return blocked
        row = await self._template_row(args.get("id"))
        if row is None:
            return _failed("template_not_found", "use an id from template list")
        name = str(args.get("name") or row["name"])
        content = str(row["content"])
        if "find" in args:
            find = str(args.get("find") or "")
            count = content.count(find) if find else 0
            if count != 1:
                return _failed("find_not_unique" if count else "find_not_found", "find must match exactly one passage", matches=count)
            content = content.replace(find, str(args.get("replace") or ""), 1)
        elif "content" in args:
            content = str(args.get("content") or "")
        error = await self._validated(content, name, agent=bool(row["is_agent_active"] and not row["is_active"]))
        if error:
            return _failed("template_invalid", error)
        await self.admin.db.conn.execute("UPDATE memory_templates SET name=?, content=?, updated_at=? WHERE id=?",
                                         (name, content, now_ts(), int(row["id"])))
        await self.admin.db.conn.commit()
        await self._audit("memory_template.save", {"id": int(row["id"]), "name": name, "contentLength": len(content), "source": "model"})
        await changes.publish("template", row["id"])
        return _ok(template={"id": row["id"], "name": name, "roles": self._roles(row), "chars": len(content)})

    async def _template_activate(self, args: dict[str, Any]) -> Any:
        if blocked := await self._check_memory_writable():
            return blocked
        row = await self._template_row(args.get("id"))
        role = str(args.get("role") or "")
        if row is None:
            return _failed("template_not_found", "use an id from template list")
        if role == "persona":
            if row["is_agent_active"] or row["is_assistant_active"]:
                return _failed("persona_unavailable", "an Agent or page assistant template cannot be a persona")
            await self.admin.db.conn.execute("UPDATE memory_templates SET is_persona=?, updated_at=? WHERE id=?",
                                             (1 if args.get("on", True) else 0, now_ts(), int(row["id"])))
        elif role in ROLES:
            error = await self._validated(str(row["content"]), str(row["name"]), agent=role == "agent")
            if error:
                return _failed("template_invalid", error)
            column = ROLES[role]
            await self.admin.db.conn.execute(f"UPDATE memory_templates SET {column}=0")
            await self.admin.db.conn.execute(f"UPDATE memory_templates SET {column}=1, updated_at=? WHERE id=?", (now_ts(), int(row["id"])))
        else:
            return _failed("role_required", "role is main, agent, assistant or persona")
        await self.admin.db.conn.commit()
        await self._audit("memory_template.save", {"id": int(row["id"]), "role": role, "on": bool(args.get("on", True)), "source": "model"})
        await changes.publish("template", row["id"])
        return _ok(id=row["id"], role=role, label=ROLE_LABELS[role])

    # Models

    async def _mutate(self, mutator: Any, kind: str, detail: dict[str, Any]) -> None:
        if not self.admin._config_writer_available():
            raise ValueError("模型配置在这个进程里无法保存")
        self.admin._apply_runtime_config(await self.admin.config_store.mutate(mutator))
        await self._audit(kind, detail)

    async def _model_list(self, args: dict[str, Any]) -> Any:
        models = self.admin.config.models
        providers = []
        for name, provider in models.providers.items():
            providers.append({
                "name": name, "protocol": provider.protocol, "enabled": bool(provider.enabled), "baseUrl": provider.base_url,
                "models": [{"id": m.id, "name": m.name, "contextWindow": m.context_window, "maxTokens": m.max_tokens,
                            "thinkingLevels": list(m.thinking_levels or []), "supportsFast": bool(m.supports_fast)}
                           for m in provider.models],
            })
        return _ok(primary=models.primary, providers=providers)

    async def _model_add(self, args: dict[str, Any]) -> Any:
        provider = str(args.get("provider") or "")
        model = args.get("model") if isinstance(args.get("model"), dict) else {}
        await self._mutate(channel_admin.create_model_mutator(provider, model), "channels.model.create",
                           {"provider": provider, "model": model.get("id"), "source": "model"})
        await changes.publish("model", f"{provider}/{model.get('id')}", "create")
        return _ok(model=f"{provider}/{model.get('id')}")

    async def _model_update(self, args: dict[str, Any]) -> Any:
        provider, model_id = str(args.get("provider") or ""), str(args.get("id") or "")
        patch = args.get("model") if isinstance(args.get("model"), dict) else {}
        if "id" in patch and str(patch["id"]) != model_id:
            return _failed("rename_unsupported", "renaming a model is done on the 模型渠道 page, which also moves its history")
        await self._mutate(channel_admin.update_model_mutator(provider, model_id, patch), "channels.model.update",
                           {"provider": provider, "model": model_id, "fields": sorted(patch), "source": "model"})
        await changes.publish("model", f"{provider}/{model_id}")
        return _ok(model=f"{provider}/{model_id}", fields=sorted(patch))

    async def _model_sync_metadata(self, args: dict[str, Any]) -> Any:
        provider, model_id = str(args.get("provider") or ""), str(args.get("id") or "")
        preview = channel_admin.models_dev_sync_preview(self.admin.config.models, provider, model_id,
                                                        getattr(self.admin, "models_dev_catalog", None))
        if not preview["changes"]:
            return _ok(model=f"{provider}/{model_id}", changes=[], note="already matches models.dev")
        await self._mutate(channel_admin.sync_model_from_models_dev_mutator(
            provider, model_id, source=preview["source"], metadata=preview["metadata"],
            catalog_sha256=str(preview.get("catalog", {}).get("sha256") or ""), metadata_sha256=str(preview.get("metadataSha256") or ""),
        ), "channels.model.models_dev_sync", {"provider": provider, "model": model_id, "source": "model",
                                              "changedFields": [item["field"] for item in preview["changes"]]})
        await changes.publish("model", f"{provider}/{model_id}")
        return _ok(model=f"{provider}/{model_id}", source=preview["source"], changes=preview["changes"])


def register_system_config_tool(registry: ToolRegistry, admin: Any) -> None:
    config = SystemConfig(admin)
    registry.add("SystemConfig", DESCRIPTION, SCHEMA, config.run, visibility={"main"})
