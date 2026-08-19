CREATE TABLE IF NOT EXISTS sessions (
  chat_id                  INTEGER PRIMARY KEY,
  created_at               INTEGER,
  updated_at               INTEGER,
  thinking_level           TEXT DEFAULT '',
  fast_mode                INTEGER DEFAULT 0,  -- 1=本会话请求支持 Fast 的模型时携带对应协议的加速参数
  show_thinking            INTEGER DEFAULT -1, -- -1=跟随配置；0=隐藏；1=显示
  session_uuid             TEXT DEFAULT '',   -- 稳定会话 id（同会话不变，新会话换新），供上游 prompt cache 亲和
  system_snapshot          TEXT DEFAULT '',   -- 系统提示词快照（会话内锁定，防缓存撕裂）
  usage_input_tokens       INTEGER DEFAULT 0,
  usage_output_tokens      INTEGER DEFAULT 0,
  usage_cache_read_tokens  INTEGER DEFAULT 0,
  usage_cache_write_tokens INTEGER DEFAULT 0,
  usage_cost_usd           REAL DEFAULT 0,
  last_input_tokens        INTEGER DEFAULT 0,
  last_output_tokens       INTEGER DEFAULT 0,
  last_cache_read_tokens   INTEGER DEFAULT 0,
  last_cache_write_tokens  INTEGER DEFAULT 0,
  last_cost_usd            REAL DEFAULT 0,  -- 最近一次模型 API 调用费用
  last_connect_ms          INTEGER DEFAULT 0,
  last_first_token_ms      INTEGER DEFAULT 0,
  last_total_time_ms       INTEGER DEFAULT 0,
  last_run_cost_usd        REAL DEFAULT 0,  -- 最近一次 Agent run 总费用
  last_run_total_time_ms   INTEGER DEFAULT 0,
  last_run_model_calls     INTEGER DEFAULT 0,
  last_run_tool_calls      INTEGER DEFAULT 0,
  last_model               TEXT DEFAULT '',
  last_protocol            TEXT DEFAULT '',
  last_think_level         TEXT DEFAULT '',
  last_created_at          INTEGER DEFAULT 0,
  -- 本轮统计；新会话时全部清零
  turn_started_at          INTEGER DEFAULT 0,   -- 本轮开始时间
  stat_user_turns          INTEGER DEFAULT 0,   -- 用户回复次数
  stat_tool_calls          INTEGER DEFAULT 0,   -- 工具调用次数
  stat_model_calls         INTEGER DEFAULT 0,   -- 模型调用次数（含重试）
  stat_model_ok            INTEGER DEFAULT 0,   -- 模型成功次数
  stat_model_retry         INTEGER DEFAULT 0,   -- 模型重试次数
  stat_model_fail          INTEGER DEFAULT 0,   -- 模型失败次数
  stat_connect_ms_sum      INTEGER DEFAULT 0,   -- 连接耗时累加（求平均分子）
  stat_first_token_ms_sum  INTEGER DEFAULT 0,   -- 首字耗时累加
  stat_total_time_ms_sum   INTEGER DEFAULT 0,   -- 总耗时累加
  stat_output_tokens_sum   INTEGER DEFAULT 0    -- 输出 token 累加（求平均 t/s）
);

CREATE TABLE IF NOT EXISTS messages (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id       INTEGER NOT NULL,
  role          TEXT NOT NULL,          -- user|assistant|tool
  content       TEXT,
  reasoning     TEXT,                   -- assistant thinking（anthropic 多轮回传含 signature）
  signature     TEXT,
  tool_calls_json TEXT,                 -- assistant 发起的工具调用（JSON）
  tool_call_id  TEXT,                   -- tool 结果回灌
  name          TEXT,                   -- tool 名
  tokens        INTEGER DEFAULT 0,
  compacted     INTEGER DEFAULT 0,      -- 1=已被压缩进摘要
  created_at    INTEGER,
  -- Durable ownership metadata for later turn deletion / audit.
  -- These fields stay in DB only; build_history/to_message never send them to the model.
  conversation_uuid    TEXT DEFAULT '',
  turn_uuid            TEXT DEFAULT '',
  parent_turn_uuid     TEXT DEFAULT '',
  run_root_turn_uuid   TEXT DEFAULT '',
  task_uuid            TEXT DEFAULT '',  -- Rath/Agent task run id (empty for pure main-controller rows)
  agent_session_uuid   TEXT DEFAULT ''   -- Rath agent session continuity id
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id, compacted);
CREATE INDEX IF NOT EXISTS idx_messages_chat_turn
  ON messages(chat_id, turn_uuid, id);
CREATE INDEX IF NOT EXISTS idx_messages_chat_root_turn
  ON messages(chat_id, run_root_turn_uuid, id);
CREATE INDEX IF NOT EXISTS idx_messages_task
  ON messages(task_uuid, id);
CREATE INDEX IF NOT EXISTS idx_messages_conversation_turn
  ON messages(conversation_uuid, turn_uuid, id);

