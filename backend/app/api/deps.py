"""Shared FastAPI dependencies: current app state and current authenticated user."""

from typing import Optional

from fastapi import HTTPException, Request

from app.events import EventBroadcaster
from app.models import User
from app.state import AppState

# Key used inside the signed session cookie (see SessionMiddleware in app/api/app.py).
SESSION_USERNAME_KEY = "username"


def get_state(request: Request) -> AppState:
    """The single AppState instance for this process (set at startup - see app/api/app.py)."""
    return request.app.state.incident_bridge


def get_broadcaster(request: Request) -> EventBroadcaster:
    """The single EventBroadcaster instance for this process (see app/events.py, app/api/app.py)."""
    return request.app.state.broadcaster


def get_client_id(request: Request) -> Optional[str]:
    """
    The browser tab's self-chosen id (see frontend/js/events.js), sent as
    a header on every mutating request so the broadcaster can skip
    notifying the very tab that caused the change. None for requests that
    don't send it (e.g. a bare curl/test call) - publish() treats that as
    "don't exclude anyone."
    """
    return request.headers.get("x-client-id")


def get_current_user(request: Request) -> User:
    """
    Reads the signed session cookie for a username and looks it up in the
    current AppState's UserStore. Raises 401 if there's no valid session -
    either because the cookie is missing, or because it names a user that
    no longer exists (in which case the stale session is also cleared).
    """
    state = get_state(request)
    username = request.session.get(SESSION_USERNAME_KEY)
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    user = state.users.get(username)
    if user is None:
        request.session.clear()
        raise HTTPException(status_code=401, detail="Not authenticated.")
    return user
