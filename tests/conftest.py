"""测试公共夹具：用 httpx MockTransport 模拟各协议响应，不打真实 Parrot。"""
from __future__ import annotations

import asyncio
import atexit
import os
import pwd
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path

import aiosqlite
import httpx
import pytest

from app.db import engine as db_engine
from app.llm.client import HTTPClient

# OpenBear's own shell has no HOME; Go then finds no module cache and the node does not build.
os.environ.setdefault("HOME", pwd.getpwuid(os.getuid()).pw_dir)

_real_db_connect = db_engine.DB._connect
_db_template: Path | None = None


async def fast_db_connect(db) -> None:
    """DB._connect for tests: a new database file starts as a copy of one migrated once per
    test process, then is opened the way DB._connect opens it. Keep the two in step;
    tests/test_db_fast_connect.py compares them. Files that already exist migrate for real."""
    global _db_template
    path = Path(db.path).expanduser()
    if db.path in ("", ":memory:") or path.exists():
        return await _real_db_connect(db)
    if _db_template is None:
        folder = tempfile.mkdtemp(prefix="openbear-db-template-")
        atexit.register(shutil.rmtree, folder, True)
        template = db_engine.DB(os.path.join(folder, "template.db"))
        await _real_db_connect(template)
        await template.close()
        with sqlite3.connect(template.path) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        _db_template = Path(template.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_db_template, path)
    writer = await db_engine.open_sqlite_connection(db.path)
    try:
        writer.row_factory = aiosqlite.Row
        await writer.execute("PRAGMA journal_mode=WAL")
        await writer.execute("PRAGMA synchronous=NORMAL")
        await writer.execute("PRAGMA busy_timeout=5000")
        reader = await db_engine.open_sqlite_connection(db.path)
    except BaseException:
        await writer.close()
        raise
    reader.row_factory = aiosqlite.Row
    await reader.execute("PRAGMA query_only=ON")
    await reader.execute("PRAGMA busy_timeout=5000")
    db._conn = db_engine.SQLiteConnectionRouter(reader=reader, writer=writer)


@pytest.fixture(autouse=True)
def _new_databases_from_template(request, monkeypatch):
    # Migrating an empty file takes ~0.1 s, more than most tests; tests marked real_db migrate.
    if "real_db" not in request.keywords:
        monkeypatch.setattr(db_engine.DB, "_connect", fast_db_connect)


def sse_response(lines: list[str]) -> httpx.Response:
    """构造一个 SSE 流式响应（lines 已是裸行，自动加换行）。"""
    body = "\n".join(lines) + "\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def make_client(handler) -> HTTPClient:
    """用 MockTransport 替换 HTTPClient 内部 httpx。"""
    client = HTTPClient()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


@pytest.fixture(autouse=True)
def _no_background_prompt_suggestions(request, monkeypatch):
    # Tests count exact upstream requests; the post-turn suggestion call runs only where marked.
    if "prompt_suggestions" in request.keywords:
        return
    from app.web_console.prompt_suggestions import WebAdminPromptSuggestionsMixin
    monkeypatch.setattr(WebAdminPromptSuggestionsMixin, "_start_prompt_suggestion_task", lambda *_args, **_kwargs: None)


@pytest.fixture(autouse=True)
def _no_github_node_releases(monkeypatch):
    # A checkout without node/bin would otherwise ask GitHub for node builds; tests that need releases serve their own.
    from app.servers import releases
    monkeypatch.setattr(releases, "_api", lambda: "http://127.0.0.1:9/no-github-in-tests")


_READ_ONLY_LOCAL = {"uname -m", "true", "id -u"}


@pytest.fixture(autouse=True)
def _no_commands_on_this_machine(request, monkeypatch):
    # The controller runs as root here: a test reaching the local target would change this machine
    # (it once reinstalled the live access gateway). Tests that mean it carry the host_commands mark.
    if "host_commands" in request.keywords:
        return
    from app.instances.servers import PORT_CHECK, Servers
    real = Servers.run

    def read_only(command):
        if command.strip() in _READ_ONLY_LOCAL:
            return True
        try:
            return command == PORT_CHECK % int(command.split("\n", 2)[1].removeprefix("p="))
        except (IndexError, ValueError):
            return False

    async def guarded(self, target, command, *args, **kwargs):
        if target.get("local") and not read_only(command):
            raise RuntimeError(f"tests must not run commands on this machine: {command[:80]!r}")
        return await real(self, target, command, *args, **kwargs)
    monkeypatch.setattr(Servers, "run", guarded)


_SYSTEM_CHANGES = re.compile(r"\b(systemctl|useradd|userdel|docker|iptables|ip6tables|nft|ufw|firewall-cmd|launchctl)\b"
                             r"|/etc/(systemd|openbear)|/usr/local/(lib|bin)/openbear|/var/lib/openbear")
_SHELLS = {"sh", "bash", "dash", "sudo"}


@pytest.fixture(autouse=True)
def _no_system_changes_from_shells(request, monkeypatch):
    # Below every fake executor: a shell a test starts must not touch services, containers,
    # firewalls or OpenBear's own system directories here, whatever path led to it.
    if "host_commands" in request.keywords:
        return
    real = asyncio.create_subprocess_exec

    def check(text):
        if found := _SYSTEM_CHANGES.search(text):
            raise RuntimeError(f"tests must not change this machine ({found.group(0)}): {text[:80]!r}")

    async def guarded(program, *args, **kwargs):
        if os.path.basename(str(program)) not in _SHELLS:
            return await real(program, *args, **kwargs)
        check(" ".join(map(str, args)))
        process = await real(program, *args, **kwargs)
        communicate = process.communicate

        async def checked(input=None):
            if input:
                try:
                    check(input.decode(errors="replace") if isinstance(input, bytes) else str(input))
                except RuntimeError:
                    process.kill()
                    await process.wait()
                    raise
            return await communicate(input)
        process.communicate = checked
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", guarded)


def pytest_addoption(parser):
    parser.addoption("--run-live", action="store_true", default=False,
                     help="运行依赖真实外部环境的 live 测试（真实 Parrot、真节点启动浏览器等）")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-live"):
        return
    skip = pytest.mark.skip(reason="需 --run-live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
