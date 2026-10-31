"""Module-local migration; no FKs to conversations (deletion is not undeploy)."""
SCHEMA = '''
CREATE TABLE IF NOT EXISTS managed_instances (
 id TEXT PRIMARY KEY, owner INTEGER NOT NULL, public_json TEXT NOT NULL,
 protected TEXT NOT NULL, report_hash TEXT NOT NULL UNIQUE,
 authority TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
 active_operation TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1
);
DROP INDEX IF EXISTS managed_instances_host;
CREATE TABLE IF NOT EXISTS instance_gateway (
 id INTEGER PRIMARY KEY CHECK(id=1), public_json TEXT NOT NULL, protected TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS instance_event_dedup (
 instance_id TEXT NOT NULL, id TEXT NOT NULL, received_ms INTEGER NOT NULL,
 PRIMARY KEY(instance_id,id)
);
CREATE INDEX IF NOT EXISTS instance_event_dedup_age ON instance_event_dedup(received_ms);
CREATE TABLE IF NOT EXISTS instance_operations (
 id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, owner INTEGER NOT NULL, kind TEXT NOT NULL,
 status TEXT NOT NULL, conversation_uuid TEXT NOT NULL UNIQUE, chat_id INTEGER NOT NULL DEFAULT 0,
 revision INTEGER NOT NULL DEFAULT 0, root TEXT NOT NULL DEFAULT '', run_id TEXT NOT NULL DEFAULT '',
 bundle TEXT NOT NULL DEFAULT '', report_json TEXT, verification_json TEXT,
 error TEXT NOT NULL DEFAULT '', created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS instance_events (
 instance_id TEXT NOT NULL, id TEXT NOT NULL, type TEXT NOT NULL, at_ms INTEGER NOT NULL,
 data_json TEXT NOT NULL, PRIMARY KEY(instance_id,id)
);
CREATE TABLE IF NOT EXISTS instance_notifications (
 id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, owner INTEGER NOT NULL,
 conversation_uuid TEXT NOT NULL DEFAULT '', text TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 next_ms INTEGER NOT NULL DEFAULT 0
);
'''


async def migrate(db):
    async with db.write_transaction(label='instances-schema') as conn:
        for statement in SCHEMA.split(';'):
            if statement.strip():
                await conn.execute(statement)
        # Module-local, additive upgrade of databases created by the first release.
        for table, additions in {
            'managed_instances': [('auth_mode', "TEXT NOT NULL DEFAULT 'local'"),
                                  ('gateway_report_hash', "TEXT NOT NULL DEFAULT ''")],
            'instance_notifications': [('created_ms', 'INTEGER NOT NULL DEFAULT 0'),
                                       ('updated_ms', 'INTEGER NOT NULL DEFAULT 0')],
            # The turn that started the operation through InstanceControl; empty: started by a person on the page.
            'instance_operations': [('origin_conversation', "TEXT NOT NULL DEFAULT ''"),
                                    ('origin_root', "TEXT NOT NULL DEFAULT ''"),
                                    # A completed operation's conversation talked to again: completed until the turn acts (acted()).
                                    ('resumed', "TEXT NOT NULL DEFAULT ''")],
        }.items():
            fields = {row[1] for row in await (await conn.execute('PRAGMA table_info('+table+')')).fetchall()}
            for name, definition in additions:
                if name not in fields:
                    await conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS managed_gateway_report ON managed_instances(gateway_report_hash) WHERE gateway_report_hash!=''")
        await conn.execute('CREATE INDEX IF NOT EXISTS instance_events_page ON instance_events(instance_id,type,at_ms,id)')
