from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

from app.agent.runs import RunRegistry
from app.config import MediaConfig
from app.media.attachments import build_llm_content, extract_text
from app.settings.specs import SPECS
from tests.test_web_admin import _login_cookie
from tests.test_web_admin import web_env as _shared_web_env

shared_web_env = _shared_web_env


@pytest.fixture
async def web_env(shared_web_env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    shared_web_env.server.config.media.download_dir = "inbound"
    yield shared_web_env


async def setup_upload(web_env, *, name="file.bin", size=4, mime="application/octet-stream", row=None):
    cookie = await _login_cookie(web_env)
    row = row or await web_env.server._create_web_conversation(123, title="HTTP uploads")
    base = f"/api/conversations/{row['conversation_uuid']}/uploads"
    response = await web_env.client.post(base, json={"name": name, "size": size, "type": mime}, cookies={"openbear_web_session": cookie})
    assert response.status == 200, await response.text()
    upload_id = (await response.json())["uploadId"]
    return row, cookie, upload_id, f"{base}/{upload_id}"


@pytest.mark.parametrize("name,mime,size,kind", [
    ("large.bin", "application/octet-stream", 70 * 1024 * 1024, "file"),
    ("large.png", "image/png", 21 * 1024 * 1024, "image"),
    ("beyond-ws.bin", "application/octet-stream", 129 * 1024 * 1024, "file"),
])
async def test_large_http_upload_then_ws_reference_and_download(web_env, monkeypatch, name, mime, size, kind):
    # Even explicit old low configuration values must no longer reject files.
    web_env.server.config.media.max_file_mb = 1
    web_env.server.config.media.max_image_mb = 1
    row, cookie, upload_id, url = await setup_upload(web_env, name=name, mime=mime, size=size)
    block = b"upload-test-01234" * (512 * 1024 // 16)
    digest = hashlib.sha256()
    for offset in range(0, size, len(block)):
        chunk = block[:min(len(block), size - offset)]
        response = await web_env.client.put(url, params={"offset": offset}, data=chunk)
        assert response.status == 200, await response.text()
        assert (await response.json())["offset"] == offset + len(chunk)
        digest.update(chunk)
    complete = await web_env.client.post(url + "/complete")
    assert complete.status == 200, await complete.text()
    assert (await complete.json())["uploadId"] == upload_id
    directory = web_env.server._upload_directory(row, upload_id)
    metadata, path = web_env.server._read_upload(directory)
    assert metadata["complete"] is True
    assert path.stat().st_size == size

    async def no_copy(*args, **kwargs):
        pytest.fail("sending a completed upload must not re-copy/hash the file")

    monkeypatch.setattr(web_env.server, "_register_web_artifact_from_path", no_copy)
    web_env.server.runs = RunRegistry()
    captured = []
    finished = asyncio.Event()

    async def run(chat_id, text, renderer, media=None, **kwargs):
        captured.extend(media)
        await renderer.finalize("received")
        await renderer.close()
        finished.set()

    monkeypatch.setattr(web_env.server, "_run_web_turn", run)
    async with web_env.client.ws_connect(
        f"/api/conversations/{row['conversation_uuid']}/ws?bootstrap=incremental",
        headers={"Cookie": f"openbear_web_session={cookie}"},
    ) as ws:
        payload = {"type": "send", "requestId": "large-upload", "text": "read attachment", "files": [{"uploadId": upload_id}]}
        assert len(json.dumps(payload)) < 256
        await ws.send_json(payload)
        async with asyncio.timeout(10):
            while True:
                message = await ws.receive_json()
                if message.get("requestId") == "large-upload":
                    assert message["type"] == "ack", message
                    break
            await finished.wait()
    assert len(captured) == 1
    assert captured[0].kind == kind
    assert not captured[0].skipped
    assert captured[0].size == size
    if kind == "image":
        blocks = build_llm_content("read image", captured)
        assert any(item.get("type") == "image" and item.get("path") == str(path) for item in blocks)
    operations = await web_env.server._web_operations(row["conversation_uuid"])
    user = next(op for op in operations if op["opType"] == "user_message")
    attachment = user["payload"]["attachments"][0]
    assert attachment["sizeBytes"] == size
    assert attachment["artifactUuid"] == metadata["artifactUuid"]
    response = await web_env.client.get(attachment["downloadUrl"])
    assert response.status == 200
    downloaded = hashlib.sha256()
    async for chunk in response.content.iter_chunked(1024 * 1024):
        downloaded.update(chunk)
    assert downloaded.hexdigest() == digest.hexdigest()
    assert (await web_env.client.delete(url)).status == 409
    assert path.exists(), "completed message attachments must not be deleted by upload cleanup"


async def test_upload_requires_auth_origin_and_conversation_ownership(web_env):
    row = await web_env.server._create_web_conversation(123, title="owner")
    base = f"/api/conversations/{row['conversation_uuid']}/uploads"
    assert (await web_env.client.post(base, json={"name": "file", "size": 0})).status == 401
    await _login_cookie(web_env)
    response = await web_env.client.post(base, json={"name": "file", "size": 0}, headers={"Origin": "https://evil.example"})
    assert response.status == 403
    other = await web_env.server._create_web_conversation(456, title="other owner")
    assert (await web_env.client.post(f"/api/conversations/{other['conversation_uuid']}/uploads", json={"name": "file", "size": 0})).status == 404


async def test_upload_offsets_completion_and_cross_conversation_references(web_env):
    row, _cookie, upload_id, url = await setup_upload(web_env)
    assert (await web_env.client.post(url + "/complete")).status == 409
    assert (await web_env.client.put(url, params={"offset": -1}, data=b"x")).status == 409
    assert (await web_env.client.put(url, params={"offset": "bad"}, data=b"x")).status == 400
    assert (await web_env.client.put(url, params={"offset": 0}, data=b"12345")).status == 409
    assert (await web_env.client.put(url, params={"offset": 0}, data=b"1234")).status == 200
    assert (await web_env.client.put(url, params={"offset": 0}, data=b"1234")).status == 409
    assert (await web_env.client.post(url + "/complete")).status == 200
    assert (await web_env.client.post(url + "/complete")).status == 200
    assert (await web_env.client.put(url, params={"offset": 4}, data=b"")).status == 409
    other = await web_env.server._create_web_conversation(123, title="same owner other conversation")
    assert (await web_env.client.post(f"/api/conversations/{other['conversation_uuid']}/uploads/{upload_id}/complete")).status == 404
    with pytest.raises(web.HTTPNotFound):
        await web_env.server._resolve_http_uploads([{"uploadId": upload_id}], conversation=other)
    with pytest.raises(web.HTTPNotFound):
        await web_env.server._resolve_http_uploads([{"uploadId": "../../etc/passwd"}], conversation=row)
    with pytest.raises(web.HTTPBadRequest):
        await web_env.server._resolve_http_uploads([{"name": "file", "data": "data:application/octet-stream;base64,YQ=="}], conversation=row)


async def test_incomplete_uploads_are_discardable_and_chunks_are_serialized(web_env):
    row, _cookie, upload_id, url = await setup_upload(web_env, size=8)
    responses = await asyncio.gather(*[
        web_env.client.put(url, params={"offset": 0}, data=b"1234") for _ in range(2)
    ])
    assert sorted(response.status for response in responses) == [200, 409]
    with pytest.raises(web.HTTPConflict):
        await web_env.server._resolve_http_uploads([{"uploadId": upload_id}], conversation=row)
    assert (await web_env.client.delete(url)).status == 200
    assert not web_env.server._upload_directory(row, upload_id).exists()
    assert (await web_env.client.delete(url)).status == 200


async def test_interrupted_chunk_rolls_back_without_losing_previous_chunks(web_env, monkeypatch):
    row, _cookie, upload_id, url = await setup_upload(web_env, size=100)
    await web_env.client.put(url, params={"offset": 0}, data=b"keep")

    async def interrupted(_size):
        yield b"partial"
        raise ConnectionResetError("client disconnected")

    async def conversation(_request):
        return row

    monkeypatch.setattr(web_env.server, "_conversation_from_request", conversation)
    request = SimpleNamespace(match_info={"upload_uuid": upload_id}, query={"offset": "4"}, content=SimpleNamespace(iter_chunked=interrupted))
    with pytest.raises(web.HTTPInsufficientStorage):
        await web_env.server.handle_api_upload_chunk(request)
    _metadata, path = web_env.server._read_upload(web_env.server._upload_directory(row, upload_id))
    assert path.read_bytes() == b"keep"


async def test_ws_rejects_legacy_inline_attachments_without_closing_socket(web_env):
    row, cookie, _upload_id, _url = await setup_upload(web_env, size=0)
    async with web_env.client.ws_connect(
        f"/api/conversations/{row['conversation_uuid']}/ws?bootstrap=incremental",
        headers={"Cookie": f"openbear_web_session={cookie}"},
    ) as ws:
        await ws.send_json({"type": "send", "requestId": "legacy", "files": [{"name": "file", "data": "YQ=="}]})
        async with asyncio.timeout(5):
            while True:
                message = await ws.receive_json()
                if message.get("requestId") == "legacy":
                    assert message == {"type": "error", "requestId": "legacy", "error": "attachment_upload_required"}
                    break
            await ws.send_json({"type": "ping"})
            assert (await ws.receive_json())["type"] == "pong"


async def test_ws_message_limit_is_128_mib(web_env, monkeypatch):
    from app.web_console import chat_api

    row = await web_env.server._create_web_conversation(123, title="limit")
    cookie = await _login_cookie(web_env)
    original = web.WebSocketResponse
    limits = []

    def websocket(*args, **kwargs):
        limits.append(kwargs["max_msg_size"])
        return original(*args, **kwargs)

    monkeypatch.setattr(chat_api.web, "WebSocketResponse", websocket)
    async with web_env.client.ws_connect(f"/api/conversations/{row['conversation_uuid']}/ws", headers={"Cookie": f"openbear_web_session={cookie}"}) as ws:
        await ws.receive_json()
    assert limits == [128 * 1024 * 1024]


async def test_node_binary_upload_to_backend_and_ws(web_env, monkeypatch):
    project = Path(__file__).resolve().parents[1]
    if not shutil.which("node"):
        pytest.skip("Node is required for the frontend HTTP integration")
    row = await web_env.server._create_web_conversation(123, title="Node HTTP upload")
    cookie = await _login_cookie(web_env)
    web_env.server.runs = RunRegistry()
    captured = []
    received = asyncio.Event()

    async def run(chat_id, text, renderer, media=None, **kwargs):
        captured.extend(media)
        received.set()
        await renderer.finalize("received")
        await renderer.close()

    monkeypatch.setattr(web_env.server, "_run_web_turn", run)
    process = await asyncio.create_subprocess_exec(
        "node", str(project / "web/test-support/httpUploadSmoke.mjs"),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    args = json.dumps({"origin": str(web_env.client.make_url("/")).rstrip("/"), "cookie": cookie, "conversationUuid": row["conversation_uuid"]}).encode()
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(args), 90)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    assert process.returncode == 0, stderr.decode()
    result = json.loads(stdout)
    assert result["ok"] is True
    assert result["bytes"] > 70 * 1024 * 1024
    assert result["chunks"] == 141
    frame = {"type": "send", "requestId": "node-upload", "text": "Read uploaded attachments", "files": result["refs"]}
    assert len(json.dumps(frame)) < 400
    assert all(set(item) == {"uploadId"} for item in frame["files"])
    async with web_env.client.ws_connect(
        f"/api/conversations/{row['conversation_uuid']}/ws?bootstrap=incremental",
        headers={"Cookie": f"openbear_web_session={cookie}"},
    ) as ws:
        await ws.send_json(frame)
        async with asyncio.timeout(10):
            while True:
                message = await ws.receive_json()
                if message.get("requestId") == "node-upload":
                    assert message["type"] == "ack", message
                    break
            await received.wait()
    assert [item.kind for item in captured] == ["file", "image", "file"]
    assert captured[0].size == 70 * 1024 * 1024
    assert captured[2].size == 0


async def test_resending_after_artifact_deletion_still_provides_a_live_url(web_env):
    row, _cookie, upload_id, url = await setup_upload(web_env, size=0)
    assert (await web_env.client.post(url + "/complete")).status == 200
    media = await web_env.server._resolve_http_uploads([{"uploadId": upload_id}], conversation=row)
    original_id = media[0].artifact_uuid
    await web_env.db.conn.execute("UPDATE web_artifacts SET deleted_at=1 WHERE artifact_uuid=?", (original_id,))
    await web_env.db.conn.commit()
    public = await web_env.server._web_media_attachments_public(row, media, turn_uuid="resend")
    assert public[0]["artifactUuid"] != original_id
    assert (await web_env.client.get(public[0]["downloadUrl"])).status == 200


def test_legacy_size_settings_are_ignored_and_hidden():
    old = {"maxImageMb": 1, "maxAudioMb": 1, "maxVideoMb": 1, "maxFileMb": 1}
    config = MediaConfig.model_validate(old)
    assert not set(old) & set(config.model_dump(by_alias=True))
    assert all(f"media.{name}" not in SPECS for name in old)


def test_text_excerpt_reads_only_bounded_prefix(tmp_path, monkeypatch):
    path = tmp_path / "large.txt"
    with path.open("wb") as file:
        file.write(b"hello")
        file.truncate(5 * 1024 ** 3)

    def no_full_read(*_args):
        pytest.fail("text excerpt must not read the whole large file")

    monkeypatch.setattr(Path, "read_bytes", no_full_read)
    text, truncated = extract_text(path, max_chars=1)
    assert truncated
    assert text.startswith("h")
