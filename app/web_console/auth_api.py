# ruff: noqa: F401,F403,F405
from __future__ import annotations

import re
from urllib.parse import urlencode, urlsplit

from app.db.connection_router import SQLiteWriterTimeout
from app.mcp.audit import record_audit
from app.web_console.core import *
from app.web_console.live_stream import *

# Deliberately exact, public GET/HEAD resources. No root/static-directory wildcard.
PWA_PUBLIC_FILES = {
    "/openbear-push-sw.js": ("openbear-push-sw.js", "application/javascript"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icons/openbear-192.png": ("icons/openbear-192.png", "image/png"),
    "/icons/openbear-512.png": ("icons/openbear-512.png", "image/png"),
    "/icons/apple-touch-icon.png": ("icons/apple-touch-icon.png", "image/png"),
}

# Build output names carry a content hash, so such a file never changes under
# its name; anything else under /assets keeps revalidating.
_HASHED_ASSET = re.compile(r"-(?=[A-Za-z0-9_-]*[0-9A-Z])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9]+$")
_IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}
# Root asset attributes of the built index.html; compiled JS/CSS resolve their
# own assets relative to themselves.
_INDEX_ASSET_REF = re.compile(r'(\s(?:src|href)=")/assets/')


def _notification_return_target(value: str) -> str:
    # Root-relative, exact app paths only: no external/protocol-relative URL,
    # backslash aliases, control characters or API/login destinations.
    if not value.startswith("/") or value.startswith("//"):
        return ""
    if "\\" in value or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        return ""
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.path not in {"/chat", "/settings"}:
        return ""
    return parts.path + (f"?{parts.query}" if parts.query else "")


def _notification_login_url(login_url: str, target: str) -> str:
    safe_target = _notification_return_target(target)
    return login_url + ("?" + urlencode({"next": safe_target}) if safe_target else "")


