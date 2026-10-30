"""
Tamper evidence for the activity log (app/audit.py): a signed hash chain,
with a checkpoint in the database.

Every record written to the log files gets three more fields:

    "seq":  1, 2, 3 ...        one sequence for the whole log, across months
    "prev": <hash of the record before it>   ("0" * 64 for the very first)
    "kid":  <id of the signing key>           first 8 hex digits of SHA-256(key)
    "hash": HMAC-SHA256(the log signing key, the record without "hash")

and the database (same file as the incidents, tables audit_chain and
audit_months) keeps a CHECKPOINT: the last seq and hash, and per month the
first/last seq, the count and the last hash. verify() walks every month in
order and reports:

    a changed record          its hash no longer matches (edited text, user, time...)
    a removed / reordered one  a gap in seq, or "prev" not pointing at the record before
    a forged record           a hash that needs the key - without it, it cannot be made
    the newest lines removed  the files end before the checkpoint's last seq
    a whole month removed     the checkpoint knows the month, the files do not
    a different key           "kid" differs: signed with another installation's key,
                              reported as such rather than as tampering

The signing key is generated at setup and kept inside the encrypted
database (app/app_secrets.py), so reading it needs the database key.
What it does NOT do: stop someone who has write access to the server's
files AND can unlock the database (the machine's key store, or the
recovery key) from rewriting the log and the checkpoint consistently.
For stronger guarantees, copy the
checkpoint (GET /logs/verify shows it) somewhere that person cannot write.
Nothing in the app can delete or edit log records: there is no endpoint for
it, admins included.
"""

import hashlib
import hmac
import json
from app import database
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

