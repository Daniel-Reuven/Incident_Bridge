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

Logs in as an admin. Without --username it first tries the first admin of
INCIDENT_BRIDGE_USERS (the server's first-start users, from your .env).
Those passwords stop working once the admin has changed theirs (the app
asks for that at the first login) - then the script asks for the current
password, up to 3 times. With --username it asks straight away.

Usage (from inside backend/, with the server already running elsewhere):
    python seed_from_jsonl.py                        # loads ../data/sample_data.jsonl
    python seed_from_jsonl.py path/to/other.jsonl    # loads a different file
    python seed_from_jsonl.py --username boss        # log in as another admin

Safe to run more than once against the same file: already-imported
records (matched by their 'id' field) are skipped, not duplicated or
overwritten - see load_from_jsonl_lines()'s docstring for the exact rules.
"""

import argparse
import getpass
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
        return "admin", "Demo#Pass1"
    users = json.loads(raw)
    admin = next((u for u in users if u["role"] == "admin"), None)
    if admin is None:
        sys.exit("INCIDENT_BRIDGE_USERS has no 'admin' entry - can't log in to import seed data.")
    return admin["username"], admin["password"]


MAX_PASSWORD_ATTEMPTS = 3


def _ask_password(username: str) -> str:
    """The admin's password, typed at the terminal (shown as * where Python supports it, else hidden)."""
    prompt = f"Password for {username!r} (then press Enter): "
    try:
        return getpass.getpass(prompt, echo_char="*")          # Python 3.14+
    except TypeError:
        print("(typing is hidden - nothing appears while you type)")
        return getpass.getpass(prompt)


def _login(client: httpx.Client, username: str, password, ask_first: bool) -> None:
    """
    Log `client` in as `username`. `password` is tried first unless
    `ask_first`; after a wrong password the user is asked (up to
    MAX_PASSWORD_ATTEMPTS times). Exits with a clear message on failure.
    """
    attempts = 0
    if ask_first or password is None:
        password, attempts = _ask_password(username), 1
    while True:
        response = client.post("/auth/login", json={"username": username, "password": password})
        if response.status_code == 200:
            if response.json().get("must_change_password"):
                sys.exit(f"{username!r} must change the password first - log in to the app once "
                         f"(http://{_CONNECT_HOST}:{_PORT}/), then run this again.")
            return
        if response.status_code == 429:
            sys.exit(response.json().get("detail", "Too many failed logins - try again later."))
        if response.status_code != 401:
            response.raise_for_status()
        if attempts == 0:
            print(f"The password for {username!r} in INCIDENT_BRIDGE_USERS no longer works "
                  f"(it is only the first password - the app asks to change it at the first login).")
        elif attempts >= MAX_PASSWORD_ATTEMPTS:
            sys.exit("Login failed - giving up. Check the username (--username) and password.")
        else:
            print("Wrong username or password, try again.")
        password, attempts = _ask_password(username), attempts + 1


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Import a JSONL seed file into the RUNNING server.")
    parser.add_argument("path", nargs="?", help="the .jsonl file (default: ../data/sample_data.jsonl)")
    parser.add_argument("--username", help="log in as this admin (the password is asked for)")
    args = parser.parse_args(argv)

    default_path = Path(__file__).resolve().parents[1] / "data" / "sample_data.jsonl"
    jsonl_path = Path(args.path) if args.path else default_path

    if not jsonl_path.is_file():
        sys.exit(
            f"No seed file found at {jsonl_path}.\n"
            f"Create data/sample_data.jsonl (one JSON object per line) first, "
            f"or pass a path: python seed_from_jsonl.py path/to/file.jsonl"
        )
    content = jsonl_path.read_text(encoding="utf-8")
    if args.username:
        username, password = args.username, None
    else:
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
        _login(client, username, password, ask_first=bool(args.username))

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
