"""OpenBear — 单人自用 Agent Web 控制台。"""

from __future__ import annotations

import re
from pathlib import Path

__version__ = "0.1.2"

_VERSION_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.M)


def installed_version() -> str:
    """读磁盘上的版本号。

    只换前端时进程不重启，模块里的 ``__version__`` 会停留在启动时的值。
    /health 和版本 API 必须看磁盘，更新器才能确认目标版本已经生效。
    """
    try:
        text = Path(__file__).read_text(encoding="utf-8")
    except OSError:
        return __version__
    match = _VERSION_RE.search(text)
    return match.group(1) if match else __version__
