"""Objective AgentWait observations, not a judgement of work quality.

Event pagination, transport activity and recorded Plan evidence have separate
cursors. Reading an old event page never counts as newly observed activity.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from app.agents import live_state
from app.agents.schemas import AgentTask


def _delivery_evidence_state(plan: dict[str, Any]) -> dict[str, Any] | None:
    if not plan:
        return None
    return {
        "planVersion": plan.get("activePlanVersion"),
        "steps": [{key: step.get(key) for key in ("stepId", "status", "criteriaState")}
                  for step in plan.get("steps", [])],
    }


def restore_review_state(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Restore cursors from the last persisted, same-scope AgentWait result."""
    restored = {}
    for agent in snapshot.get("agents", []):
        if not isinstance(agent, dict) or not agent.get("taskUuid"):
            continue
        page = agent.get("eventPage")
        if not isinstance(page, dict):
            continue  # Legacy snapshots did not carry a consumption receipt.
        try:
            after = max(0, int(page["nextAfter"]))
            observed = max(after, int(page["highWater"]))
        except (KeyError, TypeError, ValueError):
            continue
        execution = agent.get("currentExecution") if isinstance(agent.get("currentExecution"), dict) else {}
        restored[str(agent["taskUuid"])] = {
            "lastEventSeq": after, "observedEventSeq": observed,
            "observedOutputAtMs": int(execution.get("lastReportedOutputAtMs") or 0),
            "hasMore": bool(page.get("hasMore")),
            "status": agent.get("status"), "currentStatus": agent.get("currentStatus"),
            "deliveryEvidenceState": _delivery_evidence_state(agent.get("planRuntime") or {}),
            # Activity up to the delivered page was already summarised/scanned.
            "digestSeq": observed, "alertSeq": observed,
        }
    unchanged = snapshot.get("unchangedAgents") if isinstance(snapshot.get("unchangedAgents"), dict) else {}
    for task_uuid, cursor in (unchanged.get("cursors") or {}).items():
        try:
            after, observed, status = int(cursor[0]), int(cursor[1]), str(cursor[2])
        except (IndexError, TypeError, ValueError):
            continue
        restored.setdefault(str(task_uuid), {
            "lastEventSeq": after, "observedEventSeq": max(after, observed),
            "observedOutputAtMs": 0, "hasMore": False, "status": status,
        })
    return restored


