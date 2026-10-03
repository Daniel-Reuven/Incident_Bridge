"""
Unit tests for app.models.site.Site (Sites & Mailing Lists subsystem).
Plain domain objects only - no HTTP, no database.

Run from backend/:  python -m pytest tests/test_models/test_site.py -v
Uses the shared admin_user / regular_user fixtures from tests/conftest.py.
"""

from datetime import date, timedelta

import pytest

from app.models import SYSTEM_ACTOR, Site, SiteStatus


@pytest.fixture
def site() -> Site:
    return Site(site_id=1042, site_name="Customer portal", site_url="https://example.com",
                site_publish_date=date(2025, 3, 1))


# --- construction and validation -------------------------------------

def test_new_site_starts_unknown_with_empty_history(site):
    assert site.status == SiteStatus.UNKNOWN
    assert site.status_history == ()
    assert not site.is_archived
    assert site.label == "Site 1042"


@pytest.mark.parametrize("bad_id", [0, -5, "1042", 10.0, True, None])
def test_invalid_site_id_is_rejected(bad_id):
    with pytest.raises(ValueError, match="site_id"):
        Site(site_id=bad_id, site_name="x", site_url="https://example.com")


def test_site_id_is_read_only(site):
    with pytest.raises(AttributeError):
        site.site_id = 7


@pytest.mark.parametrize("bad_name", ["", "   ", None, 5, "x" * 101])
def test_invalid_name_is_rejected_on_creation_and_edit(site, bad_name):
    with pytest.raises(ValueError, match="site_name"):
        Site(site_id=1, site_name=bad_name, site_url="https://example.com")
    with pytest.raises(ValueError, match="site_name"):
        site.site_name = bad_name
    assert site.site_name == "Customer portal"  # an invalid edit leaves the old value


def test_name_and_url_are_trimmed():
    s = Site(site_id=1, site_name="  Portal  ", site_url="  https://example.com/x  ")
    assert s.site_name == "Portal"
    assert s.site_url == "https://example.com/x"


@pytest.mark.parametrize("bad_url", ["", "example.com", "ftp://example.com", "file:///etc/passwd",
                                     "https://", "javascript:alert(1)", None])
def test_invalid_url_is_rejected(site, bad_url):
    with pytest.raises(ValueError, match="site_url"):
        site.site_url = bad_url
    assert site.site_url == "https://example.com"


def test_http_url_is_allowed(site):
    site.site_url = "http://intranet.example.com:8080/health"
    assert site.site_url == "http://intranet.example.com:8080/health"


def test_publish_date_accepts_none_date_and_iso_string(site):
    site.site_publish_date = None
    assert site.site_publish_date is None
    site.site_publish_date = "2024-12-31"
    assert site.site_publish_date == date(2024, 12, 31)


@pytest.mark.parametrize("bad_date", ["31/12/2024", "2024-13-01", 20240101, "soon"])
def test_badly_formatted_publish_date_is_rejected(site, bad_date):
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        site.site_publish_date = bad_date


def test_future_publish_date_is_rejected(site):
    with pytest.raises(ValueError, match="future"):
        site.site_publish_date = date.today() + timedelta(days=1)


# --- from_dict ---------------------------------------------------------

def test_from_dict_builds_a_site():
    s = Site.from_dict({"site_id": 7, "site_name": "Docs", "site_url": "https://docs.example.com",
                        "site_publish_date": "2023-05-20", "unexpected": "ignored"})
    assert (s.site_id, s.site_name, s.site_publish_date) == (7, "Docs", date(2023, 5, 20))


def test_from_dict_publish_date_is_optional():
    assert Site.from_dict({"site_id": 7, "site_name": "Docs",
                           "site_url": "https://docs.example.com"}).site_publish_date is None


def test_from_dict_never_takes_status_from_seed_data():
    s = Site.from_dict({"site_id": 7, "site_name": "Docs", "site_url": "https://docs.example.com",
                        "status": "down"})
    assert s.status == SiteStatus.UNKNOWN


@pytest.mark.parametrize("missing", ["site_id", "site_name", "site_url"])
def test_from_dict_missing_required_field(missing):
    data = {"site_id": 7, "site_name": "Docs", "site_url": "https://docs.example.com"}
    del data[missing]
    with pytest.raises(ValueError, match=f"missing required field '{missing}'"):
        Site.from_dict(data)


def test_from_dict_error_names_the_record():
    with pytest.raises(ValueError, match="site record 7.*site_url"):
        Site.from_dict({"site_id": 7, "site_name": "Docs", "site_url": "nope"})


@pytest.mark.parametrize("not_a_dict", [[1, 2], "text", 42, None])
def test_from_dict_rejects_non_objects(not_a_dict):
    with pytest.raises(ValueError, match="JSON object"):
        Site.from_dict(not_a_dict)


