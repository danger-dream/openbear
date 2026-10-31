"""Dynamic DNS tables in the main database. Probe observations live in memory only:
nodes report them again after every reconnect."""
SCHEMA = '''
CREATE TABLE IF NOT EXISTS ddns_accounts (
 id TEXT PRIMARY KEY, owner INTEGER NOT NULL, type TEXT NOT NULL, alias TEXT NOT NULL, secret_id INTEGER NOT NULL DEFAULT 0,
 field_map_json TEXT NOT NULL DEFAULT '{}', settings_json TEXT NOT NULL DEFAULT '{}', zones_json TEXT NOT NULL DEFAULT '[]',
 zones_ms INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'unknown', error TEXT NOT NULL DEFAULT '',
 error_code TEXT NOT NULL DEFAULT '', sort INTEGER NOT NULL DEFAULT 0, created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ddns_accounts_owner ON ddns_accounts(owner, sort);
CREATE TABLE IF NOT EXISTS ddns_records (
 id TEXT PRIMARY KEY, owner INTEGER NOT NULL, account_id TEXT NOT NULL, zone TEXT NOT NULL, name TEXT NOT NULL, rtype TEXT NOT NULL,
 source_json TEXT NOT NULL, backup_json TEXT NOT NULL DEFAULT 'null', ttl INTEGER, options_json TEXT NOT NULL DEFAULT '{}',
 policy_json TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 1, provider_record_id TEXT NOT NULL DEFAULT '',
 desired TEXT NOT NULL DEFAULT '', desired_ms INTEGER NOT NULL DEFAULT 0, remote TEXT NOT NULL DEFAULT '', remote_ms INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '', error_code TEXT NOT NULL DEFAULT '',
 fail_count INTEGER NOT NULL DEFAULT 0, first_fail_ms INTEGER NOT NULL DEFAULT 0, changed_ms INTEGER NOT NULL DEFAULT 0,
 synced_ms INTEGER NOT NULL DEFAULT 0, checked_ms INTEGER NOT NULL DEFAULT 0, next_ms INTEGER NOT NULL DEFAULT 0,
 drift_times_json TEXT NOT NULL DEFAULT '[]', active_source TEXT NOT NULL DEFAULT 'primary', several_seen INTEGER NOT NULL DEFAULT 0,
 created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ddns_records_owner ON ddns_records(owner, account_id);
CREATE TABLE IF NOT EXISTS ddns_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER NOT NULL, record_id TEXT NOT NULL, at_ms INTEGER NOT NULL, kind TEXT NOT NULL,
 text TEXT NOT NULL, detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ddns_events_record ON ddns_events(record_id, id);
CREATE INDEX IF NOT EXISTS ddns_events_at ON ddns_events(at_ms);
'''
# Columns added after the first release; a record from before several_seen has not been seen with several.
ADDED = {'ddns_records': {'several_seen': 'INTEGER NOT NULL DEFAULT 0'}}


async def migrate(db):
    async with db.write_transaction(label='ddns-schema') as conn:
        for statement in SCHEMA.split(';'):
            if statement.strip():
                await conn.execute(statement)
        for table, columns in ADDED.items():
            async with conn.execute(f'PRAGMA table_info({table})') as cursor:
                present = {r[1] for r in await cursor.fetchall()}
            for column, kind in columns.items():
                if column not in present:
                    await conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {kind}')
