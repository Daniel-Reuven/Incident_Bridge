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

so it can be filtered by any field (the Logs page; `jq` or a spreadsheet on
an export - the files are encrypted). What is recorded:

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
    data      a fingerprint of every database record the app saves or
              deletes (kind, key, HMAC) - never its content (app/data_integrity.py)
    setup     the first-start setup being completed
    app       the server starting and stopping

What is NEVER recorded: passwords (typed, new or hashed), API keys, session
cookies, request or response bodies, incident descriptions and comment texts.
Incident and site titles are recorded, to make records readable.

Files (see MonthlyJsonlHandler), in a folder per installation (its id is
created at setup), so an earlier installation's log never shows: the
current month is written to
    <LOG_DIR>/<installation id>/incident_bridge-YYYY-MM.jsonl   (months in UTC)
and when the month changes - or at startup, for any older month still lying
there - the finished month is zipped (ZIP_DEFLATED, best compression; JSON
logs typically shrink to ~10%) and encrypted into
    <LOG_DIR>/<installation id>/archive/incident_bridge-YYYY-MM.zip.enc
and the month file is removed. Archives are kept until someone deletes them.

Encryption (app/log_crypto.py): every line of the current month is encrypted
on its own, archives as one blob, with a key derived from the database key.
`python manage_logs.py export` writes a readable copy.

Tamper evidence: every record is chained and signed (seq, prev, kid, hash)
and a checkpoint is kept in the database - see app/audit_integrity.py; the
stores also record a fingerprint of every record they save (category
"data" - app/data_integrity.py). The Logs page's "Check integrity" runs both
checks.

Settings (backend/.env):
    LOG_DIR            default <DATA_DIR>/logs (DATA_DIR default backend/instance;
                       relative paths are resolved against backend/)
    LOG_ARCHIVE_DIR    default <LOG_DIR>/archive
    LOG_FILE_ENABLED   default true - false writes no log files (the Logs page is then empty)
    (the key that signs every record is generated at setup and kept in the
    encrypted database - app/app_secrets.py)
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

from app import app_secrets, ip_privacy
from app.audit_integrity import AuditChain
from app.log_crypto import LogCipher

AUDIT_LOGGER = "incident_bridge.audit"
FILE_PREFIX = "incident_bridge"
BACKEND_DIR = Path(__file__).resolve().parents[1]


