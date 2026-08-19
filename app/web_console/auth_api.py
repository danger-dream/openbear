# ruff: noqa: F401,F403,F405
from __future__ import annotations

from app.mcp.audit import record_audit
from app.web_console.core import *
from app.web_console.live_stream import *


class WebAdminAuthMixin:
    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        path = request.path
        if (
            path in {"/health", "/login", "/api/auth/login/start"}
            or path.startswith("/assets/")
            or path.startswith("/api/auth/login/status/")
            or path.startswith("/api/auth/login/consume/")
        ):
            return await handler(request)
        session = await self.session_from_request(request)
        if session is None:
            if path.startswith("/api/"):
                return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
            raise web.HTTPFound("/login")
        request[_WEB_SESSION_KEY] = session
        if not self._origin_allowed(request):
            if path.startswith("/api/"):
                return web.json_response({"ok": False, "error": "csrf_origin_rejected"}, status=403)
            raise web.HTTPForbidden(text="csrf origin rejected")
        return await handler(request)

    async def handle_health(self, request: web.Request) -> web.Response:
        from app import installed_version

        return web.json_response({"ok": True, "version": installed_version()})

    def _web_dist_dir(self) -> Path:
        return Path(__file__).resolve().parents[2] / "web" / "dist"

    def _web_index_path(self) -> Path:
        return self._web_dist_dir() / "index.html"

    async def handle_index(self, request: web.Request) -> web.Response:
        index = self._web_index_path()
        if not index.is_file():
            raise web.HTTPInternalServerError(text="web/dist/index.html not found; run web build first")
        return web.FileResponse(index, headers={"Cache-Control": "no-store"})

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
        return web.FileResponse(candidate)

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
        ts = now_ts()
        cooldown = self._login_cooldown_seconds()
        cur = await self.db.conn.execute(
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
        await self.db.conn.execute(
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
        await self.db.conn.commit()
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
        return web.json_response({"ok": True, "requestUuid": req_uuid, "status": await self.login_request_status(req_uuid)})

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
        return web.json_response({"ok": True, "chatId": session.chat_id, "expiresAt": session.expires_at})

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
        if row["status"] == "pending" and int(row["expires_at"] or 0) < now_ts():
            await self.db.conn.execute(
                "UPDATE web_login_requests SET status='expired', decided_at=? WHERE request_uuid=?",
                (now_ts(), req_uuid),
            )
            await self.db.conn.commit()
            return "expired"
        return str(row["status"] or "pending")

    async def decide_login_request(self, req_uuid: str, *, approved: bool, decided_by: int) -> str:
        status = await self.login_request_status(req_uuid)
        if status != "pending":
            return status
        new_status = "approved" if approved else "rejected"
        await self.db.conn.execute(
            """
            UPDATE web_login_requests SET status=?, decided_at=?, decided_by=?
            WHERE request_uuid=? AND status='pending'
            """,
            (new_status, now_ts(), decided_by, req_uuid),
        )
        await self.db.conn.commit()
        await self.audit(
            "web.login.approved" if approved else "web.login.denied",
            actor="telegram",
            chat_id=decided_by,
            detail={"requestUuid": req_uuid},
        )
        return new_status

    async def create_session_from_request(self, req_uuid: str, request: web.Request) -> str:
        cur = await self.db.conn.execute(
            "SELECT chat_id, status, nonce_hash FROM web_login_requests WHERE request_uuid=?", (req_uuid,)
        )
        row = await cur.fetchone()
        if not row or row["status"] != "approved":
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
        ts = now_ts()
        await self.db.conn.execute(
            """
            INSERT INTO web_sessions (session_token_hash, chat_id, created_at, expires_at, last_seen_at, ip, user_agent)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                _sha256(token),
                int(row["chat_id"] or 0),
                ts,
                ts + self.config.web.session_days * 86400,
                ts,
                request.remote or "",
                request.headers.get("User-Agent", "")[:500],
            ),
        )
        await self.db.conn.execute(
            "UPDATE web_login_requests SET status='consumed' WHERE request_uuid=?", (req_uuid,)
        )
        await self.db.conn.commit()
        await self.audit("web.session.created", actor="web", chat_id=int(row["chat_id"] or 0), detail={})
        return token

    async def session_from_request(self, request: web.Request) -> WebSession | None:
        token = request.cookies.get(_COOKIE, "")
        if not token:
            return None
        token_hash = _sha256(token)
        cur = await self.db.conn.execute(
            """
            SELECT chat_id, expires_at, revoked_at, last_seen_at FROM web_sessions
            WHERE session_token_hash=?
            """,
            (token_hash,),
        )
        row = await cur.fetchone()
        if not row or int(row["revoked_at"] or 0) > 0:
            return None
        if int(row["expires_at"] or 0) < now_ts():
            return None
        ts = now_ts()
        last_seen = int(row["last_seen_at"] or 0) if "last_seen_at" in row.keys() else 0
        if ts - last_seen >= 300:
            await self.db.conn.execute(
                "UPDATE web_sessions SET last_seen_at=? WHERE session_token_hash=?",
                (ts, token_hash),
            )
            await self.db.conn.commit()
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
