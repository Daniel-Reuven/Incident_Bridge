"""
Console logging for AI analysis: one logger, `incident_bridge.ai`, used by
every file in app/ai/.

Lines use the server's shared console format (app/logs.py): the level
first, then the time, the source tag "AI", and the THREAD that wrote the
line, so you can tell a request (a FastAPI pool thread) from the worker
(`ai-analysis-worker`). The same layout as Uvicorn's own lines and the HTTP
access lines (dates trimmed here):

    INFO:     14:17:55 AI   [MainThread] AI analysis is on: Gemini (models: gemini-3.8-flash > …), timeout 180s per call - the worker starts on the first request
    INFO:     14:18:08 AI   [AnyIO worker thread] Worker thread ai-analysis-worker started (first request) - Gemini (models: …), 5s pause between jobs | queue: idle, 1 waiting (seed-075)
    INFO:     14:18:08 AI   [AnyIO worker thread] Queued fault seed-075 "Online store checkout…" for admin - position 1 | queue: idle, 1 waiting (seed-075)
    INFO:     14:18:08 AI   [ai-analysis-worker] Created the Gemini provider and its HTTP client in 0.0s
    INFO:     14:18:08 AI   [ai-analysis-worker] Started fault seed-075 "Online store checkout…" (requested by admin, waited 0.0s) - 8 comments, 3,171 characters of data | queue: running seed-075, 0 waiting
    INFO:     14:18:08 AI   [ai-analysis-worker] Asking Gemini gemini-3.8-flash (model 1 of 4)
    INFO:     14:18:08 AI   [AnyIO worker thread] Refused a request for fault seed-076 by tech1 - an analysis is already pending
    INFO:     14:18:08 AI   [AnyIO worker thread] Cancelled fault seed-079 by admin - it was waiting at position 2 | queue: running seed-075, 1 waiting (seed-076)
    WARNING:  14:18:10 AI   [ai-analysis-worker] gemini-3.8-flash is overloaded (503) after 2.0s - trying gemini-3.6-flash
    INFO:     14:18:10 AI   [ai-analysis-worker] Asking Gemini gemini-3.6-flash (model 2 of 4)
    INFO:     14:18:12 AI   [ai-analysis-worker] Answered by gemini-3.6-flash in 2.0s (3,904 characters)
    INFO:     14:18:12 AI   [ai-analysis-worker] Finished fault seed-075: posted 1 comment (4.0s) | queue: idle, 1 waiting (seed-076)
    DEBUG:    14:18:12 AI   [ai-analysis-worker] Pausing 5s before the next job
(each time also starts with the date, e.g. 2026-10-12 14:18:12).

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

from app.logs import AI_LOGGER, console_handler

LOGGER_NAME = AI_LOGGER
_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING,
           "ERROR": logging.ERROR, "OFF": logging.CRITICAL + 1}


def _build_logger() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:                     # once per process, however often this module is imported
        logger.addHandler(console_handler())     # the shared format (app/logs.py)
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
