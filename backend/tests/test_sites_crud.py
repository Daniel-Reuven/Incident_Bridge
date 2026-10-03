"""
Tests for SiteDirectory's admin create/edit operations (app/sites.py):
create_site, update_site (all-or-nothing), create_mailing_list,
update_mailing_list, URL uniqueness, and the scan_incidents() wrapper.

Run from backend/:  python -m pytest tests/test_sites_crud.py -v
Uses the shared admin_user / regular_user fixtures from tests/conftest.py.
"""

from datetime import date

import pytest

from app.models import MailingList, MaintenanceTask, Site
from app.persistence_sites import SqliteSiteStore
from app.sites import SiteDirectory


@pytest.fixture
def store():
    return SqliteSiteStore(":memory:")


@pytest.fixture
def directory(store) -> SiteDirectory:
    d = SiteDirectory(store)
    d.add_site(Site(1001, "Portal", "https://portal.example.com", date(2024, 1, 1)))
    d.add_site(Site(1002, "Store", "https://store.example.com"))
    d.add_mailing_list(MailingList("ops", "Ops", ["a@example.com"], [1001]))
    return d


# --- sites -----------------------------------------------------------------

def test_create_site_gets_the_next_id_and_is_saved(directory, store, admin_user):
    site = directory.create_site(admin_user, "Docs", "https://docs.example.com", "2023-05-01")
    assert site.site_id == 1003 and site.site_publish_date == date(2023, 5, 1)
    assert 1003 in {s.site_id for s in store.load_all().sites}


def test_create_site_with_an_explicit_id(directory, admin_user):
    assert directory.create_site(admin_user, "Docs", "https://docs.example.com", site_id=2000).site_id == 2000
    with pytest.raises(ValueError, match="already exists"):
        directory.create_site(admin_user, "Again", "https://again.example.com", site_id=2000)


def test_create_site_rules(directory, admin_user, regular_user):
    with pytest.raises(PermissionError):
        directory.create_site(regular_user, "Docs", "https://docs.example.com")
    with pytest.raises(ValueError, match="site_url"):
        directory.create_site(admin_user, "Docs", "not a url")
    with pytest.raises(ValueError, match="already uses"):
        directory.create_site(admin_user, "Copy", "HTTPS://PORTAL.example.com")


def test_an_archived_sites_url_can_be_reused(directory, admin_user):
    directory.archive_site(1002, admin_user)
    assert directory.create_site(admin_user, "New store", "https://store.example.com").site_id == 1003


def test_update_site_changes_only_the_given_fields_and_saves(directory, store, admin_user):
    directory.update_site(1001, admin_user, {"site_name": "Customer portal"})
    site = directory.get_site(1001)
    assert site.site_name == "Customer portal" and site.site_publish_date == date(2024, 1, 1)
    directory.update_site(1001, admin_user, {"site_publish_date": None})
    assert site.site_publish_date is None
    saved = {s.site_id: s for s in store.load_all().sites}[1001]
    assert saved.site_name == "Customer portal" and saved.site_publish_date is None


def test_update_site_is_all_or_nothing(directory, admin_user):
    with pytest.raises(ValueError, match="future"):
        directory.update_site(1001, admin_user, {"site_name": "Renamed", "site_publish_date": "2999-01-01"})
    site = directory.get_site(1001)
    assert site.site_name == "Portal" and site.site_publish_date == date(2024, 1, 1)


def test_update_site_url_must_stay_unique_but_may_keep_its_own(directory, admin_user):
    with pytest.raises(ValueError, match="already uses"):
        directory.update_site(1001, admin_user, {"site_url": "https://store.example.com"})
    directory.update_site(1001, admin_user, {"site_url": "https://PORTAL.example.com"})   # its own URL, recased
    assert directory.get_site(1001).site_url == "https://PORTAL.example.com"


def test_update_site_rules(directory, admin_user, regular_user):
    with pytest.raises(PermissionError):
        directory.update_site(1001, regular_user, {"site_name": "x"})
    with pytest.raises(ValueError, match="cannot be edited: status"):
        directory.update_site(1001, admin_user, {"status": "down"})
    with pytest.raises(KeyError):
        directory.update_site(9999, admin_user, {"site_name": "x"})
    directory.archive_site(1002, admin_user)
    with pytest.raises(RuntimeError, match="restore it"):
        directory.update_site(1002, admin_user, {"site_name": "x"})


