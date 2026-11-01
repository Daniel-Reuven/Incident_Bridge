"""
Tests that AppState.create() (app/state.py) loads the stored sites and
mailing lists at startup - and imports nothing by itself: sites and lists
are imported on the Server settings page (tests/test_api/test_site_imports.py).

Run from backend/:  python -m pytest tests/test_state_sites.py -v
Uses an in-memory SQLite database (or, for the restart test, a temporary
database file), so nothing outside pytest's tmp_path is touched.
"""

import json

import pytest

from app.state import AppState


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", ":memory:")


def test_startup_ignores_the_old_seed_variables(tmp_path, monkeypatch, app_log):
    """SITES_SEED_PATH / MAILING_LISTS_SEED_PATH are gone: a leftover .env line imports nothing."""
    sites_file = tmp_path / "sites.jsonl"
    sites_file.write_text(json.dumps({"site_id": 1001, "site_name": "Portal",
                                      "site_url": "https://example.com"}) + "\n", encoding="utf-8")
    monkeypatch.setenv("SITES_SEED_PATH", str(sites_file))
    state = AppState.create()
    assert list(state.sites.sites(include_archived=True)) == []
    assert "Seed import" not in app_log.text


def test_restart_keeps_app_edits_and_a_new_import_never_resurrects_archived_sites(tmp_path, monkeypatch,
                                                                                    admin_user):
    """Two AppState.create() calls on the same database file = a real restart."""
    lines = [json.dumps({"site_id": 1001, "site_name": "Portal", "site_url": "https://example.com"}),
             json.dumps({"site_id": 1002, "site_name": "Store", "site_url": "https://example.org"})]
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "app.db"))

    first = AppState.create()
    assert first.sites.load_sites_from_lines(lines).created == 2
    portal = first.sites.get_site(1001)
    portal.site_name = "Portal (edited)"
    first.sites.save_site(portal)
    first.sites.archive_site(1002, admin_user)

    second = AppState.create()
    report = second.sites.load_sites_from_lines(lines)              # the same file imported again
    assert report.created == 0 and sorted(report.already_stored_ids) == [1001, 1002]
    assert second.sites.get_site(1001).site_name == "Portal (edited)"
    assert second.sites.get_site(1002).is_archived
    assert [s.site_id for s in second.sites.sites()] == [1001]
