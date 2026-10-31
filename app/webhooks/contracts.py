"""Shared strict wire contracts. Untrusted payloads never select execution settings."""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic.alias_generators import to_camel


class WebhookError(Exception):
    def __init__(self, code, message='', status=422, *, details=None, retryable=False, **extra):
        super().__init__(message or code)
        self.status = status
        self.payload = dict(code=code, message=message or code, retryable=retryable, details=details or {}, **extra)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True, allow_nan=False)


def validate_material(value):
    """The ingress/worker shared strict JSON + UTF-8 representation boundary."""
    try:
        dumps(value).encode('utf-8',errors='strict')
    except (TypeError,ValueError,UnicodeError,RecursionError):
        raise WebhookError('invalid_material') from None
    return value


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat().replace('+00:00', 'Z') if ms is not None else None


class Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra='forbid', allow_inf_nan=False)

    def wire(self):
        return self.model_dump(by_alias=True)


# Shared by Cron tasks and Webhook endpoints: the per-task opt-in the Servers and InstanceControl tools check.
SERVER_CHANGES = ('Default false: runs of this task may only read servers and instances (Servers and InstanceControl read actions). '
                  'true lets its unattended runs change them as well (commands, file writes, node and instance operations). '
                  'Set it only when the user explicitly asks; the user turns it on in the page knowing the risk.')


class Ingress(Model):
    max_body_bytes: int = Field(1048576, ge=1024, le=16777216)
    requests_per_minute: int = Field(6000, ge=1, le=600000)


class Queue(Model):
    max_events: int = Field(50000, ge=1, le=1000000)
    max_bytes: int = Field(268435456, ge=1048576, le=8589934592)


class Concurrency(Model):
    pre_scripts: int = Field(4, ge=1, le=64)
    post_scripts: int = Field(2, ge=1, le=32)
    auto_model_runs: int = Field(2, ge=1, le=32)


class Environments(Model):
    python_path: str | None = None
    node_path: str | None = None
    default_cwd: str = Field(default_factory=lambda: str(Path.cwd()))
    default_timeout_seconds: float = Field(30, gt=0, le=3600)
    max_timeout_seconds: float = Field(600, gt=0, le=3600)
    max_stdout_bytes: int = Field(262144, ge=1)
    max_stderr_bytes: int = Field(1048576, ge=1)


class BatchLimits(Model):
    max_batch_bytes: int = Field(262144, ge=1)
    max_batch_events: int = Field(1000, ge=1, le=10000)


class Retention(Model):
    payload_days: int | None = Field(7, ge=1)
    script_log_days: int | None = Field(14, ge=1)
    processing_days: int | None = Field(90, ge=1)
    idempotency_days: int | None = Field(None, ge=1)
    metric_sample_days: int = Field(30, ge=1)
    metric_rollup_days: int = Field(365, ge=1)
    business_record_days: int | None = Field(90, ge=1)


class TelemetryLimits(Model):
    max_dimensions: int = Field(8, ge=1, le=32)
    max_categories_per_dimension: int = Field(100, ge=1, le=10000)
    max_series_per_endpoint: int = Field(1000, ge=1, le=100000)
    max_business_record_bytes: int = Field(16384, ge=1)
    gauge_default_stale_seconds: int = Field(300, ge=1)


class NotificationLimits(Model):
    digest_seconds: int = Field(60, ge=1, le=3600)


