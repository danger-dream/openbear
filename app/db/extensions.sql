-- 每个会话启用的扩展工具组（app/extensions.py 的目录键）。
-- 没有行的会话（功能上线前的会话、未声明扩展的自动执行会话）视为全部开启。
CREATE TABLE IF NOT EXISTS conversation_extensions (
  conversation_uuid TEXT PRIMARY KEY,
  enabled_json      TEXT NOT NULL DEFAULT '[]',
  auto_allow        INTEGER NOT NULL DEFAULT 0,
  revision          INTEGER NOT NULL DEFAULT 1,
  updated_at_ms     INTEGER NOT NULL DEFAULT 0
);

-- 目录给新会话的扩展默认值；folder_uuid='__temporary' 是不在目录里的会话。
-- 没有行的目录沿父目录向上找，都没有则全部关闭。
CREATE TABLE IF NOT EXISTS folder_extension_defaults (
  folder_uuid   TEXT PRIMARY KEY,
  owner_chat_id INTEGER NOT NULL,
  enabled_json  TEXT NOT NULL DEFAULT '[]',
  auto_allow    INTEGER NOT NULL DEFAULT 0,
  updated_at_ms INTEGER NOT NULL DEFAULT 0
);

CREATE TRIGGER IF NOT EXISTS conversation_extensions_deleted
AFTER DELETE ON web_conversations BEGIN
    DELETE FROM conversation_extensions WHERE conversation_uuid=OLD.conversation_uuid;
END;

CREATE TRIGGER IF NOT EXISTS folder_extension_defaults_deleted
AFTER DELETE ON web_conversation_folders BEGIN
    DELETE FROM folder_extension_defaults WHERE folder_uuid=OLD.folder_uuid;
END;
