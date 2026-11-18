-- Hierarchical user knowledge-base notes and embedded image/file assets.
-- Notes are not injected into system prompts automatically; they participate
-- in explicit @note references when chosen by the user.

CREATE TABLE IF NOT EXISTS kb_notes (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  note_uuid       TEXT NOT NULL UNIQUE,
  owner_chat_id   INTEGER NOT NULL,
  parent_uuid     TEXT NOT NULL DEFAULT '',
  title           TEXT NOT NULL,
  icon            TEXT NOT NULL DEFAULT '',
  content         TEXT NOT NULL DEFAULT '',
  summary         TEXT NOT NULL DEFAULT '',
  char_count      INTEGER NOT NULL DEFAULT 0,
  pinned_at       INTEGER NOT NULL DEFAULT 0,
  sort_order      INTEGER NOT NULL DEFAULT 0,
  is_readonly     INTEGER NOT NULL DEFAULT 0,
  revision        INTEGER NOT NULL DEFAULT 1,
  source_kind     TEXT NOT NULL DEFAULT '',
  source_ref      TEXT NOT NULL DEFAULT '',
  created_at_ms   INTEGER NOT NULL,
  updated_at_ms   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_kb_notes_tree
  ON kb_notes(owner_chat_id, parent_uuid, pinned_at DESC, sort_order ASC, id ASC);

CREATE INDEX IF NOT EXISTS idx_kb_notes_updated
  ON kb_notes(owner_chat_id, updated_at_ms DESC);

CREATE TABLE IF NOT EXISTS kb_note_assets (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_uuid      TEXT NOT NULL UNIQUE,
  owner_chat_id   INTEGER NOT NULL,
  note_uuid       TEXT NOT NULL DEFAULT '',
  filename        TEXT NOT NULL,
  mime_type       TEXT NOT NULL,
  size_bytes      INTEGER NOT NULL DEFAULT 0,
  sha256          TEXT NOT NULL DEFAULT '',
  storage_relpath TEXT NOT NULL,
  source_path     TEXT NOT NULL DEFAULT '',
  created_at_ms   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_kb_note_assets_owner_sha
  ON kb_note_assets(owner_chat_id, sha256);

CREATE INDEX IF NOT EXISTS idx_kb_note_assets_note
  ON kb_note_assets(owner_chat_id, note_uuid);
