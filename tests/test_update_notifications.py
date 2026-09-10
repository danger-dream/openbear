from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.update import service as service_module
from app.update.service import UpdateService, atomic_write_json, read_json


@pytest.fixture
async def notifications(tmp_path, monkeypatch):
    config = SimpleNamespace(
        storage=SimpleNamespace(db_path=str(tmp_path / "data" / "openbear.db")),
        telegram=SimpleNamespace(whitelist_ids=[123]),
        web=SimpleNamespace(host="127.0.0.1", port=18961),
    )
    svc = SimpleNamespace(config=config, bot=object())
    release = {"version": "0.2.0", "status": 200, "checks": 0, "draft": False, "prerelease": False}
    sent = []

    def github(_request):
        release["checks"] += 1
        if release["status"] != 200:
            return httpx.Response(release["status"])
        return httpx.Response(200, headers={"ETag": f'"check-{release["checks"]}"'}, json={
            "tag_name": f'v{release["version"]}',
            "name": f'v{release["version"]}',
            "body": f'notes-{release["checks"]}',
            "draft": release["draft"], "prerelease": release["prerelease"], "assets": [],
        })

    async def send(_bot, chat_id, text):
        sent.append((chat_id, text))
        await asyncio.sleep(0)  # The actual Telegram I/O yields too.

    monkeypatch.setattr(service_module, "installed_version", lambda: "0.1.0")
    monkeypatch.setattr(service_module, "send_rich", send)
    async with httpx.AsyncClient(transport=httpx.MockTransport(github)) as http:
        def service():
            instance = UpdateService(svc)
            instance._http = http
            return instance

        yield SimpleNamespace(service=service, release=release, sent=sent, config=config)


async def test_repeated_200_refreshes_release_metadata_but_not_notification(notifications):
    env = notifications
    service = env.service()
    for _ in range(6):
        await service._check_github()
    assert len(env.sent) == 1
    state = read_json(service.state_path)
    assert state["available"]["body"] == "notes-6"
    assert state["available"]["notifiedVersion"] == "0.2.0"
    assert state["notifiedVersions"] == ["0.2.0"]


async def test_restart_and_release_version_reappearance_do_not_repeat_notification(notifications):
    env = notifications
    await env.service()._check_github()
    for version in ("0.2.0", "0.3.0", "0.2.0", "0.3.0", "0.3.0"):
        env.release["version"] = version
        await env.service()._check_github()  # A fresh runtime each time.
    assert len(env.sent) == 2
    assert "最新：<code>v0.2.0</code>" in env.sent[0][1]
    assert "最新：<code>v0.3.0</code>" in env.sent[1][1]


@pytest.mark.parametrize("response", [304, 404, 500, "draft", "prerelease"])
async def test_temporary_release_absence_or_error_keeps_notification_history(notifications, response):
    env = notifications
    service = env.service()
    await service._check_github()
    if isinstance(response, int):
        env.release["status"] = response
    else:
        env.release[response] = True
    await service._check_github()
    env.release.update(status=200, draft=False, prerelease=False)
    for _ in range(3):
        await env.service()._check_github()
    assert len(env.sent) == 1


@pytest.mark.parametrize("first_response", [200, 304, 404, "draft"])
async def test_old_notified_version_migrates_before_release_metadata_is_overwritten(notifications, first_response):
    env = notifications
    service = env.service()
    atomic_write_json(service.state_path, {
        "phase": "idle", "customField": "preserved",
        "available": {"version": "0.2.0", "notifiedVersion": "0.2.0"},
    })
    if isinstance(first_response, int):
        env.release["status"] = first_response
    else:
        env.release["draft"] = True
    await service._check_github()
    env.release.update(status=200, draft=False)
    for _ in range(3):
        await env.service()._check_github()
    assert env.sent == []
    state = read_json(service.state_path)
    assert state["notifiedVersions"] == ["0.2.0"]
    assert state["customField"] == "preserved"
    assert read_json(service.notifications_path)["notifiedVersions"] == ["0.2.0"]


async def test_previous_state_history_migrates_before_a_304_and_survives_state_replacement(notifications):
    env = notifications
    service = env.service()
    # Upgrade from the existing state-history implementation without relying on
    # available.notifiedVersion or a successful release metadata response.
    atomic_write_json(service.state_path, {"notifiedVersions": ["0.2.0", "0.3.0"]})
    env.release["status"] = 304
    await service._check_github()
    assert read_json(service.notifications_path)["notifiedVersions"] == ["0.2.0", "0.3.0"]
    atomic_write_json(service.state_path, {"phase": "done"})
    env.release["status"] = 200
    for version in ("0.2.0", "0.3.0", "0.4.0"):
        env.release["version"] = version
        await env.service()._check_github()
    assert len(env.sent) == 1 and "最新：<code>v0.4.0</code>" in env.sent[0][1]


