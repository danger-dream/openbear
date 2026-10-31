"""pytest plugin for scripts/check.py: a version bump alone does not make testmon rerun tests.

Every test imports app/__init__.py, whose __version__ line each frontend build raises;
testmon would otherwise count that as a change to code every test ran.
"""
from __future__ import annotations

import re

from testmon import process_code

_VERSION = re.compile(r"""^(__version__\s*=\s*)(["'])[^"'\n]*\2""", re.M)
_read = process_code.bytes_to_string_and_fsha


def _without_version(byte_stream: bytes):
    text, fsha = _read(byte_stream)
    return _VERSION.sub(r'\1"0"', text), fsha


process_code.bytes_to_string_and_fsha = _without_version
