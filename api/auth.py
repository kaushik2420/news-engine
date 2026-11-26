"""API-key auth for both REST and MCP.

Send the key as `Authorization: Bearer <key>` (preferred) or `X-API-Key: <key>`.
For MCP clients that cannot set headers (e.g. some connector UIs), `?key=<key>` on the /mcp URL also works.

ENGINE_API_KEY  -> scope "admin": everything
ENGINE_READ_KEY -> scope "read":  stories, feed, profiles/events, briefs (no source/webhook/admin changes)
If neither key is set the engine runs open (local development only) and logs a warning.
"""
from __future__ import annotations

import contextvars
import hmac
import logging
from urllib.parse import parse_qs

from engine import settings as S

log = logging.getLogger("engine.auth")
scope_var: contextvars.ContextVar[str] = contextvars.ContextVar("scope", default="none")
PUBLIC = {"/", "/health", "/docs", "/redoc", "/openapi.json", "/favicon.ico"}


def resolve_scope(key: str | None) -> str:
    cfg = S.settings
    if not cfg.api_key and not cfg.read_key:
        return "admin"
    if key and cfg.api_key and hmac.compare_digest(key, cfg.api_key):
        return "admin"
    if key and cfg.read_key and hmac.compare_digest(key, cfg.read_key):
        return "read"
    return "none"


class AuthMiddleware:
    def __init__(self, app):
        self.app = app
        if not S.settings.api_key:
            log.warning("ENGINE_API_KEY is not set: the API is OPEN. Set it before deploying.")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        key = None
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()
        key = key or headers.get("x-api-key")
        if not key and path.startswith("/mcp"):
            key = (parse_qs(scope.get("query_string", b"").decode()).get("key") or [None])[0]
        level = resolve_scope(key)
        if path not in PUBLIC and level == "none":
            body = b'{"detail":"missing or invalid API key"}'
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
            return await send({"type": "http.response.body", "body": body})
        token = scope_var.set(level)
        try:
            return await self.app(scope, receive, send)
        finally:
            scope_var.reset(token)


def require_admin() -> None:
    if scope_var.get() != "admin":
        raise PermissionError("this action needs the admin key (ENGINE_API_KEY)")
