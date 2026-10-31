"""
SqliteRequestHistoryStore: a full, append-only log of every AI analysis
request - unlike app/ai/job_store.py's analysis_jobs table (the live queue,
a row only while a job is pending) this keeps one row per request forever.

Same database file as the incidents and the job queue, its own table
(analysis_requests) and its own connection, guarded by its own lock - the
same pattern as SqliteAnalysisJobStore.

A row is inserted when a job is requested (AnalysisWorker.request();
outcome and finished_at are still NULL) and filled in once the worker is
done with it (AnalysisWorker.cancel() / _finish()):
  success                the answer was posted and its trailing JSON block
                          (app/ai/result.py's AnalysisFindings) was present
                          and valid - findings_json holds it.
  success_unstructured   the answer was posted but that block was missing
                          or invalid - findings_json is NULL (never a
                          reason to fail the analysis - see app/ai/result.py).
  failed                 the provider call failed, or could not be made.
  cancelled               cancelled before an answer was posted.
  dropped                 the fault was closed or gone when the job's turn came.
"""

import json
import threading
from datetime import datetime, timezone
from typing import Dict, List, NamedTuple, Optional, Set

from app import database
from app.ai.jobs import AnalysisJob

OUTCOME_SUCCESS = "success"
OUTCOME_SUCCESS_UNSTRUCTURED = "success_unstructured"
OUTCOME_FAILED = "failed"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_DROPPED = "dropped"

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


class RequestRecord(NamedTuple):
    """One stored request, for app/reports.py (which applies the archive rule, app/archive_rules.py)."""
    incident_id: str
    outcome: Optional[str]                 # None while the request is still pending
    finished_at: Optional[datetime]        # None while the request is still pending
    affected_site_ids: tuple               # from the findings; () without findings


class SqliteRequestHistoryStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = database.connect(db_path)          # the encrypted database (app/database.py)
        self._conn.row_factory = database.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def record_requested(self, job: AnalysisJob, provider: str) -> None:
        """Insert the row for a newly queued job (outcome and finished_at are still unknown)."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO analysis_requests (id, incident_id, requested_by, requested_at, provider) "
                "VALUES (?, ?, ?, ?, ?)",
                (job.id, job.incident_id, job.requested_by, job.requested_at.isoformat(), provider),
            )
            self._conn.commit()

    def record_finished(self, job_id: str, outcome: str, model: Optional[str] = None,
                         findings: Optional[dict] = None) -> None:
        """Fill in how the request ended. Updating a row that was never inserted is not an error."""
        with self._lock:
            self._conn.execute(
                "UPDATE analysis_requests SET finished_at = ?, outcome = ?, model = ?, findings_json = ? "
                "WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), outcome, model,
                 json.dumps(findings, ensure_ascii=False) if findings else None, job_id),
            )
            self._conn.commit()

    def outcomes_by_incident(self) -> Dict[str, List[Optional[str]]]:
        """Every stored request's outcome, grouped by incident id (None = a request still pending) - for app/reports.py."""
        with self._lock:
            rows = self._conn.execute("SELECT incident_id, outcome FROM analysis_requests").fetchall()
        grouped: Dict[str, List[Optional[str]]] = {}
        for row in rows:
            grouped.setdefault(row["incident_id"], []).append(row["outcome"])
        return grouped

    def named_site_ids(self) -> Set[int]:
        """Every site id any analysis's findings ever named as affected, across every request - for app/reports.py."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT findings_json FROM analysis_requests WHERE findings_json IS NOT NULL").fetchall()
        ids: Set[int] = set()
        for row in rows:
            ids.update(json.loads(row["findings_json"]).get("affected_site_ids", []))
        return ids

    def records(self) -> List[RequestRecord]:
        """Every stored request, oldest first - app/reports.py splits them into live and archived."""
        with self._lock:
            rows = self._conn.execute("SELECT incident_id, outcome, finished_at, findings_json FROM analysis_requests "
                                      "ORDER BY requested_at").fetchall()
        return [RequestRecord(row["incident_id"], row["outcome"],
                              datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
                              tuple(json.loads(row["findings_json"]).get("affected_site_ids", []))
                              if row["findings_json"] else ())
                for row in rows]

    def __repr__(self) -> str:
        return f"SqliteRequestHistoryStore(db_path={self.db_path!r})"


__all__ = ["SqliteRequestHistoryStore", "RequestRecord", "OUTCOME_SUCCESS", "OUTCOME_SUCCESS_UNSTRUCTURED",
           "OUTCOME_FAILED", "OUTCOME_CANCELLED", "OUTCOME_DROPPED"]
