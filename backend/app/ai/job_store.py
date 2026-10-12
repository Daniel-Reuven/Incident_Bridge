"""
SqliteAnalysisJobStore: keeps the AI analysis queue in SQLite, so jobs that
are still waiting (or were running) survive a restart.

Same database file as the incidents (DATABASE_PATH), its own table and its
own connection, the same pattern as app/persistence_sites.py: one long-lived
connection guarded by a threading.Lock, because it is used both by request
threads (adding/cancelling jobs) and by the analysis worker thread.

A job is a row from the moment it is queued until it is finished, dropped
or cancelled. Rows are loaded back oldest first, so after a restart the
queue order is unchanged; a job that was running when the server stopped
is the oldest row, so it simply runs again first (with a freshly built
prompt). Restored jobs go to whichever provider is configured at that time.
"""

import sqlite3
import threading
from datetime import datetime
from typing import List

from app.ai.jobs import AnalysisJob

_SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_jobs (
    id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL
);
"""


class SqliteAnalysisJobStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def add(self, job: AnalysisJob) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO analysis_jobs (id, incident_id, requested_by, requested_at) VALUES (?, ?, ?, ?)",
                (job.id, job.incident_id, job.requested_by, job.requested_at.isoformat()),
            )
            self._conn.commit()

    def remove(self, job_id: str) -> None:
        """Delete a job. Removing a job that is already gone is not an error."""
        with self._lock:
            self._conn.execute("DELETE FROM analysis_jobs WHERE id = ?", (job_id,))
            self._conn.commit()

    def load_all(self) -> List[AnalysisJob]:
        """Every stored job, oldest first (= queue order)."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM analysis_jobs ORDER BY requested_at ASC").fetchall()
        return [AnalysisJob(incident_id=row["incident_id"], requested_by=row["requested_by"], id=row["id"],
                            requested_at=datetime.fromisoformat(row["requested_at"])) for row in rows]

    def __repr__(self) -> str:
        return f"SqliteAnalysisJobStore(db_path={self.db_path!r})"
