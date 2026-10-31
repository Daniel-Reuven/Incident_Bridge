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

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api import ai_requests, analysis, auth, demo, events, incidents, logs, server, setup, sites, users
from app.api.deps import get_session_user
from app.api.lifecycle import start_services, stop_services
from app.api.request_log import RequestLogMiddleware
from app.api.security import (HttpsOnlyRemoteMiddleware, SecurityHeadersMiddleware, SessionFromSecrets,
                              session_settings_from_env)
from app.api.setup import SetupGateMiddleware, SetupSession
from app.logs import app_log, apply_console_format
from app.version import app_version
from app.models import Role
from app.persistence_users import SqliteUserStore
from app.security.data_folders import check_markers
from app.security.installation import Installation, InstallationError
from app.settings_store import SqliteSettingsStore
from app.state import database_path


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The installation's keys (app/security/installation.py): an installed app
    # unlocks its encrypted database by itself; a new one waits for the setup
    # page (app/api/setup.py) and starts its services when that is finished.
    app.state.setup = None
    app.state.running = False
    app.state.analysis_worker = None
    installation = Installation.from_env()
    app.state.installation = installation
    status = installation.status()
    if status == "orphan":
        raise InstallationError(
            f"The database {installation.db_path} has no keys file ({installation.keys_path}). Put the keys file "
            f"back, or restore access with the recovery key: python manage_keys.py restore")
    if status == "new":
        app.state.setup = SetupSession(installation)
    else:
        installation.unlock()
        warning = installation.machine_warning()
        if warning:
            app_log.warning(f"Key store: {warning}")
        settings = SqliteSettingsStore(database_path())
        installed_id = (settings.get("installation") or {}).get("id")
        if installed_id:
            problems = check_markers(installation.folders(), installed_id)
            if problems:
                raise InstallationError("The data folders do not match this installation:\n  - " + "\n  - ".join(problems))
        users_db = SqliteUserStore(database_path())
        has_admin = any(u.role == Role.ADMIN for u in users_db.load_all())
        users_db.close()
        if has_admin:
            await start_services(app, settings)
        else:                               # keys saved, but the setup never finished: accounts only
            app.state.setup = SetupSession(installation, settings)
    yield
    stop_services(app)


# One console format for every line (app/logs.py). serve_app.py already starts
# Uvicorn with it; this also covers the bare `uvicorn app.api.app:app` command.
apply_console_format()

app = FastAPI(title="Incident Bridge API", version=app_version(), lifespan=lifespan)

# --- session cookie (signed, not encrypted - don't put secrets in it) ---
# Keyed with the installation's session secret, generated at setup and kept in
# the encrypted database (app/app_secrets.py, app/api/security.py). Lifetime
# and the Secure flag come from SESSION_MAX_AGE_HOURS (default 12) and
# SESSION_COOKIE_SECURE (default false - set it to true behind HTTPS). A bad
# value stops startup with a clear message.
_session_max_age, _session_secure = session_settings_from_env()
# Added BEFORE the session middleware, so it runs inside it and can read who
# made each request: one activity-log record per request (app/api/request_log.py).
app.add_middleware(RequestLogMiddleware)
app.add_middleware(SessionFromSecrets, same_site="lax", max_age=_session_max_age, https_only=_session_secure)
# Until the setup is finished, only the setup page answers (app/api/setup.py).
app.add_middleware(SetupGateMiddleware)

# Security headers on every response (CSP, no framing, nosniff, referrer policy).
app.add_middleware(SecurityHeadersMiddleware)
# Outermost: remote plain HTTP -> HTTPS, when the Server settings say so (app/server_settings.py).
app.add_middleware(HttpsOnlyRemoteMiddleware)

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

app.include_router(setup.router)   # first-start setup (until it is done, nothing else answers)
app.include_router(auth.router)
app.include_router(users.router)   # admin-only user management
app.include_router(logs.router)    # admin-only activity log viewer
app.include_router(incidents.router)
app.include_router(events.router)
app.include_router(sites.router)   # admin-only site portal (Sites & Mailing Lists)
app.include_router(analysis.router)   # AI analysis of faults (app/ai/)
app.include_router(ai_requests.router)  # admin: the AI request history and archive (site portal)
app.include_router(server.router)     # admin-only Server settings (HTTPS, AI)
app.include_router(demo.router)       # the requirements demo page (any logged-in user)


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
    """Liveness check, the app's version (app/version.py - the page footer and About page show it) and whether
    the first-start setup is still waiting (app/api/setup.py)."""
    return {"status": "ok", "version": app_version(), "setup_required": getattr(app.state, "setup", None) is not None}


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
    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    def login_page(request: Request):
        """
        The login page - or, for someone already logged in, straight on to the
        dashboard, decided here before any HTML is sent. Left to the page's
        script, the login form showed for a moment first (a link to "/" from
        the Guide, About and Terms pages, or the address typed by hand).
        """
        try:
            get_session_user(request)
        except Exception:                      # no valid session (or no app state yet): the login page
            return FileResponse(_frontend_dir / "index.html", headers={"Cache-Control": "no-cache"})
        return RedirectResponse("/dashboard.html", status_code=303)

    app.mount("/", StaticFiles(directory=_frontend_dir, html=True), name="frontend")
else:
    app_log.info(f"No frontend/ directory found at {_frontend_dir} - serving the API only.")
