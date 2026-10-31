"""Atomic Webhook DDL failure isolation."""
import sqlite3

import pytest
from app.db.engine import DB
from app.webhooks.repository import one

# These test migrations, so every database here is built the real way.
pytestmark = pytest.mark.real_db


async def test_schema_failure_is_atomic_and_missing_resource_is_fatal(tmp_path,monkeypatch):
    import app.webhooks.schema as schema
    original=schema.files
    class Resource:
        def joinpath(self,name): return self
        def read_text(self,**kwargs): return 'CREATE TABLE webhook_half (id TEXT);\nTHIS IS INVALID SQL;\n'
    monkeypatch.setattr(schema,'files',lambda *args:Resource())
    path=tmp_path/'failed.sqlite'; db=DB(str(path))
    with pytest.raises(sqlite3.OperationalError): await db.connect()
    with sqlite3.connect(path) as c:
        assert not c.execute("SELECT name FROM sqlite_master WHERE name LIKE 'webhook_%'").fetchall()
        assert not c.execute("SELECT name FROM schema_data_migrations WHERE name LIKE 'webhooks_v1:%'").fetchall()
    class Missing(Resource):
        def read_text(self,**kwargs): raise FileNotFoundError('webhooks.sql')
    monkeypatch.setattr(schema,'files',lambda *args:Missing())
    with pytest.raises(FileNotFoundError): await DB(str(path)).connect()
    monkeypatch.setattr(schema,'files',original)
    db=DB(str(path)); await db.connect()
    try: assert await one(db.conn,"SELECT name FROM sqlite_master WHERE name='webhook_endpoints'")
    finally: await db.close()