class WebAdminAuthMixin:
    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        path = request.path
        ready = getattr(self, "startup_ready", None)
        if ready is not None and not ready.is_set():
            return web.json_response({"ok": False, "error": "service_not_ready"}, status=503,
                                     headers={"Cache-Control": "no-store", "Retry-After": "1"})
        managed = bool(getattr(getattr(self.config, "managed", None), "enabled", False))
        from app.servers.http import PUBLIC_ROUTES as SERVER_NODE_ROUTES
        from app.webhooks.http import PUBLIC_ROUTES
        if request.match_info.route.name in PUBLIC_ROUTES or request.match_info.route.name in SERVER_NODE_ROUTES:
            return await handler(request)
        if path == "/api/auth/mode" and request.method in {"GET", "HEAD"}:
            return await handler(request)
        if managed:
            from app.managed.http import (
                business_route_allowed,
                error,
                gateway_frontend_route_allowed,
                has_gateway_credentials,
                valid_gateway_key,
                valid_management_key,
            )
            from app.managed.settings import LOCAL_OWNER_ID
            runtime = getattr(self, "managed_runtime", None)
            if (path == "/instances" or path.startswith("/api/instances")
                    or path.startswith("/api/instance-events") or path.startswith("/api/auth/login/")
                    or path.startswith("/api/system/update") or path == "/api/system/restart"
                    or path == "/api/settings/web-task-notifications/test"):
                return error("forbidden_in_managed_mode", 403)
            if has_gateway_credentials(request) or path.startswith("/api/gateway/"):
                probe = path == "/api/gateway/v1/probe" and request.method == "GET"
                if not runtime or not valid_gateway_key(request, runtime, require_session=not probe):
                    return error("gateway_unauthorized", 401)
                if not (probe or business_route_allowed(request.method, path)
                        or gateway_frontend_route_allowed(request, self)):
                    return error("gateway_route_forbidden", 403)
                request[_WEB_SESSION_KEY] = WebSession(chat_id=LOCAL_OWNER_ID, expires_at=now_ts() + 60)
                return await handler(request)
            if path.startswith("/api/managed/") or request.headers.get("Authorization"):
                if not runtime or not valid_management_key(request, runtime):
                    return error("unauthorized", 401)
                if not (path.startswith("/api/managed/v1/") or business_route_allowed(request.method, path)):
                    return error("management_route_forbidden", 403)
                # Existing business ownership uses this local principal; no cookie is minted.
                request[_WEB_SESSION_KEY] = WebSession(chat_id=LOCAL_OWNER_ID, expires_at=now_ts() + 60)
                return await handler(request)
            if runtime and runtime.settings.auth_mode == "gateway":
                if path.startswith("/api/auth/") or path == "/login":
                    return error("local_auth_disabled", 403)
                # In gateway mode documents and compiled resources come from
                # this node too, but only after the gateway credentials above.
                # Controller/local public login assets keep their old behavior.
                if path != "/health":
                    return error("gateway_unauthorized", 401)
            if path == "/api/auth/password/login":
                if not self._origin_allowed(request):
                    return error("csrf_origin_rejected", 403)
                return await handler(request)
        elif path.startswith("/api/managed/") or path.startswith("/api/auth/password/"):
            return web.json_response({"ok": False, "error": "not_found"}, status=404)
        if not managed and (path == "/login" or path.startswith("/api/auth/login/")):
            login_url = self._https_login_url(request)
            if login_url:
                if path == "/login" and request.method in {"GET", "HEAD"}:
                    raise web.HTTPFound(
                        _notification_login_url(login_url, request.query.get("next", "")),
                        headers={"Cache-Control": "no-store"},
                    )
                # Never replay the Secret Key POST to another origin. Reject before
                # reading it or creating a request/Telegram notification.
                return web.json_response(
                    {"ok": False, "error": "https_required", "loginUrl": login_url},
                    status=400,
                    headers={"Cache-Control": "no-store"},
                )
        if (
            path in {"/health", "/login", "/api/auth/login/start"}
            or (request.method in {"GET", "HEAD"} and path in PWA_PUBLIC_FILES)
            or path.startswith("/assets/")
            or path.startswith("/api/auth/login/status/")
            or path.startswith("/api/auth/login/consume/")
        ):
            return await handler(request)
        session = await self.session_from_request(request)
        if session is None:
            if path.startswith("/api/"):
                return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
            raise web.HTTPFound(_notification_login_url("/login", str(request.rel_url)))
        request[_WEB_SESSION_KEY] = session
        if not self._origin_allowed(request):
            if path.startswith("/api/"):
                return web.json_response({"ok": False, "error": "csrf_origin_rejected"}, status=403)
            raise web.HTTPForbidden(text="csrf origin rejected")
        return await handler(request)

    async def handle_api_auth_mode(self, request: web.Request) -> web.Response:
        managed = bool(getattr(getattr(self.config, "managed", None), "enabled", False))
        headers = {"Cache-Control": "no-store"}
        if managed:
            headers["X-OpenBear-Instance"] = self.config.managed.instance_id
        return web.json_response({"ok": True, "mode": "managed" if managed else "controller"},
                                 headers=headers)

    async def handle_api_password_login(self, request: web.Request) -> web.Response:
        from app.managed.http import error
        from app.managed.settings import LOCAL_OWNER_ID
        runtime = getattr(self, "managed_runtime", None)
        if not runtime or not self.config.managed.enabled:
            return error("not_found", 404)
        if runtime.settings.auth_mode != "local":
            return error("local_auth_disabled", 403)
        body = await self._json_body(request)
        username, password = body.get("username"), body.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            await runtime.emit("login_failure", {"reason": "invalid_request"})
            return error("invalid_credentials", 400)
        # Single-owner rate limit cannot be bypassed with forwarded-IP spoofing or
        # distributed guesses; serialize verification to also bound scrypt memory.
        async with runtime.login_lock:
            limited = await self._login_rate_limit_status("managed-owner")
            if limited["blocked"]:
                await runtime.emit("login_failure", {"reason": "rate_limited"})
                response = error("rate_limited", 429)
                response.headers["Retry-After"] = str(limited["retryAfter"])
                return response
            verified = await asyncio.to_thread(runtime.account.verify, username, password)
            if not verified:
                await self._record_login_failure("managed-owner")
                await runtime.emit("login_failure", {"reason": "invalid_credentials"})
                return error("invalid_credentials", 403)
            await self._clear_login_failures("managed-owner")
            token = secrets.token_urlsafe(32)
            ts = now_ts()
            async with self.db.write_transaction(label="managed-password-login") as conn:
                await conn.execute("INSERT INTO web_sessions(session_token_hash,chat_id,created_at,expires_at,last_seen_at,ip,user_agent) VALUES(?,?,?,?,?,?,?)",
                                   (_sha256(token), LOCAL_OWNER_ID, ts, ts + self.config.web.session_days * 86400,
                                    ts, request.remote or "", request.headers.get("User-Agent", "")[:500]))
        await runtime.emit("login_success", {})
        response = web.json_response({"ok": True}, headers={"Cache-Control": "no-store"})
        response.set_cookie(_COOKIE, token, httponly=True, samesite="Lax", secure=self._cookie_secure(request),
                            max_age=self.config.web.session_days * 86400, path="/")
        return response

    async def handle_api_password_change(self, request: web.Request) -> web.Response:
        from app.managed.http import error
        from app.managed.settings import LOCAL_OWNER_ID
        runtime = getattr(self, "managed_runtime", None)
        if not runtime or runtime.settings.auth_mode != "local" or request[_WEB_SESSION_KEY].chat_id != LOCAL_OWNER_ID:
            return error("forbidden", 403)
        body = await self._json_body(request)
        current, password = body.get("currentPassword"), body.get("password")
        username = body.get("username", runtime.account.value["username"])
        if not all(isinstance(v, str) for v in (current, password, username)):
            return error("invalid_credentials")
        async with runtime.login_lock:
            limited = await self._login_rate_limit_status("managed-owner")
            if limited["blocked"]:
                return error("rate_limited", 429)
            if not await asyncio.to_thread(runtime.account.verify, runtime.account.value["username"], current):
                await self._record_login_failure("managed-owner")
                return error("invalid_credentials", 403)
            try:
                await asyncio.to_thread(runtime.account.change, username, password)
            except ValueError:
                return error("invalid_account")
            await self.revoke_all_sessions(chat_id=LOCAL_OWNER_ID)
        response = web.json_response({"ok": True})
        response.del_cookie(_COOKIE)
        return response

    async def handle_health(self, request: web.Request) -> web.Response:
        from app import installed_version

        return web.json_response({"ok": True, "version": installed_version()})

    def _web_dist_dir(self) -> Path:
        return Path(__file__).resolve().parents[2] / "web" / "dist"

    def _web_index_path(self) -> Path:
        return self._web_dist_dir() / "index.html"

    def _auth_mode(self) -> str:
        if not getattr(getattr(self.config, "managed", None), "enabled", False):
            return "controller"
        runtime = getattr(self, "managed_runtime", None)
        return "gateway" if runtime and runtime.settings.auth_mode == "gateway" else "managed"

    def _asset_base(self, request: web.Request) -> str:
        # Only a gateway that routes /_i/<instance>/ announces it; behind an
        # older gateway the page keeps root asset URLs.
        if self._auth_mode() != "gateway":
            return "/"
        base = f"/_i/{self.managed_runtime.settings.instance_id}/"
        return base if request.headers.get("X-OpenBear-Gateway-Assets") == base else "/"

    async def handle_index(self, request: web.Request) -> web.Response:
        index = self._web_index_path()
        if not index.is_file():
            raise web.HTTPInternalServerError(text="web/dist/index.html not found; run web build first")
        # The page carries its login mode, so loading it needs no extra request.
        text = index.read_text(encoding="utf-8").replace('"[[controller]]"', json.dumps(self._auth_mode()), 1)
        base = self._asset_base(request)
        if base != "/":
            # Same-origin instances must not share long-lived asset cache entries.
            text = text.replace('"[[asset-base]]"', json.dumps(base), 1)
            text = _INDEX_ASSET_REF.sub(lambda match: match[1] + base + "assets/", text)
        return web.Response(text=text, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def handle_asset(self, request: web.Request) -> web.Response:
        assets = (self._web_dist_dir() / "assets").resolve()
        rel = str(request.match_info.get("path") or "")
        if not rel:
            raise web.HTTPNotFound(text="asset not found")
        candidate = (assets / rel).resolve()
        try:
            candidate.relative_to(assets)
        except ValueError:
            raise web.HTTPNotFound(text="asset not found") from None
        if not candidate.is_file():
            raise web.HTTPNotFound(text="asset not found")
        return web.FileResponse(candidate, headers=_IMMUTABLE if _HASHED_ASSET.search(candidate.name) else None)

    async def handle_pwa_asset(self, request: web.Request) -> web.Response:
        resource = PWA_PUBLIC_FILES.get(request.path)
        if resource is None:
            raise web.HTTPNotFound(text="installation resource not found")
        dist = self._web_dist_dir().resolve()
        expected = dist / resource[0]
        candidate = expected.resolve()
        if candidate != expected:
            raise web.HTTPNotFound(text="installation resource not found")
        try:
            candidate.relative_to(dist)
        except ValueError:
            raise web.HTTPNotFound(text="installation resource not found") from None
        if not candidate.is_file():
            raise web.HTTPNotFound(text="installation resource not found")
        return web.FileResponse(candidate, headers={
            "Content-Type": resource[1],
            # Stable URLs must be revalidated on upgrades, never immutable/offline.
            "Cache-Control": "no-cache, max-age=0, must-revalidate",
            "X-Content-Type-Options": "nosniff",
        })

    def _login_cooldown_seconds(self) -> int:
        return max(60, int(self.config.web.failed_login_cooldown_minutes) * 60)

    async def _login_rate_limit_status(self, ip: str) -> dict[str, Any]:
        if not ip:
            return {"blocked": False, "retryAfter": 0, "failedCount": 0}
        cur = await self.db.conn.execute(
            "SELECT failed_count, blocked_until FROM web_login_failures WHERE ip=?",
            (ip,),
        )
        row = await cur.fetchone()
        if not row:
            return {"blocked": False, "retryAfter": 0, "failedCount": 0}
        blocked_until = int(row["blocked_until"] or 0)
        retry_after = max(0, blocked_until - now_ts())
        if retry_after > 0:
            return {
                "blocked": True,
                "retryAfter": retry_after,
                "failedCount": int(row["failed_count"] or 0),
            }
        return {
            "blocked": False,
            "retryAfter": 0,
            "failedCount": int(row["failed_count"] or 0),
        }

    async def _record_login_failure(self, ip: str) -> dict[str, Any]:
        if not ip:
            return {"blocked": False, "retryAfter": 0, "failedCount": 1}
        cooldown = self._login_cooldown_seconds()
        # Read/modify/write must share the one writer transaction. Concurrent
        # incorrect secrets otherwise overwrite each other's count and postpone
        # or bypass the cooldown even though every request was rejected.
        async with self.db.write_transaction(label="record-web-login-failure") as conn:
            ts = now_ts()
            cur = await conn.execute(
                "SELECT failed_count, first_failed_at, blocked_until FROM web_login_failures WHERE ip=?",
                (ip,),
            )
            row = await cur.fetchone()
            if row and int(row["blocked_until"] or 0) > ts:
                retry_after = int(row["blocked_until"] or 0) - ts
                return {"blocked": True, "retryAfter": retry_after, "failedCount": int(row["failed_count"] or 0)}
            if row and ts - int(row["first_failed_at"] or ts) <= cooldown:
                first_failed_at = int(row["first_failed_at"] or ts)
                failed_count = int(row["failed_count"] or 0) + 1
            else:
                first_failed_at = ts
                failed_count = 1
            blocked_until = ts + cooldown if failed_count >= _LOGIN_FAIL_LIMIT else 0
            await conn.execute(
                """
                INSERT INTO web_login_failures (ip, failed_count, first_failed_at, last_failed_at, blocked_until)
                VALUES (?,?,?,?,?)
                ON CONFLICT(ip) DO UPDATE SET
                  failed_count=excluded.failed_count,
                  first_failed_at=excluded.first_failed_at,
                  last_failed_at=excluded.last_failed_at,
                  blocked_until=excluded.blocked_until
                """,
                (ip, failed_count, first_failed_at, ts, blocked_until),
            )
        return {
            "blocked": blocked_until > ts,
            "retryAfter": max(0, blocked_until - ts),
            "failedCount": failed_count,
            "limit": _LOGIN_FAIL_LIMIT,
        }

    async def _clear_login_failures(self, ip: str) -> None:
        if not ip:
            return
        await self.db.conn.execute("DELETE FROM web_login_failures WHERE ip=?", (ip,))
        await self.db.conn.commit()

    async def handle_login_post(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        secret = str(body.get("secret") or "")
        ip = request.remote or ""
        ua = request.headers.get("User-Agent", "")[:500]
        limited = await self._login_rate_limit_status(ip)
        if limited["blocked"]:
            retry_after = int(limited["retryAfter"] or 0)
            await self.audit(
                "web.login.rate_limited",
                actor="web",
                ip=ip,
                detail={"retryAfter": retry_after},
            )
            resp = web.json_response(
                {"ok": False, "error": "rate_limited", "retryAfter": retry_after},
                status=429,
            )
            resp.headers["Retry-After"] = str(max(1, retry_after))
            return resp
        if not secrets.compare_digest(secret, await self.get_secret_key()):
            failure = await self._record_login_failure(ip)
            await self.audit(
                "web.login.denied",
                actor="web",
                ip=ip,
                detail={"reason": "bad_secret", **failure},
            )
            if failure.get("blocked"):
                retry_after = int(failure.get("retryAfter") or 0)
                resp = web.json_response(
                    {"ok": False, "error": "rate_limited", "retryAfter": retry_after},
                    status=429,
                )
                resp.headers["Retry-After"] = str(max(1, retry_after))
                return resp
            return web.json_response({"ok": False, "error": "bad_secret"}, status=403)
        await self._clear_login_failures(ip)
        nonce = secrets.token_urlsafe(24)
        req_uuid = await self.create_login_request(ip=ip, user_agent=ua, nonce_hash=_sha256(nonce))
        await self.audit("web.login.pending", actor="web", ip=ip, detail={"requestUuid": req_uuid})
        resp = web.json_response({
            "ok": True,
            "requestUuid": req_uuid,
            "expiresIn": self.config.web.login_request_ttl_seconds,
            "statusUrl": f"/api/auth/login/status/{req_uuid}",
            "consumeUrl": f"/api/auth/login/consume/{req_uuid}",
        })
        resp.set_cookie(
            _LOGIN_NONCE_COOKIE,
            nonce,
            httponly=True,
            samesite="Lax",
            secure=self._cookie_secure(request),
            max_age=self.config.web.login_request_ttl_seconds,
        )
        return resp

    async def handle_api_auth_status(self, request: web.Request) -> web.Response:
        req_uuid = request.match_info.get("request_uuid", "")
        return web.json_response(
            {"ok": True, "requestUuid": req_uuid, "status": await self.login_request_status(req_uuid)},
            headers={"Cache-Control": "no-store"},
        )

    async def handle_api_auth_consume(self, request: web.Request) -> web.Response:
        req_uuid = request.match_info.get("request_uuid", "")
        status = await self.login_request_status(req_uuid)
        if status == "approved":
            token = await self.create_session_from_request(req_uuid, request)
            resp = web.json_response({"ok": True, "requestUuid": req_uuid})
            resp.set_cookie(
                _COOKIE,
                token,
                httponly=True,
                samesite="Lax",
                secure=self._cookie_secure(request),
                max_age=self.config.web.session_days * 86400,
            )
            resp.del_cookie(_LOGIN_NONCE_COOKIE)
            return resp
        code = 409 if status == "pending" else 403
        return web.json_response({"ok": False, "requestUuid": req_uuid, "status": status}, status=code)

    async def handle_api_auth_session(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        return web.json_response(
            {"ok": True, "chatId": session.chat_id, "expiresAt": session.expires_at,
             "mode": "managed" if getattr(getattr(self.config, "managed", None), "enabled", False) else "controller"},
            headers={"Cache-Control": "no-store"},
        )

    async def handle_api_auth_sessions(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        token = request.cookies.get(_COOKIE, "")
        current_hash = _sha256(token) if token else ""
        ts = now_ts()
        cur = await self.db.conn.execute(
            """
            SELECT id, session_token_hash, created_at, expires_at, last_seen_at, ip, user_agent
            FROM web_sessions
            WHERE chat_id=? AND revoked_at=0 AND expires_at>?
            ORDER BY CASE WHEN session_token_hash=? THEN 0 ELSE 1 END,
                     last_seen_at DESC, created_at DESC, id DESC
            LIMIT 100
            """,
            (session.chat_id, ts, current_hash),
        )
        items = []
        for row in await cur.fetchall():
            items.append({
                "id": int(row["id"]),
                "createdAt": int(row["created_at"] or 0),
                "expiresAt": int(row["expires_at"] or 0),
                "lastSeenAt": int(row["last_seen_at"] or 0),
                "ip": str(row["ip"] or ""),
                "userAgent": str(row["user_agent"] or ""),
                "current": bool(current_hash and secrets.compare_digest(str(row["session_token_hash"]), current_hash)),
            })
        return web.json_response(
            {"ok": True, "items": items, "total": len(items)},
            headers={"Cache-Control": "no-store"},
        )

    async def handle_api_auth_session_revoke(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        target_id = int(request.match_info["session_id"])
        token = request.cookies.get(_COOKIE, "")
        current_hash = _sha256(token) if token else ""
        ts = now_ts()
        cur = await self.db.conn.execute(
            """
            SELECT session_token_hash FROM web_sessions
            WHERE id=? AND chat_id=? AND revoked_at=0 AND expires_at>?
            """,
            (target_id, session.chat_id, ts),
        )
        row = await cur.fetchone()
        if not row:
            return web.json_response({"ok": False, "error": "session_not_found"}, status=404)
        is_current = bool(
            current_hash and secrets.compare_digest(str(row["session_token_hash"] or ""), current_hash)
        )
        changed = await self.db.conn.execute(
            """
            UPDATE web_sessions SET revoked_at=?
            WHERE id=? AND chat_id=? AND revoked_at=0 AND expires_at>?
            """,
            (ts, target_id, session.chat_id, ts),
        )
        await self.db.conn.commit()
        if int(changed.rowcount or 0) != 1:
            return web.json_response({"ok": False, "error": "session_not_found"}, status=404)
        await self.audit(
            "web.session.revoked",
            actor="web",
            chat_id=session.chat_id,
            ip=request.remote or "",
            detail={"sessionId": target_id, "current": is_current},
        )
        response = web.json_response({"ok": True, "sessionId": target_id, "current": is_current})
        if is_current:
            response.del_cookie(_COOKIE)
        return response

    def _audit_query(self, request: web.Request, *, export: bool = False) -> tuple[str, list[Any], int, int]:
        page = max(1, int(request.query.get("page", "1") or 1))
        page_size = min(10000 if export else 200, max(1, int(request.query.get("pageSize", "100") or 100)))
        where: list[str] = []
        params: list[Any] = []
        kind = request.query.get("kind") or request.query.get("action")
        if kind:
            where.append("kind=?")
            params.append(kind)
        actor = request.query.get("actor")
        if actor:
            where.append("actor=?")
            params.append(actor)
        chat_id = request.query.get("chatId") or request.query.get("chat_id")
        if chat_id:
            where.append("chat_id=?")
            params.append(int(chat_id))
        status = request.query.get("status")
        if status:
            where.append("REPLACE(detail_json, ' ', '') LIKE ?")
            params.append(f'%"status":"{status}"%')
        since = _parse_ts(request.query.get("since"))
        if since:
            where.append("created_at>=?")
            params.append(since)
        until = _parse_ts(request.query.get("until"))
        if until:
            where.append("created_at<=?")
            params.append(until)
        sql_where = " WHERE " + " AND ".join(where) if where else ""
        return sql_where, params, page, page_size

    @staticmethod
    def _audit_item(row: Any) -> dict[str, Any]:
        item = {k: row[k] for k in row.keys()}
        try:
            item["detail"] = json.loads(item.pop("detail_json") or "{}")
        except json.JSONDecodeError:
            item["detail"] = {}
        return item

    async def handle_api_audit(self, request: web.Request) -> web.Response:
        sql_where, params, page, page_size = self._audit_query(request)
        offset = (page - 1) * page_size
        cur = await self.db.conn.execute(f"SELECT COUNT(*) AS n FROM audit_logs{sql_where}", params)
        total = int((await cur.fetchone())["n"] or 0)
        cur = await self.db.conn.execute(
            f"SELECT id, kind, actor, chat_id, ip, detail_json, created_at FROM audit_logs{sql_where} "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, page_size, offset],
        )
        rows = [self._audit_item(row) for row in await cur.fetchall()]
        return web.json_response({"ok": True, "items": rows, "total": total, "page": page, "pageSize": page_size})

    async def handle_api_audit_detail(self, request: web.Request) -> web.Response:
        audit_id = int(request.match_info["audit_id"])
        cur = await self.db.conn.execute(
            "SELECT id, kind, actor, chat_id, ip, detail_json, created_at FROM audit_logs WHERE id=?",
            (audit_id,),
        )
        row = await cur.fetchone()
        if not row:
            return web.json_response({"ok": False, "error": "audit_log_not_found"}, status=404)
        return web.json_response({"ok": True, "item": self._audit_item(row)})

    async def handle_api_audit_export(self, request: web.Request) -> web.Response:
        sql_where, params, _page, page_size = self._audit_query(request, export=True)
        cur = await self.db.conn.execute(
            f"SELECT id, kind, actor, chat_id, ip, detail_json, created_at FROM audit_logs{sql_where} "
            "ORDER BY id DESC LIMIT ?",
            [*params, page_size],
        )
        rows = [self._audit_item(row) for row in await cur.fetchall()]
        data = json.dumps({"ok": True, "items": rows, "count": len(rows)}, ensure_ascii=False, indent=2).encode("utf-8")
        return web.Response(
            body=data,
            content_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="openbear-audit-logs.json"'},
        )

    async def handle_api_logout(self, request: web.Request) -> web.Response:
        session: WebSession = request[_WEB_SESSION_KEY]
        token = request.cookies.get(_COOKIE, "")
        if token:
            await self.revoke_session(token)
        await self.audit("web.logout", actor="web", chat_id=session.chat_id, ip=request.remote or "")
        resp = web.json_response({"ok": True})
        resp.del_cookie(_COOKIE)
        return resp

    async def ensure_secret_key(self) -> str:
        existing = await self.get_secret_key(default="")
        if existing:
            return existing
        key = secrets.token_urlsafe(24)
        await self.set_state(_STATE_WEB_SECRET, key)
        await self.audit("web.secret.generated", actor="system", detail={})
        return key

    async def get_secret_key(self, default: str | None = None) -> str:
        cur = await self.db.conn.execute("SELECT value FROM app_state WHERE key=?", (_STATE_WEB_SECRET,))
        row = await cur.fetchone()
        if row:
            return str(row["value"] or "")
        if default is not None:
            return default
        return await self.ensure_secret_key()

    async def reset_secret_key(self, *, actor: str = "telegram", chat_id: int = 0) -> str:
        key = secrets.token_urlsafe(24)
        await self.set_state(_STATE_WEB_SECRET, key)
        await self.db.conn.execute("UPDATE web_sessions SET revoked_at=? WHERE revoked_at=0", (now_ts(),))
        await self.db.conn.execute("UPDATE web_login_requests SET status='expired', decided_at=? WHERE status='pending'", (now_ts(),))
        await self.db.conn.commit()
        await self.audit("web.secret.reset", actor=actor, chat_id=chat_id, detail={"sessionsRevoked": True})
        return key

    async def set_state(self, key: str, value: str) -> None:
        await self.db.conn.execute(
            """
            INSERT INTO app_state (key, value, updated_at) VALUES (?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, now_ts()),
        )
        await self.db.conn.commit()

    async def create_login_request(self, *, ip: str, user_agent: str, nonce_hash: str = "") -> str:
        if getattr(getattr(self.config, "managed", None), "enabled", False):
            raise web.HTTPForbidden(text="Telegram login unavailable in managed mode")
        chat_id = int(self.config.telegram.whitelist_ids[0]) if self.config.telegram.whitelist_ids else 0
        req_uuid = secrets.token_urlsafe(18)
        ts = now_ts()
        await self.db.conn.execute(
            """
            INSERT INTO web_login_requests (request_uuid, chat_id, status, nonce_hash, ip, user_agent, created_at, expires_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (req_uuid, chat_id, "pending", nonce_hash, ip, user_agent, ts, ts + self.config.web.login_request_ttl_seconds),
        )
        await self.db.conn.commit()
        await self._send_login_confirm(chat_id, req_uuid, ip, user_agent)
        return req_uuid

    async def _send_login_confirm(self, chat_id: int, req_uuid: str, ip: str, user_agent: str) -> None:
        if not chat_id:
            return
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ 确认登录", callback_data=f"web_login:approve:{req_uuid}"),
            InlineKeyboardButton(text="❌ 拒绝", callback_data=f"web_login:deny:{req_uuid}"),
        ]])
        await self.bot.send_message(
            chat_id,
            "🔐 OpenBear Web 登录确认\n\n"
            f"IP：{ip or 'unknown'}\n"
            f"UA：{user_agent[:120] or 'unknown'}\n\n"
            "如果不是你本人操作，请点拒绝并重置 Web Secret Key。",
            reply_markup=keyboard,
        )

    async def login_request_status(self, req_uuid: str) -> str:
        if not req_uuid:
            return "missing"
        cur = await self.db.conn.execute(
            "SELECT status, expires_at FROM web_login_requests WHERE request_uuid=?", (req_uuid,)
        )
        row = await cur.fetchone()
        if not row:
            return "missing"
        if row["status"] in {"pending", "approved"} and int(row["expires_at"] or 0) <= now_ts():
            ts = now_ts()
            await self.db.conn.execute(
                "UPDATE web_login_requests SET status='expired', decided_at=? "
                "WHERE request_uuid=? AND status IN ('pending','approved') AND expires_at<=?",
                (ts, req_uuid, ts),
            )
            await self.db.conn.commit()
            return "expired"
        return str(row["status"] or "pending")

    async def decide_login_request(self, req_uuid: str, *, approved: bool, decided_by: int) -> str:
        status = await self.login_request_status(req_uuid)
        if status != "pending":
            return status
        new_status = "approved" if approved else "rejected"
        # The request can expire while another task owns the shared writer.
        # Check the TTL only after this decision actually acquires that writer.
        async with self.db.write_transaction(label="decide-web-login-request") as conn:
            ts = now_ts()
            changed = await conn.execute(
                """
                UPDATE web_login_requests SET status=?, decided_at=?, decided_by=?
                WHERE request_uuid=? AND status='pending' AND expires_at>?
                """,
                (new_status, ts, decided_by, req_uuid, ts),
            )
        if changed.rowcount != 1:
            # Another decision or expiry won after the initial status read.
            # Never report/audit an approval that did not take effect.
            return await self.login_request_status(req_uuid)
        await self.audit(
            "web.login.approved" if approved else "web.login.denied",
            actor="telegram",
            chat_id=decided_by,
            detail={"requestUuid": req_uuid},
        )
        return new_status

    async def create_session_from_request(self, req_uuid: str, request: web.Request) -> str:
        if getattr(getattr(self.config, "managed", None), "enabled", False):
            raise web.HTTPForbidden(text="Telegram login unavailable in managed mode")
        cur = await self.db.conn.execute(
            "SELECT chat_id, status, nonce_hash, expires_at FROM web_login_requests WHERE request_uuid=?", (req_uuid,)
        )
        row = await cur.fetchone()
        if not row or row["status"] != "approved" or int(row["expires_at"] or 0) <= now_ts():
            raise web.HTTPForbidden(text="login request is not approved")
        expected_nonce_hash = str(row["nonce_hash"] or "")
        if expected_nonce_hash:
            supplied_nonce = request.cookies.get(_LOGIN_NONCE_COOKIE, "")
            if not supplied_nonce or not secrets.compare_digest(_sha256(supplied_nonce), expected_nonce_hash):
                await self.audit(
                    "web.login.consume_denied",
                    actor="web",
                    chat_id=int(row["chat_id"] or 0),
                    ip=request.remote or "",
                    detail={"requestUuid": req_uuid, "reason": "nonce_mismatch"},
                )
                raise web.HTTPForbidden(text="login nonce mismatch")
        token = secrets.token_urlsafe(32)
        # Claim before issuing the cookie, atomically with its session row.
        # Two simultaneous consume requests must never mint two sessions from
        # the same Telegram approval, nor succeed after its TTL has elapsed.
        async with self.db.write_transaction(label="consume-web-login-request") as conn:
            # Acquiring the global writer can take longer than the login TTL.
            # Both the claim condition and session lifetime use the real claim time.
            ts = now_ts()
            changed = await conn.execute(
                "UPDATE web_login_requests SET status='consumed' "
                "WHERE request_uuid=? AND status='approved' AND expires_at>?",
                (req_uuid, ts),
            )
            if changed.rowcount != 1:
                raise web.HTTPForbidden(text="login request is not approved")
            await conn.execute(
                """
                INSERT INTO web_sessions (session_token_hash, chat_id, created_at, expires_at, last_seen_at, ip, user_agent)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    _sha256(token), int(row["chat_id"] or 0), ts,
                    ts + self.config.web.session_days * 86400, ts,
                    request.remote or "", request.headers.get("User-Agent", "")[:500],
                ),
            )
        await self.audit("web.session.created", actor="web", chat_id=int(row["chat_id"] or 0), detail={})
        return token

    async def session_from_request(self, request: web.Request) -> WebSession | None:
        # WebSocket handlers revalidate the original request between frames and
        # while streaming; management clients have no browser cookie to refresh.
        # Legacy standalone mixin hosts provide only a DB and use cookie auth.
        managed = bool(getattr(getattr(getattr(self, "config", None), "managed", None), "enabled", False))
        if managed:
            from app.managed.http import (
                business_route_allowed,
                has_gateway_credentials,
                valid_gateway_key,
                valid_management_key,
            )
            from app.managed.settings import LOCAL_OWNER_ID
            runtime = getattr(self, "managed_runtime", None)
            if has_gateway_credentials(request):
                if runtime and valid_gateway_key(request, runtime) and business_route_allowed(request.method, request.path):
                    return WebSession(chat_id=LOCAL_OWNER_ID, expires_at=now_ts() + 60)
                return None
            if request.headers.get("Authorization"):
                if runtime and valid_management_key(request, runtime) and business_route_allowed(request.method, request.path):
                    return WebSession(chat_id=LOCAL_OWNER_ID, expires_at=now_ts() + 60)
                return None
            if not runtime or runtime.settings.auth_mode == "gateway":
                return None
        token = request.cookies.get(_COOKIE, "")
        if not token:
            return None
        token_hash = _sha256(token)

        async def read_session():
            cur = await self.db.conn.execute(
                "SELECT chat_id, expires_at, revoked_at, last_seen_at FROM web_sessions "
                "WHERE session_token_hash=?", (token_hash,),
            )
            try:
                return await cur.fetchone()
            finally:
                # A refresh timeout must re-read a current committed snapshot,
                # not keep this cursor's old WAL snapshot alive.
                await cur.close()

        row = await read_session()
        if not row or int(row["revoked_at"] or 0) > 0 or int(row["expires_at"] or 0) <= now_ts():
            return None
        if managed:
            from app.managed.settings import LOCAL_OWNER_ID
            if int(row["chat_id"]) != LOCAL_OWNER_ID:
                return None
        last_seen = int(row["last_seen_at"] or 0)
        if now_ts() - last_seen >= 300:
            try:
                # last_seen is activity bookkeeping, not a condition for access.
                # Bound acquisition only: cancellation must not abandon queued SQL
                # or roll back another task's transaction.
                async with self.db.write_transaction(label="web-session-touch", wait_timeout_s=0.1) as conn:
                    ts = now_ts()
                    changed = await conn.execute(
                        "UPDATE web_sessions SET last_seen_at=? "
                        "WHERE session_token_hash=? AND revoked_at=0 AND expires_at>?",
                        (ts, token_hash, ts),
                    )
                    try:
                        if changed.rowcount != 1:
                            return None
                    finally:
                        await changed.close()
            except SQLiteWriterTimeout:
                # Revocation/expiry may have changed during the brief wait.
                # Never authorize from the initial stale row after skipping touch.
                row = await read_session()
        if not row or int(row["revoked_at"] or 0) > 0 or int(row["expires_at"] or 0) <= now_ts():
            return None
        return WebSession(chat_id=int(row["chat_id"] or 0), expires_at=int(row["expires_at"] or 0))

    async def revoke_session(self, token: str) -> None:
        await self.db.conn.execute(
            "UPDATE web_sessions SET revoked_at=? WHERE session_token_hash=?",
            (now_ts(), _sha256(token)),
        )
        await self.db.conn.commit()

    async def revoke_all_sessions(self, *, chat_id: int = 0, actor: str = "web", ip: str = "") -> int:
        ts = now_ts()
        if chat_id:
            cur = await self.db.conn.execute(
                "UPDATE web_sessions SET revoked_at=? WHERE chat_id=? AND revoked_at=0 AND expires_at>?",
                (ts, chat_id, ts),
            )
        else:
            cur = await self.db.conn.execute(
                "UPDATE web_sessions SET revoked_at=? WHERE revoked_at=0 AND expires_at>?",
                (ts, ts),
            )
        await self.db.conn.commit()
        revoked = int(cur.rowcount or 0)
        await self.audit(
            "web.sessions.revoked_all",
            actor=actor,
            chat_id=chat_id,
            ip=ip,
            detail={"revoked": revoked},
        )
        return revoked

    async def audit(
        self,
        kind: str,
        *,
        actor: str = "system",
        chat_id: int = 0,
        ip: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        await record_audit(self.db, kind, actor=actor, chat_id=chat_id, ip=ip, detail=detail)

__all__ = [name for name in globals() if not name.startswith("__")]
