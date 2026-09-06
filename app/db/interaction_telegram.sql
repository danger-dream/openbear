-- Telegram transport state for unified UserInteraction records.
-- The canonical interaction and answer remain owned by InteractionService.
-- No prompt bodies, option labels/defaults, or terminal results are copied here.

CREATE TABLE IF NOT EXISTS interaction_tg_outbox (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  delivery_key          TEXT NOT NULL UNIQUE,
  interaction_id        TEXT NOT NULL,
  owner_chat_id         INTEGER NOT NULL,
  event_type            TEXT NOT NULL, -- created|resolved|refresh|question|summary
  question_index        INTEGER NOT NULL DEFAULT -1,
  draft_version         INTEGER NOT NULL DEFAULT 0,
  state                 TEXT NOT NULL DEFAULT 'pending', -- pending|processing|sent|cancelled|failed
  deliver_after         INTEGER NOT NULL,
  attempts              INTEGER NOT NULL DEFAULT 0,
  telegram_message_id   INTEGER NOT NULL DEFAULT 0,
  last_error            TEXT NOT NULL DEFAULT '',
  created_at            INTEGER NOT NULL,
  updated_at            INTEGER NOT NULL,
  delivered_at          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_interaction_tg_outbox_delivery
  ON interaction_tg_outbox(state, deliver_after, id);
CREATE INDEX IF NOT EXISTS idx_interaction_tg_outbox_interaction
  ON interaction_tg_outbox(interaction_id, id);

CREATE TABLE IF NOT EXISTS interaction_tg_drafts (
  interaction_id        TEXT PRIMARY KEY,
  owner_chat_id         INTEGER NOT NULL,
  callback_token        TEXT NOT NULL UNIQUE,
  interaction_revision  INTEGER NOT NULL,
  action                TEXT NOT NULL,
  selected_values_json  TEXT NOT NULL DEFAULT '[]',
  selected_indexes_json TEXT NOT NULL DEFAULT '[]',
  text_value            TEXT NOT NULL DEFAULT '',
  confirm_decision      TEXT NOT NULL DEFAULT '',
  questionnaire_json    TEXT NOT NULL DEFAULT '{}',
  current_question_index INTEGER NOT NULL DEFAULT -1,
  view                   TEXT NOT NULL DEFAULT 'root',
  draft_version          INTEGER NOT NULL DEFAULT 1,
  active                 INTEGER NOT NULL DEFAULT 1,
  created_at             INTEGER NOT NULL,
  updated_at             INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_interaction_tg_drafts_owner
  ON interaction_tg_drafts(owner_chat_id, active, updated_at DESC);

CREATE TABLE IF NOT EXISTS interaction_tg_messages (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  delivery_id           INTEGER UNIQUE,
  interaction_id        TEXT NOT NULL,
  owner_chat_id         INTEGER NOT NULL,
  telegram_message_id   INTEGER NOT NULL,
  role                  TEXT NOT NULL, -- root|question|summary
  question_index        INTEGER NOT NULL DEFAULT -1,
  interaction_revision  INTEGER NOT NULL,
  active                INTEGER NOT NULL DEFAULT 1,
  created_at             INTEGER NOT NULL,
  updated_at             INTEGER NOT NULL,
  UNIQUE(owner_chat_id, telegram_message_id)
);
CREATE INDEX IF NOT EXISTS idx_interaction_tg_messages_interaction
  ON interaction_tg_messages(interaction_id, active, id);
CREATE INDEX IF NOT EXISTS idx_interaction_tg_messages_reply
  ON interaction_tg_messages(owner_chat_id, telegram_message_id);