class WebhooksConfig(Model):
    enabled: bool = True
    public_base_url: str | None = None
    ingress: Ingress = Field(default_factory=Ingress)
    queue: Queue = Field(default_factory=Queue)
    concurrency: Concurrency = Field(default_factory=Concurrency)
    scripts: Environments = Field(default_factory=Environments)
    batching: BatchLimits = Field(default_factory=BatchLimits)
    retention: Retention = Field(default_factory=Retention)
    telemetry: TelemetryLimits = Field(default_factory=TelemetryLimits)
    notifications: NotificationLimits = Field(default_factory=NotificationLimits)

    @model_validator(mode='after')
    def validate_relations(self):
        if self.public_base_url:
            u = urlsplit(self.public_base_url)
            if u.scheme not in ('http', 'https') or not u.netloc or u.username or u.password or u.query or u.fragment:
                raise ValueError('publicBaseUrl requires http(s) without credentials/query/fragment')
        if self.scripts.default_timeout_seconds > self.scripts.max_timeout_seconds:
            raise ValueError('defaultTimeoutSeconds exceeds maximum')
        r = self.retention
        if r.metric_rollup_days < r.metric_sample_days:
            raise ValueError('metricRollupDays must cover metricSampleDays')
        if r.idempotency_days is not None and r.processing_days is not None and r.idempotency_days < r.processing_days:
            raise ValueError('idempotencyDays must cover processingDays')
        if not Path(self.scripts.default_cwd).is_absolute():
            raise ValueError('defaultCwd must be absolute')
        return self


class Scope(Model):
    type: Literal['folder', 'conversation']
    id: str = Field(min_length=1, max_length=128)


class TargetRunConfig(Model):
    main_model: str | None = Field(None, min_length=1)
    main_thinking_level: str | None = Field(None, min_length=1)
    main_fast_mode: bool | None = Field(None, strict=True)


class ConversationRetention(Model):
    """Created conversations beyond either limit are deleted automatically; unset keeps them all."""
    keep_last: int | None = Field(None, ge=1, le=10000, strict=True)
    keep_days: int | None = Field(None, ge=1, le=3650, strict=True)

    @property
    def active(self):
        return self.keep_last is not None or self.keep_days is not None


class Target(Model):
    mode: Literal['newConversation', 'fixedConversation'] = 'newConversation'
    conversation_id: str | None = None
    run_config: TargetRunConfig = Field(default_factory=TargetRunConfig)
    conversation_retention: ConversationRetention = Field(default_factory=ConversationRetention)

    @model_validator(mode='after')
    def validate_run_config(self):
        if self.mode=='fixedConversation' and any(v is not None for v in self.run_config.wire().values()):
            raise ValueError('fixedConversation does not override conversation runConfig')
        if self.mode=='fixedConversation' and self.conversation_retention.active:
            raise ValueError('fixedConversation does not create conversations to retain')
        return self


class Processing(Model):
    instructions: str = ''
    event_template: str | None = None
    result_schema: dict | None = None
    script_scheduling: Literal['independent', 'serial'] = 'independent'
    allow_server_changes: bool = Field(False, strict=True, description=SERVER_CHANGES)


class Identity(Model):
    body_id_path: str | None = None


class Correlation(Model):
    id_path: str | None = None
    origin_path: str | None = None
    ignore_own_echo: bool = False
    own_origin_values: list[str] = Field(default_factory=list)


class Retry(Model):
    max_attempts: int = Field(1, ge=1, le=5)
    backoff_seconds: list[float] = Field(default_factory=lambda: [2, 10, 30, 60])
    idempotency_declaration: str | None = None

    @model_validator(mode='after')
    def validate_retry(self):
        if any(not math.isfinite(v) or v < 0 for v in self.backoff_seconds):
            raise ValueError('invalid retry interval')
        if self.max_attempts > 1 and not (self.idempotency_declaration or '').strip():
            raise ValueError('retry requires downstream actionKey idempotency declaration')
        return self


class Script(Model):
    enabled: bool = False
    runtime: Literal['python', 'node'] = 'python'
    code: str = ''
    timeout_seconds: float | None = Field(None, gt=0, le=3600)
    retry: Retry = Field(default_factory=Retry)
    on_error: Literal['hold', 'continueModel'] = 'hold'
    on_outcomes: list[Literal['normal', 'partialFailure', 'failed', 'cancelled']] = Field(default_factory=lambda: ['normal'])

    @model_validator(mode='after')
    def validate_script(self):
        if self.enabled and not self.code.strip():
            raise ValueError('enabled script requires code')
        return self


class Batching(Model):
    enabled: bool = True
    idle_seconds: float | None = Field(3, gt=0)
    max_wait_seconds: float | None = Field(30, gt=0)
    max_events: int = Field(100, ge=1)
    max_bytes: int = Field(65536, ge=1)
    oversize_event: Literal['hold'] = 'hold'


