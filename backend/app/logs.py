"""
One console log format for EVERY line the server prints - Uvicorn's own
messages, the HTTP access lines, the AI analysis log (app/ai/log.py) and the
app's own notices - so the console reads as one consistent stream:

    INFO:     2026-10-12 19:16:20 APP  Started server process [8124]
    WARNING:  2026-10-12 19:16:20 APP  Key store: the machine key is a plain file - ...
    INFO:     2026-10-12 19:16:30 HTTP 127.0.0.1:63317 - "GET /events?client_id=4721fdcf HTTP/1.1" 200 OK
    INFO:     2026-10-12 19:16:31 AI   [ai-analysis-worker] Started fault seed-077 "Nightly data warehouse…" ...
    WARNING:  2026-10-12 19:16:32 AI   [ai-analysis-worker] gemini-3.8-flash is overloaded (503) after 0.9s - trying ...

Each line is:  LEVEL:  (padded to 10, coloured on a terminal - Uvicorn's own
style), the date and time, a 4-letter SOURCE tag, then the message:
    APP   Uvicorn's server messages and the app's notices (app_log below)
    HTTP  one line per HTTP request (Uvicorn's access log), with the status text; the client
          address as LOG_IP_MODE allows (app/ip_privacy.py)
    AI    the AI analysis log, plus the THREAD that wrote it, so a request
          thread can be told from the worker (`ai-analysis-worker`)

How it is wired:
- serve_app.py passes LOGGING_CONFIG to uvicorn.run(), so Uvicorn uses
  ConsoleFormatter from its very first line (in the reloader process too).
- app/api/app.py calls apply_console_format() when it is imported, which
  re-formats Uvicorn's handlers - this covers starting the server with the
  bare `uvicorn app.api.app:app` command instead of serve_app.py.
- app/ai/log.py gives the AI logger a handler with ConsoleFormatter.
- The app's notices go through `app_log` (logger "incident_bridge") or
  log_line(), instead of print().

Usage:
    from app.logs import app_log, log_line
    app_log.warning("Something is not configured.")
    log_line("WARNING: Seed import: file not found")   # level taken from the prefix
"""

import logging
import sys
from http import HTTPStatus
from typing import Optional

from app import ip_privacy

DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LINE_FORMAT = "%(levelprefix)s %(asctime)s %(source)-4s %(message)s"

APP_LOGGER = "incident_bridge"
AI_LOGGER = "incident_bridge.ai"
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")

# Logger name -> source tag. Anything else is "APP".
_SOURCES = {"uvicorn.access": "HTTP", AI_LOGGER: "AI"}
# Loggers whose lines also show the thread that wrote them.
_WITH_THREAD = {AI_LOGGER}

# Uvicorn's level colours (ANSI), so our lines look exactly like its own.
_COLOURS = {logging.DEBUG: "\033[36m", logging.INFO: "\033[32m", logging.WARNING: "\033[33m",
            logging.ERROR: "\033[31m", logging.CRITICAL: "\033[91m"}
_RESET = "\033[0m"


class ConsoleFormatter(logging.Formatter):
    """
    The shared line format (see the module docstring). `use_colors`: colour
    the level - None (default) means "only when stderr is a terminal".
    Extra keyword arguments (e.g. `fmt` from a logging config) are ignored,
    so this can replace Uvicorn's formatters in a dictConfig.
    """

    def __init__(self, use_colors: Optional[bool] = None, **_ignored):
        super().__init__(LINE_FORMAT, DATE_FORMAT)
        self.use_colors = sys.stderr.isatty() if use_colors is None else use_colors

    def formatMessage(self, record: logging.LogRecord) -> str:
        level = f"{record.levelname}:".ljust(9)          # "INFO:    " - Uvicorn's padding
        if self.use_colors:
            colour = _COLOURS.get(record.levelno, "")
            level = level.replace(record.levelname, f"{colour}{record.levelname}{_RESET}", 1)
        message = record.message
        if record.name == "uvicorn.access":
            message = _with_status_text(record, _private_access_message(record, message))
        if record.name in _WITH_THREAD:
            message = f"[{record.threadName}] {message}"
        values = dict(record.__dict__, levelprefix=level, source=_SOURCES.get(record.name, "APP"), message=message)
        return self._style._fmt % values


def _private_access_message(record: logging.LogRecord, message: str) -> str:
    """
    Uvicorn's access line with the client address shown as LOG_IP_MODE allows
    (app/ip_privacy.py) - "127.0.0.1:63317" becomes e.g. "ip-3f9a1c0b7d2e";
    the port is kept only in "full" mode.
    """
    try:
        privacy = ip_privacy.current()
        if privacy.mode == "full" or not record.args:
            return message
        host = str(record.args[0]).rpartition(":")[0] or str(record.args[0])
        return record.msg % (privacy.apply(host) or "-", *record.args[1:])
    except Exception:                    # noqa: BLE001 - a log line must never fail
        return message


def _with_status_text(record: logging.LogRecord, message: str) -> str:
    """Uvicorn's access line ends with the status code; add its text, e.g. '200 OK' (as Uvicorn's own format does)."""
    try:
        return f"{message} {HTTPStatus(int(record.args[-1])).phrase}"
    except (TypeError, ValueError, IndexError, KeyError):
        return message


def console_handler() -> logging.Handler:
    """A stderr handler with ConsoleFormatter - for loggers that print on their own."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(ConsoleFormatter())
    return handler


def apply_console_format() -> None:
    """
    Give every handler of Uvicorn's loggers the shared format. Safe to call
    more than once; does nothing when Uvicorn is not running (tests).
    """
    for name in UVICORN_LOGGERS:
        for handler in logging.getLogger(name).handlers:
            if not isinstance(handler.formatter, ConsoleFormatter):
                handler.setFormatter(ConsoleFormatter())


def _build_app_log() -> logging.Logger:
    logger = logging.getLogger(APP_LOGGER)
    if not logger.handlers:                      # once per process
        logger.addHandler(console_handler())
        logger.propagate = False
        logger.setLevel(logging.INFO)
    return logger


# The app's own notices (startup warnings, seed import, notification outbox).
app_log = _build_app_log()

_PREFIXES = (("ERROR: ", logging.ERROR), ("WARNING: ", logging.WARNING), ("NOTE: ", logging.INFO))


def log_line(text: str) -> None:
    """
    Log one line through app_log. A leading "ERROR: ", "WARNING: " or "NOTE: "
    becomes the line's level (and is removed); anything else is INFO. Used as
    the default `log` callable where code takes one (seed import, outbox).
    """
    for prefix, level in _PREFIXES:
        if text.startswith(prefix):
            app_log.log(level, text[len(prefix):])
            return
    app_log.info(text)


# Uvicorn's logging configuration with ConsoleFormatter - passed to
# uvicorn.run(log_config=...) by serve_app.py. Same loggers and levels as
# Uvicorn's default config; only the formatters differ.
LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "console": {"()": "app.logs.ConsoleFormatter"},
    },
    "handlers": {
        "default": {"formatter": "console", "class": "logging.StreamHandler", "stream": "ext://sys.stderr"},
        "access": {"formatter": "console", "class": "logging.StreamHandler", "stream": "ext://sys.stdout"},
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"level": "INFO"},
        "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
    },
}


__all__ = ["ConsoleFormatter", "LOGGING_CONFIG", "app_log", "apply_console_format", "console_handler", "log_line"]
