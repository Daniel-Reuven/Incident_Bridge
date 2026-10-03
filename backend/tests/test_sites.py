"""
Unit tests for app/sites.py: SiteDirectory (lookups, duplicate and
cross-reference rules), the JSONL seed loaders, and seed_site_directory().
Plain domain objects plus temporary files - no HTTP, no database.

Run from backend/:  python -m pytest tests/test_sites.py -v
Uses the shared admin_user fixture from tests/conftest.py.
"""

import json

import pytest

from app.models import MailingList, Site
from app.sites import (
    DEFAULT_MAILING_LISTS_SEED, DEFAULT_SITES_SEED, FIRST_SITE_ID, SiteDirectory, seed_site_directory,
)


def site_line(site_id, name="Site name", url=None, **extra):
    """One JSONL site record; each site id gets its own URL unless one is given (URLs must be unique)."""
    url = url or f"https://s{site_id}.example.com"
    return json.dumps({"site_id": site_id, "site_name": name, "site_url": url, **extra})


def list_line(list_id, site_ids=(), members=("a@example.com",), name="List"):
    return json.dumps({"list_id": list_id, "name": name, "members": list(members), "site_ids": list(site_ids)})


@pytest.fixture
def directory() -> SiteDirectory:
    d = SiteDirectory()
    d.add_site(Site(1001, "Portal", "https://example.com"))
    d.add_site(Site(1002, "Store", "https://example.org"))
    return d


# --- adding and lookups ------------------------------------------------

def test_duplicate_site_id_is_rejected(directory):
    with pytest.raises(ValueError, match="Site 1001 already exists"):
        directory.add_site(Site(1001, "Other", "https://example.net"))


def test_mailing_list_must_reference_existing_active_sites(directory, admin_user):
    with pytest.raises(ValueError, match="9999 \\(no such site\\)"):
        directory.add_mailing_list(MailingList("ops", "Ops", site_ids=[1001, 9999]))
    directory.get_site(1002).archive(admin_user)
    with pytest.raises(ValueError, match="1002 \\(site is archived\\)"):
        directory.add_mailing_list(MailingList("ops", "Ops", site_ids=[1002]))


def test_duplicate_list_id_is_rejected(directory):
    directory.add_mailing_list(MailingList("ops", "Ops"))
    with pytest.raises(ValueError, match="already exists"):
        directory.add_mailing_list(MailingList("ops", "Other"))


def test_lookups_raise_key_error_for_unknown_ids(directory):
    with pytest.raises(KeyError, match="Site 5 not found"):
        directory.get_site(5)
    with pytest.raises(KeyError, match="'nope' not found"):
        directory.get_mailing_list("nope")


def test_listing_hides_archived_unless_asked(directory, admin_user):
    directory.get_site(1001).archive(admin_user)
    assert [s.site_id for s in directory.sites()] == [1002]
    assert [s.site_id for s in directory.sites(include_archived=True)] == [1001, 1002]


def test_lists_covering_returns_active_lists_only(directory, admin_user):
    directory.add_mailing_list(MailingList("a", "A", site_ids=[1001]))
    directory.add_mailing_list(MailingList("b", "B", site_ids=[1001, 1002]))
    directory.add_mailing_list(MailingList("c", "C", site_ids=[1002]))
    directory.get_mailing_list("b").archive(admin_user)
    assert [ml.list_id for ml in directory.lists_covering(1001)] == ["a"]


def test_next_site_id_never_reuses_archived_ids(directory, admin_user):
    assert SiteDirectory().next_site_id() == FIRST_SITE_ID
    directory.get_site(1002).archive(admin_user)
    assert directory.next_site_id() == 1003


# --- site loading ------------------------------------------------------

def test_load_sites_reports_created_duplicates_and_invalid():
    d = SiteDirectory()
    report = d.load_sites_from_lines([
        site_line(1001),
        "",                                   # blank: ignored silently
        site_line(1001, name="Duplicate"),    # duplicate id
        "{not json",                          # invalid JSON
        json.dumps([1, 2]),                   # valid JSON, not an object
        site_line(1003, url="ftp://x"),       # invalid value
        json.dumps({"site_id": 1004, "site_name": "No URL"}),   # missing field
        site_line(1005, site_publish_date="2020-01-01"),
    ])
    assert report.created_ids == [1001, 1005]
    assert report.skipped_duplicate_ids == [1001]
    assert [entry.split(":")[0] for entry in report.skipped_invalid] == ["line 4", "line 5", "line 6", "line 7"]
    assert "invalid JSON" in report.skipped_invalid[0]
    assert d.get_site(1001).site_name == "Site name"   # the duplicate did not overwrite it
    assert str(report) == "sites: 2 loaded, 0 already stored, 1 duplicates skipped, 4 invalid skipped, 0 warnings"


@pytest.mark.parametrize("bad_record", [
    {"site_id": {"x": 1}, "site_name": "n", "site_url": "https://example.com"},
    {"site_id": 7, "site_name": ["n"], "site_url": "https://example.com"},
    {"site_id": 7, "site_name": "n", "site_url": 42},
    {"site_id": 7, "site_name": "n", "site_url": "https://example.com", "site_publish_date": {"y": 2020}},
])
def test_wrongly_typed_values_are_skipped_not_crashing(bad_record):
    report = SiteDirectory().load_sites_from_lines([json.dumps(bad_record)])
    assert report.created == 0 and len(report.skipped_invalid) == 1


