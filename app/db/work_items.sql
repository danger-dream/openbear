-- Work items: the user-visible record of a piece of work in one Web conversation,
-- from the problem being raised through decisions, execution and completion.
-- Numbers ("4", "4.3", "4.3.1") are allocated once and never reused, including
-- after deletion, so references in dialogue stay valid.

CREATE TABLE IF NOT EXISTS work_items (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  item_uuid          TEXT NOT NULL UNIQUE,
  conversation_uuid  TEXT NOT NULL,
  number             TEXT NOT NULL,
  parent_number      TEXT NOT NULL DEFAULT '',
  seq                INTEGER NOT NULL,
  sort_order         INTEGER NOT NULL DEFAULT 0,
  title              TEXT NOT NULL,
  description        TEXT NOT NULL DEFAULT '',
  criteria_json      TEXT NOT NULL DEFAULT '[]',
  scope_method       TEXT NOT NULL DEFAULT '',
  -- top level: discussing|decided|in_progress|done|abandoned
  -- below:     todo|doing|blocked|ask|done|dropped
  status             TEXT NOT NULL,
  status_reason      TEXT NOT NULL DEFAULT '',
  evidence_json      TEXT NOT NULL DEFAULT '[]',
  revision           INTEGER NOT NULL DEFAULT 1,
  created_turn_uuid  TEXT NOT NULL DEFAULT '',
  created_at_ms      INTEGER NOT NULL,
  updated_at_ms      INTEGER NOT NULL,
  deleted_at_ms      INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_work_items_number
  ON work_items(conversation_uuid, number);
CREATE INDEX IF NOT EXISTS idx_work_items_parent
  ON work_items(conversation_uuid, parent_number, deleted_at_ms, sort_order);

CREATE TABLE IF NOT EXISTS work_item_events (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  event_uuid         TEXT NOT NULL UNIQUE,
  conversation_uuid  TEXT NOT NULL,
  item_uuid          TEXT NOT NULL,
  number             TEXT NOT NULL,
  kind               TEXT NOT NULL,
  actor              TEXT NOT NULL,      -- user|controller|cron|webhook|collab|system
  actor_label        TEXT NOT NULL DEFAULT '',
  summary            TEXT NOT NULL DEFAULT '',
  data_json          TEXT NOT NULL DEFAULT '{}',
  source_json        TEXT NOT NULL DEFAULT '{}',
  turn_uuid          TEXT NOT NULL DEFAULT '',
  created_at_ms      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_work_item_events_item
  ON work_item_events(item_uuid, id);
CREATE INDEX IF NOT EXISTS idx_work_item_events_conversation
  ON work_item_events(conversation_uuid, actor, id);
CREATE INDEX IF NOT EXISTS idx_work_item_events_turn
  ON work_item_events(conversation_uuid, turn_uuid, kind);

-- The user-confirmed scope of one root turn. One row per root turn; set again
-- to replace it within the same turn.
CREATE TABLE IF NOT EXISTS work_item_focus (
  conversation_uuid  TEXT NOT NULL,
  root_turn_uuid     TEXT NOT NULL,
  numbers_json       TEXT NOT NULL DEFAULT '[]',
  current_number     TEXT NOT NULL DEFAULT '',
  basis_json         TEXT NOT NULL DEFAULT '{}',
  created_at_ms      INTEGER NOT NULL,
  updated_at_ms      INTEGER NOT NULL,
  PRIMARY KEY (conversation_uuid, root_turn_uuid)
);
