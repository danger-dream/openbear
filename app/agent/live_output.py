"""Live view of the model call now streaming: its speed and running usage.

Providers report output tokens only when a call ends, so while it streams the
output is estimated from the text and tool-argument bytes received; reasoning a
provider keeps encrypted or hidden is not seen and not counted. A prompt the
provider reports at the start (Anthropic) is used as reported; otherwise (OpenAI
Chat/Responses) the request's context estimate stands in, split by the cache
share of the latest settled request.
"""
from __future__ import annotations

import re
import time
from collections import deque
from typing import Any

from app.llm.events import StreamEvent, Usage

_CJK = re.compile("[\u3000-\u303f\u3400-\u9fff\uf900-\ufaff\uff00-\uffef]")
# Speed is measured over this window; after this much silence nothing streams.
_WINDOW_S = 3.0
_IDLE_S = 1.5
# Tool arguments are JSON, mostly ASCII with some UTF-8 CJK.
_BYTES_PER_TOKEN = 3.5


def _text_tokens(text: str) -> float:
    cjk = len(_CJK.findall(text))
    return cjk + (len(text) - cjk) / 4


class LiveOutput:
    def __init__(self) -> None:
        self._samples: deque[tuple[float, float]] = deque()
        self._burst_started = 0.0
        self._in_call = False
        self._prompt: Usage | None = None
        self._estimated_prompt = 0
        self._cache_share: float | None = None
        self._output = 0.0
        self._tool_bytes = 0

    def note_prompt(self, usage: Usage) -> None:
        """Remember how much of a settled request's prompt was read from cache."""
        prompt = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
        if prompt > 0:
            self._cache_share = usage.cache_read_tokens / prompt

    def start_call(self, estimated_prompt_tokens: int = 0) -> None:
        self._in_call = True
        self._prompt = None
        self._estimated_prompt = max(0, int(estimated_prompt_tokens or 0))
        self._output = 0.0
        self._tool_bytes = 0

    def end_call(self, usage: Usage | None = None) -> None:
        """The call's reported usage now counts; its estimate no longer does."""
        if usage is not None:
            self.note_prompt(usage)
        self._in_call = False
        self._prompt = None
        self._estimated_prompt = 0
        self._output = 0.0
        self._tool_bytes = 0

    def observe(self, event: StreamEvent, *, now: float | None = None) -> None:
        if event.kind == "usage_preview" and event.usage is not None:
            usage = event.usage
            if usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens > 0:
                self._prompt = usage
            return
        tokens = 0.0
        if event.kind in {"content", "reasoning"} and event.text:
            tokens = _text_tokens(event.text)
        elif event.kind == "tool_input":
            received = int(event.details.get("receivedBytes") or 0)
            # Each call's tracker counts from zero again.
            delta = received - self._tool_bytes if received >= self._tool_bytes else received
            self._tool_bytes = received
            tokens = delta / _BYTES_PER_TOKEN
        if tokens <= 0:
            return
        now = time.monotonic() if now is None else now
        if not self._samples or now - self._samples[-1][0] > _IDLE_S:
            self._burst_started = now
        self._samples.append((now, tokens))
        self._output += tokens
        self._trim(now)

    def tokens_per_second(self, *, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        self._trim(now)
        if not self._samples or now - self._samples[-1][0] > _IDLE_S:
            return 0.0
        span = max(0.5, now - max(self._burst_started, now - _WINDOW_S))
        return sum(tokens for _, tokens in self._samples) / span

    def in_flight_usage(self) -> dict[str, Any] | None:
        """Usage of the unfinished call: its prompt (reported or estimated) and estimated output."""
        if not self._in_call:
            return None
        if self._prompt is not None:
            prompt, estimated = self._prompt, False
        else:
            cache_read = round(self._estimated_prompt * (self._cache_share or 0.0))
            prompt = Usage(input_tokens=self._estimated_prompt - cache_read, cache_read_tokens=cache_read)
            estimated = True
        return {
            "inputTokens": prompt.input_tokens,
            "cacheReadTokens": prompt.cache_read_tokens,
            "cacheWriteTokens": prompt.cache_write_tokens,
            "outputTokens": int(self._output),
            "promptEstimated": estimated,
        }

    def _trim(self, now: float) -> None:
        while self._samples and now - self._samples[0][0] > _WINDOW_S:
            self._samples.popleft()
