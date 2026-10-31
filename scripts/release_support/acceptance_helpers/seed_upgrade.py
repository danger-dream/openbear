#!/usr/bin/env python3
"""Run under the OLD installed interpreter; seed legitimate persistent upgrade inputs."""
import asyncio
import hashlib
import json
import os
import pathlib
import sqlite3
import sys
import time

root = pathlib.Path(sys.argv[1]).resolve()
report = pathlib.Path(sys.argv[2]).resolve()
os.chdir(root)
sys.path.insert(0, str(root))
# Deliberately import the OLD installed application, not the host checkout.
from app import installed_version  # noqa: E402
from app.config import Config  # noqa: E402
from app.db.dao import MessageDAO, SummaryDAO  # noqa: E402
from app.db.engine import DB  # noqa: E402
from app.web_admin import WebAdminServer  # noqa: E402


async def seed():
    cfg = Config.model_validate(json.loads((root / 'openbear.json').read_text()))
    db_path = pathlib.Path(cfg.storage.db_path)
    if not db_path.is_absolute():
        db_path = root / db_path
    db = DB(str(db_path))
    await db.connect()
    server = WebAdminServer(cfg, db, object())
    row = await server._create_web_conversation(123456789, title='UPGRADE_PRESERVATION_SENTINEL', model=cfg.models.primary)
    chat = int(row['internal_chat_id'])
    dao = MessageDAO(db)
    first = await dao.add(chat, 'user', 'UPGRADE_USER_ORIGINAL: preserve the existing conversation and never deploy without instruction.')
    await dao.add(chat, 'assistant', 'UPGRADE_ASSISTANT_ORIGINAL: the accepted correction must survive migration.')
    await SummaryDAO(db).add(chat, 'UPGRADE_SUMMARY_ORIGINAL: archive remains retrievable.', first, 20)
    await dao.add(chat, 'user', 'UPGRADE_RECENT_ORIGINAL: continue from the preserved correction.')
    now = int(time.time())
    # New test template versions, not an overwrite of any old template body.
    await db.conn.execute('UPDATE memory_templates SET is_active=0,is_agent_active=0')
    await db.conn.execute('INSERT INTO memory_templates(name,content,is_active,is_agent_active,updated_at) VALUES(?,?,1,0,?)',
                         ('Acceptance custom main', 'CUSTOM_MAIN_TEMPLATE_UPGRADE_SENTINEL\nDo not replace this user-owned template.', now))
    await db.conn.execute('INSERT INTO memory_templates(name,content,is_active,is_agent_active,updated_at) VALUES(?,?,0,1,?)',
                         ('Acceptance custom agent', 'CUSTOM_AGENT_TEMPLATE_UPGRADE_SENTINEL\nKeep this custom Agent version.', now))
    await db.conn.execute('INSERT INTO web_conversation_folders(folder_uuid,owner_chat_id,name,prompt_markdown,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                         ('upgrade-folder', 123456789, 'USER_FOLDER_SENTINEL', 'USER_FOLDER_PROMPT_SENTINEL', now, now))
    await db.conn.execute('UPDATE web_conversations SET folder_uuid=? WHERE conversation_uuid=?', ('upgrade-folder', row['conversation_uuid']))
    cur = await db.conn.execute('SELECT id FROM memory_categories ORDER BY id LIMIT 1')
    category = await cur.fetchone()
    assert category is not None
    await db.conn.execute('INSERT INTO memory_entries(category_id,ref,title,body,expanded,sort,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                         (category[0], 'upgrade-memory', 'USER_MEMORY_SENTINEL', 'USER_MEMORY_BODY_SENTINEL', 1, 70, now, now))
    await db.conn.execute('INSERT INTO memory_docs(name,title,content,sort,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                         ('upgrade-document', 'USER_DOCUMENT_SENTINEL', 'USER_DOCUMENT_BODY_SENTINEL', 80, now, now))
    # Release-specific migration fixtures are opt-in, never generic preservation gates.
    if '--legacy-terminal-time' in sys.argv[3:]:
        from legacy_terminal_time import seed_legacy_terminal_time
        await seed_legacy_terminal_time(db, row['conversation_uuid'])
    from heavy_upgrade import seed_heavy_history
    try:
        heavy_history = await seed_heavy_history(db, row['conversation_uuid'])
    except BaseException:
        await db.close()
        raise
    await db.conn.commit()
    await db.close()
    for relative, body in [
        ('workspace/acceptance-preserved.txt', 'WORKSPACE_UPGRADE_SENTINEL'),
        ('skills/acceptance-preserved/SKILL.md', '---\nname: acceptance-preserved\ndescription: Test fixture retained across upgrade.\n---\nSKILL_UPGRADE_SENTINEL'),
        ('mcp-servers/acceptance-preserved.txt', 'MCP_USER_FILE_UPGRADE_SENTINEL'),
        ('data/acceptance-user-state.txt', 'USER_DATA_UPGRADE_SENTINEL'),
    ]:
        p = root / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    con = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    tables = {}
    for name in ('messages', 'sessions', 'summaries', 'web_conversations', 'memory_templates', 'web_conversation_folders', 'memory_entries', 'memory_docs'):
        tables[name] = [dict(r) for r in con.execute(f'SELECT * FROM {name} ORDER BY 1')]
    con.close()
    files = {}
    for rel in ('openbear.json', 'workspace/acceptance-preserved.txt', 'skills/acceptance-preserved/SKILL.md', 'mcp-servers/acceptance-preserved.txt', 'data/acceptance-user-state.txt'):
        p = root / rel
        files[rel] = {'sha256': hashlib.sha256(p.read_bytes()).hexdigest(), 'mode': p.stat().st_mode & 0o777}
    # A newer release may add its one-time feature switches to the config; the values
    # already there are compared by digest, never exported.
    config = json.loads((root / 'openbear.json').read_text())
    features = config.pop('features', None)
    files['openbear.json']['config'] = {
        'sha256': hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest(),
        'features': {k: v for k, v in features.items() if isinstance(v, bool)} if isinstance(features, dict) else None,
    }
    result = {'version': installed_version(), 'root': str(root), 'dbPath': str(db_path), 'conversationUuid': row['conversation_uuid'], 'chatId': chat, 'files': files, 'tables': tables, 'additionalChecks': ['legacy-terminal-time'] if '--legacy-terminal-time' in sys.argv[3:] else []}
    result['heavyHistory'] = heavy_history
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    report.chmod(0o600)
    print(json.dumps({'seededVersion': result['version'], 'tableCounts': {k:len(v) for k,v in tables.items()}, 'preservedFileCount': len(files), 'historicalFrames': heavy_history['frameCount'], 'historicalPayloadBytes': heavy_history['payloadBytes']}))

asyncio.run(seed())
