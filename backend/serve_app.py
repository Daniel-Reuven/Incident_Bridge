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

from app.repository import UserProvisioningError, seed_users_from_env  # noqa: E402


def _check_users_or_exit() -> None:
    """
    Validate INCIDENT_BRIDGE_USERS BEFORE starting the server, so a bad
    value (invalid JSON, a duplicate username, a weak password, ...) stops
    the app with just its clear message and exit code 1 - no traceback, and
    no auto-reloader left waiting. The app checks the same thing again at
    startup (AppState.create()), which also covers running uvicorn directly.
    """
    try:
        seed_users_from_env(warn=False)
    except UserProvisioningError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _check_users_or_exit()
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
    )