def review_observation(
    task: AgentTask, events: dict[str, Any], plan: dict[str, Any],
    previous: dict[str, Any], *, now_ms: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    latest = events["latest"]
    high_water = events["highWater"]
    # Streaming progress is in memory only (never an event row).
    live = live_state.progress(task.task_uuid) if task.status in {"running", "resuming", "pausing"} else None
    live_at = int((live or {}).get("updatedAtMs") or 0) or None
    activity = bool(
        high_water > int(previous.get("observedEventSeq") or 0)
        or task.status != previous.get("status")
        or task.current_status != previous.get("currentStatus")
        or (live_at or 0) > int(previous.get("observedOutputAtMs") or 0)
    )
    last_at = latest.ts * 1000 if latest and latest.ts else None
    activity_source = "latest_recorded_event"
    if live_at and (last_at is None or live_at > last_at):
        last_at, activity_source = live_at, "latest_reported_output"

    def age(start: int | None) -> int | None:
        return max(0, now_ms - start) if start is not None else None

    # Only recorded transitions establish a current call. A terminal/control
    # task must not look like a still-running model because its last event lags.
    execution: dict[str, Any] = {
        "phase": task.status, "startedAtMs": None, "elapsedMs": None,
    }
    transitions = [event for event in events["current"]
                   if event.kind not in {"plan_submitted", "plan_replan_submitted"}]
    boundary = transitions[-1] if transitions else None
    if task.status in {"running", "resuming", "pausing"} and boundary:
        started = boundary.ts * 1000 if boundary.ts else None
        if boundary.kind == "model_call_started":
            attempt_id = str(boundary.detail.get("attemptId") or "")
            execution.update(phase="model", startedAtMs=started, attemptId=attempt_id)
            progress = live if live and str(live.get("attemptId") or "") == attempt_id else None
            # Reported progress is throttled; absence is not proof of idle
            # transport, lack of research, or lack of provider computation.
            execution["lastReportedOutputAtMs"] = live_at if progress else None
            execution["reportedOutputAgeMs"] = age(execution["lastReportedOutputAtMs"])
            if progress:
                tool_input = progress.get("toolInput")
                if isinstance(tool_input, dict):
                    execution["toolInput"] = {
                        key: tool_input[key] for key in ("toolNames", "receivedBytes", "phase")
                        if key in tool_input
                    }
        elif boundary.kind == "tool_call_started":
            execution.update(phase="tool", startedAtMs=started,
                             toolName=str(boundary.detail.get("name") or ""))
        elif boundary.kind == "model_call_retry_wait":
            retry = boundary.detail.get("retry") or {}
            if retry.get("active"):
                retry_at = int(retry.get("retryAtMs") or 0)
                delay = int(retry.get("delayMs") or 0)
                execution.update(phase="retry_wait", startedAtMs=retry_at - delay if retry_at else started,
                                 retryAtMs=retry_at or None, attempt=retry.get("attempt"))
        execution["elapsedMs"] = age(execution["startedAtMs"])
    elif task.status in {"paused", "needs_openbear_control"}:
        kind = "pause_applied" if task.status == "paused" else "needs_openbear_control"
        waiting = next((event for event in reversed(events["current"]) if event.kind == kind), None)
        if waiting and waiting.ts:
            execution.update(startedAtMs=waiting.ts * 1000, elapsedMs=age(waiting.ts * 1000))

    if task.status in {"running", "resuming", "pausing"} and plan.get("phase") in {
        "awaiting_plan_decision", "awaiting_replan_decision",
    }:
        pending = plan.get("pendingPlanVersion")
        submitted = next((event for event in reversed(events["current"])
                          if event.kind in {"plan_submitted", "plan_replan_submitted"}
                          and event.detail.get("planVersion") == pending), None)
        started = submitted.ts * 1000 if submitted and submitted.ts else None
        execution = {"phase": "plan_approval", "startedAtMs": started, "elapsedMs": age(started)}

    # A changed recorded criterion is evidence to review, not certification of
    # completion. Direct mode, missing Plan state, and the first baseline are
    # explicitly unknown; Read/TaskMemory/Write tool names are not classifiers.
    evidence_state = _delivery_evidence_state(plan)
    before = previous.get("deliveryEvidenceState")
    evidence = {
        "source": "recorded_plan_state" if evidence_state is not None else "unavailable",
        "known": evidence_state is not None,
        "changedSinceReview": evidence_state != before if evidence_state is not None and before is not None else None,
        "meaning": "Recorded step/criterion changes only; not verified delivery or a judgement of research value.",
    }
    observation = {
        "hasActivity": activity,
        # Compatibility alias: never promise artifact/criterion advancement.
        "hasMeaningfulProgress": activity,
        "progressSemantics": "hasMeaningfulProgress is a legacy alias for hasActivity, not delivery evidence.",
        "lastActivityAtMs": last_at, "activityAgeMs": age(last_at),
        "activitySource": activity_source,
        "currentExecution": execution, "deliveryEvidence": evidence,
        "eventPage": {key: events[key] for key in ("highWater", "hasMore", "nextAfter")},
    }
    next_state = {
        "lastEventSeq": events["nextAfter"], "observedEventSeq": high_water,
        "observedOutputAtMs": max(live_at or 0, int(previous.get("observedOutputAtMs") or 0)),
        "hasMore": events["hasMore"],
        "status": task.status, "currentStatus": task.current_status,
        "deliveryEvidenceState": evidence_state,
    }
    return observation, next_state


# --- Oversight: progress digests, anomaly alerts and review cadence -----------
#
# Everything below is derived from existing records: rath_task_events (tool
# arguments and result previews, model usage, compactions), rath_tasks counters
# and the Agent's own assistant text in context_execution_events.

# How often a sleeping AgentWait re-checks alerts and review deadlines.
POLL_SECONDS = 15.0

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})
RUNNING_STATUSES = frozenset({"running", "resuming", "pausing"})
_FILE_TOOLS = frozenset({"Edit", "EditBatch", "Write"})
_REVIEW_WAKES = frozenset({"review_due", "review_cap", "supervision_alert"})