async def test_overlapping_checks_claim_notification_before_awaiting_telegram(notifications):
    env = notifications
    service = env.service()
    await asyncio.gather(*(service._check_github() for _ in range(6)))
    assert len(env.sent) == 1
    # Separate in-process instances read the same durable version history.
    await asyncio.gather(*(env.service()._check_github() for _ in range(6)))
    assert len(env.sent) == 1


@pytest.mark.parametrize("exception", [ConnectionError, asyncio.CancelledError])
async def test_uncertain_send_or_shutdown_does_not_resend_after_restart(notifications, monkeypatch, exception):
    env = notifications
    attempts = []

    async def uncertain(_bot, chat_id, text):
        attempts.append(chat_id)
        assert read_json(env.service().state_path)["notifiedVersions"] == ["0.2.0"]
        assert read_json(env.service().notifications_path)["notifiedVersions"] == ["0.2.0"]
        raise exception("delivery outcome unknown")

    monkeypatch.setattr(service_module, "send_rich", uncertain)
    if exception is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            await env.service()._check_github()
    else:
        await env.service()._check_github()
    for _ in range(4):
        await env.service()._check_github()
    assert attempts == [123]


async def test_notification_is_not_sent_if_durable_claim_cannot_be_saved(notifications, monkeypatch):
    env = notifications

    def cannot_save(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(service_module, "atomic_write_json", cannot_save)
    with pytest.raises(OSError, match="disk full"):
        await env.service()._check_github()
    assert env.sent == []


async def test_duplicate_whitelist_entries_do_not_duplicate_same_version_notice(notifications):
    env = notifications
    env.config.telegram.whitelist_ids = [123, 123, 456, 456]
    service = env.service()
    for _ in range(4):
        await service._check_github()
    assert [chat_id for chat_id, _text in env.sent] == [123, 456]


async def test_failed_launch_keeps_release_and_notice_claim_written_while_awaiting_launch(notifications, tmp_path, monkeypatch):
    env = notifications
    service = env.service()
    service.install_root = tmp_path
    await service._check_github()
    state = read_json(service.state_path)
    state["available"]["zipUrl"] = "https://example.invalid/openbear-0.2.0.zip"
    atomic_write_json(service.state_path, state)

    async def failed_launch(_script):
        # Deterministic interleaving at the actual launch await; never launch a
        # subprocess or send a real Telegram message in this regression test.
        env.release["version"] = "0.3.0"
        await service._check_github()
        raise RuntimeError("isolated simulated launch failure")

    monkeypatch.setattr(service, "_launch_updater", failed_launch)
    result = await service.start_update(confirm=True, force=False, allow_dirty=False, running={})
    assert result["ok"] is False and result["error"].startswith("launch_failed:")
    state = read_json(service.state_path)
    assert state["phase"] == "idle"
    assert "isolated simulated launch failure" in state["lastError"]
    assert state["available"]["version"] == "0.3.0"
    assert state["notifiedVersions"] == ["0.2.0", "0.3.0"]
    for _ in range(3):
        await env.service()._check_github()
    assert len(env.sent) == 2


async def test_updater_stale_state_write_cannot_erase_notice_claim_after_restart(notifications, tmp_path, monkeypatch):
    env = notifications
    service = env.service()
    await service._check_github()
    stale_state = read_json(service.state_path)
    env.release["version"] = "0.3.0"
    await service._check_github()

    # The standalone updater can finish writing a snapshot read before the
    # latest notification. Exercise its real phase writer, confined to tmp data.
    updater_module = service_module.load_updater()
    updater = updater_module.Updater({"installRoot": str(tmp_path), "dataDir": str(service.data_dir)})
    original_read = updater_module.read_json
    monkeypatch.setattr(updater_module, "read_json", lambda path: stale_state if path == service.state_path else original_read(path))
    updater.write_state("downloading")
    assert read_json(service.state_path)["available"]["version"] == "0.2.0"
    for _ in range(3):
        await env.service()._check_github()
    assert len(env.sent) == 2
    assert read_json(service.state_path)["phase"] == "downloading"
    assert read_json(service.state_path)["notifiedVersions"] == ["0.2.0", "0.3.0"]
