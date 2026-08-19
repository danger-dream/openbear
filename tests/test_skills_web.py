from __future__ import annotations

import json
import stat
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.admin.skills_web import SkillsWebAdminServer
from app.config import Config
from app.config_store import ConfigStore
from app.db.engine import DB


class FakeBot:
    def __init__(self) -> None:
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append(SimpleNamespace(chat_id=chat_id, text=text, reply_markup=reply_markup))
        return SimpleNamespace(message_id=len(self.sent))


@pytest.fixture
async def skills_admin_env(tmp_path):
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill\n---\n\n# Demo\n",
        encoding="utf-8",
    )
    cfg_path = tmp_path / "openbear.json"
    raw = {
        "telegram": {"botToken": "t", "whitelistIds": [123]},
        "models": {
            "providers": {
                "openai": {
                    "baseUrl": "http://openai.local/v1",
                    "apiKey": "k",
                    "protocol": "chat",
                    "models": [{"id": "gpt", "name": "GPT"}],
                }
            },
            "primary": "openai/gpt",
        },
        "memory": {"provider": "builtin"},
        "tools": {"skillsDir": str(skills_dir), "disabledSkills": ["demo", "other"]},
        "storage": {"dbPath": str(tmp_path / "t.db")},
        "web": {"enabled": True, "host": "127.0.0.1", "port": 18961},
    }
    cfg_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    store = ConfigStore(cfg_path)
    cfg = Config.model_validate(raw)
    db = DB(str(tmp_path / "t.db"))
    await db.connect()
    state = {"failApplies": 0}
    holder = {}

    def apply_config(config: Config) -> None:
        if state["failApplies"] > 0:
            state["failApplies"] -= 1
            raise RuntimeError("apply failed")
        holder["server"].apply_config(config)

    server = SkillsWebAdminServer(
        cfg,
        db,
        FakeBot(),
        config_store=store,
        apply_config_hook=apply_config,
    )  # type: ignore[arg-type]
    holder["server"] = server
    await server.ensure_secret_key()
    client = TestClient(TestServer(server.make_app()))
    await client.start_server()
    key = await server.get_secret_key()
    start = await client.post("/api/auth/login/start", json={"secret": key})
    request_uuid = (await start.json())["requestUuid"]
    await server.decide_login_request(request_uuid, approved=True, decided_by=123)
    approved = await client.post(f"/api/auth/login/consume/{request_uuid}")
    cookie = {"openbear_web_session": approved.cookies["openbear_web_session"].value}
    try:
        yield SimpleNamespace(
            server=server,
            client=client,
            cookie=cookie,
            state=state,
            cfg_path=cfg_path,
            skills_dir=skills_dir,
            skill_dir=skill_dir,
        )
    finally:
        await client.close()
        await db.close()


async def test_skill_uninstall_archives_directory_and_cleans_disabled_config(skills_admin_env):
    env = skills_admin_env
    mismatch = await env.client.post(
        "/api/skills/demo/uninstall",
        cookies=env.cookie,
        json={"confirm": True, "name": "wrong"},
    )
    assert mismatch.status == 400
    assert env.skill_dir.is_dir()

    response = await env.client.post(
        "/api/skills/demo/uninstall",
        cookies=env.cookie,
        json={"confirm": True, "name": "demo"},
    )
    assert response.status == 200
    payload = await response.json()
    assert payload["uninstalled"] is True
    assert payload["name"] == "demo"
    assert not env.skill_dir.exists()
    archive_root = env.skills_dir / ".uninstalled"
    archived = list(archive_root.iterdir())
    assert len(archived) == 1
    assert archived[0].name == payload["archiveName"]
    assert (archived[0] / "SKILL.md").is_file()
    assert stat.S_IMODE(archive_root.stat().st_mode) == 0o700
    raw = json.loads(env.cfg_path.read_text(encoding="utf-8"))
    assert raw["tools"]["disabledSkills"] == ["other"]
    assert payload["stats"]["total"] == 0


async def test_skill_uninstall_restores_directory_and_config_when_runtime_apply_fails(skills_admin_env):
    env = skills_admin_env
    env.state["failApplies"] = 1

    response = await env.client.post(
        "/api/skills/demo/uninstall",
        cookies=env.cookie,
        json={"confirm": True, "name": "demo"},
    )

    assert response.status == 500
    payload = await response.json()
    assert payload["error"] == "skill_uninstall_failed_rolled_back"
    assert payload["rollbackErrors"] == []
    assert env.skill_dir.is_dir()
    assert (env.skill_dir / "SKILL.md").is_file()
    raw = json.loads(env.cfg_path.read_text(encoding="utf-8"))
    assert raw["tools"]["disabledSkills"] == ["demo", "other"]


async def test_skill_uninstall_rejects_ambiguous_skill_name(skills_admin_env):
    env = skills_admin_env
    duplicate = env.skills_dir / "duplicate"
    duplicate.mkdir()
    (duplicate / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Duplicate\n---\n",
        encoding="utf-8",
    )

    response = await env.client.post(
        "/api/skills/demo/uninstall",
        cookies=env.cookie,
        json={"confirm": True, "name": "demo"},
    )

    assert response.status == 409
    assert (await response.json())["error"] == "skill_name_ambiguous"
    assert env.skill_dir.is_dir()
    assert duplicate.is_dir()


async def test_skill_uninstall_rejects_symlink_archive_directory(skills_admin_env, tmp_path):
    env = skills_admin_env
    outside = tmp_path / "outside"
    outside.mkdir()
    (env.skills_dir / ".uninstalled").symlink_to(outside, target_is_directory=True)

    response = await env.client.post(
        "/api/skills/demo/uninstall",
        cookies=env.cookie,
        json={"confirm": True, "name": "demo"},
    )

    assert response.status == 500
    assert (await response.json())["error"] == "skill_archive_unavailable"
    assert env.skill_dir.is_dir()
    assert list(outside.iterdir()) == []


async def test_skill_uninstall_refuses_while_openbear_run_is_active(skills_admin_env):
    env = skills_admin_env
    env.server.runs = SimpleNamespace(count=lambda: 1)

    response = await env.client.post(
        "/api/skills/demo/uninstall",
        cookies=env.cookie,
        json={"confirm": True, "name": "demo"},
    )

    assert response.status == 409
    assert (await response.json())["error"] == "skill_uninstall_busy"
    assert env.skill_dir.is_dir()