def test_a_seed_site_reusing_an_active_url_is_skipped_as_invalid():
    d = SiteDirectory()
    report = d.load_sites_from_lines([site_line(1, url="https://same.example.com"),
                                      site_line(2, url="https://SAME.example.com")])
    assert report.created_ids == [1]
    assert "already uses" in report.skipped_invalid[0]


def test_seed_import_skips_ids_already_in_the_directory(directory):
    directory.get_site(1001).site_name = "Edited in the app"
    report = directory.load_sites_from_lines([site_line(1001, name="From seed"), site_line(1003)])
    assert report.created_ids == [1003]
    assert report.already_stored_ids == [1001]
    assert report.skipped_duplicate_ids == [] and report.problems == []   # already stored is not a problem
    assert directory.get_site(1001).site_name == "Edited in the app"


# --- mailing list loading ----------------------------------------------

def test_load_lists_drops_unlinkable_sites_with_a_warning(directory, admin_user):
    directory.get_site(1002).archive(admin_user)
    report = directory.load_mailing_lists_from_lines([list_line("ops", site_ids=[1001, 1002, 9999])])
    assert report.created_ids == ["ops"]
    assert directory.get_mailing_list("ops").site_ids == (1001,)
    assert len(report.warnings) == 2
    assert "site 1002 dropped (site is archived)" in report.warnings[0]
    assert "site 9999 dropped (no such site)" in report.warnings[1]


def test_already_stored_list_is_counted_not_reported(directory):
    directory.add_mailing_list(MailingList("ops", "Ops"))
    report = directory.load_mailing_lists_from_lines([list_line("ops", name="From seed")])
    assert report.already_stored_ids == ["ops"] and report.problems == []
    assert directory.get_mailing_list("ops").name == "Ops"


def test_load_lists_reports_duplicates_and_invalid(directory):
    report = directory.load_mailing_lists_from_lines([
        list_line("ops"),
        list_line("ops", name="Duplicate"),
        list_line("Bad Id"),
        list_line("bad-member", members=["not-an-email"]),
        json.dumps({"list_id": "x", "name": "X", "members": "a@example.com"}),
    ])
    assert report.created_ids == ["ops"]
    assert report.skipped_duplicate_ids == ["ops"]
    assert len(report.skipped_invalid) == 3


# --- file loading and seed_site_directory -------------------------------

def test_load_from_files(tmp_path):
    sites_file = tmp_path / "sites.jsonl"
    lists_file = tmp_path / "lists.jsonl"
    sites_file.write_text(site_line(1001) + "\n" + site_line(1002) + "\n", encoding="utf-8")
    lists_file.write_text(list_line("ops", site_ids=[1002]) + "\n", encoding="utf-8")
    d = SiteDirectory()
    reports = seed_site_directory(d, str(sites_file), str(lists_file), log=lambda _msg: None)
    assert [r.created for r in reports] == [2, 1]
    assert [ml.list_id for ml in d.lists_covering(1002)] == ["ops"]


def test_missing_file_is_a_warning_not_a_crash(tmp_path):
    messages = []
    d = SiteDirectory()
    reports = seed_site_directory(d, str(tmp_path / "missing.jsonl"), str(tmp_path / "missing2.jsonl"),
                                  log=messages.append)
    assert reports == []
    assert len(messages) == 2 and all("not found" in m for m in messages)


def test_problems_are_logged_one_per_line(tmp_path):
    sites_file = tmp_path / "sites.jsonl"
    sites_file.write_text(site_line(1001) + "\n" + site_line(1001) + "\n{bad\n", encoding="utf-8")
    messages = []
    seed_site_directory(SiteDirectory(), str(sites_file), "", log=messages.append)
    assert "1 loaded, 0 already stored, 1 duplicates skipped, 1 invalid skipped" in messages[0]
    assert messages[1] == "  - duplicate id in file skipped: 1001"
    assert messages[2].startswith("  - invalid record skipped - line 3")
    assert messages[3] == "Seed import: mailing lists seed file disabled - skipped."


def test_env_vars_choose_and_disable_seed_files(tmp_path, monkeypatch):
    sites_file = tmp_path / "custom_sites.jsonl"
    sites_file.write_text(site_line(1500) + "\n", encoding="utf-8")
    monkeypatch.setenv("SITES_SEED_PATH", str(sites_file))
    monkeypatch.setenv("MAILING_LISTS_SEED_PATH", "")
    d = SiteDirectory()
    seed_site_directory(d, log=lambda _msg: None)
    assert [s.site_id for s in d.sites()] == [1500]
    assert list(d.mailing_lists()) == []


def test_the_real_seed_files_load_without_any_problem():
    """Data-quality check for data/sites.jsonl and data/mailing_lists.jsonl (the AI-generated seed data)."""
    d = SiteDirectory()
    reports = seed_site_directory(d, str(DEFAULT_SITES_SEED), str(DEFAULT_MAILING_LISTS_SEED),
                                  log=lambda _msg: None)
    assert len(reports) == 2
    for report in reports:
        assert report.problems == [], report.problems
        assert report.created > 0
    assert sum(1 for _ in d.sites()) >= 10


def test_str_and_repr(directory, admin_user):
    directory.add_mailing_list(MailingList("ops", "Ops"))
    directory.get_site(1001).archive(admin_user)
    assert str(directory) == "<SiteDirectory: 1 active sites (2 total), 1 active mailing lists (1 total)>"
    assert repr(directory) == "SiteDirectory(sites=2, mailing_lists=1, notifications=0)"
