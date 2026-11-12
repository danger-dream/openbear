"""pytest plugin for scripts/check.py: which test each process is in, and what it waits for.

Every test process keeps a file named after its pid in $CHECK_PROGRESS_DIR holding the test and
phase it is in, empty between tests. On SIGUSR1 it writes its asyncio tasks' stacks next to it
(<pid>.stack; the test's own stderr is being captured), so check.py can show where a test
stopped making progress before ending it.
"""
from __future__ import annotations

import asyncio
import faulthandler
import os
import signal
from pathlib import Path

import pytest

_DIR = os.environ.get("CHECK_PROGRESS_DIR")


def _note(text: str) -> None:
    if _DIR:
        Path(_DIR, str(os.getpid())).write_text(text)


def _awaiting(coro):
    """The frames down a coroutine's await chain (Task.print_stack shows only the outermost)."""
    while coro is not None:
        frame = getattr(coro, "cr_frame", None) or getattr(coro, "ag_frame", None) or getattr(coro, "gi_frame", None)
        if frame is not None:
            yield frame
        coro = getattr(coro, "cr_await", None) or getattr(coro, "ag_await", None) or getattr(coro, "gi_yieldfrom", None)


def _dump(_signum, _frame) -> None:
    where = Path(_DIR, str(os.getpid())).read_text().replace("\t", " ")
    with open(Path(_DIR, f"{os.getpid()}.stack"), "w") as out:
        print(f"\n=== 卡住的测试进程 {os.getpid()}：{where}", file=out)
        try:
            tasks = asyncio.all_tasks(asyncio.get_running_loop())
        except RuntimeError:
            tasks = set()
        for task in tasks:
            print(f"--- asyncio 任务 {task.get_name()}", file=out)
            for frame in _awaiting(task.get_coro()):
                print(f"  {frame.f_code.co_filename}:{frame.f_lineno} {frame.f_code.co_name}", file=out)
        out.flush()
        faulthandler.dump_traceback(out)
        print("=== 以上", file=out)


def pytest_configure(config):
    if _DIR:
        signal.signal(signal.SIGUSR1, _dump)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    _note(f"{item.nodeid}\tsetup")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    _note(f"{item.nodeid}\tcall")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    _note(f"{item.nodeid}\tteardown")


def pytest_runtest_logfinish(nodeid, location):
    _note("")
