"""
The admin-only activity log viewer's API (frontend/logs.html). Reads the
log files written by app/audit.py through app/log_reader.py.

    GET /logs/months   months that have records, newest first
    GET /logs/verify   the integrity check of the whole log (app/audit_integrity.py)
    GET /logs          one month's records, newest first, filtered:
                       ?month=YYYY-MM (default: the newest) &category= &event= &user=
                       &level=INFO|WARNING|ERROR (that level and worse) &q=<text>
                       &limit=1..1000 (default 200) &offset=
"""

import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.deps import get_current_user
from app.audit import current_handler, log_dirs_from_env, signing_key
from app.audit_integrity import read_checkpoint, verify
from app.log_reader import available_months, iter_all_months, query
from app.state import database_path
from app.models import Role, User

router = APIRouter(prefix="/logs", tags=["logs"])

_MONTH = re.compile(r"^\d{4}-\d{2}$")


def get_log_admin(request: Request) -> User:
    user = get_current_user(request)
    if user.role != Role.ADMIN:
        raise HTTPException(status_code=403, detail="The activity log is available to admins only.")
    return user


def _dirs():
    log_dir, archive_dir, _enabled = log_dirs_from_env()
    return log_dir, archive_dir


@router.get("/months")
def list_months(_admin: User = Depends(get_log_admin)):
    return available_months(*_dirs())


@router.get("/verify")
def verify_log(_admin: User = Depends(get_log_admin)):
    """
    Re-check every record's signature and the chain, against the checkpoint
    in the database. Reads every month, so it takes a moment on a big log.
    """
    handler = current_handler()
    if handler is not None and handler.chain is not None:
        handler.acquire()                    # no record is half-written while checking
        try:
            checkpoint = handler.chain.checkpoint()
        finally:
            handler.release()
    else:
        checkpoint = read_checkpoint(database_path())
    return verify(iter_all_months(*_dirs()), signing_key(), checkpoint)


@router.get("")
def read_log(month: Optional[str] = None, category: Optional[str] = None, event: Optional[str] = None,
             user: Optional[str] = None, level: Optional[str] = None, q: Optional[str] = None,
             limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0),
             _admin: User = Depends(get_log_admin)):
    log_dir, archive_dir = _dirs()
    if month is None:
        months = available_months(log_dir, archive_dir)
        if not months:
            return {"month": None, "total": 0, "offset": 0, "limit": limit, "entries": [],
                    "facets": {"category": {}, "event": {}, "user": {}, "level": {}}, "unreadable": 0}
        month = months[0]["month"]
    if not _MONTH.match(month):
        raise ValueError("month must look like 2026-10")
    return query(log_dir, archive_dir, month, category=category or None, event=event or None,
                 user=user or None, level=level or None, text=q or None, limit=limit, offset=offset)
