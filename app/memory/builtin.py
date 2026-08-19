"""OpenBear 内置记忆后端。

第一版目标：用主 SQLite 取代运行时 prompt-memory 依赖，提供同名
Memory 工具接口，并用轻量模板引擎构建 system prompt。
"""
from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.db.engine import DB, now_ts

_REMOVED_STRUCTURAL_CATEGORIES = frozenset({"identity", "persona", "rule"})

_DEFAULT_CATEGORIES = [
    ("network", "网络", "🌐", 40),
    ("service", "已部署服务", "🧩", 50),
    ("project", "项目", "📁", 60),
    ("tools", "工具", "🛠", 70),
    ("memory", "记忆", "🧠", 80),
]

MAIN_TEMPLATE_NAME = "OpenBear-v3.2"
AGENT_TEMPLATE_NAME = "Agent基础提示词-v8"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_INSTALL_META = Path("data/install-meta.json")

_FALLBACK_TEMPLATE = """You are OpenBear, a capable AI assistant operating inside a private Web console. Speak Chinese by default.

Built-in tools: [[ ', '.join(builtinToolNames) ]]
@if mcpToolNames
MCP tools: [[ ', '.join(mcpToolNames) ]]
@endif
Workspace: [[ workspaceDir ]]
"""


def _read_prompt(*relative: str, fallback: str = "") -> str:
    for path in (Path.cwd().joinpath(*relative), _REPO_ROOT.joinpath(*relative)):
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return fallback


