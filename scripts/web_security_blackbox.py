#!/usr/bin/env python3
"""Unauthenticated black-box security probe for OpenBear Web.

This is intentionally HTTP-only and cookie-less. It verifies that public/cross-net
requests cannot read APIs, static traversal targets, config files, DB files, or
traceback/path details.
"""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


@dataclass(slots=True)
class ProbeResult:
    path: str
    status: int
    body_chars: int
    ok: bool
    reason: str = ""


FORBIDDEN_MARKERS = [
    "botToken",
    "apiKey",
    "TG_API_TOKEN",
    "BEGIN PRIVATE KEY",
    "openbear_web_session",
    "sqlite_master",
    "CREATE TABLE sessions",
    "Traceback (most recent call last)",
    "/opt/src-space/openbear/openbear.json",
]


def fetch(base_url: str, path: str, timeout: int) -> tuple[int, str]:
    opener = urllib.request.build_opener(NoRedirect)
    url = base_url.rstrip("/") + path
    req = urllib.request.Request(url, headers={"User-Agent": "openbear-security-blackbox/1.0"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            return int(resp.status), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", "replace")


def check(base_url: str, path: str, timeout: int) -> ProbeResult:
    status, body = fetch(base_url, path, timeout)
    if any(marker in body for marker in FORBIDDEN_MARKERS):
        return ProbeResult(path, status, len(body), False, "sensitive_marker_in_body")
    if status == 200:
        # Only /health is allowed to be publicly 200 in this probe set.
        return ProbeResult(path, status, len(body), False, "unexpected_public_200")
    if status in {301, 302, 303, 307, 308, 401, 403, 404, 405}:
        return ProbeResult(path, status, len(body), True)
    return ProbeResult(path, status, len(body), False, "unexpected_status")


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenBear unauthenticated Web security black-box probe")
    parser.add_argument("--url", default="http://127.0.0.1:18961", help="Base Web URL")
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    paths = [
        "/api/auth/session",
        "/api/audit-logs",
        "/api/memory/secrets?full=1",
        "/api/web/sessions",
        "/openbear.json",
        "/data/openbear.db",
        "/logs/openbear.log",
        "/../../openbear.json",
        "/%2e%2e/openbear.json",
        "/%2e%2e/%2e%2e/openbear.json",
        "/%252e%252e/%252e%252e/openbear.json",
        "/assets/../../openbear.json",
        "/assets/%2e%2e/%2e%2e/openbear.json",
        "/assets/%252e%252e/%252e%252e/openbear.json",
        "/assets/../data/openbear.db",
        "/static/../../openbear.json",
    ]
    results = [check(args.url, path, args.timeout) for path in paths]
    payload = {
        "ok": all(r.ok for r in results),
        "url": args.url,
        "results": [asdict(r) for r in results],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for r in results:
            status = "✅" if r.ok else "❌"
            print(f"{status} {r.status:>3} {r.path} {r.reason}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
