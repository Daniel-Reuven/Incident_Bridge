"""
Tests for SiteDirectory.check_site() / check_all() (app/sites.py): the
checker is used through its interface, results are recorded and saved,
archived and maintenance sites are handled, and concurrent checks of one
site are refused. Uses FakeChecker - no network.

Run from backend/:  python -m pytest tests/test_sites_checks.py -v
Uses the shared admin_user fixture from tests/conftest.py.
"""

import pytest

from app.context import SiteCheckSession
from app.models import Site, SiteStatus
from app.persistence_sites import SqliteSiteStore
from app.services import FakeChecker
from app.sites import SiteDirectory

PORTAL, STORE, DOCS = "https://portal.example.com", "https://store.example.com", "https://docs.example.com"


@pytest.fixture
def store() -> SqliteSiteStore:
    return SqliteSiteStore(":memory:")


def make_directory(store, script) -> SiteDirectory:
    d = SiteDirectory(store, checker=FakeChecker(script))
    d.add_site(Site(1001, "Portal", PORTAL))
    d.add_site(Site(1002, "Store", STORE))
    d.add_site(Site(1003, "Docs", DOCS))
    return d


def test_check_site_records_the_result_and_saves_it(store):
    d = make_directory(store, {PORTAL: [503, 503]})
    first = d.check_site(1001)
    assert (first.site_id, first.check.http_status, first.change.new_status) == (1001, 503, SiteStatus.DEGRADED)
    assert first.duration_ms >= 0
    second = d.check_site(1001)
    assert second.change.new_status == SiteStatus.DOWN

    (saved, *_), _, _ = store.load_all()
    assert saved.status == SiteStatus.DOWN and saved.consecutive_failures == 2
    assert saved.last_check.http_status == 503
    assert [c.new_status for c in saved.status_history] == [SiteStatus.DEGRADED, SiteStatus.DOWN]


def test_unchanged_status_gives_no_change(store):
    d = make_directory(store, {})
    assert d.check_site(1001).change is not None   # Unknown -> Operational
    assert d.check_site(1001).change is None


def test_check_all_skips_archived_and_maintenance_sites(store, admin_user):
    d = make_directory(store, {STORE: [None]})
    d.archive_site(1002, admin_user)
    d.get_site(1003).change_status(SiteStatus.MAINTENANCE, "upgrade", actor=admin_user)
    outcomes = d.check_all()
    assert [o.site_id for o in outcomes] == [1001]
    assert d._checker.calls == [PORTAL]


def test_check_all_returns_one_outcome_per_checked_site_in_id_order(store):
    d = make_directory(store, {STORE: [None]})
    outcomes = d.check_all()
    assert [(o.site_id, o.check.observed_status) for o in outcomes] == [
        (1001, SiteStatus.OPERATIONAL), (1002, SiteStatus.DOWN), (1003, SiteStatus.OPERATIONAL)]
    assert d.get_site(1002).status == SiteStatus.DEGRADED   # one failure only


def test_a_maintenance_site_can_still_be_checked_individually(store, admin_user):
    d = make_directory(store, {DOCS: [None]})
    d.get_site(1003).change_status(SiteStatus.MAINTENANCE, "upgrade", actor=admin_user)
    outcome = d.check_site(1003)
    assert outcome.change is None
    assert d.get_site(1003).status == SiteStatus.MAINTENANCE
    assert d.get_site(1003).last_check.observed_status == SiteStatus.DOWN


def test_archived_site_cannot_be_checked_and_is_never_requested(store, admin_user):
    d = make_directory(store, {})
    d.archive_site(1001, admin_user)
    with pytest.raises(RuntimeError, match="archived"):
        d.check_site(1001)
    assert d._checker.calls == []


def test_unknown_site_and_missing_checker(store):
    d = make_directory(store, {})
    with pytest.raises(KeyError):
        d.check_site(9999)
    bare = SiteDirectory()
    bare.add_site(Site(1, "x", "https://example.com"))
    with pytest.raises(RuntimeError, match="No availability checker"):
        bare.check_site(1)
    with pytest.raises(RuntimeError, match="No availability checker"):
        bare.check_all()


def test_a_site_being_checked_elsewhere_is_refused_or_skipped(store):
    d = make_directory(store, {})
    with SiteCheckSession(d.get_site(1002)):          # simulates another request mid-check
        with pytest.raises(RuntimeError, match="already being checked"):
            d.check_site(1002)
        assert [o.site_id for o in d.check_all()] == [1001, 1003]
    assert d.check_site(1002).site_id == 1002          # free again afterwards
