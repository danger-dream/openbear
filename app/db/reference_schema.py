"""Transactional, content-free change feed for the global Web resource catalog."""

from __future__ import annotations


def reference_schema() -> str:
    sql = [
        """
CREATE TABLE IF NOT EXISTS web_catalog_changes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    owner_chat_id INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_web_catalog_changes_entity ON web_catalog_changes(kind, entity_id, seq);
CREATE TRIGGER IF NOT EXISTS catalog_change_retention
AFTER INSERT ON web_catalog_changes WHEN NEW.seq % 1000 = 0 BEGIN
    DELETE FROM web_catalog_changes WHERE seq < NEW.seq - 10000;
END;
CREATE TABLE IF NOT EXISTS web_reference_bundles (
    bundle_uuid TEXT PRIMARY KEY,
    conversation_uuid TEXT NOT NULL,
    op_id TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    material_json TEXT NOT NULL DEFAULT '[]',
    protected_material TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_web_reference_bundles_conversation
    ON web_reference_bundles(conversation_uuid, op_id);
CREATE TRIGGER IF NOT EXISTS reference_bundle_operation_deleted
AFTER DELETE ON web_operations BEGIN
    DELETE FROM web_reference_bundles WHERE conversation_uuid=OLD.conversation_uuid AND op_id=OLD.op_id;
END;
CREATE TRIGGER IF NOT EXISTS reference_bundle_conversation_deleted
AFTER DELETE ON web_conversations BEGIN
    DELETE FROM web_reference_bundles WHERE conversation_uuid=OLD.conversation_uuid;
END;
-- Visible transcript changes are deliberately separate from chat organization
-- changes. Global realtime can refresh reference sizes immediately without
-- rebuilding tree runtime state for each streamed assistant delta.
CREATE TRIGGER IF NOT EXISTS catalog_web_conversation_content_update
AFTER UPDATE OF reference_revision ON web_conversations
WHEN OLD.reference_revision IS NOT NEW.reference_revision BEGIN
    INSERT INTO web_catalog_changes(kind, entity_id, owner_chat_id)
    VALUES('chat-content', CAST(NEW.conversation_uuid AS TEXT), NEW.owner_chat_id);
END;
"""
    ]
    sources = [
        (
            "memory_entries",
            "mem",
            "id",
            "0",
            [
                "ref",
                "title",
                "grp",
                "body",
                "fields_json",
                "enabled",
                "archived",
                "category_id",
                "sort",
            ],
        ),
        (
            "memory_docs",
            "doc",
            "id",
            "0",
            ["name", "title", "grp", "content", "summary", "enabled", "archived", "sort"],
        ),
        (
            "memory_secrets",
            "secret",
            "id",
            "0",
            ["name", "grp", "kv_json", "enabled", "archived", "sort"],
        ),
        (
            "web_conversations",
            "chat",
            "conversation_uuid",
            "owner_chat_id",
            [
                "title",
                "folder_uuid",
                "archived_at",
                "pinned_at",
                "display_order",
                "status",
                "current_status",
            ],
        ),
        (
            "web_conversation_folders",
            "folder",
            "folder_uuid",
            "owner_chat_id",
            ["name", "parent_uuid", "pinned_at", "display_order"],
        ),
    ]
    for table, kind, key, owner, columns in sources:
        for action, row in [("INSERT", "NEW"), ("DELETE", "OLD"), ("UPDATE", "NEW")]:
            when = ""
            if action == "UPDATE":
                when = "WHEN " + " OR ".join(
                    f"OLD.{column} IS NOT NEW.{column}" for column in columns
                )
            owner_value = "0" if owner == "0" else f"{row}.{owner}"
            sql.append(f"""
CREATE TRIGGER IF NOT EXISTS catalog_{table}_{action.lower()}
AFTER {action} ON {table} {when} BEGIN
    INSERT INTO web_catalog_changes(kind, entity_id, owner_chat_id)
    VALUES('{kind}', CAST({row}.{key} AS TEXT), {owner_value});
END;
""")
    return "\n".join(sql)