def _apply_display_name(content: str) -> str:
    """Seed-time substitution only. Tracked prompt files stay unchanged."""
    if not _INSTALL_META.is_file():
        return content
    try:
        raw = json.loads(_INSTALL_META.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return content
    name = str(raw.get("displayName") or "").strip()
    if not name or name == "老大":
        return content
    return content.replace("**UserName**: 老大", f"**UserName**: {name}", 1)


def slugify_ref(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[\s_]+", "-", value)
    value = re.sub(r"[^0-9a-z\-\u4e00-\u9fff]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "memory"


def _json_loads(value: str, default: Any) -> Any:
    try:
        if value in (None, ""):
            return default
        return json.loads(value)
    except Exception:
        return default


class AttrDict(dict):
    def __getattr__(self, name: str) -> Any:
        if name == "length":
            return len(self)
        try:
            return self[name]
        except KeyError:
            return ""


class AttrList(list):
    @property
    def length(self) -> int:
        return len(self)


def _wrap(value: Any) -> Any:
    if isinstance(value, dict):
        return AttrDict({k: _wrap(v) for k, v in value.items()})
    if isinstance(value, list):
        return AttrList([_wrap(v) for v in value])
    return value


class _Helpers:
    @staticmethod
    def noteSuffix(note: str) -> str:  # noqa: N802 - 模板兼容 prompt-memory 命名
        note = str(note or "").strip()
        return f"（{note}）" if note else ""

    @staticmethod
    def has(arr: Any, item: Any) -> bool:
        return isinstance(arr, (list, tuple, set, AttrList)) and item in arr

    @staticmethod
    def join(arr: Any, sep: str = "\n") -> str:
        return sep.join(str(x) for x in arr) if isinstance(arr, (list, tuple, AttrList)) else ""

    @staticmethod
    def ownerLine(ownerNumbers: Any) -> str:  # noqa: N802
        nums = [str(v).strip() for v in (ownerNumbers or []) if str(v).strip()]
        if not nums:
            return ""
        return f"Authorized senders: {', '.join(nums)}. These senders are allowlisted; do not assume they are the owner."

    @staticmethod
    def runtimeLine(ri: Any, defaultThink: str = "off") -> str:  # noqa: N802
        ri = _wrap(ri or {})
        parts = [
            f"agent={ri.agentId}" if ri.agentId else "",
            f"host={ri.host}" if ri.host else "",
            f"os={ri.os}{f' ({ri.arch})' if ri.arch else ''}" if ri.os else (f"arch={ri.arch}" if ri.arch else ""),
            f"model={ri.model}" if ri.model else "",
        ]
        return "Runtime: " + " | ".join(p for p in parts if p)

    @staticmethod
    def toolLines(toolNames: Any, toolSummaries: Any = None) -> str:  # noqa: N802
        names = [str(t).strip() for t in (toolNames or []) if str(t).strip()]
        if not names:
            return ""
        summaries = toolSummaries or {}
        out = []
        for name in names:
            summary = summaries.get(name) if isinstance(summaries, dict) else ""
            out.append(f"- {name}: {summary}" if summary else f"- {name}")
        return "\n".join(out)

    @staticmethod
    def mcpGroupLines(groups: Any) -> str:  # noqa: N802
        """Render a compact MCP catalog without duplicating tool descriptions."""
        out: list[str] = []
        for raw in groups or []:
            item = raw if isinstance(raw, dict) else {}
            server = str(item.get("server") or "mcp").strip() or "mcp"
            count = int(item.get("toolCount") or item.get("count") or 0)
            exact = str(item.get("exactToolName") or "").strip()
            namespace = str(item.get("namespacePrefix") or "").strip()
            if count == 1 and exact:
                out.append(f"- {server}: 1 tool; exact callable name `{exact}`")
            elif count > 0 and namespace:
                out.append(f"- {server}: {count} tools; public namespace prefix `{namespace}`")
            elif count > 0:
                out.append(f"- {server}: {count} tools")
        return "\n".join(out)

    @staticmethod
    def literalBlock(value: Any) -> str:  # noqa: N802
        """Wrap arbitrary dynamic text in a collision-safe Markdown code fence."""
        text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
        longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
        fence = "`" * max(3, longest + 1)
        return f"{fence}\n{text}\n{fence}"


class TemplateEngine:
    """兼容 prompt-memory 常用模板语法：[[ expr ]] / @if / @each / @raw。"""

    _expr_re = re.compile(r"\[\[\s*(.*?)\s*\]\]")
    _each_re = re.compile(r"@each\s+(\w+)\s+in\s+(.+)")

    def render(self, template: str, context: dict[str, Any]) -> str:
        ctx = {k: _wrap(v) for k, v in context.items()}
        ctx.setdefault("helpers", _Helpers())
        lines = template.splitlines()
        rendered = self._render_block(lines, ctx, 0, len(lines))
        return re.sub(r"\n{3,}", "\n\n", "\n".join(rendered))

    def _translate_expr(self, expr: str) -> str:
        # prompt-memory 模板表达式是 JS 风格；这里只做已用语法的保守转换，
        # 不改模板正文，只让原表达式按旧引擎语义继续工作。
        expr = re.sub(r"\s+\|\|\s+", " or ", expr)
        expr = re.sub(r"\s+&&\s+", " and ", expr)
        return expr

    def _eval(self, expr: str, ctx: dict[str, Any]) -> Any:
        safe_builtins = {"len": len, "str": str, "int": int, "float": float, "bool": bool, "list": list}
        return eval(self._translate_expr(expr), {"__builtins__": safe_builtins}, ctx)  # noqa: S307 - 管理员模板，非用户输入

    def _interpolate(self, line: str, ctx: dict[str, Any]) -> str:
        def repl(match: re.Match[str]) -> str:
            try:
                value = self._eval(match.group(1), ctx)
            except Exception as exc:
                value = f"[[ERROR: {type(exc).__name__}]]"
            return str(value if value is not None else "")

        return self._expr_re.sub(repl, line)

    def _find_matching(self, lines: list[str], start: int, open_kw: str, close_kw: str, else_kw: str = "") -> tuple[int, int | None]:
        depth = 0
        else_at: int | None = None
        for i in range(start, len(lines)):
            stripped = lines[i].strip()
            if stripped.startswith(open_kw):
                depth += 1
            elif stripped == close_kw:
                if depth == 0:
                    return i, else_at
                depth -= 1
            elif else_kw and stripped == else_kw and depth == 0 and else_at is None:
                else_at = i
        raise ValueError(f"missing {close_kw}")

    def _render_block(self, lines: list[str], ctx: dict[str, Any], start: int, end: int) -> list[str]:
        out: list[str] = []
        i = start
        while i < end:
            line = lines[i]
            stripped = line.strip()
            if stripped.startswith("@raw"):
                j = i + 1
                while j < end and lines[j].strip() != "@endraw":
                    out.append(lines[j])
                    j += 1
                i = j + 1
                continue
            if stripped.startswith("@if "):
                close_at, else_at = self._find_matching(lines, i + 1, "@if ", "@endif", "@else")
                cond = bool(self._eval(stripped[4:].strip(), ctx))
                if cond:
                    block_end = else_at if else_at is not None else close_at
                    out.extend(self._render_block(lines, ctx, i + 1, block_end))
                elif else_at is not None:
                    out.extend(self._render_block(lines, ctx, else_at + 1, close_at))
                i = close_at + 1
                continue
            if stripped.startswith("@each "):
                match = self._each_re.fullmatch(stripped)
                if not match:
                    raise ValueError(f"bad each syntax: {stripped}")
                var_name, expr = match.groups()
                close_at, _else_at = self._find_matching(lines, i + 1, "@each ", "@endeach")
                items = self._eval(expr, ctx) or []
                for item in items:
                    child = dict(ctx)
                    child[var_name] = _wrap(item)
                    out.extend(self._render_block(lines, child, i + 1, close_at))
                i = close_at + 1
                continue
            if stripped in {"@endif", "@endeach", "@else", "@endraw"}:
                i += 1
                continue
            out.append(self._interpolate(line, ctx))
            i += 1
        return out


@dataclass(slots=True)
class _Template:
    id: int
    name: str
    content: str


class BuiltinMemoryClient:
    def __init__(self, db: DB, *, identity: str = "openbear") -> None:
        self._db = db
        self._identity = identity
        self._bootstrapped = False
        self._engine = TemplateEngine()

    async def close(self) -> None:
        return None

    async def _bootstrap(self) -> None:
        if self._bootstrapped:
            return
        for key, name, icon, sort in _DEFAULT_CATEGORIES:
            await self._db.conn.execute(
                """
                INSERT INTO memory_categories (key, name, icon, render_type, schema_json, inject, sort)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET name=excluded.name, icon=excluded.icon, sort=excluded.sort
                """,
                (key, name, icon, "fields", '{"fields":[]}', 1, sort),
            )
        cur = await self._db.conn.execute("SELECT COUNT(*) AS n FROM memory_templates")
        row = await cur.fetchone()
        if not row or int(row["n"] or 0) == 0:
            await self._db.conn.execute(
                "INSERT INTO memory_templates (name, content, is_active, is_agent_active, updated_at) VALUES (?,?,1,0,?)",
                (MAIN_TEMPLATE_NAME, self._default_template(), now_ts()),
            )
            agent_content = self._default_agent_template()
            if agent_content.strip():
                await self._db.conn.execute(
                    "INSERT INTO memory_templates (name, content, is_active, is_agent_active, updated_at) VALUES (?,?,0,1,?)",
                    (AGENT_TEMPLATE_NAME, agent_content, now_ts()),
                )
        await self._db.conn.commit()
        self._bootstrapped = True

    def _default_template(self) -> str:
        return _apply_display_name(_read_prompt("prompts", "openbear-system.tpl", fallback=_FALLBACK_TEMPLATE))

    def _default_agent_template(self) -> str:
        return _read_prompt("prompts", "openbear-agent.tpl", fallback="")

    async def build_system_prompt(self, params: dict[str, Any] | None = None) -> str:
        return await self.render_system_prompt(params or {}, source="runtime")

    async def render_system_prompt(
        self,
        params: dict[str, Any] | None = None,
        *,
        template_content: str | None = None,
        template_name: str = "preview override",
        source: str = "preview",
    ) -> str:
        await self._bootstrap()
        template = (
            _Template(id=0, name=template_name, content=template_content)
            if template_content is not None else await self._active_template()
        )
        ctx = await self._main_template_context(params or {})
        return await self._render_template(template, ctx, source=source, params=params or {})

    async def render_agent_system_prompt(self, params: dict[str, Any] | None = None) -> str:
        """Render the explicitly selected Rath Agent base system prompt.

        Agent templates intentionally receive a smaller context than the main
        OpenBear template.  Main-only roots such as MCP, skills, and available
        Agents are not provided at all; templates that reference them should
        fail visibly instead of seeing misleading empty arrays.
        """
        await self._bootstrap()
        template = await self._agent_active_template()
        if template is None:
            return ""
        ctx = await self._agent_template_context(params or {})
        return await self._render_template(template, ctx, source="agent_runtime", params=params or {})

    async def _main_template_context(self, params: dict[str, Any]) -> dict[str, Any]:
        memory = await self._memory_object()
        ctx = dict(params or {})
        ctx.setdefault("memory", memory)
        ctx.setdefault("toolSummaries", {})
        ctx.setdefault("builtinToolNames", ctx.get("toolNames") or [])
        ctx.setdefault("builtinToolSummaries", ctx.get("toolSummaries") or {})
        ctx.setdefault("mcpToolNames", [])
        ctx.setdefault("mcpToolSummaries", {})
        ctx.setdefault("mcpToolGroups", [])
        ctx.setdefault("mcpServerInstructions", [])
        ctx.setdefault("skillsPrompt", "")
        ctx.setdefault("docsPath", "")
        ctx.setdefault("userTimezone", "")
        ctx.setdefault("heartbeatPrompt", "")
        ctx.setdefault("modelAliasLines", [])
        ctx.setdefault("reactionGuidance", None)
        ctx.setdefault("sandboxInfo", None)
        ctx.setdefault("ownerNumbers", [])
        ctx.setdefault("toolNames", [])
        ctx.setdefault("availableAgents", [])
        ctx.setdefault("agents", {"available": ctx.get("availableAgents") or []})
        ctx.setdefault("reasoningLevel", "off")
        ctx.setdefault("defaultThinkLevel", "off")
        ctx.setdefault("runtimeInfo", {})
        ctx.setdefault("workspaceDir", "")
        return ctx

    async def _agent_template_context(self, params: dict[str, Any]) -> dict[str, Any]:
        memory = await self._memory_object()
        ctx = dict(params or {})
        for main_only_key in ("mcpToolNames", "mcpToolSummaries", "mcpToolGroups", "mcpServerInstructions", "skillsPrompt", "availableAgents", "agents"):
            ctx.pop(main_only_key, None)
        tool_names = list(ctx.get("toolNames") or [])
        tool_summaries = dict(ctx.get("toolSummaries") or {})
        builtin_summaries = dict(ctx.get("builtinToolSummaries") or tool_summaries)
        ctx["memory"] = memory
        ctx["toolNames"] = tool_names
        ctx["toolSummaries"] = tool_summaries
        ctx["builtinToolNames"] = tool_names
        ctx["builtinToolSummaries"] = builtin_summaries
        ctx["tools"] = {
            "allowlist": tool_names,
            "summaries": tool_summaries,
            "builtin": {"names": tool_names, "summaries": builtin_summaries},
        }
        ctx.setdefault("reasoningLevel", "off")
        ctx.setdefault("defaultThinkLevel", "off")
        ctx.setdefault("runtimeInfo", {})
        ctx.setdefault("workspaceDir", "")
        ctx.setdefault("templateEngine", {
            "name": "openbear-template-lite",
            "supportedSyntax": ["[[ expr ]]", "@if expr", "@else", "@endif", "@each item in expr", "@endeach", "@raw"],
        })
        return ctx

    async def _render_template(self, template: _Template, ctx: dict[str, Any], *, source: str, params: dict[str, Any]) -> str:
        start = time.perf_counter()
        output = self._engine.render(template.content, ctx)
        await self._db.conn.execute(
            """
            INSERT INTO memory_render_logs (ts, params_json, output, output_len, source, template_id, template_name, ms)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                now_ts(),
                json.dumps(params or {}, ensure_ascii=False),
                output,
                len(output),
                source,
                template.id,
                template.name,
                int((time.perf_counter() - start) * 1000),
            ),
        )
        await self._db.conn.commit()
        return output

    async def tool_call(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        await self._bootstrap()
        endpoint = endpoint.strip().lower()
        if endpoint in {"identities", "identity"}:
            return {"ok": True, "current": self._identity, "items": [{"key": self._identity, "name": self._identity}]}
        if endpoint == "entry":
            return await self._entry_tool(payload)
        if endpoint == "doc":
            return await self._doc_tool(payload)
        if endpoint == "secret":
            return await self._secret_tool(payload)
        return {"ok": False, "error": f"unsupported endpoint: {endpoint}"}

    async def _active_template(self) -> _Template:
        cur = await self._db.conn.execute(
            "SELECT id, name, content FROM memory_templates WHERE is_active=1 ORDER BY id DESC LIMIT 1"
        )
        row = await cur.fetchone()
        if row:
            return _Template(id=int(row["id"]), name=str(row["name"]), content=str(row["content"]))
        return _Template(id=0, name="fallback", content=self._default_template())

    async def _agent_active_template(self) -> _Template | None:
        cur = await self._db.conn.execute(
            "SELECT id, name, content FROM memory_templates WHERE is_agent_active=1 ORDER BY id DESC LIMIT 1"
        )
        row = await cur.fetchone()
        if row:
            return _Template(id=int(row["id"]), name=str(row["name"]), content=str(row["content"]))
        return None

    async def _categories(self) -> dict[str, dict[str, Any]]:
        cur = await self._db.conn.execute("SELECT * FROM memory_categories ORDER BY sort, id")
        return {str(r["key"]): {k: r[k] for k in r.keys()} for r in await cur.fetchall()}

    async def _category_id(self, key: str) -> int:
        key = (key or "memory").strip() or "memory"
        await self._bootstrap()
        cur = await self._db.conn.execute("SELECT id FROM memory_categories WHERE key=?", (key,))
        row = await cur.fetchone()
        if row:
            return int(row["id"])
        await self._db.conn.execute(
            "INSERT INTO memory_categories (key, name, icon, sort) VALUES (?,?,?,?)",
            (key, key, "🧠", 999),
        )
        await self._db.conn.commit()
        cur = await self._db.conn.execute("SELECT id FROM memory_categories WHERE key=?", (key,))
        row = await cur.fetchone()
        return int(row["id"])

    async def _memory_object(self) -> dict[str, Any]:
        categories = await self._categories()
        cur = await self._db.conn.execute(
            """
            SELECT e.*, c.key AS category_key, c.name AS category_name
            FROM memory_entries e JOIN memory_categories c ON c.id=e.category_id
            WHERE e.enabled=1 AND e.archived=0 AND c.inject=1
            ORDER BY e.expanded DESC, c.sort, e.sort, e.id
            """
        )
        entries = [self._entry_row(r) for r in await cur.fetchall()]
        by_cat: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in entries:
            by_cat[item["category"]].append(item)
        env_entries = []
        for cat in ("network", "service", "project", "memory"):
            env_entries.extend(by_cat.get(cat, []))
        expanded_entries = [item for item in entries if item["expanded"]]
        indexed_env_entries = [item for item in env_entries if not item["expanded"]]
        indexed_tool_entries = [item for item in by_cat.get("tools", []) if not item["expanded"]]
        cur = await self._db.conn.execute(
            "SELECT name, note FROM memory_secrets WHERE enabled=1 AND archived=0 ORDER BY sort, id"
        )
        secret_names = [{"name": r["name"], "note": r["note"]} for r in await cur.fetchall()]
        cur = await self._db.conn.execute(
            "SELECT name, title, summary, project, tags FROM memory_docs WHERE enabled=1 AND archived=0 ORDER BY sort, id"
        )
        doc_names = [{k: r[k] for k in r.keys()} for r in await cur.fetchall()]
        return {
            "categories": list(categories.values()),
            # Compatibility empties keep existing templates renderable until the
            # administrator removes old structural loops from template content.
            "byCat": {
                "identity": [],
                "persona": [],
                "rule": [],
                "tools": by_cat.get("tools", []),
                "memory": env_entries,
            },
            "ruleGroups": [],
            "expandedEntries": expanded_entries,
            "groupsByCat": {
                "memory": self._groups(indexed_env_entries, default_name=""),
                "tools": self._groups(indexed_tool_entries, default_name=""),
            },
            "secretNames": secret_names,
            "docNames": doc_names,
        }

    def _groups(self, entries: list[dict[str, Any]], *, default_name: str) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        order: list[str] = []
        for item in entries:
            name = str(item.get("grp") or default_name)
            if name not in grouped:
                order.append(name)
            grouped[name].append(item)
        return [{"name": name, "entries": grouped[name]} for name in order]

    def _entry_row(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "category": row["category_key"],
            "categoryName": row["category_name"],
            "grp": row["grp"] or "",
            "ref": row["ref"] or "",
            "note": row["note"] or "",
            "title": row["title"] or "",
            "fields": _json_loads(row["fields_json"] or "{}", {}),
            "fieldsJson": row["fields_json"] or "{}",
            "body": row["body"] or "",
            "expanded": bool(row["expanded"]),
            "enabled": bool(row["enabled"]),
            "archived": bool(row["archived"]),
            "sort": int(row["sort"] or 0),
            "createdAt": int(row["created_at"] or 0),
            "updatedAt": int(row["updated_at"] or 0),
        }

    async def _entry_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        action = str(args.get("action") or "list").lower()
        if action == "list":
            category = str(args.get("category") or "").strip()
            scope = str(args.get("scope") or "").strip().lower()
            include_archived = bool(args.get("includeArchived") or args.get("archived") or args.get("showArchived"))
            sql = """
                SELECT e.*, c.key AS category_key, c.name AS category_name
                FROM memory_entries e JOIN memory_categories c ON c.id=e.category_id
                WHERE 1=1
            """
            params: list[Any] = []
            if not include_archived:
                sql += " AND e.archived=0"
            if category:
                sql += " AND c.key=?"
                params.append(category)
                order_by = "e.expanded DESC, e.sort, e.id"
            elif scope == "tools":
                sql += " AND c.key='tools'"
                order_by = "e.expanded DESC, e.sort, e.id"
            elif scope == "memory":
                sql += " AND c.key<>'tools'"
                order_by = "e.expanded DESC, e.sort, e.id"
            else:
                order_by = "e.expanded DESC, c.sort, e.sort, e.id"
            sql += f" ORDER BY {order_by}"
            cur = await self._db.conn.execute(sql, params)
            return {"ok": True, "items": [self._entry_row(r) for r in await cur.fetchall()]}
        if action == "get":
            row = await self._find_entry(args)
            return {"ok": bool(row), "item": self._entry_row(row) if row else None, "error": "not_found" if not row else ""}
        if action == "set":
            return await self._set_entry(args)
        if action == "del":
            row = await self._find_entry(args)
            if not row:
                return {"ok": False, "error": "not_found"}
            await self._db.conn.execute("DELETE FROM memory_entries WHERE id=?", (row["id"],))
            await self._db.conn.commit()
            return {"ok": True, "deleted": True, "id": row["id"]}
        return {"ok": False, "error": f"unsupported action: {action}"}

    async def _find_entry(self, args: dict[str, Any]):
        entry_id = int(args.get("id") or 0)
        if entry_id:
            cur = await self._db.conn.execute(
                """
                SELECT e.*, c.key AS category_key, c.name AS category_name
                FROM memory_entries e JOIN memory_categories c ON c.id=e.category_id WHERE e.id=?
                """,
                (entry_id,),
            )
            return await cur.fetchone()
        ref = str(args.get("ref") or "").removeprefix("@mem/").strip()
        name = str(args.get("name") or "").strip()
        if ref:
            cur = await self._db.conn.execute(
                """
                SELECT e.*, c.key AS category_key, c.name AS category_name
                FROM memory_entries e JOIN memory_categories c ON c.id=e.category_id WHERE e.ref=?
                """,
                (ref,),
            )
            return await cur.fetchone()
        if name:
            cur = await self._db.conn.execute(
                """
                SELECT e.*, c.key AS category_key, c.name AS category_name
                FROM memory_entries e JOIN memory_categories c ON c.id=e.category_id WHERE e.title=?
                """,
                (name,),
            )
            return await cur.fetchone()
        return None

    async def _set_entry(self, args: dict[str, Any]) -> dict[str, Any]:
        existing = await self._find_entry(args)
        title = str(args.get("name") or args.get("title") or (existing["title"] if existing else "")).strip()
        if not title:
            return {"ok": False, "error": "name_required"}
        category_key = str(args.get("category") or (existing["category_key"] if existing else "memory")).strip()
        if category_key in _REMOVED_STRUCTURAL_CATEGORIES:
            return {"ok": False, "error": "category_removed"}
        category_id = await self._category_id(category_key)
        old_ref = str(existing["ref"] or "") if existing else ""
        ref = str(args.get("ref") or old_ref or slugify_ref(title)).removeprefix("@mem/").strip()
        ref = slugify_ref(ref)
        fields_json = str(args.get("fieldsJson") if "fieldsJson" in args else (existing["fields_json"] if existing else "{}"))
        try:
            json.loads(fields_json or "{}")
        except Exception:
            return {"ok": False, "error": "fieldsJson must be JSON object"}
        values = (
            category_id,
            str(args.get("grp") if "grp" in args else (existing["grp"] if existing else "")),
            ref,
            str(args.get("note") if "note" in args else (existing["note"] if existing else "")),
            title,
            fields_json or "{}",
            str(args.get("body") if "body" in args else (existing["body"] if existing else "")),
            int(bool(args.get("expanded", existing["expanded"] if existing else False))),
            int(bool(args.get("enabled", existing["enabled"] if existing else True))),
            int(bool(args.get("archived", existing["archived"] if existing else False))),
            int(args.get("sort") if "sort" in args else (existing["sort"] if existing else 0)),
            now_ts(),
        )
        if existing:
            await self._db.conn.execute(
                """
                UPDATE memory_entries SET category_id=?, grp=?, ref=?, note=?, title=?, fields_json=?, body=?,
                  expanded=?, enabled=?, archived=?, sort=?, updated_at=? WHERE id=?
                """,
                (*values, existing["id"]),
            )
            if old_ref and old_ref != ref:
                await self._rename_entry_refs(old_ref, ref)
            entry_id = int(existing["id"])
        else:
            cur = await self._db.conn.execute(
                """
                INSERT INTO memory_entries (category_id, grp, ref, note, title, fields_json, body, expanded, enabled, archived, sort, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (*values[:-1], values[-1], values[-1]),
            )
            entry_id = int(cur.lastrowid or 0)
        await self._db.conn.commit()
        row = await self._find_entry({"id": entry_id})
        return {"ok": True, "item": self._entry_row(row)}

    async def _rename_entry_refs(self, old_ref: str, new_ref: str) -> None:
        old = f"@mem/{old_ref}"
        new = f"@mem/{new_ref}"
        await self._db.conn.execute(
            "UPDATE memory_entries SET body=replace(body, ?, ?), updated_at=? WHERE body LIKE ?",
            (old, new, now_ts(), f"%{old}%"),
        )
        await self._db.conn.execute(
            "UPDATE memory_docs SET content=replace(content, ?, ?), updated_at=? WHERE content LIKE ?",
            (old, new, now_ts(), f"%{old}%"),
        )

    async def _doc_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        action = str(args.get("action") or "list").lower()
        if action == "list":
            include_archived = bool(args.get("includeArchived") or args.get("archived") or args.get("showArchived"))
            where = "" if include_archived else "WHERE archived=0"
            cur = await self._db.conn.execute(
                f"SELECT id, name, title, summary, project, importance, tags, grp, enabled, archived, sort, created_at, updated_at FROM memory_docs {where} ORDER BY sort, id"
            )
            return {"ok": True, "items": [{k: r[k] for k in r.keys()} for r in await cur.fetchall()]}
        if action == "get":
            row = await self._find_doc(args)
            return {"ok": bool(row), "item": {k: row[k] for k in row.keys()} if row else None, "error": "not_found" if not row else ""}
        if action == "set":
            return await self._set_doc(args)
        if action == "del":
            row = await self._find_doc(args)
            if not row:
                return {"ok": False, "error": "not_found"}
            await self._db.conn.execute("DELETE FROM memory_docs WHERE id=?", (row["id"],))
            await self._db.conn.commit()
            return {"ok": True, "deleted": True, "id": row["id"]}
        return {"ok": False, "error": f"unsupported action: {action}"}

    async def _find_doc(self, args: dict[str, Any]):
        doc_id = int(args.get("id") or 0)
        if doc_id:
            cur = await self._db.conn.execute("SELECT * FROM memory_docs WHERE id=?", (doc_id,))
            return await cur.fetchone()
        name = str(args.get("name") or "").removeprefix("@doc/").strip()
        if name:
            cur = await self._db.conn.execute("SELECT * FROM memory_docs WHERE name=?", (name,))
            return await cur.fetchone()
        return None

    async def _set_doc(self, args: dict[str, Any]) -> dict[str, Any]:
        existing = await self._find_doc(args)
        name = str(args.get("name") or (existing["name"] if existing else "")).strip()
        if not name:
            return {"ok": False, "error": "name_required"}
        values = (
            name,
            str(args.get("title") if "title" in args else (existing["title"] if existing else name)),
            str(args.get("summary") if "summary" in args else (existing["summary"] if existing else "")),
            str(args.get("project") if "project" in args else (existing["project"] if existing else "")),
            int(args.get("importance") if "importance" in args else (existing["importance"] if existing else 3)),
            str(args.get("tags") if "tags" in args else (existing["tags"] if existing else "")),
            str(args.get("grp") if "grp" in args else (existing["grp"] if existing else "")),
            int(bool(args.get("enabled", existing["enabled"] if existing else True))),
            int(bool(args.get("archived", existing["archived"] if existing else False))),
            int(args.get("sort") if "sort" in args else (existing["sort"] if existing else 0)),
            str(args.get("content") if "content" in args else (existing["content"] if existing else "")),
            now_ts(),
        )
        if existing:
            await self._db.conn.execute(
                """
                UPDATE memory_docs SET name=?, title=?, summary=?, project=?, importance=?, tags=?, grp=?, enabled=?, archived=?, sort=?, content=?, updated_at=? WHERE id=?
                """,
                (*values, existing["id"]),
            )
            doc_id = int(existing["id"])
        else:
            cur = await self._db.conn.execute(
                """
                INSERT INTO memory_docs (name, title, summary, project, importance, tags, grp, enabled, archived, sort, content, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (*values[:-1], values[-1], values[-1]),
            )
            doc_id = int(cur.lastrowid or 0)
        await self._db.conn.commit()
        row = await self._find_doc({"id": doc_id})
        return {"ok": True, "item": {k: row[k] for k in row.keys()}}

    async def _secret_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        action = str(args.get("action") or "list").lower()
        if action == "list":
            include_archived = bool(args.get("includeArchived") or args.get("archived") or args.get("showArchived"))
            where = "" if include_archived else "WHERE archived=0"
            cur = await self._db.conn.execute(
                f"SELECT id, name, note, grp, enabled, archived, sort, created_at, updated_at FROM memory_secrets {where} ORDER BY sort, id"
            )
            return {"ok": True, "items": [{k: r[k] for k in r.keys()} for r in await cur.fetchall()]}
        if action == "get":
            row = await self._find_secret(args)
            if not row:
                return {"ok": False, "error": "not_found", "item": None}
            item = {k: row[k] for k in row.keys()}
            item["kv"] = _json_loads(row["kv_json"], [])
            return {"ok": True, "item": item}
        if action == "set":
            return await self._set_secret(args)
        if action == "del":
            row = await self._find_secret(args)
            if not row:
                return {"ok": False, "error": "not_found"}
            await self._db.conn.execute("DELETE FROM memory_secrets WHERE id=?", (row["id"],))
            await self._db.conn.commit()
            return {"ok": True, "deleted": True, "id": row["id"]}
        return {"ok": False, "error": f"unsupported action: {action}"}

    async def _find_secret(self, args: dict[str, Any]):
        sec_id = int(args.get("id") or 0)
        if sec_id:
            cur = await self._db.conn.execute("SELECT * FROM memory_secrets WHERE id=?", (sec_id,))
            return await cur.fetchone()
        name = str(args.get("name") or "").removeprefix("@secret/").strip()
        if name:
            cur = await self._db.conn.execute("SELECT * FROM memory_secrets WHERE name=?", (name,))
            return await cur.fetchone()
        return None

    async def _set_secret(self, args: dict[str, Any]) -> dict[str, Any]:
        existing = await self._find_secret(args)
        name = str(args.get("name") or (existing["name"] if existing else "")).strip()
        if not name:
            return {"ok": False, "error": "name_required"}
        kv_json = str(args.get("kvJson") if "kvJson" in args else (existing["kv_json"] if existing else "[]"))
        kv = _json_loads(kv_json, None)
        if not isinstance(kv, list):
            return {"ok": False, "error": "kvJson must be JSON array"}
        values = (
            name,
            str(args.get("note") if "note" in args else (existing["note"] if existing else "")),
            json.dumps(kv, ensure_ascii=False),
            str(args.get("grp") if "grp" in args else (existing["grp"] if existing else "")),
            int(bool(args.get("enabled", existing["enabled"] if existing else True))),
            int(bool(args.get("archived", existing["archived"] if existing else False))),
            int(args.get("sort") if "sort" in args else (existing["sort"] if existing else 0)),
            now_ts(),
        )
        if existing:
            await self._db.conn.execute(
                "UPDATE memory_secrets SET name=?, note=?, kv_json=?, grp=?, enabled=?, archived=?, sort=?, updated_at=? WHERE id=?",
                (*values, existing["id"]),
            )
            sec_id = int(existing["id"])
        else:
            cur = await self._db.conn.execute(
                "INSERT INTO memory_secrets (name, note, kv_json, grp, enabled, archived, sort, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (*values[:-1], values[-1], values[-1]),
            )
            sec_id = int(cur.lastrowid or 0)
        await self._db.conn.commit()
        row = await self._find_secret({"id": sec_id})
        item = {k: row[k] for k in row.keys()}
        item["kv"] = _json_loads(row["kv_json"], [])
        return {"ok": True, "item": item}
