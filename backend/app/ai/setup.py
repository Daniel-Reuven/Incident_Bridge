"""
Wiring for the AI analysis feature - the only two functions the rest of the
app calls:

- check_ai_settings(): validates the AI settings. serve_app.py calls it
  before starting the server, so a mistake stops the app with one clear
  message.
- create_analysis_worker(state, broadcaster, db_path): called once at
  startup by app/api/app.py's lifespan. Returns an AnalysisWorker, or None
  when the feature is off (the selected provider's API key is not set).
  Either way it says so on the console (app/ai/log.py).

Start on demand (v0.4.5): the worker is created at startup but its thread
is NOT started and the provider is NOT created. The first analysis request
starts the thread, which then creates the provider and its HTTP client and
runs Gemini model discovery. Exception: jobs restored from SQLite start the
thread right away. See app/ai/worker.py and app/ai/providers/loader.py.
"""

from typing import Optional

from app.analysis_service import AnalysisStatsListener, SiteImpactListener
from app.ai.config import AIConfig
from app.ai.log import apply_log_level, log, log_level_from_env
from app.ai.job_store import SqliteAnalysisJobStore
from app.ai.providers import provider_loader
from app.ai.worker import AnalysisWorker
from app.events import EventBroadcaster


def check_ai_settings() -> Optional[AIConfig]:
    """The settings if the feature is on, None if it is off. Raises ValueError for invalid settings."""
    log_level_from_env()              # raises ValueError for a bad AI_LOG_LEVEL
    return AIConfig.from_env()


def create_analysis_worker(state, broadcaster: EventBroadcaster, db_path: str) -> Optional[AnalysisWorker]:
    """
    Create the worker (restoring queued jobs from `db_path`), or return None
    if the feature is off. Its thread starts on the first request - or now,
    if jobs were restored.
    """
    apply_log_level()                 # AI_LOG_LEVEL from .env (loaded by now)
    config = AIConfig.from_env()
    if config is None:
        log.info(AIConfig.off_reason())
        return None
    loader = provider_loader(config)  # name + models from the settings; provider created on first use
    log.info(f"AI analysis is on: {loader.label()}, timeout {config.timeout_seconds:g}s per call "
             f"- the worker starts on the first request")
    stats = AnalysisStatsListener()
    worker = AnalysisWorker(state, loader, SqliteAnalysisJobStore(db_path), broadcaster, config.delay_seconds,
                            config.cooldown_seconds, listeners=[SiteImpactListener(state.sites), stats])
    worker.stats = stats              # kept reachable for a future report (app.state.analysis_worker.stats)
    worker.start_if_restored()        # restored jobs should not wait for a new request
    return worker
