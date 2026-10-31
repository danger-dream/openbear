"""A faster clock for one module under test: its sleeps take a tenth of the time and its
time.monotonic moves on by the rest, so polling loops and deadlines keep their order."""
from __future__ import annotations

import asyncio
import time


class _View:
    """The real module with a few names replaced."""

    def __init__(self, module, **replaced):
        self._module, self._replaced = module, replaced

    def __getattr__(self, name):
        if name in self._replaced:
            return self._replaced[name]
        return getattr(self._module, name)


class FastClock:
    def __init__(self, scale=0.1):
        self.scale, self.skipped = scale, 0.0

    def monotonic(self):
        return time.monotonic() + self.skipped

    async def sleep(self, delay, result=None):
        delay = max(0.0, delay)
        await asyncio.sleep(delay * self.scale)
        self.skipped += delay * (1 - self.scale)
        return result

    def install(self, monkeypatch, module):
        """Only module's own `time` and `asyncio` names change; other modules keep the real clock."""
        monkeypatch.setattr(module, 'time', _View(time, monotonic=self.monotonic))
        monkeypatch.setattr(module, 'asyncio', _View(asyncio, sleep=self.sleep))
        return self
