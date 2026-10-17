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
FastAPI's own API docs (/docs, /redoc) load their page from a CDN with an
inline script, so they get every header except the CSP.

It is a plain ASGI middleware (not BaseHTTPMiddleware) so it never buffers
the long-lived live-update stream (GET /events).

The frontend has no inline <script> code, so 'unsafe-inline' is only
allowed for STYLES (a few pages use style="..." attributes).
"""

import os
from typing import Tuple

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
