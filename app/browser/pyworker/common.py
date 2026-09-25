from __future__ import annotations

import asyncio
import contextvars
import time
from dataclasses import dataclass
from pathlib import Path

RESOURCES = Path(__file__).with_name("resources")
MAX_FRAME = 20 * 1024 * 1024


class BrowserError(Exception):
    def __init__(self, code: str, outcome: str | None = None, *, details=None):
        self.code = code
        op = operation.get()
        # A read can fail after doing work without ever sending a mutation.
        # not_started is reserved for requests rejected before execution.
        self.outcome = outcome or (
            "unknown" if op and op.started else "failed" if op else "not_started"
        )
        self.details = {"phase": op.phase, "effectsPossible": op.started} if op else {}
        self.details.update(details or {})
        super().__init__(code)


class DialogOpened(Exception):
    """The browser has acknowledged a modal, so return control for explicit handling."""


class ProtocolError(Exception):
    """Internal protocol detail; never send raw endpoint/expression data over IPC."""


@dataclass
class Operation:
    deadline: float
    started: bool = False
    phase: str = "initializing_page"

    @classmethod
    def create(cls, timeout_ms):
        return cls(time.monotonic() + timeout_ms / 1000)

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BrowserError("action_timeout")
        return remaining


operation: contextvars.ContextVar[Operation | None] = contextvars.ContextVar(
    "browser_operation", default=None
)


def set_phase(phase):
    if op := operation.get():
        op.phase = phase


def mark_effect():
    if op := operation.get():
        op.started = True


async def retry_pause():
    op = operation.get()
    await asyncio.sleep(min(0.05, op.remaining() if op else 0.05))


class Ring:
    def __init__(self, limit):
        self.limit, self.seq, self.rows = limit, 0, []

    def add(self, data):
        self.seq += 1
        row = {"id": self.seq, **data}
        self.rows.append(row)
        self.trim(self.limit)
        return row

    def trim(self, limit):
        self.limit = limit
        self.rows[:] = self.rows[-limit:]

    def read(self, after=0):
        return {
            "entries": [r.copy() for r in self.rows if r["id"] > after],
            "cursor": self.seq,
            "dropped": bool(self.rows and after < self.rows[0]["id"] - 1),
        }