class Expiry(Model):
    ttl_seconds: float | None = Field(None, gt=0)
    basis: Literal['receivedAt', 'sourceField'] = 'receivedAt'
    timestamp_path: str | None = None
    source_time_format: Literal['rfc3339', 'unixSeconds'] = 'rfc3339'
    missing_timestamp: Literal['hold', 'useReceivedAt'] = 'hold'
    max_future_skew_seconds: float = Field(300, ge=0)
    on_expired: Literal['expire', 'needsReview'] = 'expire'

    @model_validator(mode='after')
    def validate_source(self):
        if self.basis == 'sourceField' and not self.timestamp_path:
            raise ValueError('sourceField requires timestampPath')
        return self


class Limits(Model):
    requests_per_minute: int | None = Field(None, ge=1)
    pending_events: int | None = Field(None, ge=1)
    pending_bytes: int | None = Field(None, ge=1)
    pre_concurrency: int | None = Field(None, ge=1)
    post_concurrency: int | None = Field(None, ge=1)
    auto_model_concurrency: int | None = Field(None, ge=1)


class Dimension(Model):
    name: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]*$')
    path: str
    kind: Literal['category', 'identifier', 'text']
    missing: Literal['omit', 'unknown', 'rejectMetric'] = 'omit'
    max_length: int = Field(128, ge=1, le=4096)
    allowed_values: list[str] | None = None
    max_categories: int | None = Field(None, ge=1)


class Metric(Model):
    name: str = Field(pattern=r'^custom_[A-Za-z0-9_]+$')
    type: Literal['counter', 'gauge', 'histogram']
    unit: str = ''
    description: str = ''
    allowed_labels: list[str] = Field(default_factory=list)
    histogram_buckets: list[float] | None = None
    gauge_stale_seconds: float | None = Field(None, gt=0)

    @model_validator(mode='after')
    def validate_metric(self):
        if self.type == 'histogram':
            b = self.histogram_buckets
            if not b or any(not math.isfinite(v) for v in b) or any(a >= c for a, c in zip(b, b[1:])):
                raise ValueError('histogramBuckets must strictly increase; +Inf is implicit')
        elif self.histogram_buckets is not None:
            raise ValueError('histogramBuckets only apply to histogram')
        return self


class BusinessField(Model):
    name: str
    path: str
    value_type: Literal['string', 'number', 'boolean', 'null'] = 'string'
    queryable: bool = True


class Statistics(Model):
    dimensions: list[Dimension] = Field(default_factory=list)
    metric_definitions: list[Metric] = Field(default_factory=list)
    business_fields: list[BusinessField] = Field(default_factory=list)


class Notifications(Model):
    policy: Literal['inherit', 'errorsDigest', 'silent'] = 'inherit'
    digest_seconds: int | None = Field(None, ge=1, le=3600)


class EndpointConfig(Model):
    target: Target = Field(default_factory=Target)
    processing: Processing = Field(default_factory=Processing)
    idempotency: Identity = Field(default_factory=Identity)
    correlation: Correlation = Field(default_factory=Correlation)
    pre: Script = Field(default_factory=Script)
    post: Script = Field(default_factory=Script)
    batching: Batching = Field(default_factory=Batching)
    expiry: Expiry = Field(default_factory=Expiry)
    limits: Limits = Field(default_factory=Limits)
    statistics: Statistics = Field(default_factory=Statistics)
    notifications: Notifications = Field(default_factory=Notifications)


