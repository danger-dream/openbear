"""Read-only, indexed statistics for the Web console dashboard."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from aiohttp import web

from app.web_console.core import _WEB_SESSION_KEY, WebSession

_BEIJING = ZoneInfo("Asia/Shanghai")
_ALLOWED_DAYS = {1, 3, 7, 30, 90}
_TIMELINE_BUCKET_HOURS = {1: 1, 3: 3, 7: 6, 30: 24, 90: 24}
_CACHE_TTL_SECONDS = 30.0
_TEMPORARY_FOLDER = "__temporary__"


def _metric() -> dict[str, int | float]:
    return {
        "activeConversations": 0,
        "userTurns": 0,
        "modelCalls": 0,
        "modelOk": 0,
        "modelFailed": 0,
        "modelRetries": 0,
        "inputTokens": 0,
        "outputTokens": 0,
        "cacheReadTokens": 0,
        "cacheWriteTokens": 0,
        "costUsd": 0.0,
        "toolCalls": 0,
        "toolOk": 0,
        "toolFailed": 0,
        "totalTimeMs": 0,
        "firstTokenMs": 0,
        "firstTokenSamples": 0,
    }


def _add_model(target: dict[str, Any], row: sqlite3.Row) -> None:
    target["modelCalls"] += int(row["calls"] or 0)
    target["modelOk"] += int(row["ok_count"] or 0)
    target["modelFailed"] += int(row["fail_count"] or 0)
    target["modelRetries"] += int(row["retry_count"] or 0)
    target["inputTokens"] += int(row["input_tokens"] or 0)
    target["outputTokens"] += int(row["output_tokens"] or 0)
    target["cacheReadTokens"] += int(row["cache_read_tokens"] or 0)
    target["cacheWriteTokens"] += int(row["cache_write_tokens"] or 0)
    target["costUsd"] += float(row["cost_usd"] or 0.0)
    target["totalTimeMs"] += int(row["total_time_ms"] or 0)
    target["firstTokenMs"] += int(row["first_token_ms"] or 0)
    target["firstTokenSamples"] += int(row["first_samples"] or 0)


def _finish_metric(metric: dict[str, Any]) -> dict[str, Any]:
    calls = int(metric.get("modelCalls") or 0)
    known = int(metric.get("modelOk") or 0) + int(metric.get("modelFailed") or 0)
    first_samples = int(metric.get("firstTokenSamples") or 0)
    cache_denominator = (
        int(metric.get("inputTokens") or 0)
        + int(metric.get("cacheReadTokens") or 0)
        + int(metric.get("cacheWriteTokens") or 0)
    )
    return {
        **metric,
        "successRate": (float(metric["modelOk"]) / known * 100.0) if known else None,
        "cacheRate": (float(metric["cacheReadTokens"]) / cache_denominator * 100.0)
        if cache_denominator
        else None,
        "avgTotalMs": (float(metric["totalTimeMs"]) / calls) if calls else None,
        "avgFirstTokenMs": (float(metric["firstTokenMs"]) / first_samples)
        if first_samples
        else None,
    }


def _date_key(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), _BEIJING).date().isoformat()


def _timeline_key(ts: int, bucket_hours: int) -> str:
    local = datetime.fromtimestamp(int(ts), _BEIJING)
    bucket = local.replace(
        hour=(local.hour // bucket_hours) * bucket_hours,
        minute=0,
        second=0,
        microsecond=0,
    )
    return bucket.isoformat()


def _percentile(values: list[int], quantile: float) -> float | None:
    ordered = sorted(max(0, int(value)) for value in values if int(value) > 0)
    if not ordered:
        return None
    position = (len(ordered) - 1) * max(0.0, min(1.0, quantile))
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - lower
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction)


def _percentile_payload(values: list[int]) -> dict[str, Any]:
    return {
        "samples": len(values),
        "p50Ms": _percentile(values, 0.50),
        "p90Ms": _percentile(values, 0.90),
        "p95Ms": _percentile(values, 0.95),
        "p99Ms": _percentile(values, 0.99),
        "maxMs": float(max(values)) if values else None,
    }


def _timeline_rows(start: datetime, end: datetime, bucket_hours: int) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    cursor = start
    while cursor < end:
        key = cursor.isoformat()
        rows[key] = {
            "bucket": key,
            "bucketHours": bucket_hours,
            **_metric(),
            "reasoningMs": 0,
            "observedRuns": 0,
        }
        cursor += timedelta(hours=bucket_hours)
    return rows


def _folder_payload(rows: list[sqlite3.Row]) -> tuple[list[dict[str, Any]], dict[str, set[str]]]:
    raw = [
        {
            "uuid": str(row["folder_uuid"] or ""),
            "parentUuid": str(row["parent_uuid"] or ""),
            "name": str(row["name"] or "未命名目录"),
        }
        for row in rows
    ]
    children: dict[str, list[str]] = defaultdict(list)
    by_uuid = {item["uuid"]: item for item in raw}
    for item in raw:
        children[item["parentUuid"]].append(item["uuid"])

    descendants: dict[str, set[str]] = {}
    for folder_uuid in by_uuid:
        found: set[str] = set()
        pending = [folder_uuid]
        while pending:
            current = pending.pop()
            if current in found:
                continue
            found.add(current)
            pending.extend(children.get(current, ()))
        descendants[folder_uuid] = found

    def depth(item: dict[str, Any]) -> int:
        seen: set[str] = set()
        parent = item["parentUuid"]
        value = 0
        while parent and parent in by_uuid and parent not in seen:
            seen.add(parent)
            value += 1
            parent = by_uuid[parent]["parentUuid"]
        return value

    for item in raw:
        item["depth"] = depth(item)
    raw.sort(key=lambda item: (item["depth"], item["name"].casefold(), item["uuid"]))
    return raw, descendants


def _placeholders(values: list[Any]) -> str:
    return ",".join("?" for _ in values)


def _empty_statistics(
    *,
    days: int,
    folder_uuid: str,
    folders: list[dict[str, Any]],
    start: datetime,
    end: datetime,
    previous_start: datetime,
) -> dict[str, Any]:
    daily = []
    for index in range(days):
        day = (start + timedelta(days=index)).date().isoformat()
        daily.append({"date": day, **_finish_metric(_metric()), "reasoningMs": 0})
    bucket_hours = _TIMELINE_BUCKET_HOURS[days]
    timeline = [
        _finish_metric(item)
        | {
            "bucket": item["bucket"],
            "bucketHours": bucket_hours,
            "reasoningMs": 0,
            "observedRuns": 0,
        }
        for item in _timeline_rows(start, end, bucket_hours).values()
    ]
    return {
        "ok": True,
        "generatedAt": int(time.time() * 1000),
        "timezone": "Asia/Shanghai",
        "period": {
            "days": days,
            "start": start.date().isoformat(),
            "end": (end - timedelta(days=1)).date().isoformat(),
            "previousStart": previous_start.date().isoformat(),
            "previousEnd": (start - timedelta(days=1)).date().isoformat(),
            "folderUuid": folder_uuid,
            "bucketHours": bucket_hours,
        },
        "folders": folders,
        "summary": {"current": _finish_metric(_metric()), "previous": _finish_metric(_metric())},
        "daily": daily,
        "timeline": timeline,
        "models": [],
        "modelDaily": [],
        "modelTimeline": [],
        "tools": [],
        "errors": [],
        "latency": {
            key: {
                "calls": 0,
                "avgTotalMs": None,
                "avgTimedTotalMs": None,
                "avgConnectMs": None,
                "avgFirstTokenMs": None,
                "timingSamples": 0,
                "buckets": [0] * 6,
                "percentiles": {
                    "total": _percentile_payload([]),
                    "firstToken": _percentile_payload([]),
                },
            }
            for key in ("all", "main", "agent")
        },
        "heatmap": [[0] * 24 for _ in range(7)],
        "thinking": {"available": False, "totalMs": 0, "observedRuns": 0, "daily": []},
    }


def read_statistics(
    db_path: str | Path, owner_chat_id: int, *, days: int = 30, folder_uuid: str = ""
) -> dict[str, Any]:
    """Aggregate one bounded dashboard snapshot using only owner/time indexed reads.

    The query range includes the immediately preceding period so KPI comparisons
    do not trigger a second database pass. Model rows stay grouped by day/model;
    raw request rows and message bodies never leave SQLite.
    """
    if days not in _ALLOWED_DAYS:
        raise ValueError("days must be one of 1, 3, 7, 30, 90")

    now = datetime.now(_BEIJING)
    end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    previous_start = start - timedelta(days=days)
    range_start_ts = int(previous_start.timestamp())
    current_start_ts = int(start.timestamp())
    range_end_ts = int(end.timestamp())
    bucket_hours = _TIMELINE_BUCKET_HOURS[days]

    absolute = Path(db_path).resolve()
    connection = sqlite3.connect(f"file:{absolute}?mode=ro", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        folder_rows = connection.execute(
            """
            SELECT folder_uuid, parent_uuid, name
            FROM web_conversation_folders
            WHERE owner_chat_id=?
            ORDER BY parent_uuid, display_order, id
            """,
            (int(owner_chat_id),),
        ).fetchall()
        folders, descendants = _folder_payload(folder_rows)
        if folder_uuid and folder_uuid != _TEMPORARY_FOLDER and folder_uuid not in descendants:
            raise ValueError("folder_not_found")

        scope_sql = "SELECT internal_chat_id, conversation_uuid FROM web_conversations WHERE owner_chat_id=?"
        scope_params: list[Any] = [int(owner_chat_id)]
        if folder_uuid == _TEMPORARY_FOLDER:
            scope_sql += " AND folder_uuid=''"
        elif folder_uuid:
            selected = sorted(descendants[folder_uuid])
            scope_sql += f" AND folder_uuid IN ({_placeholders(selected)})"
            scope_params.extend(selected)
        scope_rows = connection.execute(scope_sql, scope_params).fetchall()
        chat_ids = [int(row["internal_chat_id"]) for row in scope_rows]
        conversation_uuids = [str(row["conversation_uuid"]) for row in scope_rows]
        if not chat_ids:
            return _empty_statistics(
                days=days,
                folder_uuid=folder_uuid,
                folders=folders,
                start=start,
                end=end,
                previous_start=previous_start,
            )

        chat_marks = _placeholders(chat_ids)
        bounded_params = [*chat_ids, range_start_ts, range_end_ts]
        user_rows = connection.execute(
            f"""
            SELECT chat_id, created_at
            FROM messages
            WHERE chat_id IN ({chat_marks})
              AND created_at>=? AND created_at<?
              AND role='user' AND COALESCE(task_uuid,'')=''
            """,
            bounded_params,
        ).fetchall()

        model_rows = connection.execute(
            f"""
            SELECT
              date(created_at,'unixepoch','+8 hours') AS day,
              CAST((created_at + 28800) / 3600 AS INTEGER) * 3600 - 28800 AS hour_ts,
              model,
              CASE WHEN call_kind='agent_request' THEN 'agent' ELSE 'main' END AS source,
              status,
              COALESCE(NULLIF(error_type,''),'未分类') AS error_type,
              CASE
                WHEN total_time_ms * 1.0 / COALESCE(NULLIF(model_call_count,0),1) < 5000 THEN 0
                WHEN total_time_ms * 1.0 / COALESCE(NULLIF(model_call_count,0),1) < 15000 THEN 1
                WHEN total_time_ms * 1.0 / COALESCE(NULLIF(model_call_count,0),1) < 30000 THEN 2
                WHEN total_time_ms * 1.0 / COALESCE(NULLIF(model_call_count,0),1) < 60000 THEN 3
                WHEN total_time_ms * 1.0 / COALESCE(NULLIF(model_call_count,0),1) < 120000 THEN 4
                ELSE 5
              END AS latency_bucket,
              SUM(COALESCE(NULLIF(model_call_count,0),1)) AS calls,
              SUM(COALESCE(model_ok_count,CASE WHEN status='ok' THEN 1 ELSE 0 END)) AS ok_count,
              SUM(COALESCE(model_fail_count,CASE WHEN status NOT IN ('ok','cancelled') THEN 1 ELSE 0 END)) AS fail_count,
              SUM(COALESCE(model_retry_count,0)) AS retry_count,
              SUM(COALESCE(input_tokens,0)) AS input_tokens,
              SUM(COALESCE(output_tokens,0)) AS output_tokens,
              SUM(COALESCE(cache_read_tokens,0)) AS cache_read_tokens,
              SUM(COALESCE(cache_write_tokens,0)) AS cache_write_tokens,
              SUM(COALESCE(cost_usd,0)) AS cost_usd,
              SUM(COALESCE(connect_ms,0)) AS connect_ms,
              SUM(COALESCE(first_token_ms,0)) AS first_token_ms,
              SUM(COALESCE(total_time_ms,0)) AS total_time_ms,
              SUM(CASE WHEN first_token_ms>0 THEN COALESCE(NULLIF(model_ok_count,0),COALESCE(NULLIF(model_call_count,0),1)) ELSE 0 END) AS first_samples,
              SUM(CASE WHEN connect_ms>0 AND first_token_ms>0 AND total_time_ms>0 THEN connect_ms ELSE 0 END) AS timed_connect_ms,
              SUM(CASE WHEN connect_ms>0 AND first_token_ms>0 AND total_time_ms>0 THEN first_token_ms ELSE 0 END) AS timed_first_token_ms,
              SUM(CASE WHEN connect_ms>0 AND first_token_ms>0 AND total_time_ms>0 THEN total_time_ms ELSE 0 END) AS timed_total_time_ms,
              SUM(CASE WHEN connect_ms>0 AND first_token_ms>0 AND total_time_ms>0 THEN COALESCE(NULLIF(model_ok_count,0),COALESCE(NULLIF(model_call_count,0),1)) ELSE 0 END) AS timing_samples
            FROM model_calls
            WHERE chat_id IN ({chat_marks}) AND created_at>=? AND created_at<?
            GROUP BY day, hour_ts, model, source, status, error_type, latency_bucket
            """,
            bounded_params,
        ).fetchall()

        latency_sample_rows = connection.execute(
            f"""
            SELECT CASE WHEN call_kind='agent_request' THEN 'agent' ELSE 'main' END AS source,
                   total_time_ms, first_token_ms
            FROM model_calls
            WHERE chat_id IN ({chat_marks}) AND created_at>=? AND created_at<?
              AND model_call_count=1
            """,
            [*chat_ids, current_start_ts, range_end_ts],
        ).fetchall()

        tool_rows = connection.execute(
            f"""
            SELECT date(created_at,'unixepoch','+8 hours') AS day, tool_name, status,
                   COUNT(*) AS calls, SUM(COALESCE(duration_ms,0)) AS duration_ms
            FROM tool_calls
            WHERE chat_id IN ({chat_marks}) AND created_at>=? AND created_at<?
            GROUP BY day, tool_name, status
            """,
            bounded_params,
        ).fetchall()

        reasoning_rows: list[sqlite3.Row] = []
        if conversation_uuids:
            conversation_marks = _placeholders(conversation_uuids)
            reasoning_rows = connection.execute(
                f"""
                SELECT date(created_at_ms/1000,'unixepoch','+8 hours') AS day,
                       CAST((created_at_ms / 1000 + 28800) / 3600 AS INTEGER) * 3600 - 28800 AS hour_ts,
                       SUM(MAX(0,CAST(COALESCE(json_extract(payload_json,'$.reasoningMs'),0) AS INTEGER))) AS reasoning_ms,
                       SUM(CASE WHEN CAST(COALESCE(json_extract(payload_json,'$.reasoningMs'),0) AS INTEGER)>0 THEN 1 ELSE 0 END) AS observed_runs
                FROM web_operations
                WHERE conversation_uuid IN ({conversation_marks})
                  AND op_type='stats' AND created_at_ms>=? AND created_at_ms<?
                GROUP BY day, hour_ts
                """,
                [*conversation_uuids, range_start_ts * 1000, range_end_ts * 1000],
            ).fetchall()
    finally:
        connection.close()

    current = _metric()
    previous = _metric()
    current_active: set[int] = set()
    previous_active: set[int] = set()
    daily: dict[str, dict[str, Any]] = {}
    timeline = _timeline_rows(start, end, bucket_hours)
    timeline_active: dict[str, set[int]] = defaultdict(set)
    heatmap = [[0] * 24 for _ in range(7)]
    for index in range(days):
        key = (start + timedelta(days=index)).date().isoformat()
        daily[key] = {"date": key, **_metric(), "reasoningMs": 0}

    for row in user_rows:
        ts = int(row["created_at"] or 0)
        chat_id = int(row["chat_id"] or 0)
        if ts >= current_start_ts:
            current["userTurns"] += 1
            current_active.add(chat_id)
            key = _date_key(ts)
            if key in daily:
                daily[key]["userTurns"] += 1
                bucket_key = _timeline_key(ts, bucket_hours)
                if bucket_key in timeline:
                    timeline[bucket_key]["userTurns"] += 1
                    timeline_active[bucket_key].add(chat_id)
                local = datetime.fromtimestamp(ts, _BEIJING)
                heatmap[local.weekday()][local.hour] += 1
        else:
            previous["userTurns"] += 1
            previous_active.add(chat_id)

    model_daily: dict[tuple[str, str], dict[str, Any]] = {}
    model_timeline: dict[tuple[str, str], dict[str, Any]] = {}
    model_totals: dict[str, dict[str, Any]] = {}
    errors: dict[str, int] = defaultdict(int)
    latency = {
        key: {
            "calls": 0,
            "totalMs": 0,
            "timedTotalMs": 0,
            "timedConnectMs": 0,
            "timedFirstMs": 0,
            "timingSamples": 0,
            "buckets": [0] * 6,
        }
        for key in ("all", "main", "agent")
    }
    latency_samples = {key: {"total": [], "firstToken": []} for key in ("all", "main", "agent")}
    for row in latency_sample_rows:
        source = str(row["source"] or "main")
        total_ms = int(row["total_time_ms"] or 0)
        first_token_ms = int(row["first_token_ms"] or 0)
        for latency_key in ("all", source):
            if total_ms > 0:
                latency_samples[latency_key]["total"].append(total_ms)
            if first_token_ms > 0:
                latency_samples[latency_key]["firstToken"].append(first_token_ms)

    for row in model_rows:
        day = str(row["day"] or "")
        is_current = day >= start.date().isoformat()
        target = current if is_current else previous
        _add_model(target, row)
        if not is_current:
            continue
        if day in daily:
            _add_model(daily[day], row)
        bucket_key = _timeline_key(int(row["hour_ts"] or 0), bucket_hours)
        if bucket_key in timeline:
            _add_model(timeline[bucket_key], row)
        model = str(row["model"] or "未知模型")
        model_key = (day, model)
        if model_key not in model_daily:
            model_daily[model_key] = {"date": day, "model": model, **_metric()}
        _add_model(model_daily[model_key], row)
        timeline_model_key = (bucket_key, model)
        if timeline_model_key not in model_timeline:
            model_timeline[timeline_model_key] = {
                "bucket": bucket_key,
                "bucketHours": bucket_hours,
                "model": model,
                **_metric(),
            }
        _add_model(model_timeline[timeline_model_key], row)
        if model not in model_totals:
            model_totals[model] = {"model": model, **_metric()}
        _add_model(model_totals[model], row)
        failed = int(row["fail_count"] or 0)
        if failed:
            errors[str(row["error_type"] or "未分类")] += failed
        source = str(row["source"] or "main")
        for latency_key in ("all", source):
            item = latency[latency_key]
            item["calls"] += int(row["calls"] or 0)
            item["totalMs"] += int(row["total_time_ms"] or 0)
            item["timedTotalMs"] += int(row["timed_total_time_ms"] or 0)
            item["timedConnectMs"] += int(row["timed_connect_ms"] or 0)
            item["timedFirstMs"] += int(row["timed_first_token_ms"] or 0)
            item["timingSamples"] += int(row["timing_samples"] or 0)
            item["buckets"][int(row["latency_bucket"] or 0)] += int(row["calls"] or 0)

    tools: dict[str, dict[str, Any]] = {}
    for row in tool_rows:
        day = str(row["day"] or "")
        calls = int(row["calls"] or 0)
        ok = calls if str(row["status"] or "") == "ok" else 0
        failed = calls - ok
        target = current if day >= start.date().isoformat() else previous
        target["toolCalls"] += calls
        target["toolOk"] += ok
        target["toolFailed"] += failed
        if day < start.date().isoformat():
            continue
        if day in daily:
            daily[day]["toolCalls"] += calls
            daily[day]["toolOk"] += ok
            daily[day]["toolFailed"] += failed
        name = str(row["tool_name"] or "未知工具")
        item = tools.setdefault(
            name, {"name": name, "calls": 0, "ok": 0, "failed": 0, "durationMs": 0}
        )
        item["calls"] += calls
        item["ok"] += ok
        item["failed"] += failed
        item["durationMs"] += int(row["duration_ms"] or 0)

    thinking_daily: dict[str, dict[str, Any]] = {}
    thinking_total = 0
    thinking_runs = 0
    for row in reasoning_rows:
        day = str(row["day"] or "")
        if day < start.date().isoformat():
            continue
        reasoning_ms = int(row["reasoning_ms"] or 0)
        observed_runs = int(row["observed_runs"] or 0)
        thinking_total += reasoning_ms
        thinking_runs += observed_runs
        if day in daily:
            daily[day]["reasoningMs"] += reasoning_ms
        day_item = thinking_daily.setdefault(
            day, {"date": day, "reasoningMs": 0, "observedRuns": 0}
        )
        day_item["reasoningMs"] += reasoning_ms
        day_item["observedRuns"] += observed_runs
        bucket_key = _timeline_key(int(row["hour_ts"] or 0), bucket_hours)
        if bucket_key in timeline:
            timeline[bucket_key]["reasoningMs"] += reasoning_ms
            timeline[bucket_key]["observedRuns"] += observed_runs

    current["activeConversations"] = len(current_active)
    previous["activeConversations"] = len(previous_active)
    daily_active: dict[str, set[int]] = defaultdict(set)
    for row in user_rows:
        if int(row["created_at"] or 0) >= current_start_ts:
            daily_active[_date_key(int(row["created_at"] or 0))].add(int(row["chat_id"] or 0))
    for day, ids in daily_active.items():
        if day in daily:
            daily[day]["activeConversations"] = len(ids)
    for bucket_key, ids in timeline_active.items():
        if bucket_key in timeline:
            timeline[bucket_key]["activeConversations"] = len(ids)

    finished_latency: dict[str, Any] = {}
    for key, item in latency.items():
        calls = int(item["calls"] or 0)
        timing_samples = int(item["timingSamples"] or 0)
        avg_timed_total = float(item["timedTotalMs"]) / timing_samples if timing_samples else None
        avg_first = float(item["timedFirstMs"]) / timing_samples if timing_samples else None
        avg_connect = float(item["timedConnectMs"]) / timing_samples if timing_samples else None
        finished_latency[key] = {
            "calls": calls,
            "avgTotalMs": float(item["totalMs"]) / calls if calls else None,
            "avgTimedTotalMs": avg_timed_total,
            "avgConnectMs": avg_connect,
            "avgFirstTokenMs": avg_first,
            "timingSamples": timing_samples,
            "buckets": item["buckets"],
            "percentiles": {
                "total": _percentile_payload(latency_samples[key]["total"]),
                "firstToken": _percentile_payload(latency_samples[key]["firstToken"]),
            },
        }

    finished_tools = []
    for item in tools.values():
        calls = int(item["calls"] or 0)
        known = int(item["ok"] or 0) + int(item["failed"] or 0)
        finished_tools.append(
            {
                **item,
                "avgDurationMs": float(item["durationMs"]) / calls if calls else None,
                "successRate": float(item["ok"]) / known * 100.0 if known else None,
            }
        )
    finished_tools.sort(key=lambda item: (-item["calls"], item["name"].casefold()))

    finished_models = [_finish_metric(item) for item in model_totals.values()]
    finished_models.sort(key=lambda item: (-int(item["modelCalls"]), item["model"].casefold()))
    finished_model_daily = [_finish_metric(item) for item in model_daily.values()]
    finished_model_daily.sort(key=lambda item: (item["date"], item["model"].casefold()))
    finished_timeline = [
        _finish_metric(item)
        | {
            "bucket": item["bucket"],
            "bucketHours": bucket_hours,
            "reasoningMs": item["reasoningMs"],
            "observedRuns": item["observedRuns"],
        }
        for item in timeline.values()
    ]
    finished_model_timeline = [_finish_metric(item) for item in model_timeline.values()]
    finished_model_timeline.sort(key=lambda item: (item["bucket"], item["model"].casefold()))

    return {
        "ok": True,
        "generatedAt": int(time.time() * 1000),
        "timezone": "Asia/Shanghai",
        "period": {
            "days": days,
            "start": start.date().isoformat(),
            "end": (end - timedelta(days=1)).date().isoformat(),
            "previousStart": previous_start.date().isoformat(),
            "previousEnd": (start - timedelta(days=1)).date().isoformat(),
            "folderUuid": folder_uuid,
            "bucketHours": bucket_hours,
        },
        "folders": folders,
        "summary": {"current": _finish_metric(current), "previous": _finish_metric(previous)},
        "daily": [
            _finish_metric(item) | {"date": item["date"], "reasoningMs": item["reasoningMs"]}
            for item in daily.values()
        ],
        "timeline": finished_timeline,
        "models": finished_models,
        "modelDaily": finished_model_daily,
        "modelTimeline": finished_model_timeline,
        "tools": finished_tools[:12],
        "errors": [
            {"type": name, "count": count}
            for name, count in sorted(errors.items(), key=lambda item: (-item[1], item[0]))[:6]
        ],
        "latency": finished_latency,
        "heatmap": heatmap,
        "thinking": {
            "available": thinking_runs > 0,
            "totalMs": thinking_total,
            "observedRuns": thinking_runs,
            "daily": [thinking_daily[key] for key in sorted(thinking_daily)],
        },
    }


class WebAdminStatisticsMixin:
    async def handle_api_statistics(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        try:
            days = int(request.query.get("days") or 30)
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "invalid_days"}, status=400)
        if days not in _ALLOWED_DAYS:
            return web.json_response({"ok": False, "error": "invalid_days"}, status=400)
        folder_uuid = str(request.query.get("folderUuid") or "").strip()
        key = (int(session.chat_id), days, folder_uuid)
        now = time.monotonic()
        cache: dict[tuple[int, int, str], tuple[float, dict[str, Any]]] = getattr(
            self, "_statistics_cache", {}
        )
        cached = cache.get(key)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            return web.json_response(cached[1], headers={"Cache-Control": "private, no-store"})

        lock: asyncio.Lock = getattr(self, "_statistics_cache_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._statistics_cache_lock = lock
        async with lock:
            cache = getattr(self, "_statistics_cache", {})
            cached = cache.get(key)
            now = time.monotonic()
            if cached and now - cached[0] < _CACHE_TTL_SECONDS:
                return web.json_response(cached[1], headers={"Cache-Control": "private, no-store"})
            inflight: dict[tuple[int, int, str], asyncio.Task[dict[str, Any]]] = getattr(
                self, "_statistics_inflight", {}
            )
            task = inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    asyncio.to_thread(
                        read_statistics,
                        self.db.path,
                        int(session.chat_id),
                        days=days,
                        folder_uuid=folder_uuid,
                    )
                )
                inflight[key] = task
                self._statistics_inflight = inflight
        try:
            # A disconnected browser must not cancel a query already shared by
            # another request for the same owner/range snapshot.
            payload = await asyncio.shield(task)
        except ValueError as exc:
            async with lock:
                if getattr(self, "_statistics_inflight", {}).get(key) is task:
                    self._statistics_inflight.pop(key, None)
            error = str(exc)
            status = 404 if error == "folder_not_found" else 400
            return web.json_response({"ok": False, "error": error}, status=status)
        except Exception:
            async with lock:
                if getattr(self, "_statistics_inflight", {}).get(key) is task:
                    self._statistics_inflight.pop(key, None)
            raise

        async with lock:
            if getattr(self, "_statistics_inflight", {}).get(key) is task:
                self._statistics_inflight.pop(key, None)
            cache = getattr(self, "_statistics_cache", {})
            cache[key] = (time.monotonic(), payload)
            if len(cache) > 36:
                oldest = sorted(cache.items(), key=lambda item: item[1][0])[: len(cache) - 24]
                for stale_key, _ in oldest:
                    cache.pop(stale_key, None)
            self._statistics_cache = cache
        return web.json_response(payload, headers={"Cache-Control": "private, no-store"})
