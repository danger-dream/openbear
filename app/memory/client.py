"""prompt-memory HTTP 客户端。

认证：Header X-Memory-Identity: <identity> + Authorization: Bearer <accessKey>
- build():     POST /system-prompt/build  → 组装好的系统提示词
- tool():      POST /tool/{entry|doc|secret|identities} → 记忆读写
"""
from __future__ import annotations

from typing import Any

import httpx

from app.logging import get_logger

log = get_logger("memory.client")


class MemoryClient:
    def __init__(self, base_url: str, identity: str, access_key: str,
                 *, timeout_s: float = 8.0) -> None:
        self._base = base_url.rstrip("/")
        self._identity = identity
        self._key = access_key
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=5.0))

    async def close(self) -> None:
        await self._http.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            "X-Memory-Identity": self._identity,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }

    async def build_system_prompt(self, params: dict[str, Any] | None = None) -> str:
        """拿组装好的系统提示词。失败抛异常（由调用方降级）。"""
        resp = await self._http.post(
            f"{self._base}/system-prompt/build",
            headers=self._headers(), json=params or {})
        resp.raise_for_status()
        data = resp.json()
        return data.get("prompt", "")

    async def tool_call(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        """调用 /tool/{endpoint}。endpoint ∈ entry|doc|secret|identities。"""
        body = dict(payload)
        body.setdefault("identity", self._identity)
        resp = await self._http.post(
            f"{self._base}/tool/{endpoint}",
            headers=self._headers(), json=body)
        if resp.status_code >= 400:
            return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:300]}"}
        return resp.json()
