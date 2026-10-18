"""
The activity log: one structured record per thing that happened in the app,
written to log FILES (the console log in app/logs.py is separate and
unchanged). The admin's Logs page (frontend/logs.html, app/api/logs.py)
reads these files back with filters.

Format - JSON Lines: one JSON object per line, always with these keys:

    {"ts": "2026-10-18T09:12:03.512Z",   time, UTC, ISO 8601
     "level": "INFO",                    INFO | WARNING | ERROR
     "category": "incident",             what part of the app (see CATEGORIES)
     "event": "incident.claimed",        what happened: "<category>.<action>"
     "user": "tech1",                    who did it (null when nobody is logged in)
     "ip": "ip-3f9a1c0b7d2e",            the address of the request it came from, as LOG_IP_MODE
                                         allows (app/ip_privacy.py; null for the worker thread)
     "message": "tech1 claimed fault seed-003 \"Payment API is down\"",
     ...}                                event-specific fields: ids, status, durations...

so the file can be filtered by any field (the Logs page, `jq`, a
spreadsheet). What is recorded:

    http      every API call and page load: method, path, status, duration
              (static files and the /health polling are left out - noise)
    auth      logins (success, failure with the attempted username, lockout),
              logouts, own password changes and interface settings
    users     the admin's user management: created, role/disabled changed,
              password reset
    incident  every incident action: created, commented, claimed, started,
              closed, reopened, severity changed, AI analysis requested /
              cancelled / posted / failed
    site      every site-portal action (sites, mailing lists, notifications)
    external  every call this server makes to another service: the AI
              providers (provider, model, status, duration, outcome) and the
              site availability checks
    app       the server starting and stopping

What is NEVER recorded: passwords (typed, new or hashed), API keys, session
cookies, request or response bodies, incident descriptions and comment texts.
Incident and site titles are recorded, to make records readable.

Files (see MonthlyJsonlHandler): the current month is written to
    <LOG_DIR>/incident_bridge-YYYY-MM.jsonl          (months in UTC)
and when the month changes - or at startup, for any older month still lying
there - the finished month is zipped (ZIP_DEFLATED, best compression; JSON
logs typically shrink to ~10%) into
    <LOG_ARCHIVE_DIR>/incident_bridge-YYYY-MM.zip
and the plain file is removed. Archives are kept until someone deletes them.

Tamper evidence: every record is chained and signed (seq, prev, kid, hash)
and a checkpoint is kept in the database - see app/audit_integrity.py; the
Logs page's "Check integrity" runs the check.

Settings (backend/.env):
    LOG_DIR            default backend/logs   (relative paths are resolved against backend/)
    LOG_ARCHIVE_DIR    default <LOG_DIR>/archive
    LOG_FILE_ENABLED   default true - false writes no log files (the Logs page is then empty)
    LOG_SIGNING_KEY    the key that signs every record (default: SESSION_SECRET_KEY)
    LOG_IP_MODE        hashed (default) | masked | full | off - how client addresses are
                       written, here and on the console (app/ip_privacy.py)

Usage:
    from app.audit import audit
    audit("incident.claimed", user="tech1", incident_id="seed-003", message="...")

The records also go through the standard `logging` module (logger
"incident_bridge.audit", which does not propagate to the console), so a
test can capture them with caplog.
"""

import json
import logging
import os
import re
import threading
import zipfile
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

from app import ip_privacy
from app.audit_integrity import AuditChain

AUDIT_LOGGER = "incident_bridge.audit"
FILE_PREFIX = "incident_bridge"
BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_LOG_DIR = BACKEND_DIR / "logs"

CATEGORIES = ("http", "auth", "users", "incident", "site", "external", "app")
_FIELD_ORDER = ("ts", "level", "category", "event", "user", "ip", "message")
MONTH_FILE = re.compile(rf"^{FILE_PREFIX}-(\d{{4}}-\d{{2}})\.jsonl$")
MONTH_ARCHIVE = re.compile(rf"^{FILE_PREFIX}-(\d{{4}}-\d{{2}})\.zip$")

# The address and user of the HTTP request being handled (set by
# app/api/request_log.py), so records made deep inside an action - which do
# not know the request - still say where it came from. Context variables
# follow a request into the worker thread FastAPI runs it in.
request_context: ContextVar[Optional[dict]] = ContextVar("audit_request_context", default=None)

log = logging.getLogger(AUDIT_LOGGER)
log.setLevel(logging.INFO)
log.propagate = False                   # never printed on the console - it has its own log


