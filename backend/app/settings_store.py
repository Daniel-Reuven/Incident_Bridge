"""
Installation settings kept INSIDE the encrypted database (table
app_settings), so no secret has to sit in a file:

    "secrets"   the session-cookie key and the activity-log signing key,
                generated at setup (app/app_secrets.py)
    "ai"        the AI analysis settings chosen on the setup page, by their
                .env names (AI_PROVIDER, GEMINI_API_KEY, ANTHROPIC_MODEL, ...);
                they take precedence over .env (app/ai/stored_settings.py)
    "server"    the Server settings page's HTTPS settings (app/server_settings.py)
    "tls_generated"  the password of the generated certificate's key (app/security/tls.py)

Values are JSON. Same conventions as the other stores: one connection
through app/database.py, guarded by a lock.
"""

import json
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from app import data_integrity, database

_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_settings (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class SqliteSettingsStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = database.connect(db_path)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def get(self, name: str, default: Optional[Any] = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM app_settings WHERE name = ?", (name,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, name: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO app_settings (name, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (name, json.dumps(value, sort_keys=True), datetime.now(timezone.utc).isoformat()))
            self._conn.commit()
            data_integrity.saved(self._conn, "setting", name)

    def record_all(self) -> None:
        """Fingerprint every setting now - for those saved before the activity log was running (the setup)."""
        with self._lock:
            names = [row[0] for row in self._conn.execute("SELECT name FROM app_settings")]
            for name in names:
                data_integrity.saved(self._conn, "setting", name)

    def record(self, name: str) -> None:
        """Fingerprint one setting now - saved by serve_app.py before the activity log was running."""
        with self._lock:
            data_integrity.saved(self._conn, "setting", name)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
