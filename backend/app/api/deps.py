"""Shared FastAPI dependencies: current app state, current authenticated user, and current admin."""

from typing import Optional

from fastapi import HTTPException, Request

from app.events import EventBroadcaster
from app.models import Role, User
from app.state import AppState

# Keys inside the signed session cookie (see SessionMiddleware in app/api/app.py):
# the username, and the user's session_version at login (app/models/user.py) -
# a password change/reset or disabling the account bumps the version, which
# ends every session that still carries the old one.
SESSION_USERNAME_KEY = "username"
SESSION_VERSION_KEY = "sv"

PASSWORD_CHANGE_REQUIRED = "You must change your password before you can continue."


def get_state(request: Request) -> AppState:
    """The single AppState instance for this process (set at startup - see app/api/app.py)."""
    return request.app.state.incident_bridge


def get_broadcaster(request: Request) -> EventBroadcaster:
    """The single EventBroadcaster instance for this process (see app/events.py, app/api/app.py)."""
    return request.app.state.broadcaster


def get_analysis_worker(request: Request):
    """
    The single AnalysisWorker for this process (see app/ai/worker.py), or
    None when AI analysis is off (no API key for the selected provider).
    Set at startup by app/api/app.py's lifespan.
    """
    return getattr(request.app.state, "analysis_worker", None)


def get_client_id(request: Request) -> Optional[str]:
    """
    The browser tab's self-chosen id (see frontend/js/events.js), sent as
    a header on every mutating request so the broadcaster can skip
    notifying the very tab that caused the change. None for requests that
    don't send it (e.g. a bare curl/test call) - publish() treats that as
    "don't exclude anyone."
    """
    return request.headers.get("x-client-id")


def start_session(request: Request, user: User) -> None:
    """Log `user` in on this request's session (a fresh session, carrying their current session_version)."""
    request.session.clear()
    request.session[SESSION_USERNAME_KEY] = user.username
    request.session[SESSION_VERSION_KEY] = user.session_version


def get_session_user(request: Request) -> User:
    """
    The logged-in user, from the signed session cookie. Raises 401 (and
    clears the session) when there is no session, the user no longer
    exists, is disabled, or the session's version is older than the user's
    (their password was changed or reset elsewhere). Does NOT check
    must_change_password - only the endpoints a forced password change
    needs use this directly (GET /auth/me, POST /auth/change-password).
    """
    state = get_state(request)
    username = request.session.get(SESSION_USERNAME_KEY)
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    user = state.users.get(username)
    if user is None or not user.can_log_in or request.session.get(SESSION_VERSION_KEY) != user.session_version:
        request.session.clear()
        raise HTTPException(status_code=401, detail="Not authenticated.")
    return user


def get_current_user(request: Request) -> User:
    """
    get_session_user(), plus: a user who must change their password (first
    login, or after an admin reset) gets 403 for everything else until they
    do - the frontend opens the Settings dialog's Password tab for them.
    """
    user = get_session_user(request)
    if user.must_change_password:
        raise HTTPException(status_code=403, detail=PASSWORD_CHANGE_REQUIRED)
    return user


def get_current_admin(request: Request) -> User:
    """
    Like get_current_user (401 without a valid session), but additionally
    raises 403 unless that user is an admin. Used by every endpoint of the
    admin-only site portal (app/api/sites.py), so a non-admin can't reach
    any of it - not even read-only listings - whatever the frontend shows.
    The domain layer still checks the admin rule on every change as well.
    """
    user = get_current_user(request)
    if user.role != Role.ADMIN:
        raise HTTPException(status_code=403, detail="The site portal is available to admins only.")
    return user


def get_user_admin(request: Request) -> User:
    """Like get_current_admin, for the admin-only user management (app/api/users.py)."""
    user = get_current_user(request)
    if user.role != Role.ADMIN:
        raise HTTPException(status_code=403, detail="User management is available to admins only.")
    return user
