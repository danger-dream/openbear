"""运行期配置读写。"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Config, config_path

_BACKUP_KEEP = 3
_CONFIG_FILE_MODE = 0o600
_MISSING = object()


class ConfigConflictError(RuntimeError):
    """配置在事务写入后又发生了变化，不能安全回滚。"""


@dataclass(frozen=True, slots=True)
class ConfigMutationSnapshot:
    config: Config
    before: dict[str, Any]
    after: dict[str, Any]
    revision: int


def _chmod_private(path: Path | str) -> None:
    """配置及其备份含明文密钥，落盘后必须仅当前用户可读写。"""
    with contextlib.suppress(OSError):
        os.chmod(path, _CONFIG_FILE_MODE)


class ConfigStore:
    """openbear.json 的运行期安全读写器。

    - 以原始 JSON 为持久化基础，尽量保留未知字段和原有结构。
    - 写入前用 pydantic Config 完整校验。
    - tmp + os.replace 原子写入，覆盖前保留最近 3 份备份。
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else config_path()
        self._lock = asyncio.Lock()
        self._revision = 0

    @property
    def revision(self) -> int:
        return self._revision

    async def load_raw(self) -> dict[str, Any]:
        async with self._lock:
            return self._load_raw_unlocked()

    async def load_config(self) -> Config:
        async with self._lock:
            raw = self._load_raw_unlocked()
            changed = _migrate_runtime_config(raw)
            cfg = _validate_config(raw)
            if changed:
                self._write_atomic(raw)
                self._revision += 1
            return cfg

    async def update_path(self, dotted_path: str, value: Any) -> Config:
        """更新一个点分路径，校验通过后写盘并返回新 Config。"""
        async with self._lock:
            raw = self._load_raw_unlocked()
            old = _get_path(raw, dotted_path)
            _set_path(raw, dotted_path, value)
            try:
                cfg = _validate_config(raw)
            except Exception:
                if old is _MISSING:
                    _del_path(raw, dotted_path)
                else:
                    _set_path(raw, dotted_path, old)
                raise
            self._write_atomic(raw)
            self._revision += 1
            return cfg

    async def mutate(self, mutator) -> Config:
        """以函数方式修改原始配置；mutator 可原地改 raw。"""
        snapshot = await self.mutate_with_snapshot(mutator)
        return snapshot.config

    async def mutate_with_snapshot(self, mutator) -> ConfigMutationSnapshot:
        """原子写入一次配置，并保留可用于条件回滚的前后快照。"""
        async with self._lock:
            before = self._load_raw_unlocked()
            after = copy.deepcopy(before)
            mutator(after)
            cfg = _validate_config(after)
            self._write_atomic(after)
            self._revision += 1
            return ConfigMutationSnapshot(
                config=cfg,
                before=copy.deepcopy(before),
                after=copy.deepcopy(after),
                revision=self._revision,
            )

    async def restore_snapshot(self, snapshot: ConfigMutationSnapshot) -> Config:
        """仅在配置未被进程内或外部再次修改时恢复事务快照。"""
        async with self._lock:
            current = self._load_raw_unlocked()
            if self._revision != snapshot.revision or current != snapshot.after:
                raise ConfigConflictError("config_changed_since_mutation")
            cfg = _validate_config(snapshot.before)
            self._write_atomic(snapshot.before)
            self._revision += 1
            return cfg

    def _load_raw_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            raise FileNotFoundError(f"配置文件不存在: {self.path}")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("配置文件根节点必须是对象")
        return data

    def _rotate_backups(self) -> None:
        if not self.path.exists():
            return
        base = str(self.path)
        for i in range(_BACKUP_KEEP, 1, -1):
            src = f"{base}.bak.{i - 1}"
            dst = f"{base}.bak.{i}"
            if os.path.exists(src):
                with contextlib.suppress(OSError):
                    os.replace(src, dst)
        backup = f"{base}.bak.1"
        with contextlib.suppress(OSError):
            shutil.copy2(self.path, backup)
            _chmod_private(backup)

    def _write_atomic(self, raw: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            _chmod_private(tmp)
            self._rotate_backups()
            os.replace(tmp, self.path)
            _chmod_private(self.path)
        except Exception:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise


def _migrate_runtime_config(raw: dict[str, Any]) -> bool:
    """Canonicalize retired keys without materializing evolvable prompt defaults."""
    rath = raw.get("rath")
    if not isinstance(rath, dict):
        return False
    changed = False
    for legacy, canonical in {
        "agentPlanMaxRevisionRounds": "planMaxRevisionRounds",
        "agentPlanMaxSteps": "planMaxSteps",
        "agentPlanMaxCriteriaPerStep": "planMaxCriteriaPerStep",
        "agentPlanMaxFinalOutputs": "planMaxFinalOutputs",
    }.items():
        if legacy not in rath:
            continue
        if canonical not in rath:
            rath[canonical] = rath[legacy]
        del rath[legacy]
        changed = True
    return changed


def _validate_config(raw: dict[str, Any]) -> Config:
    cfg = Config.model_validate(raw)
    errors = cfg.validate_for_startup()
    if errors:
        raise ValueError("; ".join(errors))
    return cfg


def _parts(path: str) -> list[str]:
    parts = [p for p in (path or "").split(".") if p]
    if not parts:
        raise ValueError("配置路径不能为空")
    return parts


def _get_path(raw: dict[str, Any], path: str) -> Any:
    cur: Any = raw
    for part in _parts(path):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return copy.deepcopy(cur)


def _set_path(raw: dict[str, Any], path: str, value: Any) -> None:
    cur: Any = raw
    parts = _parts(path)
    for part in parts[:-1]:
        if not isinstance(cur, dict):
            raise ValueError(f"路径 {path!r} 的父级不是对象")
        cur = cur.setdefault(part, {})
    if not isinstance(cur, dict):
        raise ValueError(f"路径 {path!r} 的父级不是对象")
    cur[parts[-1]] = value


def _del_path(raw: dict[str, Any], path: str) -> None:
    cur: Any = raw
    parts = _parts(path)
    for part in parts[:-1]:
        if not isinstance(cur, dict) or part not in cur:
            return
        cur = cur[part]
    if isinstance(cur, dict):
        cur.pop(parts[-1], None)
