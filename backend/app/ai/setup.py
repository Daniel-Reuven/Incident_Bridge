"""
Wiring for the AI analysis feature - the only two functions the rest of the
app calls:

- check_ai_settings(): validates the AI settings and that the selected
  provider's SDK is installed, WITHOUT starting anything. serve_app.py
  calls it before starting the server, so a mistake stops the app with one
  clear message.
- create_analysis_worker(state, broadcaster): called once at startup by
  app/api/app.py's lifespan. Returns a started AnalysisWorker, or None when
  the feature is off (the selected provider's API key is not set).
"""

from typing import Optional

from app.ai.config import AIConfig
from app.ai.job_store import SqliteAnalysisJobStore
from app.ai.providers import build_provider
from app.ai.worker import AnalysisWorker
from app.events import EventBroadcaster


def check_ai_settings() -> Optional[AIConfig]:
    """The settings if the feature is on, None if it is off. Raises ValueError for invalid settings."""
    config = AIConfig.from_env()
    if config is not None:
        build_provider(config)        # raises ValueError if the SDK is not installed
    return config


def create_analysis_worker(state, broadcaster: EventBroadcaster, db_path: str) -> Optional[AnalysisWorker]:
    """Build and start the worker (restoring queued jobs from `db_path`), or return None if the feature is off."""
    config = AIConfig.from_env()
    if config is None:
        print(f"NOTE: {AIConfig.off_reason()}")
        return None
    provider = build_provider(config)
    worker = AnalysisWorker(state, provider, SqliteAnalysisJobStore(db_path), broadcaster, config.delay_seconds)
    worker.start()
    print(f"AI analysis is on: {provider.label()}, timeout {config.timeout_seconds:g}s per call, "
          f"{config.delay_seconds:g}s between requests.")
    return worker