def audit(event: str, *, level: int = logging.INFO, user: Optional[str] = None, ip: Optional[str] = None,
          message: str = "", **fields) -> None:
    """
    Record one event. `event` is "<category>.<action>" (the category is the
    part before the first dot). Extra keyword arguments become fields of the
    record; None values are left out. Never raises - logging must not break
    the action it records.
    """
    try:
        context = request_context.get() or {}
        ip = ip_privacy.current().apply(ip or context.get("ip"))     # hashed / masked / full / off
        user = user or context.get("user")
        record = {"category": event.split(".", 1)[0], "event": event, "user": user, "ip": ip,
                  "message": message, **{k: v for k, v in fields.items() if v is not None}}
        log.log(level, message or event, extra={"audit": record})
    except Exception:                    # noqa: BLE001 - see the docstring
        pass


class JsonLineFormatter(logging.Formatter):
    """One JSON object per record (the fields of audit(), plus ts and level)."""

    def to_dict(self, record: logging.LogRecord) -> dict:
        """The record as plain JSON data (round-tripped, so what is signed is exactly what is written)."""
        data = dict(getattr(record, "audit", None) or {"category": "app", "event": "app.log",
                                                        "message": record.getMessage()})
        data["ts"] = datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds") \
            .replace("+00:00", "Z")
        data["level"] = record.levelname
        ordered = {key: data.pop(key, None) for key in _FIELD_ORDER}
        ordered.update(data)
        return json.loads(json.dumps(ordered, ensure_ascii=False, default=str))

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(self.to_dict(record), ensure_ascii=False)


