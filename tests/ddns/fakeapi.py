"""A fake provider API: routes requests by method and URL, keeps every request for assertions."""
from __future__ import annotations

import json as jsonlib
import re

import httpx


class FakeAPI:
    def __init__(self):
        self.routes = []
        self.requests = []

    def on(self, method, pattern, handler=None, *, status=200, json=None, text=None, headers=None):
        """pattern is a regex matched against the URL without its query string.

        handler(request, match) returns an httpx.Response, a (status, json) tuple or a JSON value;
        without a handler the given status/json/text is returned."""
        def static(request, match):
            if text is not None:
                return httpx.Response(status, text=text, headers=headers)
            return httpx.Response(status, json=json, headers=headers)
        self.routes.append((method.upper(), re.compile(pattern), handler or static))
        return self

    def __call__(self, request):
        self.requests.append(request)
        url = str(request.url.copy_with(query=None))
        for method, pattern, handler in self.routes:
            match = pattern.fullmatch(url)
            if request.method == method and match:
                result = handler(request, match)
                if isinstance(result, httpx.Response):
                    return result
                if isinstance(result, tuple):
                    return httpx.Response(result[0], json=result[1])
                return httpx.Response(200, json=result)
        return httpx.Response(599, text=f'no fake route for {request.method} {url}')

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    def calls(self, method=None):
        return [r for r in self.requests if method is None or r.method == method.upper()]

    @staticmethod
    def body(request):
        raw = request.content
        return jsonlib.loads(raw) if raw else None
