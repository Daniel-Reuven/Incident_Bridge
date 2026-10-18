"""
Tests for the shared console log format (app/logs.py): every line - Uvicorn's
own, the HTTP access lines, the AI log and the app's notices - starts with
the level, then the time and a source tag.

Run from backend/:  python -m pytest tests/test_logs.py -v
"""

import logging
import re

import pytest

from app.logs import (LOGGING_CONFIG, ConsoleFormatter, apply_console_format, log_line)

TIME = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"


def record(name, level, msg, args=(), thread="MainThread", exc_info=None):
    rec = logging.LogRecord(name, level, __file__, 1, msg, args, exc_info)
    rec.threadName = thread
    return rec


def fmt(rec, colours=False):
    return ConsoleFormatter(use_colors=colours).format(rec)


def test_app_lines_start_with_the_level_then_the_time_and_source():
    line = fmt(record("uvicorn.error", logging.INFO, "Started server process [%d]", (8124,)))
    assert re.fullmatch(rf"INFO:     {TIME} APP  Started server process \[8124\]", line)


def test_the_level_is_padded_like_uvicorns():
    assert fmt(record("x", logging.WARNING, "w")).startswith("WARNING:  ")
    assert fmt(record("x", logging.ERROR, "e")).startswith("ERROR:    ")
    assert fmt(record("x", logging.DEBUG, "d")).startswith("DEBUG:    ")


def test_access_lines_are_tagged_http_and_get_the_status_text():
    rec = record("uvicorn.access", logging.INFO, '%s - "%s %s HTTP/%s" %d',
                 ("127.0.0.1:63317", "GET", "/auth/me", "1.1", 304))
    assert re.fullmatch(rf'INFO:     {TIME} HTTP 127\.0\.0\.1:63317 - "GET /auth/me HTTP/1\.1" 304 Not Modified',
                        fmt(rec))


def test_ai_lines_are_tagged_ai_and_show_the_thread():
    line = fmt(record("incident_bridge.ai", logging.WARNING, "gemini-3.8-flash is overloaded (503)",
                      thread="ai-analysis-worker"))
    assert re.fullmatch(rf"WARNING:  {TIME} AI   \[ai-analysis-worker\] gemini-3\.8-flash is overloaded \(503\)",
                        line)


def test_colours_only_wrap_the_level_name():
    line = fmt(record("x", logging.INFO, "hi"), colours=True)
    assert line.startswith("\033[32mINFO\033[0m:     ")
    assert "\033" not in fmt(record("x", logging.INFO, "hi"), colours=False)


def test_a_traceback_is_still_appended():
    try:
        raise KeyError("boom")
    except KeyError:
        import sys
        line = fmt(record("uvicorn.error", logging.ERROR, "Exception in ASGI application", exc_info=sys.exc_info()))
    assert line.splitlines()[0].endswith("APP  Exception in ASGI application")
    assert "KeyError: 'boom'" in line


@pytest.mark.parametrize("text, level, message", [
    ("WARNING: Seed import: sites seed file not found", "WARNING", "Seed import: sites seed file not found"),
    ("ERROR: bad settings", "ERROR", "bad settings"),
    ("NOTE: serving the API only", "INFO", "serving the API only"),
    ("Notification delivered (outbox): x -> 2 recipients", "INFO", "Notification delivered (outbox): x -> 2 recipients"),
])
def test_log_line_takes_the_level_from_the_prefix(app_log, text, level, message):
    log_line(text)
    assert [(r.levelname, r.getMessage()) for r in app_log.records] == [(level, message)]


def test_apply_console_format_reformats_uvicorns_handlers():
    logger = logging.getLogger("uvicorn.access")
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    try:
        apply_console_format()
        assert isinstance(handler.formatter, ConsoleFormatter)
    finally:
        logger.removeHandler(handler)


def test_uvicorn_accepts_the_logging_config():
    """serve_app.py passes LOGGING_CONFIG to uvicorn.run(); Uvicorn must be able to apply it."""
    uvicorn = pytest.importorskip("uvicorn")
    saved = {name: (logging.getLogger(name).handlers[:], logging.getLogger(name).propagate)
             for name in ("uvicorn", "uvicorn.error", "uvicorn.access")}
    try:
        uvicorn.Config("app.api.app:app", log_config=LOGGING_CONFIG)       # configures logging
        for name in ("uvicorn", "uvicorn.access"):
            assert all(isinstance(h.formatter, ConsoleFormatter) for h in logging.getLogger(name).handlers)
    finally:
        for name, (handlers, propagate) in saved.items():
            logger = logging.getLogger(name)
            logger.handlers[:] = handlers
            logger.propagate = propagate


def test_the_ai_logger_uses_the_shared_format():
    from app.ai.log import log
    assert log.handlers and all(isinstance(h.formatter, ConsoleFormatter) for h in log.handlers
                                if type(h) is logging.StreamHandler)