-- Private main-Controller provider continuation checkpoint. state_json may
-- contain encrypted Responses reasoning and exact runtime-only model inputs;
-- it is never joined into ordinary messages, history, exports, or Web APIs.
CREATE TABLE IF NOT EXISTS controller_model_contexts (
  chat_id             INTEGER PRIMARY KEY,
  conversation_uuid   TEXT NOT NULL DEFAULT '',
  session_id          TEXT NOT NULL DEFAULT '',
  protocol            TEXT NOT NULL DEFAULT '',
  model               TEXT NOT NULL DEFAULT '',
  model_label         TEXT NOT NULL DEFAULT '',
  state_json          TEXT NOT NULL DEFAULT '{}',
  revision            INTEGER NOT NULL DEFAULT 0,
  created_at          INTEGER DEFAULT 0,
  updated_at          INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_controller_model_contexts_conversation
  ON controller_model_contexts(conversation_uuid);
CREATE INDEX IF NOT EXISTS idx_controller_model_contexts_updated
  ON controller_model_contexts(updated_at DESC);

-- Web 版多 live 会话映射表。
-- owner_chat_id 是真实登录用户(Telegram chat_id)；internal_chat_id 是 OpenBear
-- 运行时使用的隔离 chat_id。这样可以复用现有 sessions/messages/summaries/
-- model_calls/tool_calls 结构，同时让同一用户拥有多个可并行运行的热态会话。
CREATE TABLE IF NOT EXISTS web_conversations (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_uuid     TEXT NOT NULL UNIQUE,
  owner_chat_id         INTEGER NOT NULL,
  internal_chat_id      INTEGER NOT NULL UNIQUE,
  title                 TEXT DEFAULT '',
  model                 TEXT DEFAULT '',
  -- 会话级 Agent 默认运行配置；空/-1 表示跟随主会话或模型默认。
  agent_model           TEXT DEFAULT '',
  agent_think_level     TEXT DEFAULT '',
  agent_fast_mode       INTEGER DEFAULT -1, -- -1=跟随主会话 Fast；0=关；1=开
  status                TEXT DEFAULT 'idle', -- idle|running|stopping|error
  current_status        TEXT DEFAULT '',
  last_error            TEXT DEFAULT '',
  created_at            INTEGER,
  updated_at            INTEGER,
  pinned_at             INTEGER DEFAULT 0,
  -- 用户在各自置顶/非置顶组内调整的持久展示顺序；NULL 回退到创建时间排序。
  display_order         REAL,
  archived_at           INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_web_conversations_owner_time
  ON web_conversations(owner_chat_id, archived_at, pinned_at DESC, updated_at DESC, id DESC);
-- Keep the legacy owner_time index for compatibility; this new name ensures
-- existing databases receive the created-at ordering definition.
CREATE INDEX IF NOT EXISTS idx_web_conversations_owner_created
  ON web_conversations(owner_chat_id, archived_at, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_web_conversations_internal_chat
  ON web_conversations(internal_chat_id);

-- Task Memory is intentionally isolated from global memory_entries/secrets/docs.
-- task_uuid is empty for conversation scope and mandatory for agent_task scope.
CREATE TABLE IF NOT EXISTS conversation_task_memories (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_uuid            TEXT NOT NULL UNIQUE,
  conversation_uuid      TEXT NOT NULL,
  scope_type             TEXT NOT NULL CHECK(scope_type IN ('conversation','agent_task')),
  task_uuid              TEXT NOT NULL DEFAULT '',
  name                   TEXT NOT NULL,
  description            TEXT NOT NULL DEFAULT '',
  body                   TEXT NOT NULL DEFAULT '',
  auto_reinject_catalog  INTEGER NOT NULL DEFAULT 1,
  visible_to_agents      INTEGER NOT NULL DEFAULT 0,
  revision               INTEGER NOT NULL DEFAULT 1,
  created_by             TEXT NOT NULL DEFAULT '',
  source_turn_uuid       TEXT NOT NULL DEFAULT '',
  source_run_uuid        TEXT NOT NULL DEFAULT '',
  created_at             INTEGER NOT NULL DEFAULT 0,
  updated_at             INTEGER NOT NULL DEFAULT 0,
  deleted_at             INTEGER NOT NULL DEFAULT 0,
  idempotency_key        TEXT NOT NULL DEFAULT '',
  CHECK(
    (scope_type='conversation' AND task_uuid='') OR
    (scope_type='agent_task' AND task_uuid<>'')
  )
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_conversation_task_memories_active_name
  ON conversation_task_memories(conversation_uuid, scope_type, task_uuid, name COLLATE NOCASE)
  WHERE deleted_at=0;
CREATE UNIQUE INDEX IF NOT EXISTS ux_conversation_task_memories_idempotency
  ON conversation_task_memories(conversation_uuid, scope_type, task_uuid, idempotency_key)
  WHERE idempotency_key<>'';
CREATE INDEX IF NOT EXISTS idx_conversation_task_memories_scope
  ON conversation_task_memories(conversation_uuid, scope_type, task_uuid, deleted_at, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_task_memories_catalog
  ON conversation_task_memories(conversation_uuid, scope_type, task_uuid, auto_reinject_catalog, visible_to_agents, deleted_at);

-- 每个 Web 登录用户最近一次成功选择的新会话运行配置。
-- revision 随每次部分更新递增；并发标签页按服务端提交顺序 last-write-wins。
CREATE TABLE IF NOT EXISTS web_conversation_defaults (
  owner_chat_id         INTEGER PRIMARY KEY,
  main_model            TEXT DEFAULT '',
  main_thinking_level   TEXT DEFAULT '',
  main_fast_mode        INTEGER DEFAULT 0,
  agent_model           TEXT DEFAULT '',
  agent_think_level     TEXT DEFAULT '',
  agent_fast_mode       INTEGER DEFAULT -1, -- -1=跟随主会话；0=关；1=开
  revision              INTEGER NOT NULL DEFAULT 1,
  updated_at            INTEGER NOT NULL DEFAULT 0
);

-- Web 会话私有 artifact。文件内容落在私有 blob 目录，通过会话鉴权 API 读取；
-- 不把模型生成文件放进 web/dist，避免前端构建清空，也避免公开静态暴露。
CREATE TABLE IF NOT EXISTS web_artifacts (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  artifact_uuid      TEXT NOT NULL UNIQUE,
  conversation_uuid  TEXT NOT NULL,
  owner_chat_id      INTEGER NOT NULL,
  internal_chat_id   INTEGER NOT NULL,
  turn_uuid          TEXT DEFAULT '',
  message_id         INTEGER DEFAULT 0,
  op_id              TEXT DEFAULT '',
  file_name          TEXT NOT NULL,
  mime_type          TEXT DEFAULT 'application/octet-stream',
  size_bytes         INTEGER DEFAULT 0,
  sha256             TEXT NOT NULL,
  storage_path       TEXT NOT NULL,
  source_path        TEXT DEFAULT '',
  source_url         TEXT DEFAULT '',
  created_at         INTEGER,
  deleted_at         INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_web_artifacts_conversation_uuid
  ON web_artifacts(conversation_uuid, artifact_uuid);
CREATE INDEX IF NOT EXISTS idx_web_artifacts_owner_time
  ON web_artifacts(owner_chat_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_web_artifacts_conversation_hash
  ON web_artifacts(conversation_uuid, sha256, file_name, deleted_at);


-- Web Event / Operation v2: append-only transport/audit frames.
-- frame_seq is only a transport cursor inside one web conversation; it must not
-- be used as the freshness/version ordering for one UI object.
CREATE TABLE IF NOT EXISTS web_event_frames (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_uuid  TEXT NOT NULL,
  internal_chat_id   INTEGER DEFAULT 0,
  owner_chat_id      INTEGER DEFAULT 0,
  frame_seq          INTEGER NOT NULL,
  op_id              TEXT NOT NULL,
  op_type            TEXT NOT NULL,
  action             TEXT NOT NULL,
  turn_uuid          TEXT DEFAULT '',
  parent_turn_uuid   TEXT DEFAULT '',
  run_root_turn_uuid TEXT DEFAULT '',
  target_type        TEXT DEFAULT '',
  target_id          TEXT DEFAULT '',
  task_uuid          TEXT DEFAULT '',
  run_id             TEXT DEFAULT '',
  revision           INTEGER NOT NULL,
  display_seq        INTEGER NOT NULL,
  payload_json       TEXT NOT NULL,
  debug_json         TEXT DEFAULT '{}',
  created_at_ms      INTEGER NOT NULL,
  updated_at_ms      INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_web_event_frames_conv_seq
  ON web_event_frames(conversation_uuid, frame_seq);
CREATE INDEX IF NOT EXISTS idx_web_event_frames_conv_op_revision
  ON web_event_frames(conversation_uuid, op_id, revision);
CREATE INDEX IF NOT EXISTS idx_web_event_frames_conv_turn_seq
  ON web_event_frames(conversation_uuid, turn_uuid, frame_seq);
CREATE INDEX IF NOT EXISTS idx_web_event_frames_conv_target_seq
  ON web_event_frames(conversation_uuid, target_type, target_id, frame_seq);
CREATE INDEX IF NOT EXISTS idx_web_event_frames_task_seq
  ON web_event_frames(task_uuid, frame_seq);
CREATE INDEX IF NOT EXISTS idx_web_event_frames_internal_seq
  ON web_event_frames(internal_chat_id, frame_seq);

-- Web Event / Operation v2: recoverable UI operation snapshots.
-- revision / updated_at_ms are the only freshness fields for one op_id; display_seq
-- is the stable render position and frame_seq lives only in web_event_frames.
CREATE TABLE IF NOT EXISTS web_operations (
  id                          INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_uuid           TEXT NOT NULL,
  internal_chat_id            INTEGER DEFAULT 0,
  op_id                       TEXT NOT NULL,
  op_type                     TEXT NOT NULL,
  turn_uuid                   TEXT DEFAULT '',
  parent_turn_uuid            TEXT DEFAULT '',
  run_root_turn_uuid          TEXT DEFAULT '',
  target_type                 TEXT DEFAULT '',
  target_id                   TEXT DEFAULT '',
  task_uuid                   TEXT DEFAULT '',
  run_id                      TEXT DEFAULT '',
  display_seq                 INTEGER NOT NULL,
  status                      TEXT DEFAULT '',
  lifecycle                   TEXT DEFAULT '',
  internal                    INTEGER DEFAULT 0,
  source                      TEXT DEFAULT '',
  transcript_message_ids_json TEXT DEFAULT '[]',
  revision                    INTEGER NOT NULL,
  payload_json                TEXT NOT NULL,
  created_at_ms               INTEGER NOT NULL,
  updated_at_ms               INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_web_operations_conv_op
  ON web_operations(conversation_uuid, op_id);
CREATE INDEX IF NOT EXISTS idx_web_operations_conv_display
  ON web_operations(conversation_uuid, display_seq, id);
CREATE INDEX IF NOT EXISTS idx_web_operations_conv_turn_display
  ON web_operations(conversation_uuid, turn_uuid, display_seq);
CREATE INDEX IF NOT EXISTS idx_web_operations_conv_target_display
  ON web_operations(conversation_uuid, target_type, target_id, display_seq);
CREATE INDEX IF NOT EXISTS idx_web_operations_task_display
  ON web_operations(task_uuid, display_seq);
CREATE INDEX IF NOT EXISTS idx_web_operations_run_display
  ON web_operations(run_id, display_seq);
CREATE INDEX IF NOT EXISTS idx_web_operations_internal_status
  ON web_operations(internal_chat_id, status, updated_at_ms DESC);

-- Normalized many-to-many source of truth between durable UI operations and
-- model transcript rows. The JSON list on web_operations remains a denormalized
-- frontend snapshot; this table supports exact reverse lookup and transactional
-- message binding. op_id may be inserted before the matching operation snapshot
-- exists (for example an assistant tool-call persisted before its UI boundary).
CREATE TABLE IF NOT EXISTS web_operation_messages (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_uuid  TEXT NOT NULL,
  op_id               TEXT NOT NULL,
  message_id          INTEGER NOT NULL,
  relation_kind       TEXT DEFAULT 'transcript',
  created_at_ms       INTEGER NOT NULL,
  UNIQUE(conversation_uuid, op_id, message_id),
  FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_web_operation_messages_op
  ON web_operation_messages(conversation_uuid, op_id, message_id);
CREATE INDEX IF NOT EXISTS idx_web_operation_messages_message
  ON web_operation_messages(message_id, conversation_uuid, op_id);

-- Durable outbox for Rath/tool completion notifications. In-memory queues only
-- accelerate delivery; this table is the restart-safe source of truth.
CREATE TABLE IF NOT EXISTS web_task_notifications (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  notification_uuid     TEXT NOT NULL UNIQUE,
  notification_key      TEXT NOT NULL UNIQUE,
  conversation_uuid     TEXT NOT NULL,
  internal_chat_id      INTEGER NOT NULL,
  owner_chat_id         INTEGER DEFAULT 0,
  task_uuid             TEXT DEFAULT '',
  kind                  TEXT DEFAULT 'task-notification',
  task_status           TEXT DEFAULT '',
  payload_json          TEXT NOT NULL,
  state                 TEXT NOT NULL DEFAULT 'pending', -- pending|processing|delivered|suppressed
  attempts              INTEGER NOT NULL DEFAULT 0,
  claim_token           TEXT DEFAULT '',
  claimed_at            INTEGER DEFAULT 0,
  next_attempt_at       INTEGER DEFAULT 0,
  last_error            TEXT DEFAULT '',
  created_at            INTEGER NOT NULL,
  updated_at            INTEGER NOT NULL,
  delivered_at          INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_web_task_notifications_delivery
  ON web_task_notifications(state, next_attempt_at, conversation_uuid, id);
CREATE INDEX IF NOT EXISTS idx_web_task_notifications_task
  ON web_task_notifications(task_uuid, id DESC);

-- Telegram delivery state for long-running Web controller turns. Configuration
-- is snapshotted at accepted time so a live task cannot change behavior halfway.
CREATE TABLE IF NOT EXISTS web_tg_notification_runs (
  root_turn_uuid       TEXT PRIMARY KEY,
  conversation_uuid    TEXT NOT NULL,
  owner_chat_id        INTEGER NOT NULL,
  internal_chat_id     INTEGER NOT NULL,
  title                TEXT DEFAULT '',
  model                TEXT DEFAULT '',
  status               TEXT NOT NULL DEFAULT 'running', -- running|completed|failed|interrupted|short
  config_json          TEXT NOT NULL,
  result_text          TEXT DEFAULT '',
  started_at           INTEGER NOT NULL,
  threshold_at         INTEGER NOT NULL,
  completed_at         INTEGER DEFAULT 0,
  created_at           INTEGER NOT NULL,
  updated_at           INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_web_tg_notification_runs_status
  ON web_tg_notification_runs(status, threshold_at, updated_at);
CREATE INDEX IF NOT EXISTS idx_web_tg_notification_runs_conversation
  ON web_tg_notification_runs(conversation_uuid, started_at DESC);

CREATE TABLE IF NOT EXISTS web_tg_notification_outbox (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  delivery_key          TEXT NOT NULL UNIQUE,
  root_turn_uuid        TEXT NOT NULL,
  event_type            TEXT NOT NULL,
  payload_json          TEXT NOT NULL,
  state                 TEXT NOT NULL DEFAULT 'pending', -- pending|processing|sent|cancelled|failed
  deliver_after         INTEGER NOT NULL,
  attempts              INTEGER NOT NULL DEFAULT 0,
  telegram_message_ids_json TEXT DEFAULT '[]',
  last_error            TEXT DEFAULT '',
  created_at            INTEGER NOT NULL,
  updated_at            INTEGER NOT NULL,
  delivered_at          INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_web_tg_notification_outbox_delivery
  ON web_tg_notification_outbox(state, deliver_after, id);
CREATE INDEX IF NOT EXISTS idx_web_tg_notification_outbox_turn
  ON web_tg_notification_outbox(root_turn_uuid, id);

CREATE TABLE IF NOT EXISTS summaries (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id       INTEGER NOT NULL,
  summary       TEXT,
  up_to_message_id INTEGER,
  tokens        INTEGER DEFAULT 0,
  created_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_summaries_chat ON summaries(chat_id, id);

CREATE TABLE IF NOT EXISTS web_controller_context_snapshots (
  chat_id INTEGER PRIMARY KEY,
  session_uuid TEXT NOT NULL DEFAULT '',
  summary_id INTEGER NOT NULL DEFAULT 0,
  known INTEGER NOT NULL DEFAULT 1,
  tokens INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS web_memory_reminders (
  chat_id INTEGER NOT NULL,
  session_uuid TEXT NOT NULL DEFAULT '',
  summary_id INTEGER NOT NULL DEFAULT 0,
  delivered_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(chat_id, summary_id)
);

CREATE TABLE IF NOT EXISTS model_calls (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id             INTEGER NOT NULL,
  session_uuid        TEXT DEFAULT '',
  model               TEXT DEFAULT '',
  protocol            TEXT DEFAULT '',
  think_level         TEXT DEFAULT '',
  call_kind           TEXT DEFAULT '',
  input_tokens        INTEGER DEFAULT 0,
  output_tokens       INTEGER DEFAULT 0,
  cache_read_tokens   INTEGER DEFAULT 0,
  cache_write_tokens  INTEGER DEFAULT 0,
  last_input_tokens   INTEGER DEFAULT 0,
  last_output_tokens  INTEGER DEFAULT 0,
  last_cache_read_tokens   INTEGER DEFAULT 0,
  last_cache_write_tokens  INTEGER DEFAULT 0,
  expert_input_tokens       INTEGER DEFAULT 0,
  expert_output_tokens      INTEGER DEFAULT 0,
  expert_cache_read_tokens  INTEGER DEFAULT 0,
  expert_cache_write_tokens INTEGER DEFAULT 0,
  expert_tool_calls         INTEGER DEFAULT 0,
  cost_usd            REAL DEFAULT 0,
  connect_ms          INTEGER DEFAULT 0,
  first_token_ms      INTEGER DEFAULT 0,
  total_time_ms       INTEGER DEFAULT 0,
  peak_tps            REAL DEFAULT 0,  -- 本次 Agent run 内单次模型 API 调用最高 TPS
  min_tps             REAL DEFAULT 0,  -- 本次 Agent run 内单次模型 API 调用最低 TPS（仅统计有输出调用）
  status              TEXT DEFAULT 'ok',
  model_call_count    INTEGER DEFAULT 1,
  model_ok_count      INTEGER DEFAULT 1,
  model_retry_count   INTEGER DEFAULT 0,
  model_fail_count    INTEGER DEFAULT 0,
  error_type          TEXT DEFAULT '',
  created_at          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_model_calls_chat_time ON model_calls(chat_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_model_calls_model_time ON model_calls(model, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_model_calls_kind_time ON model_calls(chat_id, call_kind, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS tool_calls (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id        INTEGER NOT NULL,
  session_uuid   TEXT DEFAULT '',
  tool_name      TEXT DEFAULT '',
  status         TEXT DEFAULT 'ok',
  duration_ms    INTEGER DEFAULT 0,
  result_size    INTEGER DEFAULT 0,
  error_type     TEXT DEFAULT '',
  created_at     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_chat_time ON tool_calls(chat_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_tool_calls_name_time ON tool_calls(tool_name, created_at DESC);


CREATE TABLE IF NOT EXISTS operations (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  operation_uuid   TEXT NOT NULL UNIQUE,
  chat_id          INTEGER DEFAULT 0,
  kind             TEXT DEFAULT '',
  status           TEXT DEFAULT '',
  detail_json      TEXT DEFAULT '{}',
  error            TEXT DEFAULT '',
  started_at       INTEGER,
  finished_at      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_operations_chat_time
  ON operations(chat_id, started_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS app_state (
  key         TEXT PRIMARY KEY,
  value       TEXT DEFAULT '',
  updated_at  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS web_login_requests (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  request_uuid    TEXT NOT NULL UNIQUE,
  chat_id         INTEGER DEFAULT 0,
  status          TEXT DEFAULT 'pending',
  nonce_hash      TEXT DEFAULT '',
  ip              TEXT DEFAULT '',
  user_agent      TEXT DEFAULT '',
  created_at      INTEGER DEFAULT 0,
  expires_at      INTEGER DEFAULT 0,
  decided_at      INTEGER DEFAULT 0,
  decided_by      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_web_login_requests_uuid
  ON web_login_requests(request_uuid);
CREATE INDEX IF NOT EXISTS idx_web_login_requests_time
  ON web_login_requests(created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS web_login_failures (
  ip              TEXT PRIMARY KEY,
  failed_count    INTEGER DEFAULT 0,
  first_failed_at INTEGER DEFAULT 0,
  last_failed_at  INTEGER DEFAULT 0,
  blocked_until   INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_web_login_failures_blocked
  ON web_login_failures(blocked_until DESC);

CREATE TABLE IF NOT EXISTS web_sessions (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  session_token_hash  TEXT NOT NULL UNIQUE,
  chat_id             INTEGER DEFAULT 0,
  created_at          INTEGER DEFAULT 0,
  expires_at          INTEGER DEFAULT 0,
  last_seen_at        INTEGER DEFAULT 0,
  revoked_at          INTEGER DEFAULT 0,
  ip                  TEXT DEFAULT '',
  user_agent          TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_web_sessions_hash
  ON web_sessions(session_token_hash);
CREATE INDEX IF NOT EXISTS idx_web_sessions_expiry
  ON web_sessions(expires_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS audit_logs (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  kind         TEXT DEFAULT '',
  actor        TEXT DEFAULT '',
  chat_id      INTEGER DEFAULT 0,
  ip           TEXT DEFAULT '',
  detail_json  TEXT DEFAULT '{}',
  created_at   INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_audit_logs_time
  ON audit_logs(created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS memory_categories (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  key          TEXT UNIQUE NOT NULL,
  name         TEXT NOT NULL,
  icon         TEXT DEFAULT '',
  render_type  TEXT NOT NULL DEFAULT 'fields',
  schema_json  TEXT NOT NULL DEFAULT '{"fields":[]}',
  inject       INTEGER DEFAULT 1,
  sort         INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS memory_entries (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  category_id  INTEGER NOT NULL,
  grp          TEXT DEFAULT '',
  ref          TEXT DEFAULT '',
  note         TEXT DEFAULT '',
  title        TEXT NOT NULL,
  fields_json  TEXT NOT NULL DEFAULT '{}',
  body         TEXT DEFAULT '',
  expanded     INTEGER DEFAULT 0,
  enabled      INTEGER DEFAULT 1,
  archived     INTEGER DEFAULT 0,
  sort         INTEGER DEFAULT 0,
  created_at   INTEGER DEFAULT 0,
  updated_at   INTEGER DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_entries_ref
  ON memory_entries(ref) WHERE ref <> '';
CREATE INDEX IF NOT EXISTS idx_memory_entries_category_sort
  ON memory_entries(category_id, archived, enabled, sort, id);

CREATE TABLE IF NOT EXISTS memory_secrets (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT UNIQUE NOT NULL,
  note        TEXT DEFAULT '',
  kv_json     TEXT NOT NULL DEFAULT '[]',
  grp         TEXT DEFAULT '',
  enabled     INTEGER DEFAULT 1,
  archived    INTEGER DEFAULT 0,
  sort        INTEGER DEFAULT 0,
  created_at  INTEGER DEFAULT 0,
  updated_at  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_memory_secrets_sort
  ON memory_secrets(archived, enabled, sort, id);
CREATE INDEX IF NOT EXISTS idx_memory_secrets_group_sort
  ON memory_secrets(grp, sort, id);

CREATE TABLE IF NOT EXISTS memory_docs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT UNIQUE NOT NULL,
  title       TEXT DEFAULT '',
  summary     TEXT DEFAULT '',
  project     TEXT DEFAULT '',
  importance  INTEGER DEFAULT 3,
  tags        TEXT DEFAULT '',
  grp         TEXT DEFAULT '',
  enabled     INTEGER DEFAULT 1,
  archived    INTEGER DEFAULT 0,
  sort        INTEGER DEFAULT 0,
  content     TEXT DEFAULT '',
  created_at  INTEGER DEFAULT 0,
  updated_at  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_memory_docs_sort
  ON memory_docs(archived, enabled, importance DESC, id);
CREATE INDEX IF NOT EXISTS idx_memory_docs_group_sort
  ON memory_docs(grp, sort, id);

CREATE TABLE IF NOT EXISTS memory_templates (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL,
  content     TEXT NOT NULL DEFAULT '',
  is_active   INTEGER DEFAULT 0,
  is_agent_active INTEGER DEFAULT 0,
  updated_at  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_memory_templates_active
  ON memory_templates(is_active, id DESC);
CREATE INDEX IF NOT EXISTS idx_memory_templates_agent_active
  ON memory_templates(is_agent_active, id DESC);

CREATE TABLE IF NOT EXISTS memory_render_logs (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  ts             INTEGER DEFAULT 0,
  params_json    TEXT DEFAULT '',
  output         TEXT DEFAULT '',
  output_len     INTEGER DEFAULT 0,
  source         TEXT DEFAULT 'runtime',
  client_ip      TEXT DEFAULT '',
  template_id    INTEGER DEFAULT 0,
  template_name  TEXT DEFAULT '',
  auth_ok        INTEGER DEFAULT 1,
  auth_error     TEXT DEFAULT '',
  ms             INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_memory_render_logs_time
  ON memory_render_logs(ts DESC, id DESC);

CREATE TABLE IF NOT EXISTS rath_workflows (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_uuid   TEXT NOT NULL UNIQUE,
  slug            TEXT NOT NULL UNIQUE,
  name            TEXT NOT NULL,
  description     TEXT DEFAULT '',
  kind            TEXT DEFAULT '',
  enabled         INTEGER DEFAULT 1,
  config_json     TEXT DEFAULT '{}',
  created_at      INTEGER DEFAULT 0,
  updated_at      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rath_workflows_enabled
  ON rath_workflows(enabled, slug);

CREATE TABLE IF NOT EXISTS rath_agents (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_key            TEXT NOT NULL,
  name                 TEXT NOT NULL,
  description          TEXT DEFAULT '',
  system_prompt        TEXT DEFAULT '',
  model                TEXT DEFAULT '',
  think_level          TEXT DEFAULT '',
  tool_allowlist_json  TEXT DEFAULT '[]',
  sort                 INTEGER DEFAULT 0,
  enabled              INTEGER DEFAULT 1,
  created_at           INTEGER DEFAULT 0,
  updated_at           INTEGER DEFAULT 0,
  UNIQUE(agent_key)
);
CREATE INDEX IF NOT EXISTS idx_rath_agents_sort
  ON rath_agents(enabled, sort, id);

CREATE TABLE IF NOT EXISTS rath_agent_sessions (
  id                       INTEGER PRIMARY KEY AUTOINCREMENT,
  session_uuid             TEXT NOT NULL UNIQUE,
  openbear_session_uuid    TEXT DEFAULT '',
  chat_id                  INTEGER DEFAULT 0,
  workflow_uuid            TEXT DEFAULT '',
  agent_key                TEXT DEFAULT '',
  status                   TEXT DEFAULT 'active',
  title                    TEXT DEFAULT '',
  summary                  TEXT DEFAULT '',
  last_task_uuid           TEXT DEFAULT '',
  metadata_json            TEXT DEFAULT '{}',
  created_at               INTEGER DEFAULT 0,
  updated_at               INTEGER DEFAULT 0,
  closed_at                INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rath_agent_sessions_openbear_agent
  ON rath_agent_sessions(openbear_session_uuid, workflow_uuid, agent_key, status, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS ux_rath_agent_sessions_active_openbear_agent
  ON rath_agent_sessions(openbear_session_uuid, workflow_uuid, agent_key)
  WHERE status='active';
CREATE INDEX IF NOT EXISTS idx_rath_agent_sessions_chat_time
  ON rath_agent_sessions(chat_id, updated_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS rath_tasks (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  task_uuid             TEXT NOT NULL UNIQUE,
  chat_id               INTEGER DEFAULT 0,
  parent_session_uuid   TEXT DEFAULT '',
  agent_session_uuid    TEXT DEFAULT '',
  caller_agent_session_uuid TEXT DEFAULT '',
  parent_task_uuid      TEXT DEFAULT '',
  workflow_uuid         TEXT DEFAULT '',
  title                 TEXT DEFAULT '',
  status                TEXT DEFAULT 'queued',
  control_state         TEXT DEFAULT '',
  current_agent_key     TEXT DEFAULT '',
  current_status        TEXT DEFAULT '',
  input_json            TEXT DEFAULT '{}',
  output_json           TEXT DEFAULT '{}',
  error                 TEXT DEFAULT '',
  model_call_count      INTEGER DEFAULT 0,
  tool_call_count       INTEGER DEFAULT 0,
  work_tool_call_count  INTEGER DEFAULT 0,
  plan_tool_call_count  INTEGER DEFAULT 0,
  input_tokens          INTEGER DEFAULT 0,
  output_tokens         INTEGER DEFAULT 0,
  cache_read_tokens     INTEGER DEFAULT 0,
  cache_write_tokens    INTEGER DEFAULT 0,
  last_input_tokens     INTEGER DEFAULT 0,
  last_output_tokens    INTEGER DEFAULT 0,
  last_cache_read_tokens INTEGER DEFAULT 0,
  last_cache_write_tokens INTEGER DEFAULT 0,
  cost_usd              REAL DEFAULT 0,
  started_at            INTEGER DEFAULT 0,
  updated_at            INTEGER DEFAULT 0,
  finished_at           INTEGER DEFAULT 0,
  -- Parent Web/main-controller ownership. Agent internal transcript stays in
  -- output_json checkpoints; these fields bind the task back to the spawning turn.
  turn_uuid             TEXT DEFAULT '',
  parent_turn_uuid      TEXT DEFAULT '',
  run_root_turn_uuid    TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_rath_tasks_chat_time
  ON rath_tasks(chat_id, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_rath_tasks_status_time
  ON rath_tasks(status, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_rath_tasks_session
  ON rath_tasks(parent_session_uuid);
CREATE INDEX IF NOT EXISTS idx_rath_tasks_agent_session
  ON rath_tasks(agent_session_uuid, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_rath_tasks_caller_agent_session
  ON rath_tasks(caller_agent_session_uuid, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_rath_tasks_parent_task
  ON rath_tasks(parent_task_uuid, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_rath_tasks_root_turn
  ON rath_tasks(run_root_turn_uuid, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_rath_tasks_turn
  ON rath_tasks(turn_uuid, updated_at DESC, id DESC);

-- Internal provider continuation checkpoint. This table is deliberately not
-- exposed by Rath APIs: it can contain opaque encrypted reasoning items and
-- full tool outputs required to resume one canonical model chain.
CREATE TABLE IF NOT EXISTS rath_task_model_contexts (
  task_uuid       TEXT PRIMARY KEY,
  protocol        TEXT NOT NULL DEFAULT '',
  model           TEXT NOT NULL DEFAULT '',
  session_id      TEXT NOT NULL DEFAULT '',
  state_json      TEXT NOT NULL DEFAULT '{}',
  revision        INTEGER NOT NULL DEFAULT 0,
  created_at      INTEGER DEFAULT 0,
  updated_at      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rath_task_model_contexts_updated
  ON rath_task_model_contexts(updated_at DESC);

-- Agent Plan state is intentionally separate from the coarse Rath task status.
-- Plan definitions are immutable; execution state, evidence and decisions are
-- append/update records with their own revisions.
CREATE TABLE IF NOT EXISTS rath_task_plan_state (
  task_uuid                 TEXT PRIMARY KEY,
  phase                     TEXT NOT NULL DEFAULT 'drafting',
  active_plan_version       INTEGER NOT NULL DEFAULT 0,
  pending_plan_version      INTEGER NOT NULL DEFAULT 0,
  current_step_id           TEXT DEFAULT '',
  approval_cycle            INTEGER NOT NULL DEFAULT 0,
  revision_count            INTEGER NOT NULL DEFAULT 0,
  final_outputs_state_json  TEXT NOT NULL DEFAULT '{}',
  approved_tools_json       TEXT NOT NULL DEFAULT '[]',
  last_controller_guidance  TEXT DEFAULT '',
  row_revision              INTEGER NOT NULL DEFAULT 1,
  updated_at                INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rath_plan_state_phase
  ON rath_task_plan_state(phase, updated_at DESC);

CREATE TABLE IF NOT EXISTS rath_task_plan_versions (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  task_uuid          TEXT NOT NULL,
  version            INTEGER NOT NULL,
  plan_type          TEXT NOT NULL, -- initial|replan
  parent_version     INTEGER NOT NULL DEFAULT 0,
  status             TEXT NOT NULL DEFAULT 'submitted',
  plan_json          TEXT NOT NULL,
  plan_hash          TEXT NOT NULL,
  change_reason      TEXT DEFAULT '',
  submit_request_id  TEXT NOT NULL,
  submitted_at       INTEGER NOT NULL,
  decided_at         INTEGER NOT NULL DEFAULT 0,
  UNIQUE(task_uuid, version),
  UNIQUE(task_uuid, submit_request_id)
);
CREATE INDEX IF NOT EXISTS idx_rath_plan_versions_task_status
  ON rath_task_plan_versions(task_uuid, status, version DESC);
CREATE INDEX IF NOT EXISTS idx_rath_plan_versions_hash
  ON rath_task_plan_versions(task_uuid, plan_hash, version DESC);

CREATE TABLE IF NOT EXISTS rath_task_plan_decisions (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  decision_uuid        TEXT NOT NULL UNIQUE,
  task_uuid            TEXT NOT NULL,
  expected_version     INTEGER NOT NULL,
  action               TEXT NOT NULL,
  issues_json          TEXT NOT NULL DEFAULT '[]',
  reason               TEXT DEFAULT '',
  required_changes_json TEXT NOT NULL DEFAULT '[]',
  granted_tools_json    TEXT NOT NULL DEFAULT '[]',
  requested_by         TEXT DEFAULT '',
  user_instruction_id  TEXT DEFAULT '',
  request_id           TEXT NOT NULL,
  created_at           INTEGER NOT NULL,
  UNIQUE(task_uuid, request_id)
);
CREATE INDEX IF NOT EXISTS idx_rath_plan_decisions_task
  ON rath_task_plan_decisions(task_uuid, id ASC);

CREATE TABLE IF NOT EXISTS rath_task_plan_step_runs (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  task_uuid           TEXT NOT NULL,
  plan_version        INTEGER NOT NULL,
  step_id             TEXT NOT NULL,
  status              TEXT NOT NULL DEFAULT 'pending',
  result              TEXT DEFAULT '',
  criteria_state_json TEXT NOT NULL DEFAULT '{}',
  blocker_json        TEXT NOT NULL DEFAULT '{}',
  started_at          INTEGER NOT NULL DEFAULT 0,
  completed_at        INTEGER NOT NULL DEFAULT 0,
  updated_at          INTEGER NOT NULL DEFAULT 0,
  row_revision        INTEGER NOT NULL DEFAULT 1,
  UNIQUE(task_uuid, plan_version, step_id)
);
CREATE INDEX IF NOT EXISTS idx_rath_plan_steps_task_version
  ON rath_task_plan_step_runs(task_uuid, plan_version, status, id);

CREATE TABLE IF NOT EXISTS rath_task_plan_evidence (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  evidence_uuid  TEXT NOT NULL UNIQUE,
  task_uuid      TEXT NOT NULL,
  plan_version   INTEGER NOT NULL,
  step_id        TEXT NOT NULL,
  criterion_id   TEXT DEFAULT '',
  evidence_type  TEXT NOT NULL,
  reference      TEXT NOT NULL,
  summary        TEXT NOT NULL,
  metadata_json  TEXT NOT NULL DEFAULT '{}',
  request_id     TEXT DEFAULT '',
  created_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rath_plan_evidence_step
  ON rath_task_plan_evidence(task_uuid, plan_version, step_id, id);
CREATE INDEX IF NOT EXISTS idx_rath_plan_evidence_criterion
  ON rath_task_plan_evidence(task_uuid, plan_version, step_id, criterion_id, id);

-- Generic replay ledger for progress/finalize operations. Submit and decisions
-- have dedicated unique request columns; this table gives all other Plan writes
-- the same exactly-once response semantics.
CREATE TABLE IF NOT EXISTS rath_task_plan_requests (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  task_uuid    TEXT NOT NULL,
  request_id   TEXT NOT NULL,
  operation    TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL DEFAULT '',
  result_json  TEXT NOT NULL,
  created_at   INTEGER NOT NULL,
  UNIQUE(task_uuid, request_id)
);
CREATE INDEX IF NOT EXISTS idx_rath_plan_requests_task
  ON rath_task_plan_requests(task_uuid, id DESC);

-- Atomically create a minimal outbox fact in the same transaction that moves a
-- Rath task into a user-visible final/control state. The richer Python callback
-- may add another row, but delivery claims all pending rows for task+status.
CREATE TRIGGER IF NOT EXISTS trg_rath_task_terminal_notification_update
AFTER UPDATE OF status ON rath_tasks
WHEN OLD.status <> NEW.status
 AND NEW.status IN ('completed','failed','needs_openbear_control')
 AND EXISTS (
   SELECT 1 FROM web_conversations c
   WHERE c.conversation_uuid=NEW.parent_session_uuid
     AND c.internal_chat_id=NEW.chat_id
 )
BEGIN
  INSERT INTO web_task_notifications (
    notification_uuid, notification_key, conversation_uuid, internal_chat_id,
    owner_chat_id, task_uuid, kind, task_status, payload_json, state,
    attempts, claim_token, claimed_at, next_attempt_at, last_error,
    created_at, updated_at, delivered_at
  ) VALUES (
    lower(hex(randomblob(16))), lower(hex(randomblob(16))),
    NEW.parent_session_uuid, NEW.chat_id,
    COALESCE((SELECT c.owner_chat_id FROM web_conversations c WHERE c.conversation_uuid=NEW.parent_session_uuid LIMIT 1),0),
    NEW.task_uuid, 'task-notification', NEW.status,
    json_object(
      'taskUuid', NEW.task_uuid,
      'status', NEW.status,
      'summary', COALESCE(NULLIF(NEW.current_status,''), NULLIF(NEW.title,''), 'Agent task completed'),
      'content', COALESCE(NULLIF(NEW.output_json,''), NULLIF(NEW.error,''), ''),
      'conversationUuid', NEW.parent_session_uuid,
      'chatId', NEW.chat_id,
      'kind', 'task-notification',
      'durableFallback', 1
    ),
    'pending', 0, '', 0, 0, '', NEW.updated_at, NEW.updated_at, 0
  );
END;

CREATE TRIGGER IF NOT EXISTS trg_rath_task_terminal_notification_insert
AFTER INSERT ON rath_tasks
WHEN NEW.status IN ('completed','failed','needs_openbear_control')
 AND EXISTS (
   SELECT 1 FROM web_conversations c
   WHERE c.conversation_uuid=NEW.parent_session_uuid
     AND c.internal_chat_id=NEW.chat_id
 )
BEGIN
  INSERT INTO web_task_notifications (
    notification_uuid, notification_key, conversation_uuid, internal_chat_id,
    owner_chat_id, task_uuid, kind, task_status, payload_json, state,
    attempts, claim_token, claimed_at, next_attempt_at, last_error,
    created_at, updated_at, delivered_at
  ) VALUES (
    lower(hex(randomblob(16))), lower(hex(randomblob(16))),
    NEW.parent_session_uuid, NEW.chat_id,
    COALESCE((SELECT c.owner_chat_id FROM web_conversations c WHERE c.conversation_uuid=NEW.parent_session_uuid LIMIT 1),0),
    NEW.task_uuid, 'task-notification', NEW.status,
    json_object(
      'taskUuid', NEW.task_uuid,
      'status', NEW.status,
      'summary', COALESCE(NULLIF(NEW.current_status,''), NULLIF(NEW.title,''), 'Agent task completed'),
      'content', COALESCE(NULLIF(NEW.output_json,''), NULLIF(NEW.error,''), ''),
      'conversationUuid', NEW.parent_session_uuid,
      'chatId', NEW.chat_id,
      'kind', 'task-notification',
      'durableFallback', 1
    ),
    'pending', 0, '', 0, 0, '', NEW.updated_at, NEW.updated_at, 0
  );
END;


CREATE TABLE IF NOT EXISTS rath_task_events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  task_uuid    TEXT NOT NULL,
  seq          INTEGER NOT NULL,
  ts           INTEGER DEFAULT 0,
  kind         TEXT NOT NULL,
  agent_key    TEXT DEFAULT '',
  summary      TEXT DEFAULT '',
  detail_json  TEXT DEFAULT '{}',
  elapsed_ms   INTEGER DEFAULT 0,
  UNIQUE(task_uuid, seq)
);
CREATE INDEX IF NOT EXISTS idx_rath_task_events_task_seq
  ON rath_task_events(task_uuid, seq);
CREATE INDEX IF NOT EXISTS idx_rath_task_events_kind_time
  ON rath_task_events(kind, ts DESC, id DESC);

CREATE TABLE IF NOT EXISTS rath_task_artifacts (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  artifact_uuid     TEXT NOT NULL UNIQUE,
  task_uuid         TEXT NOT NULL,
  agent_key         TEXT DEFAULT '',
  kind              TEXT DEFAULT '',
  name              TEXT DEFAULT '',
  summary           TEXT DEFAULT '',
  content           TEXT DEFAULT '',
  content_type      TEXT DEFAULT 'text/plain',
  source_refs_json  TEXT DEFAULT '[]',
  size_bytes        INTEGER DEFAULT 0,
  created_at        INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rath_artifacts_task_kind
  ON rath_task_artifacts(task_uuid, kind, id);

CREATE TABLE IF NOT EXISTS rath_task_controls (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  control_uuid   TEXT NOT NULL UNIQUE,
  task_uuid       TEXT NOT NULL,
  action          TEXT NOT NULL,
  message         TEXT DEFAULT '',
  requested_by    TEXT DEFAULT '',
  status          TEXT DEFAULT 'pending',
  created_at      INTEGER DEFAULT 0,
  applied_at      INTEGER DEFAULT 0,
  result          TEXT DEFAULT '',
  metadata_json   TEXT NOT NULL DEFAULT '{}',
  response_status TEXT DEFAULT '',
  response_reason TEXT DEFAULT '',
  response_plan_impact TEXT DEFAULT '',
  response_next_action TEXT DEFAULT '',
  responded_at    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rath_controls_task_status
  ON rath_task_controls(task_uuid, status, id);
CREATE INDEX IF NOT EXISTS idx_rath_controls_time
  ON rath_task_controls(created_at DESC, id DESC);
