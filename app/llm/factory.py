"""Backend 工厂 —— 按模型全名解析 provider，构造对应协议 backend。"""
from __future__ import annotations

from app.config import ModelsConfig
from app.llm.anthropic import AnthropicBackend
from app.llm.base import LLMBackend, OpenBearLLMError
from app.llm.client import HTTPClient
from app.llm.openai_chat import OpenAIChatBackend
from app.llm.openai_responses import OpenAIResponsesBackend
from app.logging import get_logger

log = get_logger("llm.factory")

_BACKEND_CLS = {
    "anthropic": AnthropicBackend,
    "chat": OpenAIChatBackend,
    "responses": OpenAIResponsesBackend,
}


class BackendFactory:
    """缓存 (base_url, api_key, protocol) → backend 实例。"""

    def __init__(self, models: ModelsConfig, client: HTTPClient) -> None:
        self._models = models
        self._client = client
        self._cache: dict[tuple[str, str, str], LLMBackend] = {}

    def backend_for(self, fullname: str) -> tuple[LLMBackend, str, int]:
        """模型全名 → (backend, model_id, max_tokens)。"""
        r = self._models.resolve(fullname)
        if r is None:
            raise OpenBearLLMError(f"模型不存在: {fullname}")
        prov, model = r
        key = (prov.base_url, prov.api_key, prov.protocol)
        backend = self._cache.get(key)
        if backend is None:
            cls = _BACKEND_CLS.get(prov.protocol)
            if cls is None:
                raise OpenBearLLMError(f"未知协议: {prov.protocol}")
            backend = cls(self._client, prov.base_url, prov.api_key)
            self._cache[key] = backend
            log.info("构造 backend", 协议=prov.protocol, 地址=prov.base_url)
        return backend, model.id, model.max_tokens

    def context_window(self, fullname: str) -> int:
        r = self._models.resolve(fullname)
        return r[1].context_window if r else 128000
