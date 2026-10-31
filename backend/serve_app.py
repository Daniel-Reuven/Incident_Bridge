"""
Run the Incident Bridge API (and the frontend it serves alongside it) with
plain `python serve_app.py`, instead of the uvicorn CLI.

Equivalent to:
    uvicorn app.api.app:app --reload

...with one important difference: this script also loads a .env file
first (via python-dotenv), which the bare uvicorn CLI does NOT do on its
own. That's why INCIDENT_BRIDGE_USERS / PORT / etc.
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

Before starting the server, the installation and (while the setup is still
to be done) INCIDENT_BRIDGE_USERS are validated: if something is unusable
(a database without its keys file, invalid JSON, an invalid entry, an admin
entry, a duplicate username), the script prints the problems and exits
with code 1 instead of starting.
The AI analysis settings (AI_PROVIDER and the selected provider's key and
model - see app/ai/config.py) are checked the same way.

HTTPS (app/server_settings.py, the admin's Server settings page): with
HTTPS on, two listeners run in this one process - HTTP on PORT and HTTPS on
HTTPS_PORT - sharing the one app (so auto-reload is off; restart by hand).
The certificate is checked before anything starts: in APP_MODE=production
it must be certificate files from a certificate authority, valid now, with
remote HTTP redirected to HTTPS; while testing the generated self-signed
certificate is made on the first start that needs it (app/security/tls.py).

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

import asyncio  # noqa: E402
import signal  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from pathlib import Path  # noqa: E402

import uvicorn  # noqa: E402 - must come after load_dotenv() so PORT etc. are already set

from app.ai.setup import check_ai_settings  # noqa: E402
from app.logs import LOGGING_CONFIG, app_log  # noqa: E402
from app.api.security import session_settings_from_env  # noqa: E402
from app.repository import UserProvisioningError, first_login_change_required, seed_users_from_env  # noqa: E402
from app.security.data_folders import check_markers  # noqa: E402
from app.archive_rules import request_archive_after_from_env  # noqa: E402
from app.sites import notification_archive_after_from_env  # noqa: E402
from app.security.installation import Installation, InstallationError  # noqa: E402
from app.settings_store import SqliteSettingsStore  # noqa: E402
from app.security.key_providers import choose_provider  # noqa: E402
from app import server_settings  # noqa: E402
from app.security import tls  # noqa: E402


def _check_users_or_exit() -> None:
    """
    Validate the settings BEFORE starting the server, so a bad value stops
    the app with just its clear message and exit code 1 - no traceback, and
    no auto-reloader left waiting:
      - the installation (app/security/installation.py): a database without
        its keys file is refused;
      - INCIDENT_BRIDGE_USERS (invalid JSON, a duplicate username, a password
        that fails the policy, an admin entry...) - only while the setup is
        still to be done, because only the setup page reads it;
      - KEY_PROVIDER, REQUIRE_PASSWORD_CHANGE_ON_FIRST_LOGIN,
        SESSION_MAX_AGE_HOURS, SESSION_COOKIE_SECURE, AI_REQUEST_ARCHIVE_DAYS
        and NOTIFICATION_ARCHIVE_DAYS.
    The app checks the same things again at startup, which also covers
    running uvicorn directly.
    """
    try:
        first_login_change_required()
        session_settings_from_env()
        request_archive_after_from_env()
        notification_archive_after_from_env()
        installation = Installation.from_env()
        status = installation.status()
        if status == "orphan":
            raise ValueError(f"The database {installation.db_path} has no keys file ({installation.keys_path}). Put "
                             f"the keys file back, or restore access with the recovery key: "
                             f"python manage_keys.py restore")
        if status == "new":
            choose_provider()
            seed_users_from_env(warn=False)
        if status == "installed":                # the folders must still be this installation's
            installation.unlock()
            settings = SqliteSettingsStore(installation.db_path)
            installed_id = (settings.get("installation") or {}).get("id")
            settings.close()
            problems = check_markers(installation.folders(), installed_id) if installed_id else []
            if problems:
                raise ValueError("The data folders do not match this installation:\n  - " + "\n  - ".join(problems))
    except (UserProvisioningError, ValueError, InstallationError) as exc:
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


class _MemorySettings(dict):
    """Stands in for the settings store before the setup (no database yet): get/set in memory."""

    def set(self, name, value):
        self[name] = value


def server_plan():
    """
    (mode, ServerSettings, (cert, key, password) or None) - the listeners to
    start, from .env and the Server settings page (the page wins). Raises
    ValueError when they cannot be used (a bad value, a production rule, an
    unusable certificate).
    """
    mode = server_settings.app_mode()
    installation = Installation.from_env()
    store = None
    if installation.status() == "installed":
        installation.unlock()
        store = SqliteSettingsStore(installation.db_path)
    try:
        settings = server_settings.effective((store.get("server") if store else None) or {})
        problems = server_settings.problems(settings, mode)
        if problems:
            raise ValueError("The server settings cannot be used:\n  - " + "\n  - ".join(problems))
        if not settings.https_enabled:
            return mode, settings, None
        if settings.certificate == "generated":
            if store is None:                   # before the setup: a throwaway one, outside the (empty) data folders
                return mode, settings, tls.ensure_generated(installation.db_path, _MemorySettings(),
                                                            Path(tempfile.mkdtemp(prefix="incident-bridge-tls-")))
            return mode, settings, tls.ensure_generated(installation.db_path, store)
        checked = tls.check_files(settings.cert_file, settings.key_file, settings.key_password, mode)
        if not checked["ok"]:
            raise ValueError("The HTTPS certificate cannot be used:\n  - " + "\n  - ".join(checked["problems"]))
        for warning in checked["warnings"]:
            app_log.warning(f"HTTPS certificate: {warning}")
        return mode, settings, (settings.cert_file, settings.key_file, settings.key_password)
    finally:
        if store is not None:
            store.close()


def _server_plan_or_exit():
    try:
        return server_plan()
    except (ValueError, InstallationError) as exc:
        app_log.error(str(exc))
        sys.exit(1)


def _proxy_options(settings) -> dict:
    """Forwarded headers (client address, http/https) are believed only from the trusted proxies."""
    if not settings.trusted_proxies:
        return {"proxy_headers": False}
    return {"proxy_headers": True, "forwarded_allow_ips": ",".join(settings.trusted_proxies)}


class _Server(uvicorn.Server):
    """Two of these share the process: the signals are handled once, for both (see _serve_together)."""

    @contextmanager
    def capture_signals(self):
        yield


async def _serve_together(servers) -> None:
    def stop(signum, frame):
        for server in servers:
            if server.should_exit and signum == signal.SIGINT:
                server.force_exit = True        # a second Ctrl+C
            server.should_exit = True
    for sig in (signal.SIGINT, signal.SIGTERM) + ((signal.SIGBREAK,) if hasattr(signal, "SIGBREAK") else ()):
        signal.signal(sig, stop)
    first = asyncio.create_task(servers[0].serve())       # runs the app's startup (lifespan)
    while not servers[0].started and not first.done():
        await asyncio.sleep(0.05)
    tasks = [first] + ([asyncio.create_task(server.serve()) for server in servers[1:]] if not first.done() else [])
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    stop(signal.SIGTERM, None)                            # one stopped: stop the other too
    await asyncio.gather(*tasks)


def _serve_https(mode, settings, certificate) -> None:
    from app.api.app import app
    cert, key, password = certificate
    common = dict(host=settings.host, timeout_graceful_shutdown=3, **_proxy_options(settings))
    http = uvicorn.Config(app, port=settings.http_port, log_config=LOGGING_CONFIG, **common)
    https = uvicorn.Config(app, port=settings.https_port, lifespan="off", log_config=None, ssl_certfile=str(cert),
                           ssl_keyfile=str(key), ssl_keyfile_password=password or None, **common)
    remote = "remote HTTP is redirected to HTTPS" if settings.https_only_remote else "HTTP stays open to everyone"
    app_log.info(f"HTTPS on port {settings.https_port}, HTTP on port {settings.http_port} ({remote}; {mode} mode). "
                 f"Auto-reload is off while HTTPS is on.")
    asyncio.run(_serve_together([_Server(http), _Server(https)]))


if __name__ == "__main__":
    _check_users_or_exit()
    _check_ai_settings_or_exit()
    mode, settings, certificate = _server_plan_or_exit()
    server_settings.set_running(settings, mode, tls.certificate_info(certificate[0])["fingerprint"] if certificate else None)
    if certificate:
        try:
            _serve_https(mode, settings, certificate)
        except KeyboardInterrupt:
            pass
        sys.exit(0)
    uvicorn.run(
        "app.api.app:app",
        host=settings.host,
        port=settings.http_port,
        **_proxy_options(settings),
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