"""
Tests for the installation's own folders (app/security/data_folders.py):
the default data folder, the setup refusing folders that are not empty,
the markers checked at every start, and move_data.py.

Run from backend/:  python -m pytest tests/test_data_folders.py -v
"""

import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.security.data_folders import BACKEND_DIR, MARKER, check_new, installation_folders
from app.security.installation import InstallationError
from app.state import database_path
from tests.install_helper import finish_setup


def test_the_default_data_folder_holds_the_database_and_the_log(monkeypatch):
    for name in ("DATABASE_PATH", "DATA_DIR", "LOG_DIR"):
        monkeypatch.delenv(name, raising=False)
    from app.audit import log_dirs_from_env
    assert Path(database_path()) == BACKEND_DIR / "instance" / "incident_bridge.db"
    assert log_dirs_from_env()[0] == BACKEND_DIR / "instance" / "logs"
    monkeypatch.setenv("DATA_DIR", "somewhere")                           # relative: against backend/
    assert Path(database_path()) == BACKEND_DIR / "somewhere" / "incident_bridge.db"


def test_a_folder_counts_as_empty_when_it_holds_only_the_installations_own_empty_folders(tmp_path):
    (tmp_path / "data" / "logs").mkdir(parents=True)
    folders = installation_folders(str(tmp_path / "data" / "ib.db"), tmp_path / "data" / "ib.db.keys",
                                   tmp_path / "data" / "logs", None)
    assert [f["ok"] for f in check_new(folders)] == [True, True]
    (tmp_path / "data" / "logs" / "old.jsonl").write_text("x")
    (tmp_path / "data" / "notes.txt").write_text("x")
    report = check_new(folders)
    assert report[0]["contents"] == ["logs/old.jsonl", "notes.txt"] and report[1]["contents"] == ["old.jsonl"]


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "data" / "ib.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    return tmp_path


def test_the_setup_refuses_folders_that_are_not_empty(paths):
    (paths / "logs").mkdir()
    (paths / "logs" / "other-app.log").write_text("not ours")
    from app.api.app import app
    with TestClient(app) as client:
        code = app.state.setup.code
        started = client.post("/setup/start", json={"code": code}).json()
        assert started["folders_ok"] is False
        bad = [f for f in started["folders"] if not f["ok"]]
        assert bad[0]["path"] == str((paths / "logs").resolve()) and bad[0]["contents"] == ["other-app.log"]
        recovery = started["recovery_key"]
        response = client.post("/setup/complete", json={
            "code": code, "recovery_check": "-".join(recovery.split("-")[-2:]), "admin_username": "admin",
            "admin_password": "Demo#Pass1", "confirm_password": "Demo#Pass1"})
        assert response.status_code == 400 and "must be empty" in response.json()["detail"]
        assert app.state.installation.status() == "new"                  # nothing was saved
        (paths / "logs" / "other-app.log").unlink()                      # emptied: no restart needed
        assert client.post("/setup/start", json={"code": code}).json()["folders_ok"] is True


def test_markers_are_written_and_a_wrong_folder_stops_the_start(paths, monkeypatch):
    from app.api.app import app
    with TestClient(app) as client:
        finish_setup(client)
        installation_id = app.state.settings.get("installation")["id"]
    for folder in (paths / "data", paths / "logs"):
        assert json.loads((folder / MARKER).read_text())["installation_id"] == installation_id
    with TestClient(app):                                                # the same folders: fine
        assert app.state.running
    monkeypatch.setenv("LOG_DIR", str(paths / "elsewhere"))              # .env pointed at another folder
    with pytest.raises(InstallationError, match="not marked as this installation"):
        with TestClient(app):
            pass


def test_move_data_moves_everything_and_the_app_starts_from_the_new_folder(paths, monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_PATH")
    monkeypatch.delenv("LOG_DIR")
    monkeypatch.setenv("DATA_DIR", str(paths / "old"))                   # the default layout: logs inside
    from app.api.app import app
    with TestClient(app) as client:
        finish_setup(client)
        client.post("/auth/login", json={"username": "admin", "password": "Demo#Pass1"})
        client.post("/incidents/faults", json={"title": "Disk full", "description": "d",
                                               "details": {"system_unavailable": True}})
        sys.path.insert(0, str(BACKEND_DIR))
        import move_data
        with pytest.raises(SystemExit, match="the server is running"):    # never while it runs
            move_data.main([str(paths / "new")])
    move_data.main([str(paths / "new")])
    out = capsys.readouterr().out
    assert f"DATA_DIR={(paths / 'new').resolve()}" in out
    assert sorted(os.listdir(paths / "new")) == [MARKER, "incident_bridge.db", "incident_bridge.db.keys", "logs"]
    monkeypatch.setenv("DATA_DIR", str(paths / "new"))
    with TestClient(app) as client:
        client.post("/auth/login", json={"username": "admin", "password": "Demo#Pass1"})
        assert [i["title"] for i in client.get("/incidents").json()] == ["Disk full"]
        assert client.get("/logs/verify").json()["ok"] is True
    with pytest.raises(SystemExit, match="not empty"):                  # never into a folder in use
        move_data.main([str(paths / "old")])
