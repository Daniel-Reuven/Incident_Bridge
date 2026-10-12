"""
Console logging for AI analysis: one logger, `incident_bridge.ai`, used by
every file in app/ai/.

Each line shows the time, the level and the THREAD that wrote it, so you can
tell a request (a FastAPI pool thread) from the worker (`ai-analysis-worker`):

    14:18:08 AI INFO    [AnyIO worker thread] Queued fault seed-075 "Online store checkout…" for admin - position 1 | queue: idle, 1 waiting (seed-075)
    14:18:08 AI INFO    [AnyIO worker thread] Worker thread ai-analysis-worker started (first request) - Gemini (models: gemini-3.8-flash > …), 5s pause between jobs | queue: idle, 1 waiting (seed-075)
    14:18:08 AI INFO    [ai-analysis-worker] Loaded the Gemini SDK and client in 0.6s
    14:18:08 AI INFO    [ai-analysis-worker] Started fault seed-075 "Online store checkout…" (requested by admin, waited 0.0s) - 8 comments, 3,171 characters of data | queue: running seed-075, 0 waiting
    14:18:08 AI INFO    [ai-analysis-worker] Asking Gemini gemini-3.8-flash (model 1 of 4)
    14:18:08 AI INFO    [AnyIO worker thread] Refused a request for fault seed-076 by tech1 - an analysis is already pending
    14:18:08 AI INFO    [AnyIO worker thread] Cancelled fault seed-079 by admin - it was waiting at position 2 | queue: running seed-075, 1 waiting (seed-076)
    14:18:10 AI WARNING [ai-analysis-worker] gemini-3.8-flash is overloaded (503) after 2.0s - trying gemini-3.6-flash
    14:18:10 AI INFO    [ai-analysis-worker] Asking Gemini gemini-3.6-flash (model 2 of 4)
    14:18:12 AI INFO    [ai-analysis-worker] Answered by gemini-3.6-flash in 2.0s (3,904 characters)
    14:18:12 AI INFO    [ai-analysis-worker] Finished fault seed-075: posted 1 comment (4.0s) | queue: idle, 1 waiting (seed-076)
    14:18:12 AI DEBUG   [ai-analysis-worker] Pausing 5s before the next job
(each line also starts with the date).

The level comes from AI_LOG_LEVEL in backend/.env: INFO (default) shows
every step above; WARNING shows only problems (busy models, failed calls,
dropped jobs, a failed model refresh); DEBUG adds the pause between jobs;
OFF silences it. A bad value stops startup with a clear message.

The logger has its own console handler and does not pass lines on to the
root logger (propagate=False), so it prints the same way whether the app is
started with serve_app.py or uvicorn, and never twice.

Usage elsewhere in app/ai/:
    from app.ai.log import log
    log.info("...")
"""

import logging
import os
import sys

LOGGER_NAME = "incident_bridge.ai"
_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING,
           "ERROR": logging.ERROR, "OFF": logging.CRITICAL + 1}


def _build_logger() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:                     # once per process, however often this module is imported
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s AI %(levelname)-7s [%(threadName)s] %(message)s",
                                               datefmt="%Y-%m-%d %H:%M:%S"))
        logger.addHandler(handler)
        logger.propagate = False
    logger.setLevel(log_level_from_env())
    return logger


def log_level_from_env() -> int:
    """AI_LOG_LEVEL as a logging level. Raises ValueError for an unknown value (checked at startup)."""
    name = (os.environ.get("AI_LOG_LEVEL") or "INFO").strip().upper()
    if name not in _LEVELS:
        raise ValueError(f"AI_LOG_LEVEL={name!r} is not supported - use one of: {', '.join(_LEVELS)}.")
    return _LEVELS[name]


def apply_log_level() -> None:
    """Re-read AI_LOG_LEVEL (called at startup, after .env has been loaded)."""
    log.setLevel(log_level_from_env())


def short_title(title: str, limit: int = 40) -> str:
    """A fault title cut to `limit` characters, quoted - keeps log lines on one line."""
    return f'"{title if len(title) <= limit else title[:limit - 1] + "…"}"'


log = logging.getLogger(LOGGER_NAME)
try:
    _build_logger()
except ValueError:                              # a bad AI_LOG_LEVEL: reported properly at startup
    log.setLevel(logging.INFO)
