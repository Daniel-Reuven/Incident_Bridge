"""
FastAPI application: session middleware, routers, and centralized error
mapping.

Run with (from inside backend/):
    uvicorn app.api.app:app --reload
"""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.api import auth, events, incidents
from app.events import EventBroadcaster
from app.state import AppState


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Built once per process, on startup - see app/state.py for why this
    # must stay a single process (in-memory queues aren't shared across
    # workers).
    app.state.incident_bridge = AppState.create()

    # The broadcaster is created here (not in AppState) because it needs
    # a running event loop to bind to (see app/events.py) - AppState.create()
    # is plain sync code with no loop available yet at that point.
    app.state.broadcaster = EventBroadcaster()
    app.state.broadcaster.bind_loop(asyncio.get_running_loop())

    yield


app = FastAPI(title="Incident Bridge API", version="0.1.0", lifespan=lifespan)

# --- session cookie (signed, not encrypted - don't put secrets in it) ---
_secret_key = os.environ.get("SESSION_SECRET_KEY")
if not _secret_key:
    print(
        "WARNING: SESSION_SECRET_KEY is not set - using an insecure default. "
        "Set it before deploying anywhere reachable by anyone else."
    )
    _secret_key = "dev-only-insecure-secret-change-me"
app.add_middleware(SessionMiddleware, secret_key=_secret_key, same_site="lax")

# --- CORS: only needed if the frontend is served from a different origin ---
# than this API during development. Same-origin deployment (frontend
# served by this same app, or reverse-proxied together on Render) needs
# no CORS at all, so it's off by default rather than wildcarded.
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
app.include_router(incidents.router)
app.include_router(events.router)


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
    return {"status": "ok"}


# --- serve the frontend, same-origin, so no CORS is needed for the normal
# deployment case (frontend + API as one Render web service). This mount
# is added last and deliberately: StaticFiles bound to "/" only catches
# requests that didn't match any route registered above it (auth, incidents,
# events, /health), so the API keeps working exactly as before - this just
# adds a fallback that serves frontend/index.html, dashboard.html,
# incident.html, and their css/js, for everything else. If the frontend/
# folder isn't present alongside backend/ (e.g. the API deployed on its
# own), this is skipped and only the API is served.
_frontend_dir = Path(__file__).resolve().parents[3] / "frontend"
if _frontend_dir.is_dir():
    app.mount("/", StaticFiles(directory=_frontend_dir, html=True), name="frontend")
else:
    print(f"NOTE: no frontend/ directory found at {_frontend_dir} - serving the API only.")