# --- status changes ----------------------------------------------------

def test_manual_status_change_is_recorded(site, admin_user):
    change = site.change_status(SiteStatus.DOWN, "  Customers report errors  ", actor=admin_user)
    assert site.status == SiteStatus.DOWN
    assert change.old_status == SiteStatus.UNKNOWN
    assert change.new_status == SiteStatus.DOWN
    assert change.reason == "Customers report errors"
    assert change.actor == admin_user.username
    assert site.status_history == (change,)


def test_automated_status_change_is_attributed_to_system(site):
    change = site.change_status(SiteStatus.OPERATIONAL, "HTTP 200 in 120 ms")
    assert change.actor == SYSTEM_ACTOR


def test_same_status_again_records_nothing(site):
    site.change_status(SiteStatus.OPERATIONAL, "check 1")
    assert site.change_status(SiteStatus.OPERATIONAL, "check 2") is None
    assert len(site.status_history) == 1


def test_non_admin_cannot_change_status(site, regular_user):
    with pytest.raises(PermissionError):
        site.change_status(SiteStatus.DOWN, "reason", actor=regular_user)
    assert site.status == SiteStatus.UNKNOWN and site.status_history == ()


@pytest.mark.parametrize("reason", ["", "   ", None])
def test_status_change_requires_a_reason(site, admin_user, reason):
    with pytest.raises(ValueError, match="reason"):
        site.change_status(SiteStatus.DOWN, reason, actor=admin_user)


def test_cannot_set_unknown(site, admin_user):
    with pytest.raises(ValueError, match="Unknown"):
        site.change_status(SiteStatus.UNKNOWN, "reset", actor=admin_user)


def test_maintenance_is_manual_only(site, admin_user):
    with pytest.raises(ValueError, match="manually"):
        site.change_status(SiteStatus.MAINTENANCE, "automated")
    assert site.change_status(SiteStatus.MAINTENANCE, "Planned upgrade", actor=admin_user) is not None


def test_status_must_be_a_site_status(site, admin_user):
    with pytest.raises(ValueError, match="SiteStatus"):
        site.change_status("down", "reason", actor=admin_user)


def test_history_cannot_be_edited_by_callers(site):
    site.change_status(SiteStatus.DOWN, "x")
    with pytest.raises(AttributeError):
        site.status_history.append("tampered")  # tuples have no append


# --- archive / restore -------------------------------------------------

def test_archive_remembers_lists_and_blocks_status_changes(site, admin_user):
    site.archive(admin_user, list_ids=["ops-team", "management"])
    assert site.is_archived
    assert site.archived_list_ids == frozenset({"ops-team", "management"})
    with pytest.raises(RuntimeError, match="archived"):
        site.change_status(SiteStatus.DOWN, "x", actor=admin_user)


def test_archive_twice_is_an_error(site, admin_user):
    site.archive(admin_user)
    with pytest.raises(RuntimeError, match="already archived"):
        site.archive(admin_user)


def test_restore_returns_lists_and_resets_status_to_unknown(site, admin_user):
    site.change_status(SiteStatus.DOWN, "x", actor=admin_user)
    site.archive(admin_user, list_ids=["ops-team"])
    assert site.restore(admin_user) == frozenset({"ops-team"})
    assert not site.is_archived
    assert site.archived_list_ids == frozenset()
    assert site.status == SiteStatus.UNKNOWN
    assert site.status_history[-1].reason == "Restored from archive"


def test_restore_of_a_never_checked_site_adds_no_history(site, admin_user):
    site.archive(admin_user)
    site.restore(admin_user)
    assert site.status_history == ()


def test_restore_when_not_archived_is_an_error(site, admin_user):
    with pytest.raises(RuntimeError, match="not archived"):
        site.restore(admin_user)


def test_only_admin_can_archive_and_restore(site, admin_user, regular_user):
    with pytest.raises(PermissionError):
        site.archive(regular_user)
    site.archive(admin_user)
    with pytest.raises(PermissionError):
        site.restore(regular_user)


# --- dunder methods ----------------------------------------------------

def test_equality_and_hash_are_by_site_id(site):
    twin = Site(site_id=1042, site_name="Other name", site_url="https://other.example.com")
    assert site == twin
    assert len({site, twin}) == 1
    assert site != Site(site_id=1, site_name="x", site_url="https://example.com")


def test_str_and_repr(site, admin_user):
    assert str(site) == "[Site 1042 - UNKNOWN] Customer portal"
    site.archive(admin_user)
    assert str(site).endswith("(archived)")
    assert repr(site).startswith("Site(site_id=1042, site_name='Customer portal'")
