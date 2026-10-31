"""
HTTP-level security settings: response security headers and the session
cookie's settings (both installed in app/api/app.py).

SecurityHeadersMiddleware adds to every response:
    Content-Security-Policy  scripts, styles, images and API calls only from
                             this app (Google Fonts for the two web fonts),
                             no plugins, and no framing by other sites
    X-Frame-Options          DENY (older browsers' version of frame-ancestors)
    X-Content-Type-Options   nosniff
    Referrer-Policy          same-origin
    Permissions-Policy       camera, microphone and location switched off
    Cache-Control: no-cache  (only when the response sets no Cache-Control
                             of its own) - the browser may keep a copy but
                             must ask the server before using it; the static
                             files answer with ETag / Last-Modified, so an
                             unchanged file costs a tiny "304 Not Modified"
                             and a changed one (a new version) is downloaded
                             at once - no manual cache clearing after an
                             upgrade. Without it, browsers guess a lifetime
                             for the JS/CSS files and keep serving old ones.
HttpsOnlyRemoteMiddleware - "HTTPS only for remote connections" (the
Server settings page, app/server_settings.py): a request over plain HTTP
from another computer is redirected (308) to the HTTPS port; HTTP from this
computer itself stays open, so a broken certificate can never lock the admin
out. In production, HTTPS responses carry Strict-Transport-Security (not for
localhost, and not while testing - a browser would then refuse plain HTTP to
that host for a year).

The session cookie is marked Secure whenever the request came over HTTPS
(or always, with SESSION_COOKIE_SECURE=true), so a login made over HTTPS is
never sent back over plain HTTP.

FastAPI's own API docs (/docs, /redoc) load their page from a CDN with an
inline script, so they get every header except the CSP.

It is a plain ASGI middleware (not BaseHTTPMiddleware) so it never buffers
the long-lived live-update stream (GET /events).

The frontend has no inline <script> code, so 'unsafe-inline' is only
allowed for STYLES (a few pages use style="..." attributes).
"""

import ipaddress
import os
from typing import Tuple

from app import server_settings

CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])

_COMMON_HEADERS = [
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
]
_NO_CSP_PREFIXES = ("/docs", "/redoc")

DEFAULT_SESSION_MAX_AGE_HOURS = 12.0


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        add_csp = not scope.get("path", "").startswith(_NO_CSP_PREFIXES)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                extra = list(_COMMON_HEADERS)
                if add_csp:
                    extra.append((b"content-security-policy", CONTENT_SECURITY_POLICY.encode()))
                if b"cache-control" not in present:
                    extra.append((b"cache-control", b"no-cache"))
                headers.extend((name, value) for name, value in extra if name not in present)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


HSTS_HEADER = (b"strict-transport-security", b"max-age=31536000")


def is_loopback(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]").split("%")[0]).is_loopback
    except ValueError:
        return False


def _host_name(scope) -> str:
    for name, value in scope.get("headers", []):
        if name == b"host":
            host = value.decode("latin-1")
            return host[1:].split("]")[0] if host.startswith("[") else host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    server = scope.get("server") or ("localhost", None)
    return server[0]


class HttpsOnlyRemoteMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        running = server_settings.RUNNING
        if scope["type"] != "http" or not running.https_port:
            await self.app(scope, receive, send)
            return
        client = (scope.get("client") or ("", 0))[0]
        if scope.get("scheme") == "http" and running.https_only_remote and not is_loopback(client):
            host = _host_name(scope)
            host = f"[{host}]" if ":" in host else host
            port = "" if running.https_port == 443 else f":{running.https_port}"
            query = scope.get("query_string", b"").decode("latin-1")
            location = f"https://{host}{port}{scope.get('path', '/')}" + (f"?{query}" if query else "")
            await send({"type": "http.response.start", "status": 308,
                        "headers": [(b"location", location.encode("latin-1")), (b"content-length", b"0")]})
            await send({"type": "http.response.body", "body": b""})
            return
        if scope.get("scheme") == "https" and running.mode == "production" and not is_loopback(_host_name(scope)):
            async def send_with_hsts(message):
                if message["type"] == "http.response.start":
                    message = {**message, "headers": list(message.get("headers", [])) + [HSTS_HEADER]}
                await send(message)
            await self.app(scope, receive, send_with_hsts)
            return
        await self.app(scope, receive, send)


def session_settings_from_env() -> Tuple[int, bool]:
    """
    (max_age_seconds, https_only) for the session cookie, from
    SESSION_MAX_AGE_HOURS (default 12; a login lasts at most this long) and
    SESSION_COOKIE_SECURE (default false; set true when served over HTTPS,
    so the browser never sends the cookie over plain HTTP).
    Raises ValueError for a value that is not a positive number / true-false.
    """
    raw_hours = (os.environ.get("SESSION_MAX_AGE_HOURS") or "").strip()
    try:
        hours = float(raw_hours) if raw_hours else DEFAULT_SESSION_MAX_AGE_HOURS
    except ValueError:
        hours = -1.0
    if hours <= 0:
        raise ValueError(f"SESSION_MAX_AGE_HOURS must be a positive number of hours (got {raw_hours!r}).")
    raw_secure = (os.environ.get("SESSION_COOKIE_SECURE") or "false").strip().lower()
    if raw_secure not in ("true", "false", "1", "0", "yes", "no"):
        raise ValueError(f"SESSION_COOKIE_SECURE must be true or false (got {raw_secure!r}).")
    return int(hours * 3600), raw_secure in ("true", "1", "yes")


class SessionFromSecrets:
    """
    Starlette's SessionMiddleware (signed cookie), keyed with the
    installation's session secret - generated at setup and kept in the
    encrypted database (app/app_secrets.py), so it cannot be known when the
    app object is built. Before the setup there is no secret and no session
    (the setup page needs none; every other request is held back by
    app/api/setup.py's SetupGateMiddleware).
    """

    def __init__(self, app, **options):
        self.app = app
        self.options = options
        self._inner = {}                        # Secure flag -> SessionMiddleware
        self._built_for = None                  # (secret, max_age, always_secure) the inner ones were built with

    async def __call__(self, scope, receive, send):
        from starlette.middleware.sessions import SessionMiddleware
        from app import app_secrets
        secret = app_secrets.session_secret()
        if scope["type"] not in ("http", "websocket") or secret is None:
            await self.app(scope, receive, send)
            return
        # The lifetime and the Secure flag are the app's saved settings (app/app_config.py), in place once the
        # app has started - so they are read here, not when the app object is built.
        max_age, always_secure = session_settings_from_env()
        if (secret, max_age, always_secure) != self._built_for:
            self._inner = {secure: SessionMiddleware(self.app, secret_key=secret, https_only=secure, max_age=max_age,
                                                     **self.options)
                           for secure in (False, True)}
            self._built_for = (secret, max_age, always_secure)
        await self._inner[always_secure or scope.get("scheme") in ("https", "wss")](scope, receive, send)
