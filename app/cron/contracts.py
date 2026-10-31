"""Cron wire contracts: no inbound event, batching or queue configuration."""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from pydantic import Field, model_validator

from app.webhooks.contracts import SERVER_CHANGES, ConversationRetention, Model, TargetRunConfig


class CronError(Exception):
    def __init__(self, code, message='', status=422, **details):
        super().__init__(message or code)
        self.status = status
        self.payload = {'code': code, 'message': message or code, **details}


def timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('时间必须包含时区')
    return int(dt.timestamp() * 1000)


class Schedule(Model):
    kind: Literal['at', 'every', 'cron'] = 'cron'
    timezone: str = 'Asia/Shanghai'
    at: str | None = None
    every_seconds: int | None = Field(None, ge=1, strict=True)
    anchor_at: str | None = None
    expression: str | None = '0 9 * * *'
    start_at: str | None = None  # Inclusive boundary for a repeated calendar rule.
    end_at: str | None = None  # Exclusive boundary; UI end dates include their full day.
    second: int = Field(0, ge=0, le=59, strict=True)
    skip_holidays: bool = Field(False, strict=True)

    @model_validator(mode='after')
    def valid(self):
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('无效IANA时区') from None
        if self.kind == 'at':
            if not self.at:
                raise ValueError('一次性任务必须设置执行时间')
            timestamp(self.at)
        if self.kind == 'every':
            if self.every_seconds is None:
                raise ValueError('间隔必须是正整数秒')
            if self.anchor_at:
                timestamp(self.anchor_at)
        if self.start_at or self.end_at:
            if self.kind == 'at':
                raise ValueError('一次性任务使用执行时间，不设置重复范围')
            start = timestamp(self.start_at) if self.start_at else None
            end = timestamp(self.end_at) if self.end_at else None
            if start is not None and end is not None and start >= end:
                raise ValueError('结束时间必须晚于开始时间')
        if self.kind != 'cron' and self.second:
            raise ValueError('执行秒字段仅适用于 Cron，间隔与一次性时间直接包含秒')
        if self.kind == 'cron':
            if not self.expression or len(self.expression.split()) != 5 or not croniter.is_valid(self.expression):
                raise ValueError('Cron必须是合法的五段表达式')
        return self


class Retry(Model):
    max_attempts: int = Field(1, ge=1, le=5, strict=True)
    backoff_seconds: list[float] = Field(default_factory=lambda: [2, 10, 30, 60])

    @model_validator(mode='after')
    def valid(self):
        if any(v < 0 or v > 3600 for v in self.backoff_seconds):
            raise ValueError('重试间隔必须在0–3600秒之间')
        return self


Outcome = Literal['completed', 'failed', 'timed_out', 'tokens_exceeded', 'cost_exceeded', 'cancelled']


class Script(Model):
    enabled: bool = Field(False, strict=True)
    runtime: Literal['python', 'node'] = 'python'
    code: str = ''
    timeout_seconds: float = Field(30, gt=0, le=3600)
    retry: Retry = Field(default_factory=Retry)
    on_error: Literal['fail', 'continue'] = 'fail'

    @model_validator(mode='after')
    def valid(self):
        if self.enabled and not self.code.strip():
            raise ValueError('启用脚本必须提供代码')
        return self


class PostScript(Script):
    on_outcomes: list[Outcome] = Field(default_factory=lambda: [
        'completed', 'failed', 'timed_out', 'tokens_exceeded', 'cost_exceeded', 'cancelled'])


class Notifications(Model):
    mode: Literal['inherit', 'silent', 'telegram', 'push', 'both'] = 'inherit'
    errors_only: bool = Field(False, strict=True)


class JobConfig(Model):
    schedule: Schedule = Field(default_factory=Schedule)
    run_config: TargetRunConfig = Field(default_factory=TargetRunConfig)
    instructions: str = ''
    allow_server_changes: bool = Field(False, strict=True, description=SERVER_CHANGES)
    timeout_seconds: float | None = Field(None, gt=0)
    total_tokens_budget: int | None = Field(None, gt=0, strict=True)
    cost_budget_usd: float | None = Field(None, gt=0)
    notifications: Notifications = Field(default_factory=Notifications)
    conversation_retention: ConversationRetention = Field(default_factory=ConversationRetention)
    pre: Script = Field(default_factory=Script)
    post: PostScript = Field(default_factory=PostScript)
