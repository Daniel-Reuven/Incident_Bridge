"""
The admin-only activity log viewer's API (frontend/logs.html). Reads the
log files written by app/audit.py through app/log_reader.py.

    GET /logs/months   months that have records, newest first
    GET /logs/verify   the integrity check: the whole log (app/audit_integrity.py), and the
                       database against the fingerprints in the log (app/data_integrity.py)
    GET /logs          one month's records, newest first, filtered:
                       ?month=YYYY-MM (default: the newest) &category= &event= &user=
                       &level=INFO|WARNING|ERROR (that level and worse) &q=<text>
                       &limit=1..1000 (default 200) &offset=
"""

import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app import data_integrity, database
from app.api.deps import get_current_user
from app.audit import current_handler, signing_key
from app.audit_integrity import read_checkpoint, verify
from app.log_reader import available_months, iter_all_months, query
from app.models import Role, User
from app.state import database_path

router = APIRouter(prefix="/logs", tags=["logs"])

_MONTH = re.compile(r"^\d{4}-\d{2}$")


def get_log_admin(request: Request) -> User:
    user = get_current_user(request)
    if user.role != Role.ADMIN:
        raise HTTPException(status_code=403, detail="The activity log is available to admins only.")
    return user


def _source():
    """(folder, archive folder, cipher) of this installation's log, or None when no log is written."""
    handler = current_handler()
    return (handler.directory, handler.archive_dir, handler.cipher) if handler is not None else None


@router.get("/months")
def list_months(_admin: User = Depends(get_log_admin)):
    source = _source()
    return available_months(*source[:2]) if source else []


@router.get("/verify")
def verify_log(_admin: User = Depends(get_log_admin)):
    """
    Re-check every record's signature and the chain, against the checkpoint
    in the database. Reads every month, so it takes a moment on a big log.
    """
    handler, source = current_handler(), _source()
    if source is None:
        return {"ok": True, "records": 0, "problems": [], "problem_count": 0, "last_seq": 0, "checkpoint": None,
                "data": None, "detail": "No activity log is written (LOG_FILE_ENABLED=false)."}
    current = None
    if handler.chain is not None:
        handler.acquire()                    # no record is half-written while checking
        try:
            checkpoint = handler.chain.checkpoint()
            current = _database_snapshot()
        finally:
            handler.release()
    else:
        checkpoint = read_checkpoint(database_path())
        current = _database_snapshot()
    result = verify(iter_all_months(*source), signing_key(), checkpoint)
    if current is None:
        result["data"] = None
    else:
        records = (record for _, month in iter_all_months(*source) for record in month)
        result["data"] = data_integrity.check(records, current)
        result["ok"] = result["ok"] and result["data"]["ok"]
    return result


def _database_snapshot():
    """Every database record's fingerprint now (None for an in-memory database, which has no file to read)."""
    path = database_path()
    if path == ":memory:":
        return None
    conn = database.connect(path)
    try:
        return data_integrity.snapshot(conn)
    finally:
        conn.close()


@router.get("")
def read_log(month: Optional[str] = None, category: Optional[str] = None, event: Optional[str] = None,
             user: Optional[str] = None, level: Optional[str] = None, q: Optional[str] = None,
             limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0),
             _admin: User = Depends(get_log_admin)):
    source = _source()
    empty = {"month": None, "total": 0, "offset": 0, "limit": limit, "entries": [],
             "facets": {"category": {}, "event": {}, "user": {}, "level": {}}, "unreadable": 0}
    if source is None:
        return empty
    log_dir, archive_dir, cipher = source
    if month is None:
        months = available_months(log_dir, archive_dir)
        if not months:
            return empty
        month = months[0]["month"]
    if not _MONTH.match(month):
        raise ValueError("month must look like 2026-10")
    return query(log_dir, archive_dir, month, category=category or None, event=event or None,
                 user=user or None, level=level or None, text=q or None, limit=limit, offset=offset,
                 cipher=cipher)
