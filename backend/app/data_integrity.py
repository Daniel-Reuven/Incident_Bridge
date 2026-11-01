"""
Tamper evidence for the DATABASE, using the signed activity log
(app/audit.py, app/audit_integrity.py) as the witness.

Every time a store saves a record, it calls saved(conn, kind, key): the
record's rows are read back, hashed (HMAC-SHA256 with the log signing key,
over a canonical JSON of the rows) and written to the activity log as a
"data.saved" record - only the fingerprint, never the content. A deletion
made by the app is recorded as "data.deleted". The log itself is signed and
chained, so these fingerprints cannot be quietly changed either.

check() compares the database as it is now with the last fingerprint the
log holds for each record, and reports:

    changed       the record differs from what the app last saved (edited
                  outside the app, or rolled back to an older version)
    deleted       the log says it exists, the database does not have it
    unrecorded    the database has it, the app never saved it (added by hand)
    reappeared    the app deleted it, but it is back

A "record" (kind) is one logical thing, with all its rows:

    incident       incidents row + its comments
    user           users row
    site           sites row + its status history
    mailing_list   mailing_lists row + its members + its site links
    notification   notifications row
    setting        app_settings row (the app's secrets, the AI settings)
    ai_job         analysis_jobs row (the AI queue; deleted when done)
    ai_history     analysis_history row
"""

import hashlib
import hmac
import json
import logging
from typing import Dict, Iterable, List, Optional, Tuple

from app.audit import audit, signing_key

# kind -> (table holding the record's key, key column, [(SELECT for its rows - one "?" for the key)])
KINDS: Dict[str, Tuple[str, str, List[str]]] = {
    "incident": ("incidents", "id", ["SELECT * FROM incidents WHERE id = ?",
                                     "SELECT * FROM comments WHERE incident_id = ? ORDER BY id"]),
    "user": ("users", "username", ["SELECT * FROM users WHERE username = ?"]),
    "site": ("sites", "site_id", ["SELECT * FROM sites WHERE site_id = ?",
                                  "SELECT * FROM site_status_history WHERE site_id = ? ORDER BY seq"]),
    "mailing_list": ("mailing_lists", "list_id", [
        "SELECT * FROM mailing_lists WHERE list_id = ?",
        "SELECT * FROM mailing_list_members WHERE list_id = ? ORDER BY email",
        "SELECT * FROM mailing_list_sites WHERE list_id = ? ORDER BY site_id"]),
    "notification": ("notifications", "id", ["SELECT * FROM notifications WHERE id = ?"]),
    "setting": ("app_settings", "name", ["SELECT * FROM app_settings WHERE name = ?"]),
    "ai_job": ("analysis_jobs", "id", ["SELECT * FROM analysis_jobs WHERE id = ?"]),
    "ai_history": ("analysis_history", "incident_id", ["SELECT * FROM analysis_history WHERE incident_id = ?"]),
}
MAX_PROBLEMS = 50


def fingerprint(conn, kind: str, key) -> Optional[str]:
    """The record's fingerprint as stored now, or None if it is not in the database."""
    _, _, queries = KINDS[kind]
    parts = []
    for number, sql in enumerate(queries):
        cursor = conn.execute(sql, (key,))
        rows = [list(row) for row in cursor.fetchall()]
        if number == 0 and not rows:
            return None
        parts.append(rows)
    text = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hmac.new(signing_key(), text.encode("utf-8"), hashlib.sha256).hexdigest()


def saved(conn, kind: str, key) -> None:
    """Record the record's fingerprint as the app just saved it. Never raises (it must not break a save)."""
    try:
        value = fingerprint(conn, kind, key)
        audit("data.saved", kind=kind, key=str(key), fingerprint=value,
              message=f"{kind} {key} saved (fingerprint {value[:12] if value else '-'})")
    except Exception:                    # noqa: BLE001
        logging.getLogger(__name__).debug("fingerprint failed", exc_info=True)


def deleted(kind: str, key) -> None:
    audit("data.deleted", kind=kind, key=str(key), message=f"{kind} {key} deleted")


def snapshot(conn) -> Dict[Tuple[str, str], str]:
    """{(kind, key): fingerprint} of every record in the database (tables that do not exist are skipped)."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    result = {}
    for kind, (table, column, _) in KINDS.items():
        if table not in tables:
            continue
        for (key,) in conn.execute(f"SELECT {column} FROM {table}").fetchall():
            result[(kind, str(key))] = fingerprint(conn, kind, key)
    return result


def check(records: Iterable[Optional[dict]], current: Dict[Tuple[str, str], str]) -> dict:
    """
    `records`: the activity log, oldest first (any records; the "data.*" ones
    are used); `current`: snapshot() of the database. Returns {"ok",
    "records" (how many database records were compared), "problems" (first
    MAX_PROBLEMS), "problem_count"}.
    """
    last: Dict[Tuple[str, str], Optional[str]] = {}         # None = deleted by the app
    for record in records:
        if not record or record.get("category") != "data":
            continue
        entry = (record.get("kind"), record.get("key"))
        last[entry] = record.get("fingerprint") if record.get("event") == "data.saved" else None
    problems: List[dict] = []
    count = 0

    def problem(kind, key, kind_of_problem, text):
        nonlocal count
        count += 1
        if len(problems) < MAX_PROBLEMS:
            problems.append({"kind": kind, "key": key, "problem": kind_of_problem, "detail": text})

    for (kind, key), value in sorted(current.items()):
        if (kind, key) not in last:
            problem(kind, key, "unrecorded", "in the database, but the app never saved it (added by hand?)")
        elif last[(kind, key)] is None:
            problem(kind, key, "reappeared", "the app deleted it, but it is in the database again")
        elif last[(kind, key)] != value:
            problem(kind, key, "changed", "differs from what the app last saved (edited outside the app, "
                                          "or an older version put back)")
    for (kind, key), value in sorted(last.items()):
        if value is not None and (kind, key) not in current:
            problem(kind, key, "deleted", "the app saved it, but it is gone from the database")
    return {"ok": count == 0, "records": len(current), "problems": problems, "problem_count": count}
