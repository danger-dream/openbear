"""Read-only browser-root connection validation; never attaches to a page."""

from __future__ import annotations

import asyncio
import contextlib
import time

from app.browser.cdp import CDP
from app.browser.config import BrowserConfig


async def probe_connection(config: BrowserConfig) -> dict:
    started = time.monotonic()
    if not config.main_endpoint:
        return {"ok": False, "code": "browser_endpoint_required", "error": "请先设置浏览器连接地址"}
    client = CDP(config.main_endpoint, config.connect_timeout_s)
    try:
        # One budget for discovery, WebSocket handshake and protocol replies.
        async with asyncio.timeout(config.connect_timeout_s):
            await client.connect()
            version = await client.call("Browser.getVersion")
            targets = await client.call("Target.getTargets")
            if not isinstance(version.get("product"), str) or not isinstance(targets.get("targetInfos"), list):
                raise ValueError("invalid_cdp_reply")
        return {
            "ok": True,
            "product": version["product"][:120],
            "elapsedMs": round((time.monotonic() - started) * 1000),
        }
    except TimeoutError:
        return {"ok": False, "code": "browser_connection_timeout", "error": "浏览器连接验证超时，请检查地址、网络及服务状态"}
    except Exception:
        # Endpoint query strings can contain credentials. Do not echo URLs or
        # remote exception text into responses, audit events or ordinary logs.
        return {"ok": False, "code": "browser_connection_failed", "error": "无法完成浏览器 CDP 连接验证，请检查连接地址及浏览器服务"}
    finally:
        with contextlib.suppress(Exception):
            await client.close()


async def validate_config_change(before: dict, config: BrowserConfig) -> None:
    """Reject an invalid enable/address change before the config is persisted."""
    previous = BrowserConfig.model_validate(before or {})
    if not config.enabled or (
        previous.enabled and previous.main_endpoint == config.main_endpoint
    ):
        return
    result = await probe_connection(config)
    if not result["ok"]:
        raise ValueError(result["error"])
    # Only this in-memory config carries the successful handshake to the runtime.
    # It is not a user setting and is never serialized into openbear.json.
    config._connection_verified = True
