"""Extension tool groups: which optional tools a conversation's controller is given.

Base tools are always present. An extension is a group of tools switched on or off
per conversation; a conversation without a stored state has every extension on.
The controller reads its tool list again before every model request, so a change
takes effect from the next request; it is stored before it is published.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.utils import estimate_tokens

TEMPORARY = "__temporary"


@dataclass(frozen=True)
class Extension:
    key: str
    label: str
    about: str  # for the model's catalog
    tools: tuple[str, ...]
    hint: str = ""  # for the user's panel


BUILTIN = (
    Extension("workitems", "任务面板", "the task panel: record problems, decisions, progress and evidence of the work", ("WorkItem",),
              "记录问题、决策、进展和验收依据"),
    Extension("agents", "Agent 协作", "delegate work packages to independent Agents and supervise them",
              ("Agent", "AgentContinue", "AgentMessage", "AgentStop", "AgentWait", "AgentInfo", "AgentPlanDecision"),
              "把工作交给独立的 Agent 并监督它们"),
    Extension("conversations", "会话协作", "send requests to other conversations and lead conversation groups (with agents: replies are awaited with Agent wait)",
              ("ConversationRequest", "ConversationGroup"), "向其他会话发请求，组建和带领会话小组；会一并开启 Agent 协作，用它等待对方的结果"),
    Extension("browser", "浏览器", "a remote browser with existing logins, and the browsers of server nodes", ("Browser",),
              "操作远程浏览器和服务器节点的浏览器"),
    Extension("servers", "服务器", "monitored servers: commands, files, the mesh network, dynamic DNS and benchmarks", ("Servers",),
              "在服务器上执行命令、传文件，管理组网、动态域名和评测"),
    Extension("instances", "实例", "managed OpenBear Docker instances: deployment and maintenance", ("InstanceControl",),
              "部署和维护 OpenBear 实例"),
    Extension("automation", "自动化", "scheduled tasks (Cron) and external event triggers (Webhook)", ("Cron", "Webhook"),
              "定时任务和外部事件触发（Webhook）"),
)


# An extension whose tools only work with another one: on together, and off together with it.
REQUIRES = {"conversations": ("agents",)}


def with_required(enabled) -> set[str]:
    out = set(enabled)
    for key in list(out):
        out.update(REQUIRES.get(key, ()))
    return out


def dependents(keys) -> set[str]:
    return {key for key, needs in REQUIRES.items() if set(needs) & set(keys)}


class ExtensionError(Exception):
    def __init__(self, code: str, message: str = "", **details: Any) -> None:
        super().__init__(message or code)
        self.payload = {"ok": False, "error": code, "message": message or code, **details}


@dataclass(frozen=True)
class State:
    enabled: frozenset[str]
    auto_allow: bool
    revision: int


def schema_name(schema: dict[str, Any]) -> str:
    return str(schema.get("name") or schema.get("function", {}).get("name") or "")


def catalog(names) -> list[Extension]:
    """The extensions present among these tool names: the built-in groups, then one per MCP server."""
    present = set(names)
    out = [e for e in BUILTIN if present & set(e.tools)]
    servers: dict[str, list[str]] = {}
    for name in names:
        parts = str(name).split("__", 2)
        if len(parts) == 3 and parts[0] == "mcp" and parts[1]:
            servers.setdefault(parts[1], []).append(name)
    for server, tools in servers.items():
        short = ", ".join(t.split("__", 2)[2] for t in tools)
        out.append(Extension(f"mcp:{server}", server, f"MCP server {server}: {short}", tuple(tools),
                             f"MCP 服务：{short}"))
    return out


_USAGE = re.compile(r"^<!-- extension: ([A-Za-z0-9_:.\-]+) -->[ \t]*\n(.*?)^<!-- /extension -->[ \t]*\n?", re.M | re.S)


def split_usage(system: str) -> tuple[str, dict[str, str]]:
    """The system prompt without its `<!-- extension: key -->` blocks, and those blocks by key.

    Key mcp holds what every MCP server shares; mcp:<server> what one server adds.
    """
    usage: dict[str, str] = {}

    def take(match):
        usage[match.group(1)] = "\n\n".join(v for v in (usage.get(match.group(1), ""), match.group(2).strip()) if v)
        return ""
    return _USAGE.sub(take, system), usage


def with_usage(schemas: list[dict[str, Any]], usage: dict[str, str],
               extensions: list[Extension] | tuple[Extension, ...] = ()) -> list[dict[str, Any]]:
    """Add each present extension's usage to the description of its first tool in the list."""
    if not usage and not extensions:
        return schemas
    out = list(schemas)
    index = {schema_name(s): i for i, s in enumerate(out)}
    if not usage and extensions and "RequestExtension" in index:
        lines = "\n".join(f"- {e.key} ({e.label}): {e.about}" for e in extensions)
        out[index["RequestExtension"]] = _described(
            out[index["RequestExtension"]],
            "\n\nAvailable extensions (call RequestExtension with the key and reason when needed, "
            "without asking through UserInteraction first):\n" + lines,
        )
    if not usage:
        return out
    shared_mcp = usage.get("mcp", "")
    for e in catalog(list(index)):
        parts = [usage.get(e.key, "")]
        if e.key.startswith("mcp:") and shared_mcp:
            parts.insert(0, shared_mcp)
            shared_mcp = ""
        text = "\n\n".join(p for p in parts if p)
        if text:
            i = index[next(t for t in e.tools if t in index)]
            out[i] = _described(out[i], f"\n\nHow to use this extension ({e.key}):\n{text}")
    return out


