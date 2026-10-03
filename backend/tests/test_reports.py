"""
Unit tests for app/reports.py: the site health report (decision-support
tool) built from a SiteDirectory and incidents.

Run from backend/:  python -m pytest tests/test_reports.py -v
Memory-only directory and plain domain objects - no HTTP, no database.
Uses the shared admin_user fixture from tests/conftest.py.
"""

from datetime import datetime, timezone

import pytest

from app.models import AvailabilityCheck, MailingList, MaintenanceTask, Site, SiteStatus
from app.reports import SiteReportRow, attention_key, build_site_report, count_by
from app.sites import SiteDirectory


@pytest.fixture
def directory(admin_user) -> SiteDirectory:
    d = SiteDirectory()
    for site_id, name in [(1001, "Portal"), (1002, "Store"), (1003, "Docs"), (1004, "HR"), (1005, "Old")]:
        d.add_site(Site(site_id, name, f"https://s{site_id}.example.com"))
    d.add_mailing_list(MailingList("ops", "Ops", ["ops@example.com"], [1001, 1002]))
    d.add_mailing_list(MailingList("empty", "Zeta empty", [], [1003]))
    d.add_mailing_list(MailingList("nosites", "Alpha no sites", ["x@example.com"], []))
    d.change_site_status(1001, SiteStatus.DOWN, "outage", admin_user)       # mentioned below
    d.change_site_status(1002, SiteStatus.DEGRADED, "slow", admin_user)     # NOT mentioned
    d.change_site_status(1003, SiteStatus.OPERATIONAL, "ok", admin_user)    # mentioned below
    d.archive_site(1005, admin_user)
    return d


@pytest.fixture
def incidents(admin_user):
    a = MaintenanceTask("Site 1001 outage follow-up", "and Site 1003 is slow", admin_user)
    b = MaintenanceTask("Site 1001 again", "nothing else", admin_user)
    c = MaintenanceTask("Check Site 1005 and Site 4242", "old references", admin_user)
    return [a, b, c]


def test_rows_are_ordered_most_urgent_first(directory, incidents):
    report = build_site_report(directory, incidents)
    assert [(r.site_id, r.status) for r in report.rows] == [
        (1001, SiteStatus.DOWN), (1002, SiteStatus.DEGRADED), (1004, SiteStatus.UNKNOWN),
        (1003, SiteStatus.OPERATIONAL)]
    assert report.rows[0].incident_count == 2
    assert report.most_urgent.site_id == 1001


def test_archived_sites_have_no_row(directory, incidents):
    assert 1005 not in {r.site_id for r in build_site_report(directory, incidents).rows}


def test_the_decision_support_checks(directory, incidents):
    report = build_site_report(directory, incidents)
    assert report.unreported_outages == [1002]          # degraded, nobody mentions it
    assert report.unflagged_incidents == [1003]         # operational, but an incident mentions it
    assert report.uncovered_sites == [1004]             # no active list covers it
    assert report.lists_without_sites == ["nosites"]
    assert report.lists_without_members == ["empty"]
    assert set(report.archived_references) == {1005}
    assert set(report.unknown_references) == {4242}


def test_counts(directory, incidents):
    report = build_site_report(directory, incidents)
    assert report.status_counts == {"down": 1, "degraded": 1, "unknown": 1, "maintenance": 0, "operational": 1}
    # Unknown->Down and Unknown->Degraded are drafted; Unknown->Operational is not news
    assert report.notification_counts["draft"] == 2
    assert sum(report.notification_counts.values()) == len(list(directory.notifications()))


def test_report_counts_name_mentions(directory, admin_user):
    by_name = MaintenanceTask("HR is unreachable", "users cannot open HR", admin_user)   # site 1004 is named "HR"
    report = build_site_report(directory, [by_name])
    assert {r.site_id: r.incident_count for r in report.rows}[1004] == 1


def test_rows_carry_lists_and_last_check(directory, incidents):
    checked = datetime(2026, 1, 1, tzinfo=timezone.utc)
    directory.get_site(1003).record_check(AvailabilityCheck(checked, SiteStatus.OPERATIONAL, 200, 50.0))
    rows = {r.site_id: r for r in build_site_report(directory, incidents).rows}
    assert rows[1001].list_ids == ("ops",)
    assert rows[1003].last_checked_at == checked
    assert rows[1004].last_checked_at is None


def test_nothing_urgent(admin_user):
    d = SiteDirectory()
    d.add_site(Site(1, "Fine", "https://example.com"))
    d.change_site_status(1, SiteStatus.OPERATIONAL, "ok", admin_user)
    report = build_site_report(d, [])
    assert report.most_urgent is None
    assert "Most urgent: nothing needs attention" in report.lines()


def test_empty_directory():
    report = build_site_report(SiteDirectory(), [])
    assert report.rows == [] and report.most_urgent is None


def test_to_dict_is_json_ready(directory, incidents):
    import json
    data = build_site_report(directory, incidents).to_dict()
    json.dumps(data)                                      # must not raise
    assert data["most_urgent"] == 1001
    assert data["rows"][0]["status"] == "down" and data["rows"][0]["incident_count"] == 2
    assert data["unknown_references"].keys() == {"4242"}


def test_text_rendering(directory, incidents):
    text = str(build_site_report(directory, incidents))
    assert text.startswith("Site health report")
    assert "Most urgent: Site 1001 (Portal, down)" in text
    assert "Unreported outages: 1002" in text
    assert "Incidents mentioning unknown sites: 4242" in text


def test_attention_key_and_count_by():
    def row(site_id, status, incidents=0):
        return SiteReportRow(site_id, "x", status, tuple(str(i) for i in range(incidents)), (), None)
    rows = [row(3, SiteStatus.DOWN, 1), row(2, SiteStatus.DOWN, 5), row(1, SiteStatus.OPERATIONAL)]
    assert [r.site_id for r in sorted(rows, key=attention_key)] == [2, 3, 1]
    assert count_by(["a", "b", "a"], ["a", "b", "c"]) == {"a": 2, "b": 1, "c": 0}
