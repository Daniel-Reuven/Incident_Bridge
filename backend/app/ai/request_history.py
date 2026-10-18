"""
RequestHistoryStore: a durable record of every AI analysis REQUEST, kept in
the same SQLite file as the queue (app/ai/job_store.py) but in its own
table, analysis_requests - additive (CREATE TABLE IF NOT EXISTS), so it
never touches analysis_jobs or analysis_history.

Unlike analysis_jobs (whose row is deleted once a job is done) a row here is
written once, when the job is requested, and updated once, when it ends -
and then kept forever. This is the log the Sites report (app/reports.py)
and the request/decision history (task A1-A4, brief 6.1-6.3) read from.

Own table, own connection, the same pattern as app/ai/job_store.py: one
long-lived connection guarded by a threading.Lock, used both by request
threads (a new request) and the analysis worker thread (how it ended).
"""

import sqlite3
import threading
from datetime import datetime, timezone
from typing import List, Optional

# "success_unstructured" and findings_json are filled in by task A2 (the
# structured-findings JSON block); until then every success is "success"
# and findings_json stays NULL.
OUTCOMES = ("success", "success_unstructured", "failed", "cancelled", "dropped")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_requests (
    id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,
    provider TEXT,
    model TEXT,
    findings_json TEXT
);
"""


class RequestHistoryStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def record_requested(self, job_id: str, incident_id: str, requested_by: str, requested_at: datetime) -> None:
        """Insert the row for a newly queued job. finished_at/outcome/provider/model/findings_json start NULL."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO analysis_requests (id, incident_id, requested_by, requested_at) VALUES (?, ?, ?, ?)",
                (job_id, incident_id, requested_by, requested_at.isoformat()),
            )
            self._conn.commit()

    def record_finished(self, job_id: str, outcome: str, provider: Optional[str] = None,
                         model: Optional[str] = None, findings_json: Optional[str] = None,
                         finished_at: Optional[datetime] = None) -> None:
        """Fill in how a request ended. `outcome` must be one of OUTCOMES."""
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome {outcome!r} - must be one of {OUTCOMES}")
        finished_at = finished_at or datetime.now(timezone.utc)
        with self._lock:
            self._conn.execute(
                "UPDATE analysis_requests SET finished_at = ?, outcome = ?, provider = ?, model = ?, "
                "findings_json = ? WHERE id = ?",
                (finished_at.isoformat(), outcome, provider, model, findings_json, job_id),
            )
            self._conn.commit()

    def all_requests(self) -> List[dict]:
        """Every row, oldest first - as plain dicts (for reports and tests)."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM analysis_requests ORDER BY requested_at ASC").fetchall()
        return [dict(row) for row in rows]

    def for_incident(self, incident_id: str) -> List[dict]:
        """Every row for one incident, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM analysis_requests WHERE incident_id = ? ORDER BY requested_at ASC",
                (incident_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def __repr__(self) -> str:
        return f"RequestHistoryStore(db_path={self.db_path!r})"


__all__ = ["RequestHistoryStore", "OUTCOMES"]