def _described(schema: dict[str, Any], extra: str) -> dict[str, Any]:
    if "function" in schema:
        fn = dict(schema["function"])
        fn["description"] = str(fn.get("description") or "") + extra
        return {**schema, "function": fn}
    return {**schema, "description": str(schema.get("description") or "") + extra}


def hidden_tools(names, state: State | None) -> set[str]:
    if state is None:
        return set()
    return {tool for e in catalog(names) if e.key not in state.enabled for tool in e.tools}


class ExtensionService:
    def __init__(self, db: Any, host: Any = None) -> None:
        self.db = db
        self.host = host
        self._states: dict[str, State | None] = {}
        # One update of a conversation at a time: each computes the new set from the stored one.
        self._update_locks: dict[str, asyncio.Lock] = {}
        # extension key -> async check(conversation row) returning why it cannot be turned off now, or "".
        self.in_use: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {}
        # Tool names and schemas of the live registry; the registry is rebuilt on MCP and skill reloads.
        self.names: Callable[[], list[str]] = lambda: []
        self.schemas: Callable[[], list[dict[str, Any]]] = lambda: []
        # Usage blocks of the latest controller run, for sizing extensions of a conversation not yet created.
        self.last_usage: dict[str, str] = {}

    def catalog(self) -> list[Extension]:
        return catalog(self.names())

    async def state(self, conversation_uuid: str) -> State | None:
        if conversation_uuid in self._states:
            return self._states[conversation_uuid]
        cur = await self.db.conn.execute("SELECT * FROM conversation_extensions WHERE conversation_uuid=?", (conversation_uuid,))
        row = await cur.fetchone()
        state = _state(row) if row else None
        self._states[conversation_uuid] = state
        return state

    def cached(self, conversation_uuid: str) -> State | None:
        return self._states.get(conversation_uuid)

    def revision(self, conversation_uuid: str) -> int:
        state = self._states.get(conversation_uuid)
        return state.revision if state else 0

    async def filter_schemas(self, conversation_uuid: str, schemas: list[dict[str, Any]],
                             usage: dict[str, str] | None = None) -> list[dict[str, Any]]:
        hidden = hidden_tools([schema_name(s) for s in schemas], await self.state(conversation_uuid))
        return with_usage([s for s in schemas if schema_name(s) not in hidden], usage or {}, self.catalog())

    def effective(self, state: State | None) -> list[str]:
        keys = [e.key for e in self.catalog()]
        return keys if state is None else [k for k in keys if k in state.enabled]

    async def describe(self, conversation_uuid: str) -> dict[str, Any]:
        """The panel's view: every extension, whether it is on, and about how many tokens it adds to each request."""
        state = await self.state(conversation_uuid)
        enabled = set(self.effective(state))
        cur = await self.db.conn.execute(
            "SELECT s.system_snapshot FROM web_conversations w LEFT JOIN sessions s ON s.chat_id=w.internal_chat_id WHERE w.conversation_uuid=?",
            (conversation_uuid,))
        row = await cur.fetchone()
        usage = split_usage(str(row["system_snapshot"] or "") if row else "")[1]
        return {"ok": True, "stored": state is not None, "autoAllow": bool(state and state.auto_allow),
                "revision": state.revision if state else 0, "extensions": self.panel(enabled, usage)}

    def panel(self, enabled: set[str], usage: dict[str, str] | None = None) -> list[dict[str, Any]]:
        """Each extension for the panel, with about how many tokens it adds to every request."""
        usage = self.last_usage if usage is None else usage
        sizes = {schema_name(s): estimate_tokens(json.dumps(s, ensure_ascii=False)) for s in self.schemas()}

        def tokens(e: Extension) -> int:
            text = usage.get(e.key, "") + (usage.get("mcp", "") if e.key.startswith("mcp:") else "")
            return sum(sizes.get(t, 0) for t in e.tools) + estimate_tokens(text)
        return [{"key": e.key, "label": e.label, "hint": e.hint, "about": e.about, "requires": list(REQUIRES.get(e.key, ())),
                 "tools": [t for t in e.tools if t in sizes], "tokens": tokens(e), "enabled": e.key in enabled}
                for e in self.catalog()]

    def chosen(self, value: Any) -> dict[str, Any]:
        """A new conversation's extensions as chosen before it existed; raises on anything malformed."""
        keys = {e.key for e in self.catalog()}
        enabled = value.get("enabled") if isinstance(value, dict) else None
        auto_allow = value.get("autoAllow", False) if isinstance(value, dict) else None
        if not isinstance(enabled, list) or any(not isinstance(k, str) for k in enabled) or not isinstance(auto_allow, bool):
            raise ExtensionError("invalid_extensions", "extensions must be {enabled: [keys], autoAllow: boolean}")
        # An extension gone since the draft was made (an MCP server removed) is dropped, not refused.
        return {"enabled": sorted(with_required(k for k in enabled if k in keys)), "autoAllow": auto_allow}

    async def update(self, conversation_uuid: str, *, enable=(), disable=(), auto_allow: bool | None = None,
                     note: str = "") -> State:
        """Change a conversation's extensions; refused while a disabled one is still in use."""
        async with self._update_locks.setdefault(conversation_uuid, asyncio.Lock()):
            return await self._update(conversation_uuid, enable, disable, auto_allow, note)

    async def _update(self, conversation_uuid: str, enable, disable, auto_allow: bool | None, note: str) -> State:
        row = await self._conversation(conversation_uuid)
        keys = {e.key: e for e in self.catalog()}
        unknown = [k for k in (*enable, *disable) if k not in keys]
        if unknown:
            raise ExtensionError("unknown_extension", f"未知扩展：{', '.join(unknown)}", extensions=list(keys))
        state = await self.state(conversation_uuid)
        current = set(self.effective(state))
        off = set(disable) | dependents(disable)
        enabled = with_required(current | set(enable)) - off
        turning_off = [e.key for e in self.catalog() if e.key in current - enabled]
        turning_on = [e.key for e in self.catalog() if e.key in enabled - current]
        for key in turning_off:
            check = self.in_use.get(key)
            reason = await check(row) if check else ""
            if reason:
                raise ExtensionError("extension_in_use", f"{keys[key].label}正在使用：{reason}", extension=key)
        before = bool(state and state.auto_allow)
        allow = before if auto_allow is None else bool(auto_allow)
        if state is not None and enabled == set(state.enabled) and allow == state.auto_allow:
            return state
        saved = await self._save(conversation_uuid, sorted(enabled), allow)
        changes = [f"开启{keys[k].label}" for k in turning_on] + [f"关闭{keys[k].label}" for k in turning_off]
        if allow != before:
            changes.append("自动允许模型开启扩展：" + ("开" if allow else "关"))
        if changes:
            await self._notice(row, "扩展：" + "、".join(changes) + (f"（{note}）" if note else ""))
        return saved

    async def _save(self, conversation_uuid: str, enabled: list[str], auto_allow: bool) -> State:
        async with self.db.conn.transaction(label="conversation-extensions") as conn:
            await conn.execute(
                "INSERT INTO conversation_extensions(conversation_uuid, enabled_json, auto_allow, revision, updated_at_ms) VALUES(?,?,?,1,?) "
                "ON CONFLICT(conversation_uuid) DO UPDATE SET enabled_json=excluded.enabled_json, auto_allow=excluded.auto_allow, "
                "revision=conversation_extensions.revision+1, updated_at_ms=excluded.updated_at_ms",
                (conversation_uuid, json.dumps(enabled), int(auto_allow), _now()))
            cur = await conn.execute("SELECT * FROM conversation_extensions WHERE conversation_uuid=?", (conversation_uuid,))
            state = _state(await cur.fetchone())
        self._states[conversation_uuid] = state
        return state

    async def insert_initial(self, conn: Any, conversation_uuid: str, value: dict[str, Any] | None) -> None:
        """Store a new conversation's starting state inside its creation transaction; None keeps every extension on."""
        self._states.pop(conversation_uuid, None)
        if value is None:
            return
        await conn.execute(
            "INSERT OR REPLACE INTO conversation_extensions(conversation_uuid, enabled_json, auto_allow, revision, updated_at_ms) VALUES(?,?,?,1,?)",
            (conversation_uuid, json.dumps(sorted(with_required(value.get("enabled") or []))), int(bool(value.get("autoAllow"))), _now()))

    async def folder_default(self, owner: int, folder_uuid: str, *, inherited: bool = False) -> dict[str, Any]:
        """The nearest folder setting up the tree (inherited: above this folder); without one every extension starts off."""
        current, seen = folder_uuid or TEMPORARY, set()
        while current and current not in seen:
            seen.add(current)
            cur = await self.db.conn.execute(
                "SELECT * FROM folder_extension_defaults WHERE folder_uuid=? AND owner_chat_id=?", (current, owner))
            row = None if inherited and len(seen) == 1 else await cur.fetchone()
            if row:
                return {"enabled": json.loads(row["enabled_json"]), "autoAllow": bool(row["auto_allow"]), "from": current, "explicit": True}
            if current == TEMPORARY:
                break
            cur = await self.db.conn.execute(
                "SELECT parent_uuid FROM web_conversation_folders WHERE folder_uuid=? AND owner_chat_id=?", (current, owner))
            parent = await cur.fetchone()
            current = str(parent["parent_uuid"] or "") if parent else ""
        return {"enabled": [], "autoAllow": False, "from": "", "explicit": False}

    async def set_folder_default(self, owner: int, folder_uuid: str, value: dict[str, Any] | None) -> dict[str, Any]:
        """Set a folder's default, or remove it (None) so the folder inherits again."""
        folder = folder_uuid or TEMPORARY
        if value is None:
            await self.db.conn.execute("DELETE FROM folder_extension_defaults WHERE folder_uuid=? AND owner_chat_id=?", (folder, owner))
        else:
            keys = {e.key for e in self.catalog()}
            enabled = sorted(with_required(value.get("enabled") or []))
            unknown = [k for k in enabled if k not in keys]
            if unknown:
                raise ExtensionError("unknown_extension", f"未知扩展：{', '.join(unknown)}", extensions=sorted(keys))
            await self.db.conn.execute(
                "INSERT INTO folder_extension_defaults(folder_uuid, owner_chat_id, enabled_json, auto_allow, updated_at_ms) VALUES(?,?,?,?,?) "
                "ON CONFLICT(folder_uuid) DO UPDATE SET enabled_json=excluded.enabled_json, auto_allow=excluded.auto_allow, updated_at_ms=excluded.updated_at_ms",
                (folder, owner, json.dumps(enabled), int(bool(value.get("autoAllow"))), _now()))
        await self.db.conn.commit()
        return await self.folder_default(owner, folder)

    async def for_new(self, owner: int, folder_uuid: str) -> dict[str, Any]:
        default = await self.folder_default(owner, folder_uuid)
        return {"enabled": default["enabled"], "autoAllow": default["autoAllow"]}

    async def for_unattended(self, owner: int, folder_uuid: str, declared: list[str] | None) -> dict[str, Any] | None:
        """A scheduled or event run: what its task declares, else an explicit folder setting, else everything (None)."""
        default = await self.folder_default(owner, folder_uuid)
        if declared is not None:
            return {"enabled": list(declared), "autoAllow": default["autoAllow"] if default["explicit"] else False}
        return {"enabled": default["enabled"], "autoAllow": default["autoAllow"]} if default["explicit"] else None

    async def copy_of(self, conversation_uuid: str) -> dict[str, Any] | None:
        state = await self.state(conversation_uuid)
        return None if state is None else {"enabled": sorted(state.enabled), "autoAllow": state.auto_allow}

    async def _conversation(self, conversation_uuid: str) -> dict[str, Any]:
        cur = await self.db.conn.execute("SELECT * FROM web_conversations WHERE conversation_uuid=?", (conversation_uuid,))
        row = await cur.fetchone()
        if not row:
            raise ExtensionError("conversation_not_found")
        return dict(row)

    async def _notice(self, row: dict[str, Any], text: str) -> None:
        live_for = getattr(self.host, "_live_for", None)
        if callable(live_for):
            await live_for(row).publish({"type": "notice", "text": text, "eventUuid": "extensions:" + uuid.uuid4().hex})