def parse_config(value, scope, system, *, enabled=False):
    try:
        value = dict(value or {})
        if scope.type == 'conversation' and 'target' not in value:
            value['target'] = {'mode': 'fixedConversation', 'conversationId': scope.id}
        c = EndpointConfig.model_validate(value)
    except (ValidationError, TypeError, ValueError) as e:
        raise WebhookError('invalid_config', str(e)) from None
    if scope.type == 'conversation' and (c.target.mode != 'fixedConversation' or c.target.conversation_id != scope.id):
        raise WebhookError('invalid_target', '会话入口必须固定当前会话')
    if c.target.mode == 'fixedConversation' and not c.target.conversation_id:
        raise WebhookError('invalid_target', '固定模式必须选择具体会话')
    if c.target.mode == 'newConversation' and c.target.conversation_id is not None:
        raise WebhookError('invalid_target', '新会话模式不应指定已有会话')
    if enabled and not c.processing.instructions.strip():
        raise WebhookError('instructions_required')
    if c.correlation.ignore_own_echo and not c.correlation.own_origin_values:
        raise WebhookError('own_origin_required')
    checks = [(c.batching.max_events, system.batching.max_batch_events, 'batching.maxEvents'),
              (c.batching.max_bytes, system.batching.max_batch_bytes, 'batching.maxBytes'),
              (len(c.statistics.dimensions), system.telemetry.max_dimensions, 'statistics.dimensions')]
    for prop, maximum in [('requests_per_minute', system.ingress.requests_per_minute),
                          ('pending_events', system.queue.max_events), ('pending_bytes', system.queue.max_bytes),
                          ('pre_concurrency', system.concurrency.pre_scripts), ('post_concurrency', system.concurrency.post_scripts),
                          ('auto_model_concurrency', system.concurrency.auto_model_runs)]:
        checks.append((getattr(c.limits, prop), maximum, 'limits.' + to_camel(prop)))
    for s in (c.pre, c.post):
        checks.append((s.timeout_seconds, system.scripts.max_timeout_seconds, 'timeoutSeconds'))
    for val, maximum, path in checks:
        if val is not None and val > maximum:
            raise WebhookError('system_limit_exceeded', details={'field': path, 'maximum': maximum})
    for values in (c.statistics.dimensions, c.statistics.metric_definitions, c.statistics.business_fields):
        if len({v.name for v in values}) != len(values):
            raise WebhookError('duplicate_statistics_name')
    return c


MISSING = object()


def path_value(data, path, default=MISSING):
    if not path:
        return default
    value = data
    for part in path.split('.'):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return default
    return value


def material(query_pairs, body, derived=None):
    query = {}
    for k, v in query_pairs:
        if k in query:
            query[k] = [*query[k], v] if isinstance(query[k], list) else [query[k], v]
        else:
            query[k] = v
    return {'query': query, 'body': body, 'derived': derived}


def validate_match(predicate):
    if not isinstance(predicate, dict) or len(predicate) != 1 or not ({'all', 'any'} & predicate.keys()):
        raise WebhookError('invalid_match')
    entries = next(iter(predicate.values()))
    if not isinstance(entries, list) or not 1 <= len(entries) <= 32:
        raise WebhookError('invalid_match')
    for clause in entries:
        if not isinstance(clause, dict) or set(clause) != {'path', 'op', 'value'} or clause['op'] not in ('eq', 'ne', 'in', 'exists', 'lt', 'lte', 'gt', 'gte'):
            raise WebhookError('invalid_match')
        if not re.match(r'^(query|body|derived)\.[A-Za-z0-9_.-]+$', clause['path']):
            raise WebhookError('invalid_match_path')
        if clause['op'] == 'in' and not isinstance(clause['value'], list):
            raise WebhookError('invalid_match_value')
    return predicate


def matches(predicate, data):
    def compare(clause):
        v = path_value(data, clause['path']); x = clause['value']; op = clause['op']
        if op == 'exists':
            return (v is not MISSING) == bool(x)
        if v is MISSING:
            return False
        if op == 'eq':
            return type(v) is type(x) and v == x
        if op == 'ne':
            return not (type(v) is type(x) and v == x)
        if op == 'in':
            return any(type(v) is type(i) and v == i for i in x)
        if type(v) is not type(x) or not isinstance(v, (int, float, str)):
            return False
        return {'lt': lambda: v < x, 'lte': lambda: v <= x, 'gt': lambda: v > x, 'gte': lambda: v >= x}[op]()
    return (all if 'all' in predicate else any)(compare(c) for c in next(iter(predicate.values())))
