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
.env), so the same script works unchanged locally and on a host like
Render, which assigns the listening port via its own PORT variable.

Local dev (values from .env, falls back to the defaults below if unset):
    python serve_app.py

Production / Render (auto-reload off; Render sets PORT itself):
    RELOAD=false python serve_app.py
"""

import os

from dotenv import load_dotenv

# Loads backend/.env (if present) into os.environ. Does NOT override a
# variable that's already set in the real environment (e.g. one Render
# injects directly) - .env is a local-dev convenience, not an authority
# over real deployment secrets.
load_dotenv()

import uvicorn  # noqa: E402 - must come after load_dotenv() so PORT etc. are already set

if __name__ == "__main__":
    uvicorn.run(
        "app.api.app:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", 8000)),
        reload=os.environ.get("RELOAD", "true").lower() == "true",
    )