CATEGORIES = ("http", "auth", "users", "incident", "site", "external", "data", "setup", "server", "app")
_FIELD_ORDER = ("ts", "level", "category", "event", "user", "ip", "message")
MONTH_FILE = re.compile(rf"^{FILE_PREFIX}-(\d{{4}}-\d{{2}})\.jsonl$")
MONTH_ARCHIVE = re.compile(rf"^{FILE_PREFIX}-(\d{{4}}-\d{{2}})\.zip(\.enc)?$")

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
                 chain: Optional[AuditChain] = None, cipher: Optional[LogCipher] = None):
        """
        `chain`: signs every record and keeps the checkpoint (app/audit_integrity.py); None = unsigned.
        `cipher`: encrypts every line and archive (app/log_crypto.py) - always set by the running app;
        None = plain text (unit tests of the file handling).
        """
        super().__init__()
        self.directory = Path(directory)
        self.archive_dir = Path(archive_dir)
        self._clock = clock
        self.chain = chain
        self.cipher = cipher
        self._month: Optional[str] = None
        self._stream = None
        self.directory.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.setFormatter(JsonLineFormatter())
        self.archive_finished()

    def path_for(self, month: str) -> Path:
        return self.directory / f"{FILE_PREFIX}-{month}.jsonl"

    def archive_path_for(self, month: str) -> Path:
        return self.archive_dir / f"{FILE_PREFIX}-{month}.zip{'.enc' if self.cipher else ''}"

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
            line = json.dumps(data, ensure_ascii=False)
            if self.cipher is not None:
                line = self.cipher.encrypt_line(line, month)
            self._stream.write(line + "\n")
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
            if self.cipher is not None:
                if not self._archive_encrypted(path, target, month):
                    continue                 # keep the month file if the archive could not be checked
                path.unlink()
                archived.append(month)
                continue
            with zipfile.ZipFile(target, "a", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                member = path.name if path.name not in archive.namelist() else f"{path.stem}-{len(archive.namelist())}.jsonl"
                archive.write(path, arcname=member)
            with zipfile.ZipFile(target) as archive:
                if archive.testzip() is not None:
                    continue                 # keep the plain file if the zip is damaged
            path.unlink()
            archived.append(month)
        return archived

    def _archive_encrypted(self, path: Path, target: Path, month: str) -> bool:
        """
        Decrypt the month's lines, zip them (best compression), encrypt the zip
        as one blob, write it beside, check it opens again, then replace the
        archive. A line that does not decrypt is kept as it is (the integrity
        check reports it). Existing archive content is kept as earlier members.
        """
        import io
        members = []
        if target.exists():
            with zipfile.ZipFile(io.BytesIO(self.cipher.decrypt_archive(target.read_bytes(), month))) as old:
                members = [(name, old.read(name)) for name in old.namelist()]
        plain = []
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    plain.append(self.cipher.decrypt_line(raw, month))
                except ValueError:
                    plain.append(raw)
        name = path.name if all(n != path.name for n, _ in members) else f"{path.stem}-{len(members)}.jsonl"
        members.append((name, ("\n".join(plain) + "\n").encode("utf-8")))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for member, data in members:
                archive.writestr(member, data)
        blob = self.cipher.encrypt_archive(buffer.getvalue(), month)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_bytes(blob)
        try:
            with zipfile.ZipFile(io.BytesIO(self.cipher.decrypt_archive(temporary.read_bytes(), month))) as check:
                if check.testzip() is not None:
                    return False
        except (ValueError, zipfile.BadZipFile):
            return False
        os.replace(temporary, target)
        return True

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
    from app.security.data_folders import data_dir
    log_dir = _resolve(os.environ.get("LOG_DIR"), data_dir() / "logs")
    archive_dir = _resolve(os.environ.get("LOG_ARCHIVE_DIR"), log_dir / "archive")
    raw = (os.environ.get("LOG_FILE_ENABLED") or "true").strip().lower()
    if raw not in ("true", "false", "1", "0", "yes", "no"):
        raise ValueError(f"LOG_FILE_ENABLED must be true or false (got {raw!r}).")
    return log_dir, archive_dir, raw in ("true", "1", "yes")


_configure_lock = threading.Lock()


_DEV_SIGNING_KEY = "dev-only-insecure-log-signing-key"


def signing_key() -> bytes:
    """
    The installation's log signing key - generated at setup and kept in the
    encrypted database (app/app_secrets.py), never in a file. Before the
    setup (and in tests that write records without starting the app) a fixed
    development key, which the integrity check then reports as "another key".
    """
    return app_secrets.log_signing_key() or _DEV_SIGNING_KEY.encode("utf-8")


def configure_audit_log(installation_id: Optional[str] = None) -> Optional[MonthlyJsonlHandler]:
    """
    (Re)attach the file handler from the settings - called at every app
    startup (app/api/lifecycle.py). Replaces a handler attached before, so
    tests that start the app many times never stack handlers. Every record
    is signed and chained (app/audit_integrity.py), with the checkpoint in
    the app's database (DATABASE_PATH), and encrypted (app/log_crypto.py).
    The files go to a folder of their own per installation -
    <LOG_DIR>/<installation id>/ - so a new installation never shows (or
    mixes with) the log of an earlier one. Returns the handler, or None when
    LOG_FILE_ENABLED=false.
    """
    from app.state import database_path          # here: app.state imports much of the app

    log_dir, archive_dir, enabled = log_dirs_from_env()
    ip_privacy.configure(ip_privacy.mode_from_env(), signing_key())    # a bad LOG_IP_MODE stops startup
    if installation_id:
        log_dir, archive_dir = installation_log_dirs(installation_id)
    with _configure_lock:
        for handler in list(log.handlers):
            log.removeHandler(handler)
            handler.close()
        if not enabled:
            return None
        handler = MonthlyJsonlHandler(log_dir, archive_dir, chain=AuditChain(signing_key(), database_path()),
                                      cipher=LogCipher.for_database())
        log.addHandler(handler)
        return handler


def close_audit_log() -> None:
    """
    Detach and close the file handler - at app shutdown (app/api/lifecycle.py),
    so the month's file is not left open: Windows cannot replace or move an
    open file (manage_keys.py new-recovery-key, move_data.py).
    """
    with _configure_lock:
        for handler in list(log.handlers):
            if isinstance(handler, MonthlyJsonlHandler):
                log.removeHandler(handler)
                handler.close()


def installation_log_dirs(installation_id: str):
    """
    (folder, archive folder) of an installation's log (see configure_audit_log):
    <LOG_DIR>/<id>/ and <LOG_DIR>/<id>/archive/ - or <LOG_ARCHIVE_DIR>/<id>/ when that is set.
    """
    log_dir, archive_dir, _ = log_dirs_from_env()
    own = log_dir / installation_id
    configured = (os.environ.get("LOG_ARCHIVE_DIR") or "").strip()
    return own, (archive_dir / installation_id if configured else own / "archive")


def reencrypt_logs(directory: Path, archive_dir: Path, old: LogCipher, new: LogCipher) -> int:
    """
    Re-encrypt every log line and archive with `new` (after the database got
    a new key - manage_keys.py new-recovery-key). Returns how many files
    were rewritten. Lines that do not decrypt are kept as they are.
    """
    rewritten = 0
    for folder in (Path(directory), Path(archive_dir)):
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            month_file, month_archive = MONTH_FILE.match(path.name), MONTH_ARCHIVE.match(path.name)
            if month_file:
                month, lines = month_file.group(1), []
                for raw in path.read_text(encoding="utf-8").splitlines():
                    try:
                        lines.append(new.encrypt_line(old.decrypt_line(raw, month), month) if raw.strip() else raw)
                    except ValueError:
                        lines.append(raw)
                content = "\n".join(lines) + ("\n" if lines else "")
            elif month_archive and month_archive.group(2):
                month = month_archive.group(1)
                content = new.encrypt_archive(old.decrypt_archive(path.read_bytes(), month), month)
            else:
                continue
            temporary = path.with_name(path.name + ".tmp")
            if isinstance(content, str):
                temporary.write_text(content, encoding="utf-8")
            else:
                temporary.write_bytes(content)
            os.replace(temporary, path)
            rewritten += 1
    return rewritten


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
