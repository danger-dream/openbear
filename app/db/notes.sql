-- User-only conversation sticky notes and global multi-sheet sticky notes.
-- Never exposed to model prompts, context windows, or tool schemas.

CREATE TABLE IF NOT EXISTS user_note_sheets (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  sheet_uuid     TEXT NOT NULL UNIQUE,
  owner_chat_id  INTEGER NOT NULL,
  title          TEXT NOT NULL,
  sort_order     INTEGER NOT NULL DEFAULT 0,
  created_at_ms  INTEGER NOT NULL,
  updated_at_ms  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_note_sheets_owner
  ON user_note_sheets(owner_chat_id, sort_order, id);

CREATE TABLE IF NOT EXISTS user_notes (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  note_uuid           TEXT NOT NULL UNIQUE,
  owner_chat_id       INTEGER NOT NULL,
  scope               TEXT NOT NULL,  -- session | global
  conversation_uuid   TEXT NOT NULL DEFAULT '',
  conversation_title  TEXT NOT NULL DEFAULT '',
  sheet_uuid          TEXT NOT NULL DEFAULT '',
  turn_uuid           TEXT NOT NULL DEFAULT '',
  turn_number         INTEGER NOT NULL DEFAULT 0,
  op_id               TEXT NOT NULL DEFAULT '',
  quote               TEXT NOT NULL DEFAULT '',
  content             TEXT NOT NULL DEFAULT '',
  created_at_ms       INTEGER NOT NULL,
  updated_at_ms       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_notes_session
  ON user_notes(owner_chat_id, scope, conversation_uuid, id DESC);
CREATE INDEX IF NOT EXISTS idx_user_notes_sheet
  ON user_notes(owner_chat_id, scope, sheet_uuid, id DESC);
CREATE INDEX IF NOT EXISTS idx_user_notes_conversation_cleanup
  ON user_notes(scope, conversation_uuid);
