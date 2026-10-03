"""
SQLite-backed persistence for the Sites & Mailing Lists subsystem: sites
(with their status history), mailing lists (with their members and site
links), and notifications.

Deliberately a separate store from SqliteIncidentStore (app/persistence.py):
the incident store stays untouched, and either subsystem can change its
schema, or move to another backend, without affecting the other. Both use
the same database FILE (DATABASE_PATH - see app/state.py), each through its
own connection. Their tables never overlap.

Same conventions as SqliteIncidentStore:
  - stdlib sqlite3, hand-written SQL, no ORM;
  - one connection kept open for the process's lifetime (required for a
    ":memory:" database to work at all), every access serialized with a
    threading.Lock, because FastAPI runs sync endpoints in a thread pool;
  - the in-memory SiteDirectory (app/sites.py) is the live source of truth
    during a run; this store is written on every change and read once, at
    startup (load_all()).

Writes are upserts, so saving the same object again just updates it.

Schema changes: CREATE TABLE IF NOT EXISTS never alters a table that already
exists, so columns added after a table was first created are added by
_migrate() (ALTER TABLE ... ADD COLUMN) when the store opens. That keeps an
existing database file working after an upgrade, with no manual step.
save_batch() writes several objects in ONE transaction - used by
SiteDirectory.archive_site()/restore_site(), where a site and every list it
was unlinked from must be saved together or not at all.
"""

import json
import sqlite3
import threading
from datetime import date, datetime
from typing import Iterable, List, NamedTuple, Optional

from app.models import (
    AvailabilityCheck, MailingList, Notification, NotificationState, Site, SiteStatus, SiteStatusChange,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    site_id INTEGER PRIMARY KEY,
    site_name TEXT NOT NULL,
    site_url TEXT NOT NULL,
    site_publish_date TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    archived_at TEXT,
    archived_list_ids TEXT NOT NULL DEFAULT '[]',
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_check TEXT
);

-- Append-only: seq is the entry's position in the site's history (0, 1, ...),
-- so re-saving a site only inserts entries that are not stored yet.
CREATE TABLE IF NOT EXISTS site_status_history (
    site_id INTEGER NOT NULL REFERENCES sites(site_id),
    seq INTEGER NOT NULL,
    old_status TEXT NOT NULL,
    new_status TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    PRIMARY KEY (site_id, seq)
);

CREATE TABLE IF NOT EXISTS mailing_lists (
    list_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    archived_at TEXT
);

CREATE TABLE IF NOT EXISTS mailing_list_members (
    list_id TEXT NOT NULL REFERENCES mailing_lists(list_id),
    email TEXT NOT NULL,
    PRIMARY KEY (list_id, email)
);

-- The many-to-many link between mailing lists and sites.
CREATE TABLE IF NOT EXISTS mailing_list_sites (
    list_id TEXT NOT NULL REFERENCES mailing_lists(list_id),
    site_id INTEGER NOT NULL REFERENCES sites(site_id),
    PRIMARY KEY (list_id, site_id)
);

