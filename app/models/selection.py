"""当前选中模型 —— 运行时切换 + 持久化回写 openbear.json。"""
from __future__ import annotations

import json
from pathlib import Path

from app.config import ModelsConfig
from app.logging import get_logger

log = get_logger("models.selection")

# 协议家族：parrot 只支持「openai 家族内」chat ↔ responses 互转；
# anthropic 自成一家，不能与 openai 家族跨家族切换（parrot 不做这种转换）。
_PROTOCOL_FAMILY = {
    "chat": "openai",
    "responses": "openai",
    "anthropic": "anthropic",
}


def protocol_family(protocol: str | None) -> str | None:
    if not protocol:
        return None
    return _PROTOCOL_FAMILY.get(protocol, protocol)


class ModelSelection:
    """维护当前 primary（运行时可切），切换时回写 openbear.json 的 models.primary。"""

    def __init__(self, models: ModelsConfig, config_path: Path) -> None:
        self._models = models
        self._path = config_path
        self._current = models.primary

    @property
    def current(self) -> str:
        return self._current

    def protocol_of(self, fullname: str) -> str | None:
        resolved = self._models.resolve(fullname)
        return resolved[0].protocol if resolved else None

    def family_of(self, fullname: str) -> str | None:
        return protocol_family(self.protocol_of(fullname))

    def same_family_as_current(self, fullname: str) -> bool:
        """目标模型与当前模型是否同一协议家族（openai 家族内 chat/responses 互通）。"""
        cur = self.family_of(self._current)
        target = self.family_of(fullname)
        return bool(cur and target and cur == target)

    def set(self, fullname: str) -> bool:
        if self._models.resolve(fullname) is None:
            return False
        self._current = fullname
        self._persist(fullname)
        log.info("切换模型", 新模型=fullname)
        return True

    def _persist(self, fullname: str) -> None:
        """回写 openbear.json 的 models.primary（失败仅告警，不影响内存态）。"""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            raw.setdefault("models", {})["primary"] = fullname
            self._path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            log.warning("回写 primary 失败", 错误=str(e)[:120])
