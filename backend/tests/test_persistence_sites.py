"""
Tests for app/persistence_sites.py (SqliteSiteStore): every object type
survives a save -> load round trip, updates are upserts, history is
append-only, and data really survives a "restart" (a second store opened
on the same file).

Run from backend/:  python -m pytest tests/test_persistence_sites.py -v
Uses an in-memory database, or a temporary file for the restart test.
Uses the shared admin_user fixture from tests/conftest.py.
"""

from datetime import date

import pytest

from app.models import MailingList, Notification, NotificationState, Site, SiteStatus
from app.persistence_sites import SqliteSiteStore


@pytest.fixture
def store() -> SqliteSiteStore:
    return SqliteSiteStore(":memory:")


@pytest.fixture
def site() -> Site:
    return Site(1042, "Portal", "https://example.com", date(2025, 3, 1))


def test_empty_store_loads_nothing(store):
    assert store.load_all() == ([], [], [])


def test_site_round_trip_with_history_and_archive(store, site, admin_user):
    site.change_status(SiteStatus.DOWN, "HTTP 503")
    site.change_status(SiteStatus.OPERATIONAL, "Recovered", actor=admin_user)
    site.archive(admin_user, ["ops", "mgmt"])
    store.save_site(site)

    (loaded,), _, _ = store.load_all()
    assert (loaded.site_id, loaded.site_name, loaded.site_url, loaded.site_publish_date) == (
        1042, "Portal", "https://example.com", date(2025, 3, 1))
    assert loaded.status == SiteStatus.OPERATIONAL
    assert loaded.status_history == site.status_history
    assert loaded.created_at == site.created_at
    assert loaded.archived_at == site.archived_at
    assert loaded.archived_list_ids == frozenset({"ops", "mgmt"})


def test_saving_again_updates_and_appends_history_once(store, site):
    store.save_site(site)
    site.site_name = "Renamed"
    site.change_status(SiteStatus.DOWN, "one")
    store.save_site(site)
    store.save_site(site)          # saving twice must not duplicate history
    site.change_status(SiteStatus.DEGRADED, "two")
    store.save_site(site)

    (loaded,), _, _ = store.load_all()
    assert loaded.site_name == "Renamed"
    assert [c.reason for c in loaded.status_history] == ["one", "two"]


def test_site_without_publish_date(store):
    store.save_site(Site(7, "No date", "https://example.org"))
    (loaded,), _, _ = store.load_all()
    assert loaded.site_publish_date is None


def test_mailing_list_round_trip_replaces_members_and_links(store, admin_user):
    ml = MailingList("ops", "Ops", ["a@example.com", "b@example.com"], [1001, 1002])
    store.save_mailing_list(ml)
    ml.remove_member("a@example.com")
    ml.add_member("c@example.com")
    ml.unlink_site(1001)
    ml.archive(admin_user)
    store.save_mailing_list(ml)

    _, (loaded,), _ = store.load_all()
    assert loaded.members == ("b@example.com", "c@example.com")
    assert loaded.site_ids == (1002,)
    assert loaded.is_archived and loaded.archived_at == ml.archived_at
    assert loaded.created_at == ml.created_at


def test_notification_round_trip_and_update(store, site, admin_user):
    site.change_status(SiteStatus.DOWN, "x")
    n = Notification.draft_for(site, SiteStatus.UNKNOWN, [MailingList("ops", "Ops", ["a@example.com"], [1042])],
                               created_by=admin_user)
    store.save_notification(n)
    n.dismiss(admin_user, "false alarm")
    store.save_notification(n)

    _, _, (loaded,) = store.load_all()
    assert (loaded.id, loaded.state, loaded.dismiss_reason, loaded.decided_by) == (
        n.id, NotificationState.DISMISSED, "false alarm", admin_user.username)
    assert loaded.recipients == n.recipients and loaded.list_ids == ("ops",)
    assert (loaded.old_status, loaded.new_status, loaded.message) == (SiteStatus.UNKNOWN, SiteStatus.DOWN, n.message)


def test_save_batch_is_all_or_nothing(store, site):
    class Broken:
        """Not a MailingList - writing it fails halfway through the batch."""
        list_id = "broken"
        name = "Broken"
        created_at = None

    with pytest.raises(Exception):
        store.save_batch(sites=[site], mailing_lists=[Broken()])
    assert store.load_all().sites == []   # the site written before the failure was rolled back


def test_data_survives_a_restart(tmp_path, site, admin_user):
    db_file = str(tmp_path / "sites.db")
    first = SqliteSiteStore(db_file)
    site.change_status(SiteStatus.DOWN, "x")
    first.save_batch(sites=[site], mailing_lists=[MailingList("ops", "Ops", ["a@example.com"], [1042])])

    second = SqliteSiteStore(db_file)   # a new process opening the same file
    sites, lists, _ = second.load_all()
    assert [s.site_id for s in sites] == [1042] and sites[0].status == SiteStatus.DOWN
    assert [ml.list_id for ml in lists] == ["ops"]


def test_str(store):
    assert str(store) == "SqliteSiteStore(db_path=':memory:')"


# --- availability-check state (step 4) -------------------------------------

def test_last_check_and_failure_count_round_trip(store, site):
    from app.services import FakeChecker
    fake = FakeChecker({site.site_url: [None]})
    site.record_check(fake.check(site.site_url))
    store.save_site(site)
    (loaded,), _, _ = store.load_all()
    assert loaded.consecutive_failures == 1
    assert loaded.last_check == site.last_check


def test_site_never_checked_has_no_last_check(store, site):
    store.save_site(site)
    (loaded,), _, _ = store.load_all()
    assert loaded.last_check is None and loaded.consecutive_failures == 0


def test_an_older_database_file_gets_the_new_columns(tmp_path, site):
    """A sites table created by step 3 (without the check columns) is upgraded when the store opens."""
    import sqlite3
    db_file = str(tmp_path / "old.db")
    conn = sqlite3.connect(db_file)
    conn.execute("""CREATE TABLE sites (site_id INTEGER PRIMARY KEY, site_name TEXT NOT NULL,
                    site_url TEXT NOT NULL, site_publish_date TEXT, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, archived_at TEXT, archived_list_ids TEXT NOT NULL DEFAULT '[]')""")
    conn.execute("INSERT INTO sites (site_id, site_name, site_url, status, created_at) "
                 "VALUES (1, 'Old', 'https://example.com', 'unknown', '2026-01-01T00:00:00+00:00')")
    conn.commit()
    conn.close()

    store = SqliteSiteStore(db_file)
    (old,), _, _ = store.load_all()
    assert old.site_name == "Old" and old.last_check is None and old.consecutive_failures == 0
    store.save_site(site)   # writing the new columns works too
    assert len(store.load_all().sites) == 2
