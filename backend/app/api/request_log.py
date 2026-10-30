"""
RequestLogMiddleware: one activity-log record (app/audit.py, category
"http") per API call and page load:

    {"event": "http.request", "user": "tech1", "ip": "127.0.0.1",
     "method": "POST", "path": "/incidents/faults/seed-003/claim",
     "status": 200, "duration_ms": 12.4, "message": "POST /incidents/... 200 (12 ms)"}

Left out, because they would bury everything else: static files (/css/,
/js/, /screenshots/, images, fonts) and GET /health, which every open tab
polls. The live-update stream (GET /events) is recorded when it opens.

The user is read from the session (this middleware sits INSIDE Starlette's
SessionMiddleware - see app/api/app.py): the user at the start of the
request, or the one who just logged in. The time is until the response
starts (for the live-update stream: until it is accepted). Request and
response bodies are never recorded; the query string is (it never holds
secrets - passwords and keys only travel in bodies and headers).

Levels: WARNING for 4xx answers, ERROR for 5xx, INFO otherwise.

It also sets app/audit.py's request_context (address and user), so every
other record made while handling the request - an action, an external
call - carries the same address.

A plain ASGI middleware (not BaseHTTPMiddleware), so streaming responses
pass through untouched.
"""

import logging
import time

from app.audit import audit, request_context

_SKIPPED_PREFIXES = ("/css/", "/js/", "/screenshots/")
_SKIPPED_PATHS = {"/health", "/favicon.ico"}
_SKIPPED_SUFFIXES = (".png", ".jpg", ".svg", ".ico", ".woff", ".woff2", ".map")


def is_logged(path: str) -> bool:
    return not (path in _SKIPPED_PATHS or path.startswith(_SKIPPED_PREFIXES) or path.endswith(_SKIPPED_SUFFIXES))


class RequestLogMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not is_logged(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        session = scope.get("session") or {}
        user_before = session.get("username")
        client = scope.get("client")
        token = request_context.set({"ip": client[0] if client else None, "user": user_before})

        async def send_and_log(message):
            if message["type"] == "http.response.start":
                status = message["status"]
                duration = round((time.perf_counter() - started) * 1000, 1)
                path = scope.get("path", "")
                query = scope.get("query_string", b"").decode("latin-1")
                user = (scope.get("session") or {}).get("username") or user_before
                level = logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
                audit("http.request", level=level, user=user, ip=client[0] if client else None,
                      method=scope.get("method"), path=path, query=query or None, status=status,
                      duration_ms=duration, message=f"{scope.get('method')} {path} {status} ({duration:g} ms)")
            await send(message)

        try:
            await self.app(scope, receive, send_and_log)
        finally:
            request_context.reset(token)
