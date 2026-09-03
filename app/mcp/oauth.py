"""OAuth connector support for remote MCP servers.

OAuth credentials are deliberately kept out of ``openbear.json`` and out of the
model/tool context.  Access/refresh tokens are encrypted before they are stored
in the OpenBear SQLite database.  The browser only ever sees the authorization
URL; the authorization code and token exchange stay on the server.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse

import httpx
from cryptography.fernet import Fernet, InvalidToken

from app.config import MCPOAuthConfig, MCPServerConfig
from app.logging import get_logger

log = get_logger("mcp.oauth")

_KEY_FILE_NAME = ".mcp-oauth.key"
_STATE_TTL_SECONDS = 600
_REFRESH_SKEW_SECONDS = 60

# GitHub's remote MCP metadata advertises a larger set.  These are intentionally
# configurable; the default omits the organization/admin/delete scopes.
GITHUB_DEFAULT_SCOPES = (
    "repo",
    "read:org",
    "read:enterprise",
    "read:user",
    "user:email",
    "read:packages",
    "read:project",
    "project",
    "gist",
    "notifications",
    "workflow",
    "codespace",
)


class MCPOAuthError(RuntimeError):
    """An OAuth flow could not be started or completed."""


@dataclass(slots=True)
class _PendingAuthorization:
    server_key: str
    code_verifier: str
    redirect_uri: str
    client_id: str
    token_endpoint: str
    scopes: tuple[str, ...]
    created_at: int


class MCPAuthManager:
    """Encrypted token store plus OAuth authorization-code/refresh flows."""

    def __init__(self, db: Any) -> None:
        self.db = db
        self._pending: dict[str, _PendingAuthorization] = {}

    def _key_path(self) -> Path:
        db_path = getattr(self.db, "path", None) or getattr(self.db, "_path", "")
        if not db_path:
            raise MCPOAuthError("oauth_storage_unavailable")
        return Path(str(db_path)).expanduser().resolve().with_name(_KEY_FILE_NAME)

    async def _fernet(self) -> Fernet:
        if self.db is None or getattr(self.db, "conn", None) is None:
            raise MCPOAuthError("oauth_storage_unavailable")
        path = self._key_path()
        try:
            if path.exists():
                encoded = path.read_text(encoding="ascii").strip()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                encoded = Fernet.generate_key().decode("ascii")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                fd = os.open(path, flags, 0o600)
                try:
                    os.write(fd, encoded.encode("ascii"))
                finally:
                    os.close(fd)
                with contextlib.suppress(OSError):
                    os.chmod(path, 0o600)
        except FileExistsError:
            encoded = path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise MCPOAuthError("oauth_storage_key_unavailable") from exc
        try:
            return Fernet(encoded.encode("ascii"))
        except Exception as exc:
            raise MCPOAuthError("oauth_storage_key_invalid") from exc

    async def _load_token(self, server_key: str) -> dict[str, Any] | None:
        cur = await self.db.conn.execute(
            "SELECT ciphertext FROM mcp_oauth_tokens WHERE server_key=?",
            (server_key,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        try:
            plaintext = (await self._fernet()).decrypt(str(row["ciphertext"]).encode("ascii"))
            value = json.loads(plaintext.decode("utf-8"))
        except (InvalidToken, ValueError, TypeError, json.JSONDecodeError) as exc:
            log.error("mcp.oauth.token_store_corrupt", server=server_key, error_type=type(exc).__name__)
            return None
        return value if isinstance(value, dict) else None

    async def _save_token(self, server_key: str, token: dict[str, Any]) -> None:
        safe_payload = {
            key: token[key]
            for key in (
                "access_token",
                "refresh_token",
                "token_type",
                "expires_at",
                "scope",
                "token_endpoint",
                "client_id",
            )
            if key in token and token[key] not in (None, "")
        }
        ciphertext = (await self._fernet()).encrypt(
            json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        expires_at = int(safe_payload.get("expires_at") or 0)
        scopes = str(safe_payload.get("scope") or "")
        now = int(time.time())
        await self.db.conn.execute(
            """
            INSERT INTO mcp_oauth_tokens (server_key, ciphertext, scopes, expires_at, created_at, updated_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(server_key) DO UPDATE SET
              ciphertext=excluded.ciphertext,
              scopes=excluded.scopes,
              expires_at=excluded.expires_at,
              updated_at=excluded.updated_at
            """,
            (server_key, ciphertext, scopes, expires_at, now, now),
        )
        await self.db.conn.commit()

    async def revoke(self, server_key: str) -> bool:
        cur = await self.db.conn.execute(
            "DELETE FROM mcp_oauth_tokens WHERE server_key=?",
            (server_key,),
        )
        await self.db.conn.commit()
        self._pending = {
            state: item for state, item in self._pending.items() if item.server_key != server_key
        }
        return bool(cur.rowcount)

    async def status(self, server_key: str, config: MCPServerConfig | None) -> dict[str, Any]:
        oauth = getattr(config, "oauth", None) if config is not None else None
        configured = bool(oauth and oauth.enabled and oauth.client_id.strip())
        result: dict[str, Any] = {
            "enabled": bool(oauth and oauth.enabled),
            "configured": configured,
            "authorized": False,
            "expiresAt": 0,
            "scopes": [],
            "hasRefreshToken": False,
        }
        if not configured:
            return result
        configured_scopes = [item for item in (oauth.scopes if oauth else []) if str(item).strip()][:50]
        if configured_scopes:
            result["scopes"] = configured_scopes
        token = await self._load_token(server_key)
        if not token:
            return result
        scope = str(token.get("scope") or "").strip()
        result.update({
            "authorized": bool(token.get("access_token")),
            "expiresAt": int(token.get("expires_at") or 0),
            "scopes": [item for item in scope.split() if item][:50] or configured_scopes,
            "hasRefreshToken": bool(token.get("refresh_token")),
        })
        return result

    @staticmethod
    def _secret(config: MCPOAuthConfig) -> str:
        env_name = str(config.client_secret_env or "").strip()
        if env_name:
            value = os.environ.get(env_name, "").strip()
            if value:
                return value
        file_name = str(config.client_secret_file or "").strip()
        if file_name:
            try:
                return Path(file_name).expanduser().read_text(encoding="utf-8").strip()
            except OSError:
                return ""
        return ""

    @staticmethod
    def _protected_resource_metadata_url(server_config: MCPServerConfig) -> str:
        oauth = server_config.oauth
        if oauth and oauth.resource_metadata_url.strip():
            return oauth.resource_metadata_url.strip()
        parsed = urlparse(server_config.url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        path = parsed.path or "/"
        if not path.startswith("/"):
            path = "/" + path
        return urlunparse((parsed.scheme, parsed.netloc, "/.well-known/oauth-protected-resource" + path, "", "", ""))

    async def _discover_endpoints(self, server_config: MCPServerConfig) -> tuple[str, str, str, tuple[str, ...]]:
        oauth = server_config.oauth
        if oauth is None or not oauth.enabled:
            raise MCPOAuthError("oauth_not_configured")
        metadata_url = self._protected_resource_metadata_url(server_config)
        resource_metadata: dict[str, Any] = {}
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            if metadata_url:
                try:
                    response = await client.get(metadata_url, headers={"Accept": "application/json"})
                    if response.status_code < 400:
                        payload = response.json()
                        if isinstance(payload, dict):
                            resource_metadata = payload
                except (httpx.HTTPError, ValueError):
                    resource_metadata = {}

            authorization_server = oauth.authorization_server.strip()
            if not authorization_server:
                servers = resource_metadata.get("authorization_servers")
                if isinstance(servers, list):
                    authorization_server = next((str(item).strip() for item in servers if str(item).strip()), "")
            authorization_server = authorization_server.rstrip("/")

            authorization_endpoint = oauth.authorization_endpoint.strip()
            token_endpoint = oauth.token_endpoint.strip()
            authorization_metadata: dict[str, Any] = {}
            if authorization_server and (not authorization_endpoint or not token_endpoint) and not authorization_server.endswith("/login/oauth"):
                candidates = [f"{authorization_server}/.well-known/oauth-authorization-server"]
                parsed = urlparse(authorization_server)
                if parsed.path:
                    candidates.append(f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-authorization-server")
                for candidate in dict.fromkeys(candidates):
                    try:
                        response = await client.get(candidate, headers={"Accept": "application/json"})
                        if response.status_code < 400:
                            payload = response.json()
                            if isinstance(payload, dict):
                                authorization_metadata = payload
                                break
                    except (httpx.HTTPError, ValueError):
                        continue

            authorization_endpoint = authorization_endpoint or str(authorization_metadata.get("authorization_endpoint") or "")
            token_endpoint = token_endpoint or str(authorization_metadata.get("token_endpoint") or "")
            # GitHub's OAuth App endpoint intentionally does not publish RFC 8414
            # authorization-server metadata.  The protected-resource metadata
            # still names https://github.com/login/oauth, whose endpoints are stable.
            if authorization_server.endswith("/login/oauth"):
                authorization_endpoint = authorization_endpoint or f"{authorization_server}/authorize"
                token_endpoint = token_endpoint or f"{authorization_server}/access_token"

        if not authorization_endpoint or not token_endpoint:
            raise MCPOAuthError("oauth_endpoints_not_discovered")
        configured_scopes = tuple(item.strip() for item in oauth.scopes if str(item).strip())
        discovered_scopes = resource_metadata.get("scopes_supported")
        if configured_scopes:
            scopes = configured_scopes
        elif isinstance(discovered_scopes, list):
            scopes = tuple(str(item).strip() for item in discovered_scopes if str(item).strip())
        else:
            scopes = GITHUB_DEFAULT_SCOPES
        return authorization_endpoint, token_endpoint, authorization_server, scopes[:50]

    async def begin_authorization(
        self,
        server_key: str,
        server_config: MCPServerConfig,
        *,
        external_base_url: str,
    ) -> dict[str, Any]:
        oauth = server_config.oauth
        if oauth is None or not oauth.enabled:
            raise MCPOAuthError("oauth_not_configured")
        client_id = oauth.client_id.strip()
        if not client_id:
            raise MCPOAuthError("oauth_client_id_missing")
        authorization_endpoint, token_endpoint, authorization_server, scopes = await self._discover_endpoints(server_config)
        redirect_uri = oauth.redirect_uri.strip()
        if not redirect_uri:
            base = external_base_url.rstrip("/")
            redirect_uri = f"{base}/api/mcp/oauth/callback/{quote(server_key, safe='')}"
        parsed_redirect = urlparse(redirect_uri)
        if parsed_redirect.scheme not in {"http", "https"} or not parsed_redirect.netloc:
            raise MCPOAuthError("oauth_redirect_uri_invalid")
        if parsed_redirect.scheme != "https" and parsed_redirect.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise MCPOAuthError("oauth_redirect_uri_must_use_https")

        verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        state = secrets.token_urlsafe(32)
        now = int(time.time())
        self._pending[state] = _PendingAuthorization(
            server_key=server_key,
            code_verifier=verifier,
            redirect_uri=redirect_uri,
            client_id=client_id,
            token_endpoint=token_endpoint,
            scopes=scopes,
            created_at=now,
        )
        self._pending = {
            key: item for key, item in self._pending.items() if now - item.created_at <= _STATE_TTL_SECONDS
        }
        query = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        separator = "&" if "?" in authorization_endpoint else "?"
        return {
            "authorizationUrl": authorization_endpoint + separator + urlencode(query),
            "redirectUri": redirect_uri,
            "scopes": list(scopes),
            "authorizationServer": authorization_server,
        }

    async def complete_authorization(
        self,
        server_key: str,
        server_config: MCPServerConfig,
        params: dict[str, str],
    ) -> dict[str, Any]:
        state = str(params.get("state") or "").strip()
        pending = self._pending.pop(state, None)
        if pending is None or pending.server_key != server_key:
            raise MCPOAuthError("oauth_state_invalid_or_expired")
        if int(time.time()) - pending.created_at > _STATE_TTL_SECONDS:
            raise MCPOAuthError("oauth_state_invalid_or_expired")
        if params.get("error"):
            raise MCPOAuthError("oauth_authorization_denied")
        code = str(params.get("code") or "").strip()
        if not code:
            raise MCPOAuthError("oauth_code_missing")
        oauth = server_config.oauth
        if oauth is None:
            raise MCPOAuthError("oauth_not_configured")
        form = {
            "grant_type": "authorization_code",
            "client_id": pending.client_id,
            "code": code,
            "redirect_uri": pending.redirect_uri,
            "code_verifier": pending.code_verifier,
        }
        client_secret = self._secret(oauth)
        if client_secret:
            form["client_secret"] = client_secret
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                response = await client.post(
                    pending.token_endpoint,
                    data=form,
                    headers={"Accept": "application/json"},
                )
        except httpx.HTTPError as exc:
            raise MCPOAuthError("oauth_token_exchange_failed") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {key: values[0] for key, values in parse_qs(response.text).items() if values}
        if response.status_code >= 400 or not isinstance(payload, dict) or not payload.get("access_token"):
            error_code = str(payload.get("error") or "token_exchange_rejected") if isinstance(payload, dict) else "token_exchange_rejected"
            raise MCPOAuthError(f"oauth_token_exchange_rejected:{error_code[:80]}")
        expires_in = 0
        try:
            expires_in = max(0, int(payload.get("expires_in") or 0))
        except (TypeError, ValueError):
            expires_in = 0
        scope = str(payload.get("scope") or " ".join(pending.scopes)).strip()
        token = {
            "access_token": str(payload.get("access_token") or ""),
            "refresh_token": str(payload.get("refresh_token") or ""),
            "token_type": str(payload.get("token_type") or "Bearer"),
            "expires_at": int(time.time()) + expires_in if expires_in else 0,
            "scope": scope,
            "token_endpoint": pending.token_endpoint,
            "client_id": pending.client_id,
        }
        await self._save_token(server_key, token)
        return {
            "authorized": True,
            "expiresAt": int(token["expires_at"]),
            "scopes": [item for item in scope.split() if item][:50],
        }

    async def get_access_token(
        self,
        server_key: str,
        server_config: MCPServerConfig,
        *,
        force_refresh: bool = False,
    ) -> str:
        token = await self._load_token(server_key)
        if not token:
            return ""
        access_token = str(token.get("access_token") or "")
        refresh_token = str(token.get("refresh_token") or "")
        expires_at = int(token.get("expires_at") or 0)
        should_refresh = bool(refresh_token and (force_refresh or (expires_at and expires_at <= int(time.time()) + _REFRESH_SKEW_SECONDS)))
        if not should_refresh:
            return access_token
        oauth = server_config.oauth
        if oauth is None:
            return access_token
        token_endpoint = str(token.get("token_endpoint") or oauth.token_endpoint or "").strip()
        client_id = str(token.get("client_id") or oauth.client_id or "").strip()
        client_secret = self._secret(oauth)
        if not token_endpoint or not client_id:
            return access_token
        form = {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        if client_secret:
            form["client_secret"] = client_secret
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                response = await client.post(token_endpoint, data=form, headers={"Accept": "application/json"})
        except httpx.HTTPError:
            return access_token
        try:
            payload = response.json()
        except ValueError:
            payload = {key: values[0] for key, values in parse_qs(response.text).items() if values}
        if response.status_code >= 400 or not isinstance(payload, dict) or not payload.get("access_token"):
            return access_token
        try:
            expires_in = max(0, int(payload.get("expires_in") or 0))
        except (TypeError, ValueError):
            expires_in = 0
        refreshed = {
            **token,
            "access_token": str(payload.get("access_token") or access_token),
            "refresh_token": str(payload.get("refresh_token") or refresh_token),
            "token_type": str(payload.get("token_type") or token.get("token_type") or "Bearer"),
            "expires_at": int(time.time()) + expires_in if expires_in else 0,
            "scope": str(payload.get("scope") or token.get("scope") or ""),
            "token_endpoint": token_endpoint,
            "client_id": client_id,
        }
        await self._save_token(server_key, refreshed)
        log.info("mcp.oauth.token_refreshed", server=server_key)
        return str(refreshed["access_token"])

    async def provider_token(self, server_key: str, server_config: MCPServerConfig, force_refresh: bool = False) -> str:
        return await self.get_access_token(server_key, server_config, force_refresh=force_refresh)
