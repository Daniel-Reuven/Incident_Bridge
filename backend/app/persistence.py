"""
SQLite-backed persistence for incidents and their comments.

Users are NOT persisted here - they stay env-provisioned via
INCIDENT_BRIDGE_USERS (see app/repository.py's seed_users_from_env). This
store only exists so incidents survive a process restart instead of
disappearing when the in-memory dict does; the in-memory IncidentRepository
and the queues remain the live source of truth *during* a run - see
app/repository.py (which calls into this store on every save) and
app/state.py (which loads from it once, at startup, and feeds the results
back into the queues).

Uses the stdlib sqlite3 module directly rather than an ORM - "zero setup"
was the explicit choice for this step, and the schema is small enough
that hand-written SQL stays perfectly readable.

Thread safety: FastAPI runs sync endpoint functions in a thread pool, so
more than one request's handler could touch this store concurrently.
sqlite3 connections aren't safe to share across threads without care, so
a single connection is kept open for the process's lifetime (this is also
required for a ":memory:" database to work at all - a fresh connection
would just see an empty database) and every access is serialized with a
lock. This is the project's first real use of `threading`: a lock
protecting state that's genuinely shared across request-handling threads,
not a contrived example.
"""

import json
import sqlite3
import threading
from datetime import datetime
from typing import TYPE_CHECKING, Iterable, List, Optional

from app.models import (
    Comment,
    Fault,
    Incident,
    IncidentStatus,
    MaintenanceTask,
    ResolutionType,
    Role,
    SeverityCategory,
    User,
)

if TYPE_CHECKING:
    # Only needed for the type hint below - importing it at runtime would
    # create a circular import, since app.repository imports this module.
    from app.repository import UserStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('maintenance', 'fault')),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL,
    resolution_type TEXT,
    resolution_message TEXT,
    created_by TEXT NOT NULL,
    assigned_to TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    severity INTEGER,
    severity_score REAL,
    details TEXT,
    queue_position INTEGER
);