# --- mailing lists ----------------------------------------------------------

def test_create_mailing_list(directory, store, admin_user, regular_user):
    ml = directory.create_mailing_list(admin_user, "mgmt", "Management", ["b@example.com"], [1001, 1002])
    assert ml.site_ids == (1001, 1002)
    assert "mgmt" in {m.list_id for m in store.load_all().mailing_lists}
    with pytest.raises(PermissionError):
        directory.create_mailing_list(regular_user, "x", "X")
    with pytest.raises(ValueError, match="no such site"):
        directory.create_mailing_list(admin_user, "y", "Y", site_ids=[9999])


def test_update_mailing_list_replaces_members_and_sites(directory, store, admin_user):
    directory.update_mailing_list("ops", admin_user, name="Operations", members=["b@example.com", "C@example.com"],
                                  site_ids=[1002])
    ml = directory.get_mailing_list("ops")
    assert (ml.name, ml.members, ml.site_ids) == ("Operations", ("b@example.com", "c@example.com"), (1002,))
    saved = {m.list_id: m for m in store.load_all().mailing_lists}["ops"]
    assert saved.members == ("b@example.com", "c@example.com") and saved.site_ids == (1002,)


def test_update_mailing_list_leaves_omitted_parts_alone(directory, admin_user):
    directory.update_mailing_list("ops", admin_user, name="Renamed")
    ml = directory.get_mailing_list("ops")
    assert ml.members == ("a@example.com",) and ml.site_ids == (1001,)


@pytest.mark.parametrize("kwargs, message", [
    ({"name": "Ok", "members": ["broken"]}, "email"),
    ({"name": "Ok", "site_ids": [9999]}, "cannot be linked"),
    ({"name": "Ok", "site_ids": [0]}, "site_id"),
    ({"name": " ", "members": ["z@example.com"]}, "name"),
])
def test_invalid_list_edit_changes_nothing(directory, admin_user, kwargs, message):
    with pytest.raises(ValueError, match=message):
        directory.update_mailing_list("ops", admin_user, **kwargs)
    ml = directory.get_mailing_list("ops")
    assert (ml.name, ml.members, ml.site_ids) == ("Ops", ("a@example.com",), (1001,))


def test_update_mailing_list_rules(directory, admin_user, regular_user):
    with pytest.raises(PermissionError):
        directory.update_mailing_list("ops", regular_user, name="x")
    with pytest.raises(KeyError):
        directory.update_mailing_list("nope", admin_user, name="x")
    directory.archive_mailing_list("ops", admin_user)
    with pytest.raises(RuntimeError, match="restore it"):
        directory.update_mailing_list("ops", admin_user, name="x")


# --- scan wrapper -------------------------------------------------------------

def test_scan_incidents_uses_the_directorys_sites(directory, admin_user):
    directory.archive_site(1002, admin_user)
    incident = MaintenanceTask("Site 1001, Site 1002 and Site 7777", "d", admin_user)
    result = directory.scan_incidents([incident])
    assert set(result.by_site) == {1001} and set(result.archived) == {1002} and set(result.unknown) == {7777}


def test_scan_incidents_matches_full_site_names(directory, admin_user):
    by_name = MaintenanceTask("Error loading Portal", "d", admin_user)          # site 1001 is named "Portal"
    partial = MaintenanceTask("The Store front is slow", "d", admin_user)     # "Store" is site 1002's full name
    neither = MaintenanceTask("Portals everywhere", "d", admin_user)
    result = directory.scan_incidents([by_name, partial, neither])
    assert result.incidents_for(1001) == (by_name.id,)
    assert result.incidents_for(1002) == (partial.id,)


def test_renaming_a_site_changes_name_matches(directory, admin_user):
    incident = MaintenanceTask("Customer dashboard is down", "d", admin_user)
    assert directory.scan_incidents([incident]).incidents_for(1001) == ()
    directory.update_site(1001, admin_user, {"site_name": "Customer dashboard"})
    assert directory.scan_incidents([incident]).incidents_for(1001) == (incident.id,)
