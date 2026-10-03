"""
Tests for SiteDirectory's archive/restore cascades, notification holding,
and saving through the store (app/sites.py, step 3 of the Sites & Mailing
Lists subsystem).

Run from backend/:  python -m pytest tests/test_sites_archive.py -v
Uses the shared admin_user / regular_user fixtures from tests/conftest.py.
"""

import pytest

from app.models import MailingList, Notification, Site, SiteStatus
from app.persistence_sites import SqliteSiteStore
from app.sites import SiteDirectory


@pytest.fixture
def store() -> SqliteSiteStore:
    return SqliteSiteStore(":memory:")


@pytest.fixture
def directory(store, admin_user) -> SiteDirectory:
    d = SiteDirectory(store)
    d.add_site(Site(1001, "Portal", "https://example.com"))
    d.add_site(Site(1002, "Store", "https://example.org"))
    d.add_mailing_list(MailingList("ops", "Ops", ["a@example.com"], [1001, 1002]))
    d.add_mailing_list(MailingList("mgmt", "Mgmt", ["b@example.com"], [1001]))
    d.add_mailing_list(MailingList("dormant", "Dormant", ["c@example.com"], [1001]))
    d.archive_mailing_list("dormant", admin_user)
    return d


def stored(store):
    """What a restart would see."""
    sites, lists, notifications = store.load_all()
    return {s.site_id: s for s in sites}, {ml.list_id: ml for ml in lists}, notifications


# --- archive_site ------------------------------------------------------

def test_archive_site_unlinks_it_from_every_list_including_archived(directory, admin_user):
    unlinked = directory.archive_site(1001, admin_user)
    assert unlinked == ["dormant", "mgmt", "ops"]
    assert directory.get_site(1001).is_archived
    for list_id in unlinked:
        assert not directory.get_mailing_list(list_id).covers(1001)
    assert directory.get_mailing_list("ops").covers(1002)   # other links untouched


def test_archive_site_is_saved(directory, store, admin_user):
    directory.archive_site(1001, admin_user)
    sites, lists, _ = stored(store)
    assert sites[1001].is_archived
    assert sites[1001].archived_list_ids == frozenset({"dormant", "mgmt", "ops"})
    assert lists["ops"].site_ids == (1002,) and lists["mgmt"].site_ids == ()


def test_archive_site_errors_change_nothing(directory, regular_user, admin_user):
    with pytest.raises(PermissionError):
        directory.archive_site(1001, regular_user)
    assert directory.get_mailing_list("ops").covers(1001)
    with pytest.raises(KeyError):
        directory.archive_site(9999, admin_user)
    directory.archive_site(1001, admin_user)
    with pytest.raises(RuntimeError, match="already archived"):
        directory.archive_site(1001, admin_user)


def test_archived_site_disappears_from_listings_but_keeps_its_id(directory, admin_user):
    directory.archive_site(1002, admin_user)
    assert [s.site_id for s in directory.sites()] == [1001]
    assert directory.next_site_id() == 1003


# --- restore_site ------------------------------------------------------

def test_restore_site_relinks_its_lists(directory, admin_user):
    directory.get_site(1001).change_status(SiteStatus.DOWN, "x")
    directory.archive_site(1001, admin_user)
    relinked = directory.restore_site(1001, admin_user)
    assert relinked == ["dormant", "mgmt", "ops"]
    site = directory.get_site(1001)
    assert not site.is_archived and site.status == SiteStatus.UNKNOWN
    assert all(directory.get_mailing_list(i).covers(1001) for i in relinked)
    # the archived list got its link back too, but still sends nothing while archived
    assert [ml.list_id for ml in directory.lists_covering(1001)] == ["mgmt", "ops"]


def test_restore_site_is_saved(directory, store, admin_user):
    directory.archive_site(1001, admin_user)
    directory.restore_site(1001, admin_user)
    sites, lists, _ = stored(store)
    assert not sites[1001].is_archived and sites[1001].archived_list_ids == frozenset()
    assert lists["ops"].site_ids == (1001, 1002) and lists["mgmt"].site_ids == (1001,)


def test_restore_errors(directory, admin_user, regular_user):
    with pytest.raises(RuntimeError, match="not archived"):
        directory.restore_site(1001, admin_user)
    directory.archive_site(1001, admin_user)
    with pytest.raises(PermissionError):
        directory.restore_site(1001, regular_user)
    assert directory.get_site(1001).is_archived


# --- mailing list archive / restore -------------------------------------

def test_archive_and_restore_mailing_list_keep_links_and_are_saved(directory, store, admin_user):
    directory.archive_mailing_list("ops", admin_user)
    assert [ml.list_id for ml in directory.mailing_lists()] == ["mgmt"]
    assert stored(store)[1]["ops"].is_archived
    directory.restore_mailing_list("ops", admin_user)
    _, lists, _ = stored(store)
    assert not lists["ops"].is_archived and lists["ops"].site_ids == (1001, 1002)


def test_mailing_list_archive_requires_admin(directory, regular_user):
    with pytest.raises(PermissionError):
        directory.archive_mailing_list("ops", regular_user)


# --- notifications -------------------------------------------------------

def test_notifications_are_held_newest_first_and_saved(directory, store, admin_user):
    site = directory.get_site(1001)
    site.change_status(SiteStatus.DOWN, "x")
    first = Notification.draft_for(site, SiteStatus.UNKNOWN, directory.mailing_lists())
    site.change_status(SiteStatus.OPERATIONAL, "y")
    second = Notification.draft_for(site, SiteStatus.DOWN, directory.mailing_lists())
    directory.add_notification(first)
    directory.add_notification(second)
    assert [n.id for n in directory.notifications()] == [second.id, first.id]
    assert directory.get_notification(first.id) is first
    assert {n.id for n in stored(store)[2]} == {first.id, second.id}
    with pytest.raises(ValueError, match="already exists"):
        directory.add_notification(first)
    with pytest.raises(KeyError):
        directory.get_notification("nope")


# --- saving and loading ---------------------------------------------------

def test_direct_edits_need_an_explicit_save(directory, store):
    directory.get_site(1001).site_name = "Renamed"
    assert stored(store)[0][1001].site_name == "Portal"
    directory.save_site(directory.get_site(1001))
    assert stored(store)[0][1001].site_name == "Renamed"


def test_bulk_load_then_seed_skips_stored_ids(store, admin_user):
    first = SiteDirectory(store)
    first.add_site(Site(1001, "Edited in the app", "https://example.com"))
    first.archive_site(1001, admin_user)

    restarted = SiteDirectory(store)
    restarted.bulk_load(*store.load_all())
    report = restarted.load_sites_from_lines(['{"site_id": 1001, "site_name": "Seed", '
                                              '"site_url": "https://example.com"}'])
    assert report.already_stored_ids == [1001] and report.problems == []
    assert restarted.get_site(1001).site_name == "Edited in the app"
    assert restarted.get_site(1001).is_archived   # the seed file did not bring it back


def test_memory_only_directory_works_without_a_store(admin_user):
    d = SiteDirectory()
    d.add_site(Site(1, "x", "https://example.com"))
    d.archive_site(1, admin_user)
    d.restore_site(1, admin_user)
    assert not d.get_site(1).is_archived
