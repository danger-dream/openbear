#!/usr/bin/env python3
"""Verify preserved old values without exporting any database or configuration bodies."""
import hashlib
import json
import pathlib
import sqlite3
import sys

before = json.loads(pathlib.Path(sys.argv[1]).read_text())
root = pathlib.Path(before['root'])
output = pathlib.Path(sys.argv[2])
mode = sys.argv[3] if len(sys.argv) > 3 else 'restart'
if mode not in {'restart', 'refresh'}:
    raise ValueError('expected restart or refresh verification mode')
# Optional feature switches a release writes once on first start (app/features.py).
FEATURE_SWITCHES = {'instances', 'servers'}


def config_preserved(p, expected):
    """Every earlier config value kept; only feature switches the earlier config lacked may be added."""
    try:
        config = json.loads(p.read_text())
    except (OSError, ValueError):
        return False
    features = config.pop('features', None)
    canonical = json.dumps(config, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    if hashlib.sha256(canonical).hexdigest() != expected['sha256']:
        return False
    old = expected.get('features') or {}
    if features is None:
        return not old
    return (isinstance(features, dict) and set(features) <= FEATURE_SWITCHES and all(isinstance(v, bool) for v in features.values())
            and all(features.get(k) == v for k, v in old.items()))


errors, migrated = [], []
for rel, expected in before['files'].items():
    p = root / rel
    if not p.is_file():
        errors.append({'file': rel, 'error': 'missing'})
    elif p.stat().st_mode & 0o777 != expected['mode']:
        errors.append({'file': rel, 'error': 'changed bytes or permissions'})
    elif hashlib.sha256(p.read_bytes()).hexdigest() != expected['sha256']:
        if 'config' in expected and config_preserved(p, expected['config']):
            migrated.append(rel)
        else:
            errors.append({'file': rel, 'error': 'changed bytes or permissions'})
con = sqlite3.connect(f"file:{before['dbPath']}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
integrity = con.execute('PRAGMA integrity_check').fetchone()[0]
foreign_keys = con.execute('PRAGMA foreign_key_check').fetchall()
if integrity != 'ok' or foreign_keys:
    errors.append({'database': 'integrity/foreign_keys', 'integrity': integrity, 'foreignKeyViolations': len(foreign_keys)})
counts = {}
# Runtime timestamps may advance during normal startup; semantic user fields may not.
ignored = {'updated_at', 'last_active_at'}
for name, old_rows in before['tables'].items():
    current = [dict(r) for r in con.execute(f'SELECT * FROM {name} ORDER BY 1')]
    columns = [dict(r) for r in con.execute(f'PRAGMA table_info({name})')]
    key = next(x['name'] for x in columns if x['pk'])
    lookup = {x[key]: x for x in current}
    for old in old_rows:
        got = lookup.get(old[key])
        if got is None:
            errors.append({'table': name, 'row': old[key], 'error': 'removed'})
            continue
        changed = [k for k,v in old.items() if k not in ignored and got.get(k) != v]
        if changed:
            errors.append({'table': name, 'row': old[key], 'changedFields': changed})
    counts[name] = {'before': len(old_rows), 'after': len(current)}
additional = {}
if 'heavyHistory' in before:
    from heavy_upgrade import verify_heavy_history
    additional['heavyHistory'] = verify_heavy_history(con, before['heavyHistory'])
    if not additional['heavyHistory']['ok']:
        errors.append({'additionalCheck': 'large historical frame/body preservation failed'})
if 'legacy-terminal-time' in before.get('additionalChecks', []):
    from legacy_terminal_time import verify_legacy_terminal_time
    additional['legacyTerminalTime'] = verify_legacy_terminal_time(con, before['conversationUuid'])
    if not additional['legacyTerminalTime']['ok']:
        errors.append({'additionalCheck': 'legacy terminal time/frame preservation failed'})
con.close()
backups = []
for p in (root / 'data' / 'backups').glob('*.sqlite3'):
    c = sqlite3.connect(f'file:{p}?mode=ro', uri=True)
    check = c.execute('PRAGMA integrity_check').fetchone()[0]
    try:
        count = c.execute("SELECT COUNT(*) FROM messages WHERE content LIKE 'UPGRADE_%'").fetchone()[0]
    except sqlite3.OperationalError:
        count = 0
    c.close()
    backups.append({'name': p.name, 'integrity': check, 'seededMessages': count})
if mode == 'restart' and not any(x['integrity']=='ok' and x['seededMessages']==3 for x in backups):
    errors.append({'backup': 'no verified pre-upgrade backup with original messages'})
result = {'fromVersion': before['version'], 'ok': not errors, 'filesPreserved': len(before['files'])-sum('file' in x for x in errors), 'configMigrated': migrated,
          'tableCounts': counts, 'integrity': integrity, 'foreignKeyViolations': len(foreign_keys), 'backups': backups, 'backupRequired': mode == 'restart', 'errors': errors, 'additionalChecks': additional}
output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
print(json.dumps(result, ensure_ascii=False))
raise SystemExit(0 if not errors else 1)