class ExtensionToolRegistry:
    """The controller's view of a registry: base tools plus the conversation's enabled extensions, in schema and dispatch."""

    def __init__(self, registry: Any, service: ExtensionService, conversation_uuid: str,
                 usage: dict[str, str] | None = None) -> None:
        self.registry = registry
        self.service = service
        self.conversation_uuid = conversation_uuid
        self.usage = usage or {}
        self.file_state = getattr(registry, "file_state", None)
        self._seen = -1

    def __getattr__(self, name: str) -> Any:
        return getattr(self.registry, name)

    def _hidden(self, names) -> set[str]:
        return hidden_tools(names, self.service.cached(self.conversation_uuid))

    def schemas(self, scope=None):
        out = self.registry.schemas(scope=scope)
        if scope not in (None, "main"):
            return out
        self._seen = self.service.revision(self.conversation_uuid)
        hidden = self._hidden([schema_name(s) for s in out])
        return with_usage([s for s in out if schema_name(s) not in hidden], self.usage, self.service.catalog())

    def schemas_if_changed(self, scope=None):
        """New schemas when the conversation's extensions changed since the last schemas() call, else None."""
        if self.service.revision(self.conversation_uuid) == self._seen:
            return None
        return self.schemas(scope)

    def names(self, scope=None, source=None):
        names = self.registry.names(scope=scope, source=source)
        hidden = self._hidden(names) if scope in (None, "main") else set()
        return [n for n in names if n not in hidden]

    def summaries(self, scope=None, source=None):
        summaries = self.registry.summaries(scope=scope, source=source)
        hidden = self._hidden(list(summaries)) if scope in (None, "main") else set()
        return {k: v for k, v in summaries.items() if k not in hidden}

    async def dispatch(self, name, arguments, **kwargs):
        names = self.service.names()
        if name in self._hidden(names):
            from app.runtime.tool_result import ToolOutcome
            from app.tools.base import current_tool_context
            key = next(e.key for e in catalog(names) if name in e.tools)
            context = kwargs.get("context") or current_tool_context()
            result = ToolOutcome(json.dumps({"ok": False, "error": "extension_not_enabled", "extension": key,
                                             "message": f"{name} belongs to the extension {key}, which is off in this conversation; "
                                                        "request it with RequestExtension"}, ensure_ascii=False), "failed", "not_started")
            context.tool_outcome = result
            return result.content
        return await self.registry.dispatch(name, arguments, **kwargs)


def _state(row: Any) -> State:
    return State(frozenset(json.loads(row["enabled_json"] or "[]")), bool(row["auto_allow"]), int(row["revision"]))


def _now() -> int:
    return int(time.time() * 1000)
