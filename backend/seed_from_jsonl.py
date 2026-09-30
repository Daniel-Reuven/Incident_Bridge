"""
Trigger a LIVE import of synthetic incidents from data/sample_data.jsonl
into a RUNNING server, via the admin-only POST /incidents/import-jsonl
endpoint (see app/api/incidents.py's import_jsonl() and
app/repository.py's IncidentRepository.load_from_jsonl_lines() for what
actually does the reading/validation/conversion).

Requires the server to already be running (python serve_app.py, in
another terminal) - that's what makes this show up live, with no restart
needed: the import runs inside that already-running process, so every
newly-created incident is enqueued into its real in-memory
maintenance/fault queues and broadcast over SSE the moment this script's
request completes, exactly as if it had been created by hand through the
dashboard. (An earlier version of this script wrote directly to the
database from its own separate process instead - that worked, but the
already-running server had no way to know about it until restarted; this
version fixes that by going through the server itself, over HTTP, instead
of around it.)

Logs in as an admin using credentials read from the exact same
INCIDENT_BRIDGE_USERS your .env already configures the server with -
nothing hardcoded, nothing typed by hand.

Usage (from inside backend/, with the server already running elsewhere):
    python seed_from_jsonl.py                       # loads ../data/sample_data.jsonl
    python seed_from_jsonl.py path/to/other.jsonl    # loads a different file

Safe to run more than once against the same file: already-imported
records (matched by their 'id' field) are skipped, not duplicated or
overwritten - see load_from_jsonl_lines()'s docstring for the exact rules.
"""

import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

_HOST = os.environ.get("HOST", "127.0.0.1")
# The server can (and often should) bind to 0.0.0.0, but a client can't
# connect TO 0.0.0.0 - talk to localhost instead in that case (same fix
# already used by the project's old seed_demo_data.py).
_CONNECT_HOST = "127.0.0.1" if _HOST == "0.0.0.0" else _HOST
_PORT = os.environ.get("PORT", "8000")
BASE_URL = f"http://{_CONNECT_HOST}:{_PORT}"


def _find_admin_credentials():
    """
    Reads INCIDENT_BRIDGE_USERS - the same env var the server itself reads
    at startup (see app/repository.py's seed_users_from_env) - and returns
    the first admin's (username, password) so this script can log in the
    same way a person would. Falls back to the exact same demo admin the
    server falls back to when the env var isn't set, so this still works
    against a freshly-cloned project with no .env yet.
    """
    raw = os.environ.get("INCIDENT_BRIDGE_USERS")
    if not raw:
        return "admin", "Passw0rd1"
    users = json.loads(raw)
    admin = next((u for u in users if u["role"] == "admin"), None)
    if admin is None:
        sys.exit("INCIDENT_BRIDGE_USERS has no 'admin' entry - can't log in to import seed data.")
    return admin["username"], admin["password"]


def main() -> None:
    default_path = Path(__file__).resolve().parents[1] / "data" / "sample_data.jsonl"
    jsonl_path = Path(sys.argv[1]) if len(sys.argv) > 1 else default_path

    if not jsonl_path.is_file():
        sys.exit(
            f"No seed file found at {jsonl_path}.\n"
            f"Create data/sample_data.jsonl (one JSON object per line) first, "
            f"or pass a path: python seed_from_jsonl.py path/to/file.jsonl"
        )
    content = jsonl_path.read_text(encoding="utf-8")
    username, password = _find_admin_credentials()

    print(f"Connecting to {BASE_URL} ...")
    try:
        httpx.get(f"{BASE_URL}/health", timeout=3).raise_for_status()
    except httpx.ConnectError:
        sys.exit(
            f"Could not reach {BASE_URL}.\n"
            f"Start the server first, in another terminal:\n"
            f"    python serve_app.py\n"
            f"then run this script again."
        )

    with httpx.Client(base_url=BASE_URL) as client:
        login = client.post("/auth/login", json={"username": username, "password": password})
        login.raise_for_status()

        print(f"Importing {jsonl_path} as {username!r} ...")
        response = client.post("/incidents/import-jsonl", json={"content": content})
        response.raise_for_status()
        result = response.json()

    print(f"\nCreated:             {result['created']}")
    print(f"Skipped (duplicate): {len(result['skipped_duplicate_ids'])}")
    for incident_id in result["skipped_duplicate_ids"]:
        print(f"    - already exists: {incident_id}")
    print(f"Skipped (invalid):   {len(result['skipped_invalid'])}")
    for reason in result["skipped_invalid"]:
        print(f"    - {reason}")

    if result["created"]:
        print("\nDone - the dashboard should already show the new incidents, live, with no restart needed.")


if __name__ == "__main__":
    main()