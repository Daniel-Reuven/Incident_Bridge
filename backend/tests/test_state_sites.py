"""
Tests that AppState.create() (app/state.py) imports the sites and mailing
lists seed files at startup into AppState.sites.

Run from backend/:  python -m pytest tests/test_state_sites.py -v
Each test points the seed env vars at temporary files and uses an
in-memory SQLite database (or, for the restart test, a temporary database
file), so nothing outside pytest's tmp_path is touched.
"""

import json

import pytest

from app.state import AppState


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", ":memory:")


def test_startup_imports_both_seed_files(tmp_path, monkeypatch):
    sites_file = tmp_path / "sites.jsonl"
    lists_file = tmp_path / "lists.jsonl"
    sites_file.write_text(json.dumps({"site_id": 1001, "site_name": "Portal",
                                      "site_url": "https://example.com"}) + "\n", encoding="utf-8")
    lists_file.write_text(json.dumps({"list_id": "ops", "name": "Ops", "members": ["a@example.com"],
                                      "site_ids": [1001]}) + "\n", encoding="utf-8")
    monkeypatch.setenv("SITES_SEED_PATH", str(sites_file))
    monkeypatch.setenv("MAILING_LISTS_SEED_PATH", str(lists_file))

    state = AppState.create()

    assert [s.site_id for s in state.sites.sites()] == [1001]
    assert [ml.list_id for ml in state.sites.lists_covering(1001)] == ["ops"]


def test_startup_survives_missing_seed_files(tmp_path, monkeypatch):
    monkeypatch.setenv("SITES_SEED_PATH", str(tmp_path / "nope.jsonl"))
    monkeypatch.setenv("MAILING_LISTS_SEED_PATH", str(tmp_path / "nope2.jsonl"))
    state = AppState.create()
    assert list(state.sites.sites()) == []


def test_startup_with_seed_files_disabled(monkeypatch):
    monkeypatch.setenv("SITES_SEED_PATH", "")
    monkeypatch.setenv("MAILING_LISTS_SEED_PATH", "")
    state = AppState.create()
    assert list(state.sites.sites()) == [] and list(state.sites.mailing_lists()) == []


def test_restart_keeps_app_edits_and_never_resurrects_archived_sites(tmp_path, monkeypatch, admin_user):
    """Two AppState.create() calls on the same database file = a real restart."""
    sites_file = tmp_path / "sites.jsonl"
    sites_file.write_text(
        json.dumps({"site_id": 1001, "site_name": "Portal", "site_url": "https://example.com"}) + "\n"
        + json.dumps({"site_id": 1002, "site_name": "Store", "site_url": "https://example.org"}) + "\n",
        encoding="utf-8")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("SITES_SEED_PATH", str(sites_file))
    monkeypatch.setenv("MAILING_LISTS_SEED_PATH", "")

    first = AppState.create()
    portal = first.sites.get_site(1001)
    portal.site_name = "Portal (edited)"
    first.sites.save_site(portal)
    first.sites.archive_site(1002, admin_user)

    second = AppState.create()
    assert second.sites.get_site(1001).site_name == "Portal (edited)"
    assert second.sites.get_site(1002).is_archived
    assert [s.site_id for s in second.sites.sites()] == [1001]


def test_startup_without_seed_variables_imports_and_prints_nothing(monkeypatch, capsys):
    monkeypatch.delenv("SITES_SEED_PATH", raising=False)
    monkeypatch.delenv("MAILING_LISTS_SEED_PATH", raising=False)
    state = AppState.create()
    assert list(state.sites.sites(include_archived=True)) == []
    assert "Seed import" not in capsys.readouterr().out
