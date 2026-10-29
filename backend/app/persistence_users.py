"""
SQLite-backed persistence for user accounts (v0.5.x).

Same conventions as the other stores (app/persistence.py,
app/persistence_sites.py): SQLite through app/database.py (encrypted), the same database FILE
(DATABASE_PATH) through its own connection, one connection for the
process's lifetime (needed for ":memory:"), every access under a
threading.Lock, upserts. The in-memory UserStore (app/repository.py) is the
live source of truth during a run; this store is written on every change
and read once at startup.

Each user's interface preferences are stored as JSON in the "preferences"
column (app/models/preferences.py); the column is added to older databases
at startup.

Only the password HASH is stored (see app/models/user.py for its format),
never a password. The built-in system users (app/models/system_users.py)
are not stored here - they are defined in code.
"""

import json
from app import data_integrity, database
import threading
from datetime import datetime
from typing import List

from app.models import Role, User

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    disabled INTEGER NOT NULL DEFAULT 0,
    session_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    preferences TEXT NOT NULL DEFAULT '{}'
);
"""


def _json(text):
    """A stored preferences value, or None if it is not valid JSON (the defaults are used then)."""
    try:
        return json.loads(text or "{}")
    except ValueError:
        return None


class SqliteUserStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = database.connect(db_path)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            # Databases from before v0.6.0 have no preferences column yet.
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(users)")}
            if "preferences" not in columns:
                self._conn.execute("ALTER TABLE users ADD COLUMN preferences TEXT NOT NULL DEFAULT '{}'")
            self._conn.commit()

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def load_all(self) -> List[User]:
        """Every stored user, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT username, role, password_hash, must_change_password, disabled, session_version, created_at, "
                "preferences FROM users ORDER BY created_at, username").fetchall()
        return [User.from_record(username, Role(role), password_hash, bool(must_change), bool(disabled),
                                 session_version, datetime.fromisoformat(created_at), _json(prefs))
                for username, role, password_hash, must_change, disabled, session_version, created_at, prefs in rows]

    def save(self, user: User) -> None:
        """Insert or update one user."""
        self.save_many([user])

    def save_many(self, users) -> None:
        """Insert or update several users in ONE transaction (the first-start creation from .env)."""
        now = datetime.now().astimezone().isoformat()
        with self._lock:
            self._conn.executemany(
                """INSERT INTO users (username, role, password_hash, must_change_password, disabled,
                                      session_version, created_at, updated_at, preferences)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(username) DO UPDATE SET
                       role = excluded.role, password_hash = excluded.password_hash,
                       must_change_password = excluded.must_change_password, disabled = excluded.disabled,
                       session_version = excluded.session_version, updated_at = excluded.updated_at,
                       preferences = excluded.preferences""",
                [(u.username, u.role.value, u.password_hash, int(u.must_change_password), int(u.disabled),
                  u.session_version, u.created_at.isoformat(), now, json.dumps(u.preferences, sort_keys=True))
                 for u in users])
            self._conn.commit()
            for user in users:                     # tamper evidence (app/data_integrity.py)
                data_integrity.saved(self._conn, "user", user.username)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __repr__(self) -> str:
        return f"SqliteUserStore({self.db_path!r})"
