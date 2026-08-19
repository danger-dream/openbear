"""AnySearch-backed web search tools.

The implementation intentionally shells out to the bundled AnySearch CLI instead
of adding a new Python dependency.  API keys stay inside the skill .env / env and
are redacted defensively from tool output.
"""
from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from typing import Any

from app.tools.base import ToolRegistry

_TIMEOUT_S = 45.0
_OUTPUT_LIMIT = 120_000
_SECRET_PATTERNS = (
    re.compile(r"(api[_-]?key|authorization|bearer)\s*[:=]\s*['\"]?([A-Za-z0-9._\-]{12,})", re.I),
    re.compile(r"(auto_registered[^\n]{0,80}api_key[^\n:=]*[:=]\s*)['\"]?([A-Za-z0-9._\-]{12,})", re.I),
)


def _skill_cli(skills_dir: str) -> tuple[str, Path] | None:
    root = Path(skills_dir or "./skills").expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    cli = root / "anysearch" / "scripts" / "anysearch_cli.js"
    node = shutil.which("node")
    if node and cli.exists():
        return node, cli
    return None


def _redact(text: str) -> str:
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(lambda m: f"{m.group(1)}[已隐藏]", out)
    return out


async def _run_anysearch(skills_dir: str, args: list[str]) -> str:
    found = _skill_cli(skills_dir)
    if found is None:
        return "error: AnySearch CLI 不可用：缺少 node 或 skills/anysearch/scripts/anysearch_cli.js"
    node, cli = found
    proc = await asyncio.create_subprocess_exec(
        node,
        str(cli),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cli.parent.parent),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        return "error: AnySearch 请求超时"
    text = stdout.decode("utf-8", "replace").strip()
    err = stderr.decode("utf-8", "replace").strip()
    merged = text if proc.returncode == 0 else (err or text or f"AnySearch exited with code {proc.returncode}")
    merged = _redact(merged)
    if len(merged) > _OUTPUT_LIMIT:
        merged = merged[:_OUTPUT_LIMIT] + "\n…[AnySearch 输出过长已截断]"
    return merged or "[空结果]"


def register_web_search_tools(reg: ToolRegistry, *, skills_dir: str) -> None:
    async def web_search(args: dict[str, Any]) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            return "error: 缺少 query"
        cmd = ["search", query]
        max_results = int(args.get("max_results") or args.get("maxResults") or 8)
        cmd += ["--max_results", str(max(1, min(max_results, 20)))]
        content_types = str(args.get("content_types") or args.get("contentTypes") or "").strip()
        if content_types:
            cmd += ["--content_types", content_types]
        freshness = str(args.get("freshness") or "").strip().lower()
        if freshness in {"day", "week", "month", "year"}:
            cmd += ["--freshness", freshness]
        zone = str(args.get("zone") or "").strip().lower()
        if zone in {"cn", "intl"}:
            cmd += ["--zone", zone]
        return await _run_anysearch(skills_dir, cmd)

    async def web_extract(args: dict[str, Any]) -> str:
        url = str(args.get("url") or "").strip()
        if not url:
            return "error: 缺少 url"
        if not (url.startswith("http://") or url.startswith("https://")):
            return "error: url 必须以 http:// 或 https:// 开头"
        return await _run_anysearch(skills_dir, ["extract", url])

    reg.add(
        "WebSearch",
        "Real-time AnySearch web/news search; returns titles, snippets, and source links.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词或问题"},
                "max_results": {"type": "integer", "description": "结果数量，默认 8，上限 20"},
                "content_types": {"type": "string", "description": "可选：web/news/code/doc/academic/data/image/video/audio，逗号分隔"},
                "freshness": {"type": "string", "description": "可选：day/week/month/year"},
                "zone": {"type": "string", "description": "可选：cn/intl"},
            },
            "required": ["query"],
        },
        web_search,
    )
    reg.add(
        "WebExtract",
        "Extract a web page as Markdown via AnySearch; use after WebSearch when deeper reading is needed.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要抽取的 http(s) URL"},
            },
            "required": ["url"],
        },
        web_extract,
    )