@dataclass(frozen=True)
class OversightSettings:
    first_review_s: float = 300.0
    max_review_interval_s: float = 900.0
    repeat_failures: int = 3
    loop_repeats: int = 4
    stall_s: float = 600.0
    budget_percent: float = 80.0
    duration_budget_s: float = 3600.0
    cost_budget_usd: float = 5.0
    tool_call_limit: int = 0
    model_call_limit: int = 0

    @classmethod
    def from_config(cls, agents: Any) -> "OversightSettings":
        def number(name: str, default: float) -> float:
            try:
                return float(getattr(agents, name, default))
            except (TypeError, ValueError):
                return default
        return cls(
            first_review_s=number("supervision_first_review_s", 300),
            max_review_interval_s=number("supervision_max_review_interval_s", 900),
            repeat_failures=int(number("supervision_repeat_failures", 3)),
            loop_repeats=int(number("supervision_loop_repeats", 4)),
            stall_s=number("supervision_stall_minutes", 10) * 60,
            budget_percent=number("supervision_budget_alert_percent", 80),
            duration_budget_s=number("supervision_duration_budget_minutes", 60) * 60,
            cost_budget_usd=number("supervision_cost_budget_usd", 5.0),
            tool_call_limit=int(number("agent_tool_call_limit", 0)),
            model_call_limit=int(number("agent_model_call_limit", 0)),
        )


def requires_assessment(wake_reason: str) -> bool:
    return wake_reason in _REVIEW_WAKES


# Commands with an effect outside the Agent's working copy. Each rule is
# (key, label, pattern); a match only wakes the controller, it never blocks.
EXTERNAL_COMMAND_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = tuple(
    (key, label, re.compile(pattern, re.IGNORECASE)) for key, label, pattern in (
        ("git_push", "推送到远端仓库", r"\bgit\b(?:\s+-C\s+\S+)?\s+push\b"),
        ("service_control", "启停系统服务",
         r"\b(?:systemctl|service)\b[^\n;|&]*\b(?:start|stop|restart|reload|enable|disable|mask|kill)\b"),
        ("docker", "容器变更",
         r"\bdocker(?:-compose|\s+compose)?\s+(?:run|exec|rm|rmi|stop|kill|restart|start|up|down|build|push|pull"
         r"|create|prune|system|volume|network|cp|commit|tag|login|update)\b"),
        ("remote_shell", "远程登录或传输", r"(?:^\s*|[;&|(]\s*|\bsudo\s+|\btimeout\s+\S+\s+)(?:ssh|scp|sftp)\s|\brsync\b[^\n]*\s[\w.@-]+:\S*"),
        ("recursive_delete", "递归强制删除",
         r"\brm\s+(?:-[a-z]*r[a-z]*f[a-z]*|-[a-z]*f[a-z]*r[a-z]*|-r\s+-f|-f\s+-r|--recursive\s+--force|--force\s+--recursive)\b"),
        ("publish", "发布或修改托管平台",
         r"\b(?:npm|pnpm|yarn)\s+publish\b|\btwine\s+upload\b|\bgh\s+(?:release|pr|issue|repo)\s+(?:create|edit|merge|close|delete|upload)\b"),
    )
)
_HTTP_WRITE = re.compile(
    r"\bcurl\b[^\n]*(?:-X\s*['\"]?(?:POST|PUT|PATCH|DELETE)|--request\s+['\"]?(?:POST|PUT|PATCH|DELETE)"
    r"|\s(?:-d|--data(?:-[a-z]+)?|-F|--form|-T|--upload-file)[\s=])"
    r"|\bwget\b[^\n]*--(?:post-data|post-file|method)", re.IGNORECASE)
