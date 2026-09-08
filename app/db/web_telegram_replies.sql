-- Exact message bindings for replies to Web task notifications, not a TG chat session.
-- Keep small binding/deduplication records even after notification history is pruned:
-- old/deleted conversation replies must fail closed rather than select another chat.
CREATE TABLE IF NOT EXISTS web_tg_messages (
  owner_chat_id          INTEGER NOT NULL,
  telegram_message_id    INTEGER NOT NULL,
  conversation_uuid      TEXT NOT NULL,
  root_turn_uuid         TEXT NOT NULL DEFAULT '',
  role                   TEXT NOT NULL DEFAULT 'notification',
  created_at             INTEGER NOT NULL,
  PRIMARY KEY (owner_chat_id, telegram_message_id)
);
CREATE INDEX IF NOT EXISTS idx_web_tg_messages_conversation
  ON web_tg_messages(conversation_uuid);

-- Claim before invoking the shared Web ingress. A dispatching row is never
-- automatically replayed after a crash: the execution outcome may be uncertain.
CREATE TABLE IF NOT EXISTS web_tg_reply_inbox (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_chat_id          INTEGER NOT NULL,
  telegram_message_id    INTEGER NOT NULL,
  reply_to_message_id    INTEGER NOT NULL,
  conversation_uuid      TEXT NOT NULL,
  text                   TEXT NOT NULL,
  state                  TEXT NOT NULL DEFAULT 'pending',
  root_turn_uuid         TEXT NOT NULL DEFAULT '',
  response_text          TEXT NOT NULL DEFAULT '',
  response_sent          INTEGER NOT NULL DEFAULT 0,
  response_attempts      INTEGER NOT NULL DEFAULT 0,
  response_after         INTEGER NOT NULL DEFAULT 0,
  created_at             INTEGER NOT NULL,
  updated_at             INTEGER NOT NULL,
  UNIQUE (owner_chat_id, telegram_message_id)
);
CREATE INDEX IF NOT EXISTS idx_web_tg_reply_inbox_pending
  ON web_tg_reply_inbox(state, id);
CREATE INDEX IF NOT EXISTS idx_web_tg_reply_inbox_receipts
  ON web_tg_reply_inbox(response_sent, response_after, id);
