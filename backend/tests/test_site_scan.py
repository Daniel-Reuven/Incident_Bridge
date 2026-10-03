"""
Unit tests for app/site_scan.py: the id pattern ("Site 1042", "Site #1042",
"Site-1042"), full-name matching, the lazy pipeline, and grouping into
active / archived / unknown site buckets.

Run from backend/:  python -m pytest tests/test_site_scan.py -v
Uses the shared admin_user fixture from tests/conftest.py.
"""

import pytest

from app.models import Fault, IncidentStatus, MaintenanceTask, ResolutionType, SeverityCategory
from app.site_scan import ID_PATTERN, NameMatcher, SiteMention, iter_site_mentions, scan_incidents

NAMES = {1007: "Internal HR portal", 1002: "Customer portal", 1020: "Portal", 1009: "Status page",
         1030: "Internal HR system"}


def task(user, title, description="routine", status=IncidentStatus.OPEN):
    t = MaintenanceTask(title=title, description=description, created_by=user)
    t.status = status
    return t


# --- ids ---------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("Site 1042 is down", [1042]),
    ("site 1042 and SITE 7", [1042, 7]),
    ("Site   1042 (extra spaces)", [1042]),
    ("Site #1042", [1042]),
    ("Site#1042", [1042]),
    ("Site # 1042", [1042]),
    ("Site-1042", [1042]),
    ("Site - 1042", [1042]),
    ("site-1042, Site #7 and Site 9", [1042, 7, 9]),
    ("(Site 1042).", [1042]),
    ("Site 10420 is not site 1042", [10420, 1042]),
    ("Website 1042 should not match", []),
    ("Website-1042 should not match", []),
    ("Site1042 needs a separator", []),
    ("Site: 1042 is not a supported separator", []),
    ("Site #-1042 mixes two separators", []),
    ("Site ABC is not a number", []),
    ("Sites 1042 is plural - no match", []),
    ("Site 1042a is not a whole id", []),
])
def test_id_pattern(text, expected):
    assert [int(m.group(1)) for m in ID_PATTERN.finditer(text)] == expected


# --- names -------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("Internal HR portal is down", [1007]),
    ("Error loading `Internal HR portal`", [1007]),
    ("internal hr portal keeps timing out", [1007]),          # letter case is ignored
    ("Login fails (Internal HR portal).", [1007]),
    ("HR portal is down", [1020]),        # not "Internal HR portal" (partial) - but "Portal" IS a full site name
    ("HR system is down", []),                                # partial name, and no other name: no match
    ("Internal HR portals are slow", []),                     # glued to more letters: no match
    ("Internal  HR system", []),                              # spacing must be exact
    ("Customer portal is down", [1002]),                      # longest name wins, not also "Portal"
    ("The Portal and the Customer portal", [1020, 1002]),
    ("Status page and Internal HR portal", [1009, 1007]),
])
def test_name_matching(text, expected):
    assert list(NameMatcher(NAMES).find(text)) == expected


def test_duplicate_names_count_for_every_site_with_that_name():
    assert sorted(NameMatcher({1: "Portal", 2: "portal"}).find("Portal is down")) == [1, 2]


def test_names_with_regex_characters_are_matched_literally():
    matcher = NameMatcher({5: "Shop (EU) v2.0", 6: "a+b"})
    assert list(matcher.find("Shop (EU) v2.0 is slow")) == [5]
    assert list(matcher.find("Shop (EU) v2X0 is slow")) == []      # "." is not a wildcard
    assert list(matcher.find("a+b down")) == [6]


def test_empty_name_list_matches_nothing():
    assert list(NameMatcher({}).find("Internal HR portal")) == []


# --- the pipeline -------------------------------------------------------------

def test_mentions_are_found_in_title_description_and_comments(admin_user):
    t = task(admin_user, "Patch Site 1001", "Also affects site #1002")
    t.add_comment(admin_user, "Customers on Site-1003 noticed")
    assert [(m.site_id, m.found_in) for m in iter_site_mentions([t])] == [
        (1001, "title"), (1002, "description"), (1003, "comment")]


def test_name_mentions_are_tagged_as_such(admin_user):
    t = task(admin_user, "Internal HR portal is down", "see Site 1007")
    assert [(m.site_id, m.found_in, m.matched_by) for m in iter_site_mentions([t], NAMES)] == [
        (1007, "title", "name"), (1007, "description", "id")]


def test_without_names_only_ids_are_matched(admin_user):
    assert list(iter_site_mentions([task(admin_user, "Internal HR portal is down")])) == []


def test_only_open_and_in_progress_incidents_are_scanned(admin_user):
    open_task = task(admin_user, "Site 1 open")
    working = task(admin_user, "Site 2 in progress", status=IncidentStatus.IN_PROGRESS)
    closed = task(admin_user, "Site 3 closed")
    closed.close(admin_user, ResolutionType.RESOLVED, "done")
    assert [m.site_id for m in iter_site_mentions([open_task, working, closed])] == [1, 2]


def test_faults_are_scanned_too(admin_user):
    fault = Fault("Site 1042 returns 503", "checkout broken", admin_user, SeverityCategory.CRITICAL, 1.0)
    assert list(iter_site_mentions([fault])) == [SiteMention(1042, fault.id, "title", "id")]


def test_the_pipeline_is_lazy(admin_user):
    pulled = []

    def source():
        for i in range(1, 100):
            pulled.append(i)
            yield task(admin_user, f"Site {i}")

    mentions = iter_site_mentions(source(), NAMES)
    assert pulled == []                     # building the pipeline reads nothing
    assert next(mentions).site_id == 1
    assert next(mentions).site_id == 2
    assert pulled == [1, 2]                 # only what was needed was read


# --- grouping -------------------------------------------------------------------

def test_grouping_counts_each_incident_once_per_site(admin_user):
    a = task(admin_user, "Site 1007 down", "Internal HR portal again")
    a.add_comment(admin_user, "still Site #1007")
    b = task(admin_user, "Site-1007 slow")
    result = scan_incidents([a, b], active_site_ids=[1007, 1002], site_names=NAMES)
    assert result.incidents_for(1007) == tuple(sorted([a.id, b.id]))
    assert result.incidents_for(1002) == ()
    assert result.sites_with_incidents == {1007}


def test_archived_and_unknown_sites_get_their_own_buckets(admin_user):
    a = task(admin_user, "Site 1001 and Site 1500 and Site 9999", "also the Status page")
    result = scan_incidents([a], active_site_ids=[1001], archived_site_ids=[1500, 1009], site_names=NAMES)
    assert set(result.by_site) == {1001}
    assert result.archived == {1500: {a.id}, 1009: {a.id}}     # 1009 by name
    assert result.unknown == {9999: {a.id}}
    assert str(result) == ("SiteScanResult(1 sites with incidents, 2 archived referenced, "
                           "1 unknown referenced)")


def test_nothing_to_scan():
    result = scan_incidents([], active_site_ids=[1])
    assert result.by_site == {} and result.sites_with_incidents == set()
