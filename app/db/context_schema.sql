-- Private context state is not part of the public Web transcript. A window is
-- independent of provider-native checkpoints and historical summary IDs.
CREATE TABLE IF NOT EXISTS context_windows (
  owner_key TEXT PRIMARY KEY,
  owner_kind TEXT NOT NULL CHECK(owner_kind IN ('controller','agent')),
  conversation_uuid TEXT NOT NULL DEFAULT '',
  session_uuid TEXT NOT NULL DEFAULT '',
  agent_session_uuid TEXT NOT NULL DEFAULT '',
  task_uuid TEXT NOT NULL DEFAULT '',
  revision INTEGER NOT NULL DEFAULT 0,
  window_version INTEGER NOT NULL DEFAULT 0,
  source_revision INTEGER NOT NULL DEFAULT 0,
  source_high_water INTEGER NOT NULL DEFAULT 0,
  route_fingerprint TEXT NOT NULL DEFAULT '',
  state_json TEXT NOT NULL DEFAULT '{}',
  usage_known INTEGER NOT NULL DEFAULT 0,
  usage_tokens INTEGER NOT NULL DEFAULT 0,
  usage_request_id TEXT NOT NULL DEFAULT '',
  request_sequence INTEGER NOT NULL DEFAULT 0,
  usage_request_sequence INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_context_windows_conversation ON context_windows(conversation_uuid);
CREATE INDEX IF NOT EXISTS idx_context_windows_session ON context_windows(session_uuid);
CREATE INDEX IF NOT EXISTS idx_context_windows_agent ON context_windows(agent_session_uuid);

-- Agent checkpoints may replace an active window, but never its source history.
-- Ordinary Controller history stays in messages; message_id may refer to that
-- existing source rather than making another full transcript copy.
CREATE TABLE IF NOT EXISTS context_execution_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_key TEXT NOT NULL,
  event_id TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'execution',
  task_uuid TEXT NOT NULL DEFAULT '',
  turn_uuid TEXT NOT NULL DEFAULT '',
  message_id INTEGER,
  payload_json TEXT NOT NULL DEFAULT '{}',
  fingerprint TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE(owner_key,event_id)
);
CREATE INDEX IF NOT EXISTS idx_context_execution_owner_seq ON context_execution_events(owner_key,seq);
CREATE INDEX IF NOT EXISTS idx_context_execution_task ON context_execution_events(task_uuid,seq);
CREATE INDEX IF NOT EXISTS idx_context_execution_message ON context_execution_events(message_id);

-- Local maintenance telemetry: no model/tool call or billing entry is created.
CREATE TABLE IF NOT EXISTS context_window_rotations (
  rotation_id TEXT PRIMARY KEY,
  owner_key TEXT NOT NULL,
  window_version INTEGER NOT NULL,
  reason TEXT NOT NULL,
  detail_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE(owner_key,window_version)
);