_URL_HOST = re.compile(r"https?://([^/\s:'\"?]+)", re.IGNORECASE)
_LOCAL_HOSTS = re.compile(r"^(?:localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|\[?::1\]?)$", re.IGNORECASE)


def external_effects(command: str) -> list[tuple[str, str]]:
    """Return (key, label) for each conservative external-effect rule the command hits."""
    hits = [(key, label) for key, label, pattern in EXTERNAL_COMMAND_RULES if pattern.search(command)]
    if _HTTP_WRITE.search(command):
        hosts = _URL_HOST.findall(command)
        if not hosts or not all(_LOCAL_HOSTS.match(host) for host in hosts):
            hits.append(("http_write", "向外部发送写请求"))
    return hits


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _arguments(detail: dict[str, Any]) -> dict[str, Any]:
    raw = detail.get("arguments")
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        match = re.search(r'"(?:path|command)"\s*:\s*"((?:[^"\\]|\\.)*)"', str(raw or ""))
        return {"_raw": str(raw or ""), **({"path": match.group(1)} if match else {})}
    return value if isinstance(value, dict) else {}


def failure_code(preview: str) -> str:
    """Error code of a failed tool result preview, or "" when it succeeded."""
    text = str(preview or "").lstrip()
    if text.startswith("error:"):
        return _clip(text[6:].splitlines()[0] if text[6:].strip() else "error", 80) or "error"
    status = re.match(r"status:\s*(failed|timeout|error|blocked)\b", text)
    if status:
        code = re.search(r"exit_code:\s*(-?\d+)", text)
        return f"exit_code {code.group(1)}" if code and status.group(1) == "failed" else status.group(1)
    if re.match(r'\{\s*"ok"\s*:\s*false', text):
        code = re.search(r'"error"\s*:\s*"([^"]+)"', text)
        return code.group(1) if code else "ok=false"
    return ""


def _signature(name: str, args: dict[str, Any]) -> str:
    if name == "Bash":
        return "Bash:" + " ".join(str(args.get("command") or args.get("_raw") or "").split())
    if name in _FILE_TOOLS:
        body = json.dumps({key: value for key, value in args.items() if key != "description"},
                          sort_keys=True, ensure_ascii=False)
        return f"{name}:{args.get('path') or ''}:{hashlib.sha1(body.encode()).hexdigest()[:12]}"
    return ""


async def _tool_events(db: Any, task_uuid: str, *, after_seq: int = 0, newest: int = 0) -> list[dict[str, Any]]:
    kinds = "('tool_call_started','tool_call_finished')"
    if newest:
        cur = await db.conn.execute(
            f"""SELECT seq, ts, kind, detail_json FROM rath_task_events
                WHERE task_uuid=? AND kind IN {kinds} AND seq>? ORDER BY seq DESC LIMIT ?""",
            (task_uuid, after_seq, newest))
        rows = list(reversed(await cur.fetchall()))
    else:
        cur = await db.conn.execute(
            f"""SELECT seq, ts, kind, detail_json FROM rath_task_events
                WHERE task_uuid=? AND kind IN {kinds} AND seq>? ORDER BY seq ASC LIMIT 4000""",
            (task_uuid, after_seq))
        rows = await cur.fetchall()
    calls: list[dict[str, Any]] = []
    for row in rows:
        try:
            detail = json.loads(str(row["detail_json"] or "{}"))
        except (TypeError, ValueError):
            detail = {}
        detail = detail if isinstance(detail, dict) else {}
        name = str(detail.get("name") or "")
        if row["kind"] == "tool_call_started":
            calls.append({"seq": int(row["seq"]), "ts": int(row["ts"] or 0), "tool": name,
                          "args": _arguments(detail), "preview": None, "durationMs": None, "finishSeq": None})
            continue
        # Agent tools run one at a time: a finish closes the latest open call.
        for call in reversed(calls):
            if call["preview"] is None and call["tool"] == name:
                call["preview"] = str(detail.get("resultPreview") or "")
                call["durationMs"] = int(detail.get("durationMs") or 0)
                call["finishSeq"] = int(row["seq"])
                break
    return calls


