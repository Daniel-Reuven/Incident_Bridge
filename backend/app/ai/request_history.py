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
  success                the answer was posted and followed the expected
                          section headings (app/ai/prompt.py's
                          ANSWER_SECTIONS) - findings_json holds them.
  success_unstructured   the answer was posted but did not use those
                          headings - findings_json is NULL.
  failed                 the provider call failed, or could not be made.
  cancelled               cancelled before an answer was posted.
  dropped                 the fault was closed or gone when the job's turn came.
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

from app.ai.jobs import AnalysisJob
from app.ai.prompt import ANSWER_SECTIONS

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


class SqliteRequestHistoryStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
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

    def __repr__(self) -> str:
        return f"SqliteRequestHistoryStore(db_path={self.db_path!r})"


def parse_findings(answer: str) -> Optional[dict]:
    """
    `answer`'s sections as {heading: [lines]} if it used every heading in
    ANSWER_SECTIONS exactly as instructed (app/ai/prompt.py), else None - an
    "unstructured" answer.
    """
    headings = {f"{name}:": name for name in ANSWER_SECTIONS}
    sections: dict = {}
    current = None
    for line in answer.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if stripped in headings:
            current = headings[stripped]
            sections[current] = []
        elif current is not None and stripped:
            sections[current].append(stripped)
    return sections if set(sections) == set(ANSWER_SECTIONS) else None


__all__ = ["SqliteRequestHistoryStore", "parse_findings", "OUTCOME_SUCCESS", "OUTCOME_SUCCESS_UNSTRUCTURED",
           "OUTCOME_FAILED", "OUTCOME_CANCELLED", "OUTCOME_DROPPED"]
