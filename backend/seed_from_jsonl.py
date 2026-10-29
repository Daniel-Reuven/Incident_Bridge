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

Logs in as an admin - "admin", or the one named with --username - and asks
for the password (up to 3 times): admin passwords are never kept in .env.

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
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv


def _base_url() -> str:
    """
    The running server's address, from HOST and PORT (.env is loaded by
    main() - never at import, so importing this module, e.g. in the tests,
    does not pull backend/.env into the environment). The server can bind to
    0.0.0.0, but a client can't connect TO 0.0.0.0 - localhost is used then.
    """
    host = os.environ.get("HOST", "127.0.0.1")
    host = "127.0.0.1" if host == "0.0.0.0" else host
    return f"http://{host}:{os.environ.get('PORT', '8000')}"


def _find_admin_credentials():
    """
    The admin to log in as: "admin" (the usual name chosen on the setup
    page), with the password asked for - admin passwords are never kept in
    .env (the first admin is created on the setup page). --username picks
    another admin.
    """
    return "admin", None


MAX_PASSWORD_ATTEMPTS = 3
_warned_visible = False     # the "password will be visible" warning is shown once


def _ask_password(username: str) -> str:
    """
    The admin's password, typed at the terminal.

    In a real terminal window (PowerShell, cmd, Windows Terminal, a Linux or
    macOS shell) it is read with getpass, so the typing is hidden (shown as *
    on Python 3.14+). getpass on Windows reads straight from the console
    window - inside an IDE's Run window (PyCharm, VS Code's debug console...)
    that window is invisible, so the prompt never shows and the script waits
    forever for keys typed somewhere you cannot see. When there is no real
    terminal (sys.stdin is not a TTY), the password is read as normal input
    instead, after a warning that it will be visible.
    """
    prompt = f"Password for {username!r} (then press Enter): "
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        global _warned_visible
        if not _warned_visible:
            print("This console cannot hide what you type - the password WILL BE VISIBLE. "
                  "Run the script from a terminal window (e.g. PowerShell) to hide it.", flush=True)
            _warned_visible = True
        print(prompt, end="", flush=True)
        return sys.stdin.readline().rstrip("\r\n")
    sys.stdout.flush()
    try:
        return getpass.getpass(prompt, echo_char="*")          # Python 3.14+
    except TypeError:
        print("(typing is hidden - nothing appears while you type)", flush=True)
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
                         f"({client.base_url}), then run this again.")
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
    load_dotenv()
    base_url = _base_url()

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

    print(f"Connecting to {base_url} ...")
    try:
        httpx.get(f"{base_url}/health", timeout=3).raise_for_status()
    except httpx.ConnectError:
        sys.exit(
            f"Could not reach {base_url}.\n"
            f"Start the server first, in another terminal:\n"
            f"    python serve_app.py\n"
            f"then run this script again."
        )

    with httpx.Client(base_url=base_url) as client:
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
