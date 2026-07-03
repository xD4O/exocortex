"""Local-trust guard for the operator web server (A2).

The UI is a lens for a browser on the operator's own machine. Two attacks are
reachable without this guard even when the server binds to 127.0.0.1:

  * Cross-site WebSocket hijack — WebSockets are exempt from the same-origin
    policy, so any page the operator visits can open ``ws://127.0.0.1:PORT``
    and stream the full audit-event feed.
  * CSRF on mutating routes — a malicious page can POST to state-changing
    endpoints (including ``/run``, which spawns real agent subprocesses).

Both are foiled by checking the ``Origin`` header: browsers always attach it on
cross-origin ``fetch`` / WebSocket, while same-origin loads and non-browser
clients (CLI, curl, the test client) send none. We therefore reject only
requests that carry a *non-loopback* Origin. An optional shared token adds a
second factor when the loopback assumption is too weak (e.g. a shared host).

Implemented as pure-ASGI middleware so it covers the ``websocket`` scope too —
Starlette's ``BaseHTTPMiddleware`` never sees WebSocket connections.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]

# Hostnames that identify the operator's own machine. Any port is fine — the
# threat is a *different origin*, not a different local port.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"}


def origin_is_allowed(origin: str, extra_allowed: set[str]) -> bool:
    """True if ``origin`` is loopback or explicitly allow-listed."""
    if origin in extra_allowed:
        return True
    try:
        host = urlsplit(origin).hostname
    except ValueError:
        return False
    return host in _LOOPBACK_HOSTS


class LocalGuardMiddleware:
    """Reject cross-origin browser traffic; optionally require a token."""

    def __init__(
        self,
        app: Callable[[Scope, Receive, Send], Awaitable[None]],
        *,
        allowed_origins: set[str] | None = None,
        token: str = "",
    ) -> None:
        self.app = app
        self.allowed_origins = allowed_origins or set()
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        reason = self._rejection_reason(scope, headers)
        if reason is None:
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            await self._reject_ws(receive, send)
        else:
            await self._reject_http(send, reason)

    def _rejection_reason(self, scope: Scope, headers: dict[str, str]) -> str | None:
        origin = headers.get("origin")
        if origin is not None and not origin_is_allowed(origin, self.allowed_origins):
            return "cross-origin request rejected"
        if self.token:
            provided = headers.get("x-exocortex-token") or self._query_token(scope)
            if provided != self.token:
                return "missing or invalid token"
        return None

    @staticmethod
    def _query_token(scope: Scope) -> str | None:
        raw = scope.get("query_string", b"")
        if not raw:
            return None
        values = parse_qs(raw.decode("latin-1")).get("token")
        return values[0] if values else None

    @staticmethod
    async def _reject_http(send: Send, reason: str) -> None:
        body = b'{"error":"forbidden","detail":"%s"}' % reason.encode("latin-1")
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _reject_ws(receive: Receive, send: Send) -> None:
        # Consume the initial connect, then close the handshake with a
        # policy-violation code without ever accepting it.
        await receive()
        await send({"type": "websocket.close", "code": 1008})