CREATE TABLE IF NOT EXISTS comments (
    id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    author TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

# Cache of placeholder Users for usernames that appear in persisted data
# but no longer exist in the current INCIDENT_BRIDGE_USERS - see
# _unknown_user() below. Keyed so the same placeholder object is reused
# rather than minting a new throwaway account on every load.
_unknown_user_cache: dict = {}


def _unknown_user(original_username: str) -> User:
    """
    Fallback for a persisted created_by/assigned_to/author username that
    no longer matches any currently-provisioned user (e.g. someone was
    removed from INCIDENT_BRIDGE_USERS between restarts). Keeps a restart
    from crashing over one stale reference; the placeholder is clearly
    labeled rather than silently reassigned to a real account.
    """
    if original_username not in _unknown_user_cache:
        print(f"WARNING: persisted data references unknown user {original_username!r} "
              f"(not in current INCIDENT_BRIDGE_USERS) - using a placeholder account for it.")
        _unknown_user_cache[original_username] = User(
            username=f"{original_username} (removed)", role=Role.USER, password="PlaceholderPassw0rd"
        )
    return _unknown_user_cache[original_username]


class SqliteIncidentStore:
    def __init__(self, db_path: str = "incident_bridge.db"):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def save_incident(self, incident: Incident) -> None:
        """
        Upsert the incident's current fields, and append any comments not
        already stored. Call this after ANY mutation (creation, status
        change, closing, a new comment, a severity change) - see
        app/repository.py's save(), which every mutating API endpoint
        calls exactly once at the end (app/api/incidents.py).
        """
        is_fault = isinstance(incident, Fault)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO incidents (id, kind, title, description, status, resolution_type,
                                        resolution_message, created_by, assigned_to, created_at,
                                        updated_at, severity, severity_score, details, queue_position)
                VALUES (:id, :kind, :title, :description, :status, :resolution_type,
                        :resolution_message, :created_by, :assigned_to, :created_at,
                        :updated_at, :severity, :severity_score, :details, :queue_position)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title, description=excluded.description, status=excluded.status,
                    resolution_type=excluded.resolution_type, resolution_message=excluded.resolution_message,
                    assigned_to=excluded.assigned_to, updated_at=excluded.updated_at,
                    severity=excluded.severity, severity_score=excluded.severity_score,
                    details=excluded.details, queue_position=excluded.queue_position
                """,
                {
                    "id": incident.id,
                    "kind": "fault" if is_fault else "maintenance",
                    "title": incident.title,
                    "description": incident.description,
                    "status": incident.status.value,
                    "resolution_type": incident.resolution_type.value if incident.resolution_type else None,
                    "resolution_message": incident.resolution_message,
                    "created_by": incident.created_by.username,
                    "assigned_to": incident.assigned_to.username if incident.assigned_to else None,
                    "created_at": incident.created_at.isoformat(),
                    "updated_at": incident.updated_at.isoformat(),
                    "severity": incident.severity.value if is_fault else None,
                    "severity_score": incident.severity_score if is_fault else None,
                    "details": json.dumps(incident.details) if is_fault else None,
                    "queue_position": None if is_fault else incident.queue_position,
                },
            )
            for comment in incident.comments:
                self._conn.execute(
                    "INSERT OR IGNORE INTO comments (id, incident_id, author, text, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (comment.id, incident.id, comment.author.username, comment.text, comment.created_at.isoformat()),
                )
            self._conn.commit()

    def load_all(self, user_store: "UserStore") -> List[Incident]:
        """
        Reconstruct every persisted incident, with its comments attached,
        ordered by creation time. Callers (app/state.py) rely on that
        order to rebuild the maintenance FIFO and the fault priority
        queue's tiebreak order the same way they were before a restart.
        """
        with self._lock:
            incident_rows = self._conn.execute("SELECT * FROM incidents ORDER BY created_at ASC").fetchall()
            comment_rows = self._conn.execute("SELECT * FROM comments ORDER BY created_at ASC").fetchall()

        comments_by_incident: dict = {}
        for row in comment_rows:
            comments_by_incident.setdefault(row["incident_id"], []).append(row)

        incidents = []
        for row in incident_rows:
            incident = self._row_to_incident(row, user_store)
            for comment_row in comments_by_incident.get(row["id"], []):
                incident.comments.append(self._row_to_comment(comment_row, user_store))
            incidents.append(incident)
        return incidents

    def _resolve_user(self, username: Optional[str], user_store: "UserStore") -> Optional[User]:
        if username is None:
            return None
        return user_store.get(username) or _unknown_user(username)

    def _row_to_incident(self, row: sqlite3.Row, user_store: "UserStore") -> Incident:
        created_by = self._resolve_user(row["created_by"], user_store)
        assigned_to = self._resolve_user(row["assigned_to"], user_store)

        if row["kind"] == "fault":
            incident: Incident = Fault(
                title=row["title"], description=row["description"], created_by=created_by,
                severity=SeverityCategory(row["severity"]), severity_score=row["severity_score"],
                details=json.loads(row["details"]) if row["details"] else {}, assigned_to=assigned_to,
            )
        else:
            incident = MaintenanceTask(
                title=row["title"], description=row["description"], created_by=created_by, assigned_to=assigned_to,
            )
            incident.queue_position = row["queue_position"]

        # Overwrite the freshly-generated identity/timestamps/lifecycle
        # fields with the persisted ones. This is the one place outside
        # the domain layer allowed to reach into these fields directly -
        # its whole job is reconstituting prior state (see module
        # docstring), which the normal constructors don't support since
        # they always mint a fresh id/created_at for a *new* incident.
        incident.id = row["id"]
        incident.status = IncidentStatus(row["status"])
        incident.resolution_type = ResolutionType(row["resolution_type"]) if row["resolution_type"] else None
        incident.resolution_message = row["resolution_message"]
        incident.created_at = datetime.fromisoformat(row["created_at"])
        incident.updated_at = datetime.fromisoformat(row["updated_at"])
        return incident

    def _row_to_comment(self, row: sqlite3.Row, user_store: "UserStore") -> Comment:
        author = self._resolve_user(row["author"], user_store)
        comment = Comment(author=author, text=row["text"])
        comment.id = row["id"]
        comment.created_at = datetime.fromisoformat(row["created_at"])
        return comment
