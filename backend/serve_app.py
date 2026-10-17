"""
Run the Incident Bridge API (and the frontend it serves alongside it) with
plain `python serve_app.py`, instead of the uvicorn CLI.

Equivalent to:
    uvicorn app.api.app:app --reload

...with one important difference: this script also loads a .env file
first (via python-dotenv), which the bare uvicorn CLI does NOT do on its
own. That's why INCIDENT_BRIDGE_USERS / SESSION_SECRET_KEY / PORT / etc.
in your .env were being silently ignored before - nothing was ever
reading that file into the environment. load_dotenv() below fixes that,
and does it early enough (before app.api.app is imported/run) that the
values are already in os.environ by the time the app reads them.

Host/port/reload are read from the environment (which now includes your
.env), so they can be changed without editing this file. The project runs
locally for now (see README.md, section 15).

Usual run (values from .env, falls back to the defaults below if unset):
    python serve_app.py

Without auto-reload (e.g. for a longer-running session):
    RELOAD=false python serve_app.py

Stopping the server (Ctrl+C) is immediate even with browser tabs open: the
app ends every open live-update stream first (app/events.py's
install_shutdown_hook), and the tabs reconnect automatically once the server
is running again. timeout_graceful_shutdown below is only a safety net.

Before starting the server, INCIDENT_BRIDGE_USERS is validated: if it is
set but unusable (invalid JSON, an invalid entry, a duplicate username),
the script prints the problems and exits with code 1 instead of starting.
The AI analysis settings (AI_PROVIDER and the selected provider's key and
model - see app/ai/config.py) are checked the same way.

Console output: every line - Uvicorn's, the HTTP access log, the AI log and
the app's notices - uses one format (app/logs.py), e.g.
    INFO:     2026-10-12 19:16:30 HTTP 127.0.0.1:63317 - "GET /auth/me HTTP/1.1" 200 OK
    INFO:     2026-10-12 19:16:31 AI   [ai-analysis-worker] Started fault seed-077 ...
"""

import os

from dotenv import load_dotenv

# Loads backend/.env (if present) into os.environ. Does NOT override a
# variable that's already set in the real environment (e.g. one exported
# in your shell) - .env is a convenience, not an authority over values
# set explicitly.
load_dotenv()

import sys  # noqa: E402

import uvicorn  # noqa: E402 - must come after load_dotenv() so PORT etc. are already set

from app.ai.setup import check_ai_settings  # noqa: E402
from app.logs import LOGGING_CONFIG, app_log  # noqa: E402
from app.api.security import session_settings_from_env  # noqa: E402
from app.persistence_users import SqliteUserStore  # noqa: E402
from app.repository import UserProvisioningError, first_login_change_required, seed_users_from_env  # noqa: E402
from app.state import database_path  # noqa: E402


def _check_users_or_exit() -> None:
    """
    Validate the user and session settings BEFORE starting the server, so a
    bad value stops the app with just its clear message and exit code 1 -
    no traceback, and no auto-reloader left waiting:
      - INCIDENT_BRIDGE_USERS (invalid JSON, a duplicate username, a
        password that fails the policy, ...) - only when the users table is
        still empty, because that is the only time it is used (see
        app/repository.py's load_users());
      - REQUIRE_PASSWORD_CHANGE_ON_FIRST_LOGIN, SESSION_MAX_AGE_HOURS and
        SESSION_COOKIE_SECURE.
    The app checks the same things again at startup, which also covers
    running uvicorn directly.
    """
    try:
        first_login_change_required()
        session_settings_from_env()
        users_db = SqliteUserStore(database_path())
        try:
            if users_db.count() == 0:
                seed_users_from_env(warn=False)
        finally:
            users_db.close()
    except (UserProvisioningError, ValueError) as exc:
        app_log.error(str(exc))
        sys.exit(1)


def _check_ai_settings_or_exit() -> None:
    """
    Validate the AI analysis settings BEFORE starting the server (an unknown
    AI_PROVIDER, a key without a model, a bad timeout/delay) - same idea as _check_users_or_exit(). The feature simply being
    off (no API key) is not an error.
    """
    try:
        check_ai_settings()
    except ValueError as exc:
        app_log.error(str(exc))
        sys.exit(1)


if __name__ == "__main__":
    _check_users_or_exit()
    _check_ai_settings_or_exit()
    uvicorn.run(
        "app.api.app:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", 8000)),
        reload=os.environ.get("RELOAD", "true").lower() == "true",
        # Safety net. Open browser tabs keep a live-update stream (GET /events)
        # open, and Uvicorn's graceful shutdown waits for every open
        # connection. Normally the app ends those streams itself the moment
        # Ctrl+C / SIGTERM arrives (app/events.py's install_shutdown_hook), so
        # this limit is never reached; if a stream somehow stays open, it is
        # cancelled after this many seconds instead of hanging forever.
        timeout_graceful_shutdown=3,
        # Every console line in one format - Uvicorn's own, the HTTP access
        # lines and the app's (see app/logs.py).
        log_config=LOGGING_CONFIG,
    )