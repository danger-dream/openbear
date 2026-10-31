"""Module-local tables in the main database. Metrics live in their own file."""
SCHEMA = '''
CREATE TABLE IF NOT EXISTS server_profiles (
 server_id TEXT PRIMARY KEY, owner INTEGER NOT NULL, profile_json TEXT NOT NULL, updated_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS server_nodes (
 server_id TEXT PRIMARY KEY, owner INTEGER NOT NULL, public_key TEXT NOT NULL UNIQUE,
 info_json TEXT NOT NULL DEFAULT '{}', enrolled_ms INTEGER NOT NULL, seen_ms INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS server_enrollments (
 code_hash TEXT PRIMARY KEY, owner INTEGER NOT NULL, server_id TEXT NOT NULL, expires_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS server_settings (
 id INTEGER PRIMARY KEY CHECK(id=1), settings_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS server_terminal_log (
 id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER NOT NULL, server_id TEXT NOT NULL,
 started_ms INTEGER NOT NULL, ended_ms INTEGER NOT NULL DEFAULT 0, ip TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS server_terminal_log_server ON server_terminal_log(server_id, started_ms);
CREATE TABLE IF NOT EXISTS server_commands (
 id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER NOT NULL, name TEXT NOT NULL, command TEXT NOT NULL,
 sort INTEGER NOT NULL DEFAULT 0, updated_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS server_identity (
 id INTEGER PRIMARY KEY CHECK(id=1), sealed TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS server_exec_log (
 id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER NOT NULL, server_id TEXT NOT NULL, source TEXT NOT NULL,
 command TEXT NOT NULL, started_ms INTEGER NOT NULL, duration_ms INTEGER NOT NULL DEFAULT 0,
 exit_code INTEGER, outcome TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS server_exec_log_server ON server_exec_log(server_id, id);
CREATE TABLE IF NOT EXISTS server_registration_codes (
 id TEXT PRIMARY KEY, owner INTEGER NOT NULL, name TEXT NOT NULL, code_hash TEXT NOT NULL UNIQUE, sealed TEXT NOT NULL,
 profile_json TEXT NOT NULL DEFAULT '{}', expires_ms INTEGER NOT NULL DEFAULT 0, max_uses INTEGER NOT NULL DEFAULT 0,
 uses INTEGER NOT NULL DEFAULT 0, created_ms INTEGER NOT NULL, last_used_ms INTEGER NOT NULL DEFAULT 0, revoked_ms INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS server_notifications (
 id TEXT PRIMARY KEY, owner INTEGER NOT NULL, server_id TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, text TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, next_ms INTEGER NOT NULL DEFAULT 0,
 created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mesh_network (
 owner INTEGER PRIMARY KEY, network_id TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
 applied_revision INTEGER NOT NULL DEFAULT 0, applied_ms INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS mesh_settings (
 owner INTEGER NOT NULL, stage TEXT NOT NULL, settings_json TEXT NOT NULL, relay_mode TEXT NOT NULL DEFAULT 'full',
 PRIMARY KEY(owner, stage)
);
CREATE TABLE IF NOT EXISTS mesh_members (
 owner INTEGER NOT NULL, stage TEXT NOT NULL, server_id TEXT NOT NULL, sort INTEGER NOT NULL, member_json TEXT NOT NULL,
 PRIMARY KEY(owner, stage, server_id)
);
CREATE TABLE IF NOT EXISTS mesh_links (
 owner INTEGER NOT NULL, stage TEXT NOT NULL, kind TEXT NOT NULL, sort INTEGER NOT NULL, link_json TEXT NOT NULL,
 PRIMARY KEY(owner, stage, kind, sort)
);
CREATE TABLE IF NOT EXISTS mesh_rules (
 owner INTEGER NOT NULL, stage TEXT NOT NULL, sort INTEGER NOT NULL, rule_json TEXT NOT NULL,
 PRIMARY KEY(owner, stage, sort)
);
CREATE TABLE IF NOT EXISTS mesh_revisions (
 owner INTEGER NOT NULL, revision INTEGER NOT NULL, created_ms INTEGER NOT NULL, reason TEXT NOT NULL,
 doc_json TEXT NOT NULL, configs_json TEXT NOT NULL, status TEXT NOT NULL, PRIMARY KEY(owner, revision)
);
CREATE TABLE IF NOT EXISTS mesh_changes (
 owner INTEGER PRIMARY KEY, change_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mesh_nodes (
 server_id TEXT PRIMARY KEY, owner INTEGER NOT NULL, public_key TEXT NOT NULL, control_key TEXT NOT NULL,
 platform TEXT NOT NULL DEFAULT '', data_version TEXT NOT NULL DEFAULT '', local_json TEXT NOT NULL DEFAULT '[]',
 tls_json TEXT NOT NULL DEFAULT '', tls_checked_ms INTEGER NOT NULL DEFAULT 0,
 confirmed_revision INTEGER NOT NULL DEFAULT 0, confirmed_hash TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
 status_json TEXT NOT NULL DEFAULT '', events_json TEXT NOT NULL DEFAULT '[]', updated_ms INTEGER NOT NULL DEFAULT 0,
 data_build TEXT NOT NULL DEFAULT '', upgrade_failed_build TEXT NOT NULL DEFAULT '', upgrade_error TEXT NOT NULL DEFAULT '',
 upgrade_ms INTEGER NOT NULL DEFAULT 0, addresses_json TEXT NOT NULL DEFAULT '[]', config_schemas TEXT NOT NULL DEFAULT '',
 sign_key TEXT NOT NULL DEFAULT '', e2e_key TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS mesh_layout (
 owner INTEGER NOT NULL, server_id TEXT NOT NULL, x REAL NOT NULL, y REAL NOT NULL, PRIMARY KEY(owner, server_id)
);
CREATE TABLE IF NOT EXISTS server_bench_runs (
 id TEXT PRIMARY KEY, owner INTEGER NOT NULL, server_id TEXT NOT NULL, test TEXT NOT NULL, options_json TEXT NOT NULL DEFAULT '{}',
 package TEXT NOT NULL DEFAULT '', suite TEXT NOT NULL DEFAULT '', state TEXT NOT NULL, stop INTEGER NOT NULL DEFAULT 0,
 job_id TEXT NOT NULL DEFAULT '', job_state TEXT NOT NULL DEFAULT '', exit_code INTEGER, error TEXT NOT NULL DEFAULT '',
 detail TEXT NOT NULL DEFAULT '', created_ms INTEGER NOT NULL, started_ms INTEGER NOT NULL DEFAULT 0, ended_ms INTEGER NOT NULL DEFAULT 0,
 sections BLOB, log BLOB, links_json TEXT NOT NULL DEFAULT '[]', attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS server_bench_runs_server ON server_bench_runs(server_id, created_ms);
'''
# Columns added after a table was first created; existing databases gain them on start.
ADDED = {'mesh_nodes': {'data_build': "TEXT NOT NULL DEFAULT ''", 'upgrade_failed_build': "TEXT NOT NULL DEFAULT ''",
                        'upgrade_error': "TEXT NOT NULL DEFAULT ''", 'upgrade_ms': 'INTEGER NOT NULL DEFAULT 0',
                        'addresses_json': "TEXT NOT NULL DEFAULT '[]'",
                        # [min, max] config schemas the data plane reads, as JSON; '' until it says.
                        'config_schemas': "TEXT NOT NULL DEFAULT ''",
                        # The signing (Ed25519) and end-to-end (X25519) public keys; '' until the node says.
                        'sign_key': "TEXT NOT NULL DEFAULT ''", 'e2e_key': "TEXT NOT NULL DEFAULT ''"},
         # Runs again after its package was found damaged on the node and pushed anew (once).
         'server_bench_runs': {'attempts': 'INTEGER NOT NULL DEFAULT 0'}}


async def migrate(db):
    async with db.write_transaction(label='servers-schema') as conn:
        for statement in SCHEMA.split(';'):
            if statement.strip():
                await conn.execute(statement)
        for table, columns in ADDED.items():
            async with conn.execute(f'PRAGMA table_info({table})') as cursor:
                present = {r[1] for r in await cursor.fetchall()}
            for column, kind in columns.items():
                if column not in present:
                    await conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {kind}')
