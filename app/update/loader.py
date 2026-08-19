from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

_CACHED: ModuleType | None = None


def updater_script_path() -> Path:
    return Path(__file__).resolve().parents[2] / "scripts" / "updater.py"


def load_updater() -> ModuleType:
    global _CACHED
    if _CACHED is not None:
        return _CACHED
    path = updater_script_path()
    spec = importlib.util.spec_from_file_location("openbear_standalone_updater", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载更新器 {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _CACHED = module
    return module
