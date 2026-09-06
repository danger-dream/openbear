#!/usr/bin/env python3
"""Agent 委派行为评测：重放三个真实失败场景，检查控制器实际发出的工具调用。

用法（在仓库根目录）：

    .venv/bin/python evals/agent_delegation/run_eval.py                    # 评测 DB 当前激活提示词
    .venv/bin/python evals/agent_delegation/run_eval.py --prompt file:prompts/openbear-system.tpl
    .venv/bin/python evals/agent_delegation/run_eval.py --prompt "db:OpenBear-v3.3" --scenario runtime-doc

候选系统提示词走生产同一条渲染管线（BuiltinMemoryClient + 生产 DB 副本），
对话按真实场景重放，模型真实调用本机 Parrot 上游；前台工具用只读桩响应，
Agent 工具调用被捕获后按场景检查。任何 FAIL 即整体失败（exit 1），该版本不应激活。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import load_config  # noqa: E402
from app.context.builder import build_system_prompt_params  # noqa: E402
from app.db.engine import DB  # noqa: E402
from app.llm.base import ToolCall  # noqa: E402
from app.llm.client import HTTPClient  # noqa: E402
from app.llm.factory import BackendFactory  # noqa: E402
from app.memory.builtin import BuiltinMemoryClient  # noqa: E402
from app.rath.builtin_workflows import ensure_builtin_workflows  # noqa: E402
from app.rath.dao import RathDAO  # noqa: E402
from app.rath.manager import RathTaskManager  # noqa: E402
from app.rath.prompting import available_agent_prompt_items  # noqa: E402
from app.rath.single_agent import _assistant_result_message  # noqa: E402
from app.tools.agents import register_agent_tools  # noqa: E402
from app.tools.base import ToolRegistry  # noqa: E402

from scenarios import SCENARIOS, Finding, ToolEvent  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPORTS = Path(__file__).resolve().parent / "reports"
MAX_ROUNDS = 8
TOOL_OUTPUT_CAP = 8000

# 只读命令白名单：评测桩会真实执行这些命令（模型可能要看 parrot/codex 源码）。
_BASH_ALLOWED = {"ls", "rg", "cat", "head", "tail", "wc", "pwd", "file", "stat", "tree", "grep", "find"}
_BASH_FORBIDDEN = (">", ">>", ";", "&&", "||", "`", "$(", "<(", "rm ", "sudo ", "mv ", "cp ")


class _EvalSelection:
    current = ""


# ---------------------------------------------------------------------------
# 前台工具桩 schema（与生产内置工具同名同参数骨架，行为为只读桩）
# ---------------------------------------------------------------------------

def _foreground_schemas() -> list[dict[str, Any]]:
    return [
        {
            "name": "Bash",
            "description": "Run a shell command and wait for its terminal result.",
            "parameters": {"type": "object", "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string"},
                "timeout": {"type": "number"},
                "description": {"type": "string"},
            }, "required": ["command"]},
        },
        {
            "name": "Read",
            "description": "Read text files; supports offset/limit.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer"},
                "limit": {"type": "integer"},
            }, "required": ["path"]},
        },
        {
            "name": "Memory",
            "description": "Read and write OpenBear memory. resource=identity|secret|entry|doc; actions list|get|set|del.",
            "parameters": {"type": "object", "properties": {
                "resource": {"type": "string"},
                "action": {"type": "string"},
                "name": {"type": "string"},
                "ref": {"type": "string"},
                "query": {"type": "string"},
            }, "required": ["resource", "action"]},
        },
        {
            "name": "TaskMemory",
            "description": "Manage memory scoped to the current conversation.",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string"},
                "query": {"type": "string"},
                "name": {"type": "string"},
                "body": {"type": "string"},
            }, "required": ["action"]},
        },
        {
            "name": "WebSearch",
            "description": "Real-time web search.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string"},
            }, "required": ["query"]},
        },
        {
            "name": "WebExtract",
            "description": "Extract a web page as Markdown.",
            "parameters": {"type": "object", "properties": {
                "url": {"type": "string"},
            }, "required": ["url"]},
        },
        {
            "name": "UserInteraction",
            "description": "Ask the Web user for an interaction (confirm/select/prompt).",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string"},
                "title": {"type": "string"},
                "body": {"type": "string"},
            }, "required": ["action", "title", "body"]},
        },
    ]


def _run_safe_bash(command: str) -> str:
    lowered = command.strip()
    if any(tok in lowered for tok in _BASH_FORBIDDEN):
        return "（评测桩）该命令包含写入/串联操作，评测环境只执行只读检查命令。"
    for segment in lowered.split("|"):
        try:
            head = shlex.split(segment.strip())
        except ValueError:
            return "（评测桩）命令无法解析，未执行。"
        if not head or head[0] not in _BASH_ALLOWED:
            return f"（评测桩）命令 `{head[0] if head else ''}` 不在评测只读白名单内，未执行。"
    try:
        proc = subprocess.run(command, shell=True, capture_output=True, text=True,
                              timeout=15, cwd=str(ROOT))
    except subprocess.TimeoutExpired:
        return "（评测桩）命令执行超时（15s），已终止。"
    out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    out = out.strip() or f"(exit {proc.returncode}, no output)"
    return out[:TOOL_OUTPUT_CAP]


class StubDispatcher:
    """前台工具只读桩 + Agent 工具捕获。"""

    def __init__(self, doc_body: str) -> None:
        self.doc_body = doc_body
        self.events: list[ToolEvent] = []
        self.launch_count = 0
        self.saw_agent_wait = False

    def dispatch(self, name: str, args: dict[str, Any], round_no: int) -> str:
        self.events.append(ToolEvent(round_no=round_no, name=name, args=args))
        handler = getattr(self, f"_h_{name.lower()}", None)
        if handler is None:
            return json.dumps({"ok": False, "error": f"（评测桩）工具 {name} 在评测环境不可用"},
                              ensure_ascii=False)
        return handler(args)

    def _h_bash(self, args: dict[str, Any]) -> str:
        return _run_safe_bash(str(args.get("command") or ""))

    def _h_read(self, args: dict[str, Any]) -> str:
        path = Path(str(args.get("path") or ""))
        if not path.is_file():
            return f"（评测桩）文件不存在: {path}"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            return f"（评测桩）读取失败: {exc}"
        offset = max(int(args.get("offset") or 0), 0)
        limit = int(args.get("limit") or 0)
        lines = text.splitlines()
        if offset or limit:
            lines = lines[offset:offset + limit] if limit else lines[offset:]
        return "\n".join(lines)[:TOOL_OUTPUT_CAP]

    def _h_memory(self, args: dict[str, Any]) -> str:
        resource = str(args.get("resource") or "")
        action = str(args.get("action") or "")
        name = str(args.get("name") or args.get("ref") or "")
        if resource == "doc" and action == "list":
            return json.dumps({"ok": True, "items": [
                {"name": "openbear-agent-runtime-unification",
                 "summary": "OpenBear 主/子 Agent Runtime 统一重构方案", "chars": len(self.doc_body)},
            ]}, ensure_ascii=False)
        if resource == "doc" and action == "get" and "runtime-unification" in name:
            return json.dumps({"ok": True, "item": {
                "name": "openbear-agent-runtime-unification", "content": self.doc_body,
            }}, ensure_ascii=False)
        if resource == "secret":
            return json.dumps({"ok": False, "error": "（评测桩）评测环境不提供 secret"}, ensure_ascii=False)
        return json.dumps({"ok": False, "error": f"（评测桩）memory 中无此条目: {name or action}"},
                          ensure_ascii=False)

    def _h_taskmemory(self, args: dict[str, Any]) -> str:
        return json.dumps({"ok": True, "items": [], "note": "（评测桩）当前会话无任务记忆"},
                          ensure_ascii=False)

    def _h_websearch(self, args: dict[str, Any]) -> str:
        return json.dumps({"ok": False, "error": "（评测桩）评测环境不提供网络检索"}, ensure_ascii=False)

    def _h_webextract(self, args: dict[str, Any]) -> str:
        return json.dumps({"ok": False, "error": "（评测桩）评测环境不提供网络访问"}, ensure_ascii=False)

    def _h_userinteraction(self, args: dict[str, Any]) -> str:
        return json.dumps({"ok": True, "cancelled": True,
                           "note": "（评测桩）用户未在场，请按已有信息继续"}, ensure_ascii=False)

    def _h_agent(self, args: dict[str, Any]) -> str:
        self.launch_count += 1
        return json.dumps({"ok": True, "detached": True, "status": "running", "task": {
            "taskUuid": f"eval-task-{self.launch_count:02d}", "status": "running",
        }, "message": "（评测桩）Agent 已受理并在后台运行；不要轮询，可继续其他工作或等待"},
            ensure_ascii=False)

    def _h_agentwait(self, args: dict[str, Any]) -> str:
        self.saw_agent_wait = True
        return json.dumps({"ok": True, "status": "waiting",
                           "message": "（评测桩）等待窗口结束，Agent 仍在运行"}, ensure_ascii=False)

    def _h_agentmessage(self, args: dict[str, Any]) -> str:
        return json.dumps({"ok": True, "note": "（评测桩）消息已送达 Agent"}, ensure_ascii=False)

    def _h_agentstop(self, args: dict[str, Any]) -> str:
        return json.dumps({"ok": True, "note": "（评测桩）Agent 已停止"}, ensure_ascii=False)

    def _h_agentplandecision(self, args: dict[str, Any]) -> str:
        return json.dumps({"ok": True, "note": "（评测桩）决策已记录"}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 候选提示词加载与渲染
# ---------------------------------------------------------------------------

def load_candidate(spec: str, db_path: Path) -> tuple[str, str]:
    """返回 (候选名, 模板原文)。spec: db-active | db:<名字> | file:<路径> | <路径>。"""
    if spec == "db-active":
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT name, content FROM memory_templates WHERE is_active=1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row is None:
            raise SystemExit("DB 中没有激活的主提示词模板")
        return str(row[0]), str(row[1])
    if spec.startswith("db:"):
        name = spec[3:]
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT name, content FROM memory_templates WHERE name=? ORDER BY id DESC LIMIT 1",
            (name,),
        ).fetchone()
        conn.close()
        if row is None:
            raise SystemExit(f"DB 中没有名为 {name} 的模板")
        return str(row[0]), str(row[1])
    path = Path(spec[5:] if spec.startswith("file:") else spec)
    if not path.is_file():
        raise SystemExit(f"模板文件不存在: {path}")
    return path.name, path.read_text(encoding="utf-8")


async def render_candidate(template_text: str, template_name: str, *, cfg,
                           db_copy: Path, schemas: list[dict[str, Any]],
                           model_label: str) -> str:
    tool_names = [str(s["name"]) for s in schemas]
    tool_summaries = {str(s["name"]): str(s.get("description") or "").split("\n")[0]
                      for s in schemas}
    db = DB(str(db_copy))
    await db.connect()
    try:
        agents = await available_agent_prompt_items(RathDAO(db))
        params = build_system_prompt_params(
            tool_names=tool_names,
            tool_summaries=tool_summaries,
            builtin_tool_names=tool_names,
            builtin_tool_summaries=tool_summaries,
            workspace_dir=str(ROOT / "workspace"),
            current_model=model_label,
            available_agents=agents,
        )
        mem = BuiltinMemoryClient(db, identity=cfg.memory.identity)
        return await mem.render_system_prompt(
            params, template_content=template_text,
            template_name=template_name, source="preview")
    finally:
        await db.close()


async def build_agent_schemas(cfg, factory) -> list[dict[str, Any]]:
    """在临时 DB 上注册真实 Agent 工具，只为提取生产 schema。"""
    with tempfile.TemporaryDirectory() as tmp:
        db = DB(str(Path(tmp) / "eval-agents.db"))
        await db.connect()
        try:
            dao = RathDAO(db)
            await ensure_builtin_workflows(dao)
            reg = ToolRegistry()
            register_agent_tools(
                reg, config=cfg, dao=dao, manager=RathTaskManager(dao),
                llm_factory=factory, model_selection=_EvalSelection())
            return reg.schemas(scope="main")
        finally:
            await db.close()


# ---------------------------------------------------------------------------
# 场景执行
# ---------------------------------------------------------------------------

async def run_scenario(scenario, *, backend, model_id: str, max_tokens: int,
                       system_prompt: str, schemas: list[dict[str, Any]],
                       doc_body: str) -> dict[str, Any]:
    stub = StubDispatcher(doc_body)
    messages: list[dict[str, Any]] = [dict(m) for m in scenario.messages]
    final_text = ""
    rounds_used = 0
    for round_no in range(MAX_ROUNDS):
        rounds_used = round_no + 1
        result = await backend.complete(
            messages, model=model_id, system=system_prompt,
            tools=schemas, max_tokens=min(max_tokens, 16000))
        calls = [
            c if c.id else ToolCall(id=f"call_{round_no}_{i}_{c.name}", name=c.name,
                                    arguments=c.arguments)
            for i, c in enumerate(result.tool_calls or [])
        ]
        if not calls:
            final_text = (result.text or "").strip()
            break
        messages.append(_assistant_result_message(result, calls))
        for call in calls:
            try:
                parsed = json.loads(call.arguments) if isinstance(call.arguments, str) \
                    else dict(call.arguments or {})
            except (TypeError, ValueError):
                parsed = {"_raw": str(call.arguments)}
            output = stub.dispatch(call.name, parsed, round_no)
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "name": call.name, "content": output})
        if stub.saw_agent_wait:
            break
    findings: list[Finding] = scenario.check(stub.events)
    fails = [f for f in findings if f.level == "fail"]
    warns = [f for f in findings if f.level == "warn"]
    return {
        "scenario": scenario, "events": stub.events, "findings": findings,
        "fails": fails, "warns": warns, "final_text": final_text,
        "rounds": rounds_used,
        "verdict": "FAIL" if fails else ("PASS(warn)" if warns else "PASS"),
    }


def _format_report(candidate_name: str, model_label: str,
                   outcomes: list[dict[str, Any]]) -> str:
    lines = [
        "# Agent 委派行为评测报告",
        "",
        f"- 候选提示词: {candidate_name}",
        f"- 模型: {model_label}",
        f"- 时间: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        "",
    ]
    for outcome in outcomes:
        scenario = outcome["scenario"]
        lines.append(f"## [{outcome['verdict']}] {scenario.id} — {scenario.title}")
        lines.append("")
        lines.append(f"轮数: {outcome['rounds']}；历史失败: {scenario.notes}")
        lines.append("")
        lines.append("### 工具调用序列")
        for event in outcome["events"]:
            if event.name == "Agent":
                args = event.args
                prompt = str(args.get("prompt") or "")
                lines.append(
                    f"- r{event.round_no} **Agent** desc={args.get('description')!r} "
                    f"planMode={args.get('planMode', 'direct')!r} tools={args.get('tools')!r} "
                    f"attachments={args.get('attachments', [])!r} prompt={len(prompt)}字符")
                snippet = prompt[:600].replace("\n", "\n  > ")
                lines.append(f"  > {snippet}{'…' if len(prompt) > 600 else ''}")
            else:
                brief = json.dumps(event.args, ensure_ascii=False)[:150]
                lines.append(f"- r{event.round_no} {event.name} {brief}")
        if outcome["final_text"]:
            lines.append("")
            lines.append(f"最终回复（前 300 字符）: {outcome['final_text'][:300]}")
        lines.append("")
        lines.append("### 检查结论")
        if not outcome["findings"]:
            lines.append("- （无发现）")
        for f in outcome["findings"]:
            mark = {"fail": "✗ FAIL", "warn": "△ WARN", "info": "· INFO"}[f.level]
            lines.append(f"- {mark} {f.message}" + (f" ｜证据: {f.evidence}" if f.evidence else ""))
        lines.append("")
    total_fail = sum(1 for o in outcomes if o["fails"])
    lines.append("---")
    lines.append(f"总判定: {'FAIL' if total_fail else 'PASS'}"
                 f"（{len(outcomes)} 个场景，{total_fail} 个失败）")
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prompt", default="db-active",
                        help="候选提示词: db-active | db:<模板名> | file:<路径>")
    parser.add_argument("--model", default="", help="模型全名，默认 config models.primary")
    parser.add_argument("--scenario", default="", help="只跑指定场景 id（逗号分隔）")
    parser.add_argument("--db", default=str(ROOT / "data" / "openbear.db"),
                        help="生产 DB 路径（只读复制用于渲染）")
    args = parser.parse_args()

    cfg = load_config(str(ROOT / "openbear.json"))
    model_label = args.model or cfg.models.primary
    db_path = Path(args.db)
    candidate_name, candidate_text = load_candidate(args.prompt, db_path)

    doc_fixture = FIXTURES / "runtime-unification-doc.md"
    doc_body = doc_fixture.read_text(encoding="utf-8") if doc_fixture.is_file() else ""
    if not doc_body:
        print(f"警告: 缺少文档夹具 {doc_fixture}，runtime-doc 场景的 Memory 桩将返回空",
              file=sys.stderr)

    wanted = {s.strip() for s in args.scenario.split(",") if s.strip()}
    scenarios = [s for s in SCENARIOS if not wanted or s.id in wanted]
    if not scenarios:
        raise SystemExit(f"没有匹配的场景: {args.scenario}")

    client = HTTPClient()
    try:
        factory = BackendFactory(cfg.models, client)
        backend, model_id, max_tokens = factory.backend_for(model_label)
        _EvalSelection.current = model_label
        agent_schemas = await build_agent_schemas(cfg, factory)
        schemas = agent_schemas + _foreground_schemas()

        with tempfile.TemporaryDirectory() as tmp:
            db_copy = Path(tmp) / "openbear-render.db"
            src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            dst = sqlite3.connect(str(db_copy))
            src.backup(dst)
            src.close()
            dst.close()
            system_prompt = await render_candidate(
                candidate_text, candidate_name, cfg=cfg, db_copy=db_copy,
                schemas=schemas, model_label=model_label)

        print(f"候选: {candidate_name}（模板 {len(candidate_text)} 字符，"
              f"渲染后 {len(system_prompt)} 字符）；模型: {model_label}")
        outcomes = []
        for scenario in scenarios:
            print(f"— 场景 {scenario.id} …", flush=True)
            outcome = await run_scenario(
                scenario, backend=backend, model_id=model_id, max_tokens=max_tokens,
                system_prompt=system_prompt, schemas=schemas, doc_body=doc_body)
            outcomes.append(outcome)
            print(f"  {outcome['verdict']}（{len(outcome['fails'])} fail / "
                  f"{len(outcome['warns'])} warn，{outcome['rounds']} 轮）")
    finally:
        await client.close()

    report = _format_report(candidate_name, model_label, outcomes)
    REPORTS.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^\w.-]+", "-", candidate_name)
    report_path = REPORTS / f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"\n报告: {report_path}")
    print(report.split("---")[-1].strip())
    return 1 if any(o["fails"] for o in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