GENESIS = "0" * 64
_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_chain (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    seq INTEGER NOT NULL,
    hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_months (
    month TEXT PRIMARY KEY,
    first_seq INTEGER NOT NULL,
    last_seq INTEGER NOT NULL,
    count INTEGER NOT NULL,
    last_hash TEXT NOT NULL
);
"""
MAX_PROBLEMS = 50


def key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:8]


def canonical(record: dict) -> bytes:
    """The bytes that are signed: the record without "hash", keys sorted, no spaces."""
    return json.dumps({k: v for k, v in record.items() if k != "hash"}, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign(key: bytes, record: dict) -> str:
    return hmac.new(key, canonical(record), hashlib.sha256).hexdigest()


class AuditChain:
    """
    Adds seq / prev / kid / hash to each record (sign_record()) and keeps the
    checkpoint in the database. Thread-safe; one per log handler.
    """

    def __init__(self, key: bytes, db_path: str):
        self.key = key
        self.kid = key_id(key)
        self._conn = database.connect(db_path)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute("SELECT seq, hash FROM audit_chain WHERE id = 1").fetchone()
        self.seq, self.last_hash = (row[0], row[1]) if row else (0, GENESIS)

    def sign_record(self, record: dict, month: str) -> dict:
        """Chain and sign `record` (a JSON-ready dict) in place, then move the checkpoint. Returns it."""
        with self._lock:
            record["seq"] = self.seq + 1
            record["prev"] = self.last_hash
            record["kid"] = self.kid
            record["hash"] = sign(self.key, record)
            now = datetime.now(timezone.utc).isoformat()
            self._conn.execute(
                "INSERT INTO audit_chain (id, seq, hash, updated_at) VALUES (1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET seq = excluded.seq, hash = excluded.hash, updated_at = excluded.updated_at",
                (record["seq"], record["hash"], now))
            self._conn.execute(
                "INSERT INTO audit_months (month, first_seq, last_seq, count, last_hash) VALUES (?, ?, ?, 1, ?) "
                "ON CONFLICT(month) DO UPDATE SET last_seq = excluded.last_seq, count = count + 1, "
                "last_hash = excluded.last_hash",
                (month, record["seq"], record["seq"], record["hash"]))
            self._conn.commit()
            self.seq, self.last_hash = record["seq"], record["hash"]
            return record

    def checkpoint(self) -> dict:
        with self._lock:
            months = self._conn.execute(
                "SELECT month, first_seq, last_seq, count, last_hash FROM audit_months ORDER BY month").fetchall()
        return {"seq": self.seq, "hash": self.last_hash,
                "months": {m: {"first_seq": f, "last_seq": l, "count": c, "last_hash": h} for m, f, l, c, h in months}}

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def verify(months_records, key: bytes, checkpoint: Optional[dict]) -> dict:
    """
    Check the whole chain. `months_records`: [(month, iterable of records or
    None for unreadable lines)] in month order (app/log_reader.py's
    iter_all_months()); `checkpoint`: AuditChain.checkpoint(), or None if the
    database has none. Returns {"ok", "records", "problems": [...first
    MAX_PROBLEMS], "problem_count", "checkpoint": {"seq", "hash"}, "last_seq"}.
    """
    kid = key_id(key)
    problems: List[Dict[str, object]] = []
    count_problems = [0]

    def problem(month, seq, text):
        count_problems[0] += 1
        if len(problems) < MAX_PROBLEMS:
            problems.append({"month": month, "seq": seq, "problem": text})

    expected_prev, expected_seq, total = GENESIS, 1, 0
    seen_months = {}
    # Records written while the check runs (after the checkpoint was taken) are not judged yet.
    upto = checkpoint["seq"] if checkpoint else None
    for month, records in months_records:
        counted, first, last, last_hash = 0, None, None, None
        for record in records:
            if record is None:
                problem(month, None, "a line that is not a valid record")
                continue
            seq = record.get("seq")
            if "hash" not in record or not isinstance(seq, int):
                problem(month, seq, "an unsigned record (written before signing was turned on, or added by hand)")
                continue
            total += 1
            if upto is None or seq <= upto:      # newer ones are checked, but not against the checkpoint
                counted += 1
                first = seq if first is None else first
                last, last_hash = seq, record["hash"]
            if record.get("kid") != kid:
                problem(month, seq, f"signed with another key (kid {record.get('kid')}) - the signing key was changed")
            elif not hmac.compare_digest(sign(key, record), str(record["hash"])):
                problem(month, seq, "changed after it was written (the signature does not match)")
            if seq != expected_seq:
                if seq > expected_seq:
                    problem(month, seq, f"records {expected_seq}-{seq - 1} are missing")
                else:
                    problem(month, seq, f"out of order or repeated (expected {expected_seq})")
            elif record.get("prev") != expected_prev:
                problem(month, seq, "does not follow the record before it (prev hash differs)")
            expected_seq, expected_prev = seq + 1, record["hash"]
        seen_months[month] = {"count": counted, "first_seq": first, "last_seq": last, "last_hash": last_hash}

    if checkpoint:
        for month, saved in checkpoint["months"].items():
            found = seen_months.get(month)
            if found is None or not found["count"]:
                problem(month, None, f"the whole month is missing ({saved['count']} records were written)")
            elif (found["last_seq"], found["last_hash"]) != (saved["last_seq"], saved["last_hash"]) \
                    or found["count"] != saved["count"]:
                problem(month, found["last_seq"],
                        f"the month ends differently than written: {found['count']} of {saved['count']} records, "
                        f"last seq {found['last_seq']} of {saved['last_seq']}")
        if expected_seq - 1 < checkpoint["seq"]:
            problem(None, checkpoint["seq"], f"the log ends at record {expected_seq - 1}, but record "
                                             f"{checkpoint['seq']} was written last - the newest records are missing")
    else:
        if total:
            problem(None, None, "no checkpoint in the database (the database was replaced or reset)")

    return {"ok": count_problems[0] == 0, "records": total, "problems": problems,
            "problem_count": count_problems[0], "last_seq": expected_seq - 1,
            "checkpoint": {"seq": checkpoint["seq"], "hash": checkpoint["hash"]} if checkpoint else None}


def read_checkpoint(db_path: str) -> Optional[dict]:
    """The checkpoint stored in `db_path`, without needing a running chain (None if there is none)."""
    if db_path != ":memory:" and not Path(db_path).exists():
        return None
    conn = database.connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        row = conn.execute("SELECT seq, hash FROM audit_chain WHERE id = 1").fetchone()
        if not row:
            return None
        months = conn.execute("SELECT month, first_seq, last_seq, count, last_hash FROM audit_months").fetchall()
    finally:
        conn.close()
    return {"seq": row[0], "hash": row[1],
            "months": {m: {"first_seq": f, "last_seq": l, "count": c, "last_hash": h} for m, f, l, c, h in months}}
