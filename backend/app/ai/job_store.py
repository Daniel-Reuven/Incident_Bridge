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

A second table, analysis_history, keeps the LAST SUCCESSFUL analysis of each
fault (provider, model, data fingerprint, time) - what the re-analysis rule
needs (app/ai/reanalysis.py). One row per fault, replaced on each success,
so it survives restarts and never grows beyond the number of faults.
"""

from app import database
import threading
from datetime import datetime
from typing import List, Optional

from app.ai.jobs import AnalysisJob
from app.ai.reanalysis import LastAnalysis

_SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_jobs (
    id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_history (
    incident_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    finished_at TEXT NOT NULL
);
"""


class SqliteAnalysisJobStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = database.connect(db_path)
        self._conn.row_factory = database.Row
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

    def record_success(self, incident_id: str, last: LastAnalysis) -> None:
        """Remember `last` as the fault's last successful analysis (replacing any earlier one)."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO analysis_history (incident_id, provider, model, fingerprint, finished_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (incident_id, last.provider, last.model, last.fingerprint, last.finished_at.isoformat()),
            )
            self._conn.commit()

    def last_success(self, incident_id: str) -> Optional[LastAnalysis]:
        """The fault's last successful analysis, or None if it never had one."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM analysis_history WHERE incident_id = ?", (incident_id,)).fetchone()
        if row is None:
            return None
        return LastAnalysis(row["provider"], row["model"], row["fingerprint"], datetime.fromisoformat(row["finished_at"]))

    def __repr__(self) -> str:
        return f"SqliteAnalysisJobStore(db_path={self.db_path!r})"
