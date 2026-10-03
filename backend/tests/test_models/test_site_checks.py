"""
Unit tests for Site.record_check() - the availability-check state rules
(app/models/site.py): FAILURES_BEFORE_DOWN, recovery, maintenance and archive.

Run from backend/:  python -m pytest tests/test_models/test_site_checks.py -v
Uses the shared admin_user fixture from tests/conftest.py.
"""

from datetime import datetime, timezone

import pytest

from app.models import FAILURES_BEFORE_DOWN, SYSTEM_ACTOR, AvailabilityCheck, Site, SiteStatus


def check(status: SiteStatus, http_status=None, response_ms=None, error=None) -> AvailabilityCheck:
    if status != SiteStatus.DOWN and http_status is None:
        http_status, response_ms = 200, 100.0
    if status == SiteStatus.DOWN and http_status is None and error is None:
        error = "timed out after 5s"
    return AvailabilityCheck(datetime.now(timezone.utc), status, http_status, response_ms, error)


@pytest.fixture
def site() -> Site:
    return Site(1042, "Portal", "https://example.com")


def test_rule_needs_two_failures():
    assert FAILURES_BEFORE_DOWN == 2


def test_first_successful_check_makes_it_operational(site):
    change = site.record_check(check(SiteStatus.OPERATIONAL))
    assert site.status == SiteStatus.OPERATIONAL
    assert change.actor == SYSTEM_ACTOR
    assert change.reason == "Automated check: HTTP 200 in 100 ms"
    assert site.last_check.http_status == 200


def test_one_failure_is_only_degraded_the_second_is_down(site):
    site.record_check(check(SiteStatus.OPERATIONAL))
    first = site.record_check(check(SiteStatus.DOWN))
    assert site.status == SiteStatus.DEGRADED and site.consecutive_failures == 1
    assert first.reason == "Automated check: no response: timed out after 5s (failure 1 in a row)"
    second = site.record_check(check(SiteStatus.DOWN, http_status=503, response_ms=40.0))
    assert site.status == SiteStatus.DOWN and site.consecutive_failures == 2
    assert second.reason == "Automated check: HTTP 503 in 40 ms (failure 2 in a row)"


def test_further_failures_add_no_history(site):
    for _ in range(4):
        site.record_check(check(SiteStatus.DOWN))
    assert site.consecutive_failures == 4
    assert [c.new_status for c in site.status_history] == [SiteStatus.DEGRADED, SiteStatus.DOWN]


def test_a_success_resets_the_streak(site):
    site.record_check(check(SiteStatus.DOWN))
    site.record_check(check(SiteStatus.OPERATIONAL))
    assert site.consecutive_failures == 0
    site.record_check(check(SiteStatus.DOWN))
    assert site.status == SiteStatus.DEGRADED   # a fresh streak starts at 1 again


def test_recovery_from_down(site):
    site.record_check(check(SiteStatus.DOWN))
    site.record_check(check(SiteStatus.DOWN))
    change = site.record_check(check(SiteStatus.OPERATIONAL))
    assert (change.old_status, change.new_status) == (SiteStatus.DOWN, SiteStatus.OPERATIONAL)


def test_observed_degraded_is_taken_as_is(site):
    site.record_check(check(SiteStatus.DEGRADED, http_status=404, response_ms=30.0))
    assert site.status == SiteStatus.DEGRADED and site.consecutive_failures == 0


def test_same_status_records_the_check_but_no_change(site):
    site.record_check(check(SiteStatus.OPERATIONAL))
    second = check(SiteStatus.OPERATIONAL)
    assert site.record_check(second) is None
    assert site.last_check is second and len(site.status_history) == 1


def test_maintenance_keeps_status_and_does_not_count_failures(site, admin_user):
    site.change_status(SiteStatus.MAINTENANCE, "Planned upgrade", actor=admin_user)
    assert site.record_check(check(SiteStatus.DOWN)) is None
    assert site.status == SiteStatus.MAINTENANCE
    assert site.consecutive_failures == 0
    assert site.last_check.observed_status == SiteStatus.DOWN


def test_manual_change_resets_the_streak(site, admin_user):
    site.record_check(check(SiteStatus.DOWN))
    site.change_status(SiteStatus.OPERATIONAL, "Verified by hand", actor=admin_user)
    assert site.consecutive_failures == 0


def test_archived_sites_cannot_record_checks(site, admin_user):
    site.archive(admin_user)
    with pytest.raises(RuntimeError, match="archived"):
        site.record_check(check(SiteStatus.OPERATIONAL))


def test_restore_resets_the_streak_but_keeps_the_last_check(site, admin_user):
    site.record_check(check(SiteStatus.DOWN))
    site.archive(admin_user)
    site.restore(admin_user)
    assert site.consecutive_failures == 0 and site.last_check is not None


@pytest.mark.parametrize("bad", ["down", None, 503])
def test_record_check_requires_a_check(site, bad):
    with pytest.raises(ValueError, match="AvailabilityCheck"):
        site.record_check(bad)


@pytest.mark.parametrize("status", [SiteStatus.UNKNOWN, SiteStatus.MAINTENANCE])
def test_a_check_cannot_observe_unknown_or_maintenance(site, status):
    with pytest.raises(ValueError, match="can only observe"):
        site.record_check(AvailabilityCheck(datetime.now(timezone.utc), status))