-- A notification is a snapshot, so list ids and recipients are stored as
-- JSON arrays on the row itself rather than as links to the live tables.
CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    site_id INTEGER NOT NULL,
    site_name TEXT NOT NULL,
    old_status TEXT NOT NULL,
    new_status TEXT NOT NULL,
    list_ids TEXT NOT NULL,
    recipients TEXT NOT NULL,
    message TEXT NOT NULL,
    state TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    dismiss_reason TEXT
);
"""

# Columns added to existing tables after their first release: table ->
# [(column, SQL definition)]. _migrate() adds any that an older database
# file is missing. The definitions must match the CREATE TABLE above.
_ADDED_COLUMNS = {
    "sites": [
        ("consecutive_failures", "INTEGER NOT NULL DEFAULT 0"),
        ("last_check", "TEXT"),
    ],
}


class StoredSiteData(NamedTuple):
    """Everything load_all() returns, in the order SiteDirectory.bulk_load() takes it."""
    sites: List[Site]
    mailing_lists: List[MailingList]
    notifications: List[Notification]


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _from_iso(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def _check_to_json(check: Optional[AvailabilityCheck]) -> Optional[str]:
    """An AvailabilityCheck as a JSON object (or None) - stored in sites.last_check."""
    if check is None:
        return None
    return json.dumps({"checked_at": check.checked_at.isoformat(), "observed_status": check.observed_status.value,
                       "http_status": check.http_status, "response_ms": check.response_ms, "error": check.error})


def _check_from_json(raw: Optional[str]) -> Optional[AvailabilityCheck]:
    if not raw:
        return None
    data = json.loads(raw)
    return AvailabilityCheck(checked_at=datetime.fromisoformat(data["checked_at"]),
                             observed_status=SiteStatus(data["observed_status"]),
                             http_status=data.get("http_status"), response_ms=data.get("response_ms"),
                             error=data.get("error"))


class SqliteSiteStore:
    """Persists Site, MailingList and Notification objects. See the module docstring."""

    def __init__(self, db_path: str = "incident_bridge.db"):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add any column from _ADDED_COLUMNS that an older database file's table is missing. Caller holds the lock."""
        for table, columns in _ADDED_COLUMNS.items():
            existing = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            for column, definition in columns:
                if column not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def save_site(self, site: Site) -> None:
        """Upsert one site and append any history entries not stored yet."""
        self.save_batch(sites=[site])

    def save_mailing_list(self, mailing_list: MailingList) -> None:
        """Upsert one mailing list and replace its stored members and site links with the current ones."""
        self.save_batch(mailing_lists=[mailing_list])

    def save_notification(self, notification: Notification) -> None:
        """Upsert one notification."""
        self.save_batch(notifications=[notification])

    def save_batch(self, sites: Iterable[Site] = (), mailing_lists: Iterable[MailingList] = (),
                   notifications: Iterable[Notification] = ()) -> None:
        """
        Save any mix of sites, lists and notifications in a single
        transaction: either everything is written or (on an error) nothing
        is - the `with self._conn:` block commits on success and rolls back
        on an exception.
        """
        with self._lock, self._conn:
            for site in sites:
                self._write_site(site)
            for mailing_list in mailing_lists:
                self._write_mailing_list(mailing_list)
            for notification in notifications:
                self._write_notification(notification)

    def _write_site(self, site: Site) -> None:
        self._conn.execute(
            """
            INSERT INTO sites (site_id, site_name, site_url, site_publish_date, status,
                               created_at, archived_at, archived_list_ids, consecutive_failures, last_check)
            VALUES (:site_id, :site_name, :site_url, :site_publish_date, :status,
                    :created_at, :archived_at, :archived_list_ids, :consecutive_failures, :last_check)
            ON CONFLICT(site_id) DO UPDATE SET
                site_name=excluded.site_name, site_url=excluded.site_url,
                site_publish_date=excluded.site_publish_date, status=excluded.status,
                archived_at=excluded.archived_at, archived_list_ids=excluded.archived_list_ids,
                consecutive_failures=excluded.consecutive_failures, last_check=excluded.last_check
            """,
            {
                "site_id": site.site_id,
                "site_name": site.site_name,
                "site_url": site.site_url,
                "site_publish_date": site.site_publish_date.isoformat() if site.site_publish_date else None,
                "status": site.status.value,
                "created_at": _iso(site.created_at),
                "archived_at": _iso(site.archived_at),
                "archived_list_ids": json.dumps(sorted(site.archived_list_ids)),
                "consecutive_failures": site.consecutive_failures,
                "last_check": _check_to_json(site.last_check),
            },
        )
        for seq, change in enumerate(site.status_history):
            self._conn.execute(
                "INSERT OR IGNORE INTO site_status_history "
                "(site_id, seq, old_status, new_status, reason, actor, changed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (site.site_id, seq, change.old_status.value, change.new_status.value,
                 change.reason, change.actor, _iso(change.changed_at)),
            )

    def _write_mailing_list(self, mailing_list: MailingList) -> None:
        self._conn.execute(
            """
            INSERT INTO mailing_lists (list_id, name, created_at, archived_at)
            VALUES (:list_id, :name, :created_at, :archived_at)
            ON CONFLICT(list_id) DO UPDATE SET name=excluded.name, archived_at=excluded.archived_at
            """,
            {"list_id": mailing_list.list_id, "name": mailing_list.name,
             "created_at": _iso(mailing_list.created_at), "archived_at": _iso(mailing_list.archived_at)},
        )
        # Members and links are SETS, so the simplest correct sync is
        # "replace with the current set" rather than computing a diff.
        self._conn.execute("DELETE FROM mailing_list_members WHERE list_id = ?", (mailing_list.list_id,))
        self._conn.executemany("INSERT INTO mailing_list_members (list_id, email) VALUES (?, ?)",
                               [(mailing_list.list_id, email) for email in mailing_list.members])
        self._conn.execute("DELETE FROM mailing_list_sites WHERE list_id = ?", (mailing_list.list_id,))
        self._conn.executemany("INSERT INTO mailing_list_sites (list_id, site_id) VALUES (?, ?)",
                               [(mailing_list.list_id, site_id) for site_id in mailing_list.site_ids])

    def _write_notification(self, notification: Notification) -> None:
        self._conn.execute(
            """
            INSERT INTO notifications (id, site_id, site_name, old_status, new_status, list_ids, recipients,
                                       message, state, created_by, created_at, decided_by, decided_at,
                                       dismiss_reason)
            VALUES (:id, :site_id, :site_name, :old_status, :new_status, :list_ids, :recipients,
                    :message, :state, :created_by, :created_at, :decided_by, :decided_at, :dismiss_reason)
            ON CONFLICT(id) DO UPDATE SET
                message=excluded.message, state=excluded.state, decided_by=excluded.decided_by,
                decided_at=excluded.decided_at, dismiss_reason=excluded.dismiss_reason
            """,
            {
                "id": notification.id,
                "site_id": notification.site_id,
                "site_name": notification.site_name,
                "old_status": notification.old_status.value,
                "new_status": notification.new_status.value,
                "list_ids": json.dumps(list(notification.list_ids)),
                "recipients": json.dumps(sorted(notification.recipients)),
                "message": notification.message,
                "state": notification.state.value,
                "created_by": notification.created_by,
                "created_at": _iso(notification.created_at),
                "decided_by": notification.decided_by,
                "decided_at": _iso(notification.decided_at),
                "dismiss_reason": notification.dismiss_reason,
            },
        )

    # ------------------------------------------------------------------
    # Reading (once, at startup)
    # ------------------------------------------------------------------

    def load_all(self) -> StoredSiteData:
        """
        Rebuild every stored site (with history), mailing list (with members
        and links) and notification. Sites come back in site_id order, lists
        in list_id order, notifications oldest first.
        """
        with self._lock:
            site_rows = self._conn.execute("SELECT * FROM sites ORDER BY site_id").fetchall()
            history_rows = self._conn.execute(
                "SELECT * FROM site_status_history ORDER BY site_id, seq").fetchall()
            list_rows = self._conn.execute("SELECT * FROM mailing_lists ORDER BY list_id").fetchall()
            member_rows = self._conn.execute("SELECT * FROM mailing_list_members").fetchall()
            link_rows = self._conn.execute("SELECT * FROM mailing_list_sites").fetchall()
            notification_rows = self._conn.execute(
                "SELECT * FROM notifications ORDER BY created_at").fetchall()

        history_by_site: dict = {}
        for row in history_rows:
            history_by_site.setdefault(row["site_id"], []).append(SiteStatusChange(
                old_status=SiteStatus(row["old_status"]), new_status=SiteStatus(row["new_status"]),
                reason=row["reason"], actor=row["actor"], changed_at=datetime.fromisoformat(row["changed_at"]),
            ))
        members_by_list: dict = {}
        for row in member_rows:
            members_by_list.setdefault(row["list_id"], []).append(row["email"])
        sites_by_list: dict = {}
        for row in link_rows:
            sites_by_list.setdefault(row["list_id"], []).append(row["site_id"])

        sites = [
            Site.from_persisted(
                site_id=row["site_id"], site_name=row["site_name"], site_url=row["site_url"],
                site_publish_date=date.fromisoformat(row["site_publish_date"]) if row["site_publish_date"] else None,
                status=SiteStatus(row["status"]), history=history_by_site.get(row["site_id"], []),
                created_at=datetime.fromisoformat(row["created_at"]), archived_at=_from_iso(row["archived_at"]),
                archived_list_ids=json.loads(row["archived_list_ids"]),
                last_check=_check_from_json(row["last_check"]),
                consecutive_failures=row["consecutive_failures"],
            )
            for row in site_rows
        ]
        mailing_lists = [
            MailingList.from_persisted(
                list_id=row["list_id"], name=row["name"], members=members_by_list.get(row["list_id"], []),
                site_ids=sites_by_list.get(row["list_id"], []),
                created_at=datetime.fromisoformat(row["created_at"]), archived_at=_from_iso(row["archived_at"]),
            )
            for row in list_rows
        ]
        notifications = [
            Notification.from_persisted(
                notification_id=row["id"], site_id=row["site_id"], site_name=row["site_name"],
                old_status=SiteStatus(row["old_status"]), new_status=SiteStatus(row["new_status"]),
                list_ids=json.loads(row["list_ids"]), recipients=json.loads(row["recipients"]),
                message=row["message"], state=NotificationState(row["state"]), created_by=row["created_by"],
                created_at=datetime.fromisoformat(row["created_at"]), decided_by=row["decided_by"],
                decided_at=_from_iso(row["decided_at"]), dismiss_reason=row["dismiss_reason"],
            )
            for row in notification_rows
        ]
        return StoredSiteData(sites, mailing_lists, notifications)

    def __str__(self) -> str:
        return f"SqliteSiteStore(db_path={self.db_path!r})"

    def __repr__(self) -> str:
        return self.__str__()
