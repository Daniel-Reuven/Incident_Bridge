"""
FastAPI application: session middleware, routers, and centralized error
mapping.

Run with (from inside backend/):
    uvicorn app.api.app:app --reload
"""
import mimetypes

# Windows can have broken or missing registry MIME-type entries for
# common static file types. When that happens, Starlette's StaticFiles
# (which relies on Python's mimetypes module) reports "text/plain" for
# .js files instead of a JS MIME type - and browsers refuse to execute
# <script type="module"> when the response's MIME type isn't
# JavaScript ("disallowed MIME type" error), which silently breaks the
# whole frontend (the submit handler in login.js, and everything else,
# never runs). Registering these explicitly overrides whatever (if
# anything) the OS-level registry says, on every platform, so this
# doesn't depend on a given machine's configuration.
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.ai.setup import create_analysis_worker
from app.api import analysis, auth, events, incidents, logs, sites, users
from app.api.request_log import RequestLogMiddleware
from app.audit import audit, configure_audit_log, record_published_event
from app.api.security import SecurityHeadersMiddleware, session_settings_from_env
from app.events import EventBroadcaster, install_shutdown_hook
from app.logs import app_log, apply_console_format
from app.version import app_version
from app.state import AppState, database_path


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Built once per process, on startup - see app/state.py for why this
    # must stay a single process (in-memory queues aren't shared across
    # workers).
    # The activity log files (app/audit.py) - configured first, so everything
    # after this is recorded. A bad LOG_FILE_ENABLED stops startup.
    configure_audit_log()
    app.state.incident_bridge = AppState.create()

    # The broadcaster is created here (not in AppState) because it needs
    # a running event loop to bind to (see app/events.py) - AppState.create()
    # is plain sync code with no loop available yet at that point.
    app.state.broadcaster = EventBroadcaster()
    app.state.broadcaster.bind_loop(asyncio.get_running_loop())
    # Every published event is an action someone took: record it (app/audit.py).
    app.state.broadcaster.add_listener(record_published_event)
    # Ctrl+C / SIGTERM first ends every open live-update stream, so the
    # server stops at once without a CancelledError traceback (see
    # app/events.py's install_shutdown_hook).
    install_shutdown_hook(app.state.broadcaster)

    # AI analysis (app/ai/): the one named worker thread and its persisted
    # queue, or None when the feature is off (no API key for the selected
    # provider - see app/ai/config.py). Created after the broadcaster, which
    # it publishes live updates through.
    app.state.analysis_worker = create_analysis_worker(
        app.state.incident_bridge, app.state.broadcaster, database_path()
    )

    audit("app.started", version=app_version(), message=f"Incident Bridge {app_version()} started")
    yield
    audit("app.stopped", message="Incident Bridge stopped")

    if app.state.analysis_worker is not None:
        app.state.analysis_worker.stop()
    app.state.broadcaster.close()   # any stream still open ends now


# One console format for every line (app/logs.py). serve_app.py already starts
# Uvicorn with it; this also covers the bare `uvicorn app.api.app:app` command.
apply_console_format()

app = FastAPI(title="Incident Bridge API", version=app_version(), lifespan=lifespan)

# --- session cookie (signed, not encrypted - don't put secrets in it) ---
_secret_key = os.environ.get("SESSION_SECRET_KEY")
if not _secret_key:
    app_log.warning(
        "SESSION_SECRET_KEY is not set - using an insecure default. "
        "Set it before deploying anywhere reachable by anyone else."
    )
    _secret_key = "dev-only-insecure-secret-change-me"
# Lifetime and the Secure flag come from SESSION_MAX_AGE_HOURS (default 12) and
# SESSION_COOKIE_SECURE (default false - set it to true behind HTTPS); see
# app/api/security.py. A bad value stops startup with a clear message.
_session_max_age, _session_secure = session_settings_from_env()
# Added BEFORE the session middleware, so it runs inside it and can read who
# made each request: one activity-log record per request (app/api/request_log.py).
app.add_middleware(RequestLogMiddleware)
app.add_middleware(SessionMiddleware, secret_key=_secret_key, same_site="lax",
                   max_age=_session_max_age, https_only=_session_secure)

# Security headers on every response (CSP, no framing, nosniff, referrer policy).
app.add_middleware(SecurityHeadersMiddleware)

# --- CORS: only needed if the frontend is served from a different origin ---
# than this API during development. The normal setup (frontend served by
# this same app, or both behind one reverse proxy) needs no CORS at all,
# so it's off by default rather than wildcarded.
_cors_origins = os.environ.get("ALLOWED_ORIGINS")
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in _cors_origins.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(auth.router)
app.include_router(users.router)   # admin-only user management
app.include_router(logs.router)    # admin-only activity log viewer
app.include_router(incidents.router)
app.include_router(events.router)
app.include_router(sites.router)   # admin-only site portal (Sites & Mailing Lists)
app.include_router(analysis.router)   # AI analysis of faults (app/ai/)


# --- centralized exception -> HTTP status mapping ---
# The domain layer (app/models, app/queues) raises plain Python exceptions
# for business-rule violations rather than HTTP-aware ones, so it stays
# usable outside a web context too (e.g. from demo.py). These handlers are
# the one place that translates them - see app/api/incidents.py and
# app/api/auth.py for where each one gets raised.

@app.exception_handler(PermissionError)
def _handle_permission_error(request: Request, exc: PermissionError):
    return JSONResponse(status_code=403, content={"detail": str(exc)})


@app.exception_handler(ValueError)
def _handle_value_error(request: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(RuntimeError)
def _handle_runtime_error(request: Request, exc: RuntimeError):
    # e.g. "a maintenance task is already in progress" - the request was
    # well-formed, but the current state doesn't allow it. 409 Conflict.
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(IndexError)
def _handle_index_error(request: Request, exc: IndexError):
    # e.g. "no pending maintenance tasks" / "no faults in the queue".
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(KeyError)
def _handle_key_error(request: Request, exc: KeyError):
    # IncidentRepository.get() raises this with a clear message when an id isn't found.
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.get("/health")
def health_check():
    """Liveness check, plus the app's version (app/version.py) - the page footer and About page show it."""
    return {"status": "ok", "version": app_version()}


# --- the README's screenshots, also used by the Guide page (frontend/guide.html).
# One copy, in docs/screenshots/ at the repository root, served at
# /screenshots/<file>.png. Mounted before the frontend's "/" catch-all below;
# skipped if the folder is not there.
_screenshots_dir = Path(__file__).resolve().parents[3] / "docs" / "screenshots"
if _screenshots_dir.is_dir():
    app.mount("/screenshots", StaticFiles(directory=_screenshots_dir), name="screenshots")


# --- serve the frontend, same-origin, so no CORS is needed in the normal
# setup (frontend + API served by this one app). This mount
# is added last and deliberately: StaticFiles bound to "/" only catches
# requests that didn't match any route registered above it (auth, incidents, sites,
# events, /health), so the API keeps working exactly as before - this just
# adds a fallback that serves frontend/index.html, dashboard.html,
# incident.html, and their css/js, for everything else. If the frontend/
# folder isn't present alongside backend/ (e.g. the API deployed on its
# own), this is skipped and only the API is served.
_frontend_dir = Path(__file__).resolve().parents[3] / "frontend"
if _frontend_dir.is_dir():
    app.mount("/", StaticFiles(directory=_frontend_dir, html=True), name="frontend")
else:
    app_log.info(f"No frontend/ directory found at {_frontend_dir} - serving the API only.")
