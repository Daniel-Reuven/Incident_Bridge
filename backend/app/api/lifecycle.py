"""
Starting and stopping the app's services once the database is unlocked -
at startup for an installed app (app/api/app.py's lifespan), or the moment
the setup page finishes (app/api/setup.py). Until then only the setup page
works.
"""

import asyncio

from app import app_secrets
from app.ai import stored_settings
from app.ai.setup import create_analysis_worker
from app.audit import audit, configure_audit_log, record_published_event
from app.events import EventBroadcaster, install_shutdown_hook
from app.settings_store import SqliteSettingsStore
from app.state import AppState, database_path
from app.version import app_version


async def start_services(app, settings: SqliteSettingsStore) -> None:
    # Stored settings first: the AI settings saved at setup, and the app's own
    # secrets (session cookie, log signing key - app/app_secrets.py).
    stored_settings.use(settings.get("ai"))
    app_secrets.load_or_create(settings)
    app.state.settings = settings

    # The activity log files (app/audit.py) - configured first, so everything
    # after this is recorded. A bad LOG_FILE_ENABLED stops startup.
    configure_audit_log()
    # Built once per process - see app/state.py for why this must stay a
    # single process (in-memory queues aren't shared across workers).
    app.state.incident_bridge = AppState.create()

    # The broadcaster needs a running event loop to bind to (app/events.py).
    app.state.broadcaster = EventBroadcaster()
    app.state.broadcaster.bind_loop(asyncio.get_running_loop())
    # Every published event is an action someone took: record it (app/audit.py).
    app.state.broadcaster.add_listener(record_published_event)
    # Ctrl+C / SIGTERM first ends every open live-update stream (app/events.py).
    install_shutdown_hook(app.state.broadcaster)

    # AI analysis (app/ai/): the one named worker thread, or None when the
    # feature is off. Created after the broadcaster, which it publishes through.
    app.state.analysis_worker = create_analysis_worker(
        app.state.incident_bridge, app.state.broadcaster, database_path()
    )
    app.state.running = True
    audit("app.started", version=app_version(), message=f"Incident Bridge {app_version()} started")


def stop_services(app) -> None:
    if not getattr(app.state, "running", False):
        return
    audit("app.stopped", message="Incident Bridge stopped")
    if app.state.analysis_worker is not None:
        app.state.analysis_worker.stop()
    app.state.broadcaster.close()          # any stream still open ends now
    app.state.running = False
