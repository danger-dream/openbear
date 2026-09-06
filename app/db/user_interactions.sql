-- Canonical interaction state. Future objects and sensitive answer plaintext
-- are never persisted here. TG deliveries use their own transport tables.
CREATE TABLE IF NOT EXISTS user_interactions (
  interaction_id    TEXT PRIMARY KEY,
  owner_chat_id     INTEGER NOT NULL,
  conversation_uuid TEXT NOT NULL,
  turn_uuid         TEXT NOT NULL DEFAULT '',
  tool_call_id      TEXT NOT NULL DEFAULT '',
  payload_json      TEXT NOT NULL DEFAULT '{}',
  status            TEXT NOT NULL DEFAULT 'pending',
  revision          INTEGER NOT NULL DEFAULT 1,
  expires_at_ms     INTEGER NOT NULL,
  created_at_ms     INTEGER NOT NULL,
  resolved_at_ms    INTEGER NOT NULL DEFAULT 0,
  result_json       TEXT NOT NULL DEFAULT '',
  answer_digest     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_user_interactions_conversation
  ON user_interactions(conversation_uuid, status, created_at_ms);
CREATE INDEX IF NOT EXISTS idx_user_interactions_terminal
  ON user_interactions(status, resolved_at_ms);