async def recent_tool_calls(db: Any, task_uuid: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """The last ``limit`` tool calls with argument and result summaries."""
    calls = (await _tool_events(db, task_uuid, newest=limit * 2 + 1))[-limit:]
    out = []
    for call in calls:
        args = {key: value for key, value in call["args"].items() if key != "_raw"} or call["args"].get("_raw", "")
        preview = call["preview"]
        out.append({
            "seq": call["seq"], "ts": call["ts"], "tool": call["tool"],
            "arguments": _clip(json.dumps(args, ensure_ascii=False) if not isinstance(args, str) else args, 600),
            "status": "running" if preview is None else ("failed" if failure_code(preview) else "ok"),
            **({"error": failure_code(preview)} if preview and failure_code(preview) else {}),
            "result": _clip(preview, 400) if preview is not None else "",
            "durationMs": call["durationMs"],
        })
    return out


def _resolve(path: str, base: str) -> str:
    path = os.path.expanduser(str(path or "").strip())
    return os.path.normpath(path if os.path.isabs(path) or not base else os.path.join(base, path))


def outside_owned(path: str, owned: list[str], base: str) -> bool:
    target = _resolve(path, base)
    for item in owned:
        root = _resolve(item, base)
        if target == root or target.startswith(root.rstrip(os.sep) + os.sep):
            return False
    return True


async def latest_agent_note(db: Any, task_uuid: str, *, limit: int = 400) -> str:
    """The Agent's most recent own text between tool calls (not tool output)."""
    cur = await db.conn.execute(
        """SELECT payload_json FROM context_execution_events
           WHERE task_uuid=? AND kind='execution' ORDER BY seq DESC LIMIT 40""",
        (task_uuid,))
    for row in await cur.fetchall():
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("role") != "assistant":
            continue
        content = payload.get("content")
        if isinstance(content, list):
            content = "\n".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
        text = str(content or "").strip()
        if text:
            return _clip(text, limit)
    return ""


async def task_brief(db: Any, task: AgentTask) -> dict[str, Any]:
    """Objective and completion standard the Agent was given."""
    data = task.input if isinstance(task.input, dict) else {}
    brief: dict[str, Any] = {
        "objective": _clip(data.get("originalPrompt") or data.get("instruction") or data.get("raw") or task.title, 300),
    }
    cur = await db.conn.execute(
        """SELECT v.plan_json FROM rath_task_plan_state s JOIN rath_task_plan_versions v
             ON v.task_uuid=s.task_uuid AND v.version=s.active_plan_version
           WHERE s.task_uuid=? AND s.active_plan_version>0""", (task.task_uuid,))
    row = await cur.fetchone()
    if row is not None:
        try:
            plan = json.loads(str(row["plan_json"] or "{}"))
        except (TypeError, ValueError):
            plan = {}
        if isinstance(plan, dict):
            if plan.get("objective"):
                brief["objective"] = _clip(plan["objective"], 300)
            brief["completionCriteria"] = [
                _clip(f"{item.get('title') or item.get('id')}: {item.get('description') or ''}", 140)
                for item in (plan.get("finalOutputs") or [])[:5] if isinstance(item, dict)
            ]
    owned = data.get("ownedPaths")
    if isinstance(owned, list) and owned:
        brief["ownedPaths"] = [str(item) for item in owned[:10]]
    return brief


async def progress_digest(db: Any, task: AgentTask, *, after_seq: int, through_seq: int) -> dict[str, Any]:
    """What the Agent did in (after_seq, through_seq], kept to roughly 1-2 KB."""
    # A call still running at after_seq was counted then; how it ended belongs here.
    # Agent tools run one at a time, so it is the last one started by then.
    cur = await db.conn.execute(
        "SELECT MAX(seq) FROM rath_task_events WHERE task_uuid=? AND kind='tool_call_started' AND seq<=?",
        (task.task_uuid, after_seq))
    running = (await cur.fetchone())[0]
    calls, ended = [], []
    for call in await _tool_events(db, task.task_uuid, after_seq=running - 1 if running else after_seq):
        finished = call["finishSeq"] is not None and call["finishSeq"] <= through_seq
        if call["seq"] <= after_seq:
            if finished and call["finishSeq"] > after_seq:
                ended.append(call)
        elif call["seq"] <= through_seq:
            calls.append(call if finished else {**call, "preview": None})
    counts: dict[str, int] = {}
    files: list[str] = []
    commands: list[str] = []
    external: list[str] = []
    failures: list[dict[str, str]] = []
    failure_count = 0
    for call in calls:
        counts[call["tool"]] = counts.get(call["tool"], 0) + 1
        args = call["args"]
        if call["tool"] in _FILE_TOOLS and args.get("path") and str(args["path"]) not in files:
            files.append(str(args["path"]))
        if call["tool"] == "Bash":
            command = str(args.get("command") or args.get("_raw") or "")
            if len(commands) < 6:
                commands.append(_clip(command, 160))
            for _key, label in external_effects(command):
                external.append(f"{label}: {_clip(command, 120)}")
    for call in ended + calls:
        args = call["args"]
        code = failure_code(call["preview"]) if call["preview"] else ""
        if code:
            failure_count += 1
            if len(failures) < 5:
                target = args.get("command") or args.get("path") or ""
                failures.append({"tool": call["tool"], "error": code, **({"target": _clip(target, 100)} if target else {})})
    cur = await db.conn.execute(
        """SELECT kind, detail_json FROM rath_task_events
           WHERE task_uuid=? AND seq>? AND seq<=?
             AND kind IN ('model_call_finished', 'model_context_compaction_completed')""",
        (task.task_uuid, after_seq, through_seq))
    tokens = 0
    cost = 0.0
    compactions = 0
    for row in await cur.fetchall():
        if row["kind"] == "model_context_compaction_completed":
            compactions += 1
            continue
        try:
            detail = json.loads(str(row["detail_json"] or "{}"))
            tokens += sum(int(detail.get(key) or 0) for key in (
                "inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens"))
            cost += float(detail.get("costUsd") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
    digest: dict[str, Any] = {
        "sinceSeq": after_seq, "throughSeq": through_seq,
        "toolCalls": counts,
        **({"finishedRunning": [{"tool": c["tool"], "durationMs": c["durationMs"]} for c in ended]} if ended else {}),
        **({"filesChanged": files[:12]} if files else {}),
        **({"filesChangedMore": len(files) - 12} if len(files) > 12 else {}),
        **({"commands": commands} if commands else {}),
        **({"commandCount": counts.get("Bash", 0)} if counts.get("Bash", 0) > len(commands) else {}),
        **({"externalEffects": external[:5]} if external else {}),
        **({"failures": failures, "failureCount": failure_count} if failure_count else {}),
        **({"contextCompactions": compactions} if compactions else {}),
        "usageDelta": {"tokens": tokens, "costUsd": round(cost, 4)},
    }
    note = await latest_agent_note(db, task.task_uuid)
    if note:
        digest["agentNote"] = note
    return digest


def digest_has_progress(digest: dict[str, Any]) -> bool:
    return bool(digest.get("toolCalls") or digest.get("finishedRunning") or digest.get("contextCompactions")
                or digest.get("usageDelta", {}).get("tokens"))


def digest_highlights(digest: dict[str, Any]) -> list[str]:
    """Short human lines for the supervision card."""
    lines = []
    counts = digest.get("toolCalls") or {}
    if counts:
        lines.append("工具 " + "，".join(f"{name}×{count}" for name, count in sorted(counts.items(), key=lambda item: -item[1])[:6]))
    if digest.get("filesChanged"):
        more = digest.get("filesChangedMore") or 0
        lines.append("改动 " + "、".join(os.path.basename(path) or path for path in digest["filesChanged"][:5])
                     + (f" 等 {len(digest['filesChanged']) + more} 个文件" if len(digest["filesChanged"]) > 5 or more else ""))
    if digest.get("failureCount"):
        first = digest["failures"][0]
        lines.append(f"失败 {digest['failureCount']} 次（{first['tool']} {first['error']}）")
    for item in digest.get("externalEffects") or []:
        lines.append(f"外部影响：{_clip(item, 80)}")
    if digest.get("contextCompactions"):
        lines.append(f"上下文压缩 {digest['contextCompactions']} 次")
    usage = digest.get("usageDelta") or {}
    if usage.get("tokens"):
        lines.append(f"token +{usage['tokens']:,}，费用 +${usage.get('costUsd', 0):.2f}")
    return lines[:8]


async def scan_alerts(
    db: Any, task: AgentTask, state: dict[str, Any], settings: OversightSettings, *,
    workspace: str, now_ms: int | None = None,
) -> list[dict[str, Any]]:
    """New anomaly alerts for one Agent. Each situation has a stable key in
    ``state['raisedAlerts']`` and is reported once; a changed situation gets
    a new key. Detection never pauses or blocks the Agent."""
    now_ms = now_ms or int(time.time() * 1000)
    raised = set(state.get("raisedAlerts") or [])
    found: list[dict[str, Any]] = []

    def add(key: str, kind: str, message: str, **detail: Any) -> None:
        if key in raised or any(item["key"] == key for item in found):
            return
        found.append({"key": key, "kind": kind, "taskUuid": task.task_uuid, "title": task.title,
                      "message": message, **detail})

    recent = await _tool_events(db, task.task_uuid, newest=80)
    # Per tool: its own trailing run of failed calls, other tools in between
    # do not break it. The key is the run's first call, so a run that only
    # grows is one situation; a new run after a success is another.
    streaks: dict[str, list[dict[str, Any]]] = {}
    for call in recent:
        if call["preview"] is None:
            continue
        if failure_code(call["preview"]):
            streaks.setdefault(call["tool"], []).append(call)
        else:
            streaks[call["tool"]] = []
    for tool, streak in streaks.items():
        if len(streak) >= settings.repeat_failures:
            target = streak[-1]["args"].get("command") or streak[-1]["args"].get("path") or ""
            add(f"fail:{tool}:{streak[0]['seq']}", "repeated_failure",
                f"{tool} 连续失败 {len(streak)} 次，最近错误：{failure_code(streak[-1]['preview'])}",
                tool=tool, count=len(streak), target=_clip(target, 200))
    signatures: dict[str, list[dict[str, Any]]] = {}
    for call in recent[-40:]:
        signature = _signature(call["tool"], call["args"])
        if signature:
            signatures.setdefault(signature, []).append(call)
    for signature, same in signatures.items():
        if len(same) >= settings.loop_repeats:
            args = same[-1]["args"]
            target = args.get("command") or args.get("path") or ""
            what = "命令" if same[-1]["tool"] == "Bash" else "编辑"
            add("loop:" + hashlib.sha1(signature.encode()).hexdigest()[:16], "loop",
                f"同一{what}重复 {len(same)} 次：{_clip(target, 120)}",
                tool=same[-1]["tool"], count=len(same), target=_clip(target, 200))

    alert_seq = int(state.get("alertSeq") or 0)
    owned = task.input.get("ownedPaths") if isinstance(task.input, dict) else None
    owned = [str(item) for item in owned if str(item).strip()] if isinstance(owned, list) else []
    for call in await _tool_events(db, task.task_uuid, after_seq=alert_seq):
        args = call["args"]
        if owned and call["tool"] in _FILE_TOOLS and args.get("path") and outside_owned(str(args["path"]), owned, workspace):
            path = _resolve(str(args["path"]), workspace)
            add(f"scope:{path}", "out_of_scope", f"改动了声明范围之外的文件：{path}",
                tool=call["tool"], path=path, ownedPaths=owned)
        if call["tool"] == "Bash":
            command = str(args.get("command") or args.get("_raw") or "")
            for key, label in external_effects(command):
                digest = hashlib.sha1(command.encode()).hexdigest()[:12]
                add(f"external:{key}:{digest}", "external_effect", f"{label}：{_clip(command, 160)}",
                    rule=key, command=_clip(command, 400))
    cur = await db.conn.execute(
        """SELECT seq, detail_json FROM rath_task_events
           WHERE task_uuid=? AND kind='model_context_compaction_completed' AND seq>?""",
        (task.task_uuid, alert_seq))
    for row in await cur.fetchall():
        try:
            detail = json.loads(str(row["detail_json"] or "{}"))
        except (TypeError, ValueError):
            detail = {}
        add(f"compaction:{row['seq']}", "context_compaction", "Agent 发生了上下文压缩",
            source=str(detail.get("source") or ""), strategy=str(detail.get("strategy") or ""))

    if task.status in RUNNING_STATUSES:
        cur = await db.conn.execute(
            "SELECT seq, ts, kind FROM rath_task_events WHERE task_uuid=? ORDER BY seq DESC LIMIT 1",
            (task.task_uuid,))
        latest = await cur.fetchone()
        in_tool = latest is not None and latest["kind"] == "tool_call_started"
        live_at_ms = int((live_state.progress(task.task_uuid) or {}).get("updatedAtMs") or 0)
        last_ms = max(int(latest["ts"] or 0) * 1000 if latest else 0, live_at_ms)
        if latest is not None and not in_tool and last_ms and now_ms - last_ms >= settings.stall_s * 1000:
            add(f"stall:{latest['seq']}", "stalled",
                f"已有 {int((now_ms - last_ms) / 60000)} 分钟没有模型或工具活动",
                idleSeconds=int((now_ms - last_ms) / 1000))
    ratio = settings.budget_percent / 100.0
    budgets = (
        ("tools", task.work_tool_call_count, settings.tool_call_limit, "工作工具调用"),
        ("models", task.model_call_count, settings.model_call_limit, "模型调用"),
        ("duration", (now_ms / 1000 - task.started_at) if task.started_at else 0, settings.duration_budget_s, "运行时长"),
        ("cost", task.cost_usd, settings.cost_budget_usd, "费用"),
    )
    for key, used, limit, label in budgets:
        if limit and limit > 0 and used >= limit * ratio and task.status not in TERMINAL_STATUSES:
            add(f"budget:{key}", "budget", f"{label}已达预算的 {int(used * 100 / limit)}%",
                budget=key, used=round(float(used), 4), limit=limit)
    return found


def review_due_at_ms(task: AgentTask, state: dict[str, Any], settings: OversightSettings,
                     *, in_long_step: bool) -> int | None:
    """Latest time this running Agent must be reviewed, or None when exempt.

    The first review is due ``first_review_s`` after start and later reviews
    at most ``max_review_interval_s`` apart. While the Agent is inside one tool
    call that began before its last review and nothing else happened since,
    there is nothing new to review and the deadline is lifted until it ends.
    """
    if task.status not in RUNNING_STATUSES or not task.started_at:
        return None
    reviewed = int(state.get("reviewedAtMs") or 0)
    if not reviewed:
        return int((task.started_at + settings.first_review_s) * 1000)
    if in_long_step:
        return None
    return reviewed + int(settings.max_review_interval_s * 1000)


async def in_long_single_step(db: Any, task_uuid: str, state: dict[str, Any]) -> bool:
    """True while the newest event is a tool call started at or before the last digest."""
    cur = await db.conn.execute(
        "SELECT seq, kind FROM rath_task_events WHERE task_uuid=? ORDER BY seq DESC LIMIT 1", (task_uuid,))
    latest = await cur.fetchone()
    return bool(latest is not None and latest["kind"] == "tool_call_started"
                and int(latest["seq"]) <= int(state.get("digestSeq") or 0))