class MonthlyJsonlHandler(logging.Handler):
    """
    Writes to <directory>/incident_bridge-YYYY-MM.jsonl for the record's UTC
    month, switching files when the month changes; every finished month is
    zipped into `archive_dir` and its plain file removed (archive_finished()).
    Thread-safe (logging.Handler's lock). `clock` is injectable for tests.
    """

    def __init__(self, directory: Path, archive_dir: Path,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 chain: Optional[AuditChain] = None):
        """`chain`: signs every record and keeps the checkpoint (app/audit_integrity.py); None = unsigned."""
        super().__init__()
        self.directory = Path(directory)
        self.archive_dir = Path(archive_dir)
        self._clock = clock
        self.chain = chain
        self._month: Optional[str] = None
        self._stream = None
        self.directory.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.setFormatter(JsonLineFormatter())
        self.archive_finished()

    def path_for(self, month: str) -> Path:
        return self.directory / f"{FILE_PREFIX}-{month}.jsonl"

    def archive_path_for(self, month: str) -> Path:
        return self.archive_dir / f"{FILE_PREFIX}-{month}.zip"

    def current_month(self) -> str:
        return self._clock().strftime("%Y-%m")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            data = self.formatter.to_dict(record)
            month = self.current_month()
            if month != self._month:
                self._switch_to(month)
            if self.chain is not None:
                self.chain.sign_record(data, month)
            self._stream.write(json.dumps(data, ensure_ascii=False) + "\n")
            self._stream.flush()
        except Exception:                # noqa: BLE001 - logging's own convention
            self.handleError(record)

    def _switch_to(self, month: str) -> None:
        if self._stream is not None:
            self._stream.close()
        self._month = month
        self._stream = open(self.path_for(month), "a", encoding="utf-8")
        self.archive_finished()

    def archive_finished(self) -> List[str]:
        """
        Zip every month file older than the current month into archive_dir
        and delete the plain file - only after the zip is written and checked.
        If an archive for that month already exists (e.g. a server restarted
        mid-archive), the leftover lines are added to it as a second member.
        Returns the archived months.
        """
        current = self.current_month()
        archived = []
        for path in sorted(self.directory.glob(f"{FILE_PREFIX}-*.jsonl")):
            match = MONTH_FILE.match(path.name)
            if not match or match.group(1) >= current:
                continue
            month = match.group(1)
            target = self.archive_path_for(month)
            with zipfile.ZipFile(target, "a", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                member = path.name if path.name not in archive.namelist() else f"{path.stem}-{len(archive.namelist())}.jsonl"
                archive.write(path, arcname=member)
            with zipfile.ZipFile(target) as archive:
                if archive.testzip() is not None:
                    continue                 # keep the plain file if the zip is damaged
            path.unlink()
            archived.append(month)
        return archived

    def close(self) -> None:
        self.acquire()
        try:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
            if self.chain is not None:
                self.chain.close()
                self.chain = None
        finally:
            self.release()
        super().close()


def _resolve(raw: Optional[str], default: Path) -> Path:
    if not raw or not raw.strip():
        return default
    path = Path(raw.strip())
    return path if path.is_absolute() else BACKEND_DIR / path


def log_dirs_from_env():
    """(log_dir, archive_dir, enabled) from LOG_DIR, LOG_ARCHIVE_DIR and LOG_FILE_ENABLED."""
    log_dir = _resolve(os.environ.get("LOG_DIR"), DEFAULT_LOG_DIR)
    archive_dir = _resolve(os.environ.get("LOG_ARCHIVE_DIR"), log_dir / "archive")
    raw = (os.environ.get("LOG_FILE_ENABLED") or "true").strip().lower()
    if raw not in ("true", "false", "1", "0", "yes", "no"):
        raise ValueError(f"LOG_FILE_ENABLED must be true or false (got {raw!r}).")
    return log_dir, archive_dir, raw in ("true", "1", "yes")


_configure_lock = threading.Lock()


_DEV_SIGNING_KEY = "dev-only-insecure-log-signing-key"


def signing_key() -> bytes:
    """
    LOG_SIGNING_KEY, else SESSION_SECRET_KEY, else an insecure development
    default. Changing it later makes the older records show up as "signed
    with another key" in the integrity check - keep it once chosen.
    """
    for name in ("LOG_SIGNING_KEY", "SESSION_SECRET_KEY"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value.encode("utf-8")
    return _DEV_SIGNING_KEY.encode("utf-8")


def configure_audit_log() -> Optional[MonthlyJsonlHandler]:
    """
    (Re)attach the file handler from the settings - called at every app
    startup (app/api/app.py's lifespan). Replaces a handler attached before,
    so tests that start the app many times never stack handlers. Every record
    is signed and chained (app/audit_integrity.py), with the checkpoint in
    the app's database (DATABASE_PATH). Returns the handler, or None when
    LOG_FILE_ENABLED=false.
    """
    from app.state import database_path          # here: app.state imports much of the app

    log_dir, archive_dir, enabled = log_dirs_from_env()
    ip_privacy.configure(ip_privacy.mode_from_env(), signing_key())    # a bad LOG_IP_MODE stops startup
    with _configure_lock:
        for handler in list(log.handlers):
            log.removeHandler(handler)
            handler.close()
        if not enabled:
            return None
        handler = MonthlyJsonlHandler(log_dir, archive_dir, chain=AuditChain(signing_key(), database_path()))
        log.addHandler(handler)
        return handler


def current_handler() -> Optional[MonthlyJsonlHandler]:
    """The attached file handler (the Logs page reads its directories), or None."""
    return next((h for h in log.handlers if isinstance(h, MonthlyJsonlHandler)), None)


# --- live-update events -> records (an observer of app/events.py's broadcaster) ---------

_INCIDENT_WORDS = {
    "created": "created", "commented": "commented on", "claimed": "claimed", "started": "started",
    "closed": "closed", "reopened": "reopened", "severity_changed": "changed the severity of",
    "ai_analysis_requested": "requested an AI analysis of", "ai_analysis_cancelled": "cancelled the AI analysis of",
    "ai_analysis_posted": "posted an AI analysis on", "ai_analysis_failed": "posted a failed AI analysis on",
}


def record_published_event(event: dict) -> None:
    """
    Turn one live-update event (app/events.py) into an activity record - every
    incident, AI-analysis and site-portal action already publishes exactly one
    such event, so this one listener logs them all. The AI queue snapshots
    (scope "ai_analysis") are not actions and are skipped.
    """
    scope = event.get("scope")
    if scope == "sites":
        action = event.get("action", "unknown")
        extra = {k: v for k, v in event.items() if k not in ("scope", "action", "actor", "subject")}
        audit(f"site.{action}", user=event.get("actor"), subject=event.get("subject"),
              message=f"{event.get('actor')} {action.replace('_', ' ')}: {event.get('subject')}", **extra)
    elif scope is None and "id" in event:
        action = event.get("action", "unknown")
        kind = "fault" if event.get("type") == "fault" else "maintenance task"
        failed = action == "ai_analysis_failed"
        audit(f"incident.{action}", level=logging.WARNING if failed else logging.INFO, user=event.get("actor"),
              incident_id=event["id"], incident_type=event.get("type"), title=event.get("title"),
              affected_ids=event.get("affected_ids"),
              message=f"{event.get('actor')} {_INCIDENT_WORDS.get(action, action)} {kind} {event['id']} "
                      f"\"{event.get('title', '')}\"")
