"""
Tests for the encrypted activity log (app/log_crypto.py, app/audit.py,
app/log_reader.py), its folder per installation, and the tamper evidence
for the database (app/data_integrity.py): fingerprints in the signed log,
checked by GET /logs/verify.

Run from backend/:  python -m pytest tests/test_log_encryption_and_data.py -v
"""

import io
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import data_integrity, database
from app.audit import MonthlyJsonlHandler, audit, current_handler, log
from app.log_crypto import LogCipher, LogDecryptError
from app.log_reader import available_months, query
from tests.install_helper import finish_setup


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """An installed app on a FILE database (the data check reads the file); yields the logged-in admin client."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "ib.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    from app.api.app import app
    with TestClient(app) as client:
        finish_setup(client)
        client.post("/auth/login", json={"username": "admin", "password": "Demo#Pass1"})
        yield client


def new_fault(client, title="Disk full on the payroll server"):
    response = client.post("/incidents/faults", json={"title": title, "description": "d",
                                                      "details": {"system_unavailable": True}})
    assert response.status_code in (200, 201), response.text
    return response.json()["id"]


def month():
    return datetime.now(timezone.utc).strftime("%Y-%m")


# --- the encrypted files ----------------------------------------------------------------

def test_every_line_is_encrypted_in_a_folder_of_its_own(installed, tmp_path):
    new_fault(installed)
    handler = current_handler()
    installation_id = installed.app.state.settings.get("installation")["id"]
    assert handler.directory == tmp_path / "logs" / installation_id
    raw = handler.path_for(month()).read_text(encoding="utf-8")
    assert "payroll" not in raw and "admin" not in raw and "incident.created" not in raw
    first = json.loads(raw.splitlines()[0])
    assert set(first) == {"v", "n", "c"}
    entries = installed.get("/logs", params={"category": "incident"}).json()["entries"]
    assert entries[0]["title"] == "Disk full on the payroll server"          # the page decrypts it


def test_two_installations_keep_their_logs_apart(tmp_path, monkeypatch):
    from app.api.app import app
    months_seen = []
    for number in (1, 2):                                                # two fresh installations
        monkeypatch.setenv("DATABASE_PATH", str(tmp_path / f"i{number}" / "ib.db"))
        monkeypatch.setenv("LOG_DIR", str(tmp_path / f"i{number}" / "logs"))
        with TestClient(app) as client:
            finish_setup(client)
            client.post("/auth/login", json={"username": "admin", "password": "Demo#Pass1"})
            months_seen.append([m["month"] for m in client.get("/logs/months").json()])
            assert client.get("/logs/verify").json()["ok"] is True
    assert months_seen == [[month()], [month()]]


def test_a_line_from_another_month_or_a_plain_line_does_not_decrypt():
    cipher = LogCipher.from_database_key(b"k" * 32)
    line = cipher.encrypt_line('{"event": "x"}', "2026-10")
    assert cipher.decrypt_line(line, "2026-10") == '{"event": "x"}'
    with pytest.raises(LogDecryptError):
        cipher.decrypt_line(line, "2026-09")
    with pytest.raises(LogDecryptError):
        cipher.decrypt_line('{"event": "added by hand"}', "2026-10")
    with pytest.raises(LogDecryptError):
        LogCipher.from_database_key(b"x" * 32).decrypt_line(line, "2026-10")   # another installation's key


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


def test_a_finished_month_is_compressed_then_encrypted(tmp_path):
    cipher = LogCipher.from_database_key(b"k" * 32)
    clock = Clock()
    handler = MonthlyJsonlHandler(tmp_path / "logs", tmp_path / "archive", clock=clock, cipher=cipher)
    log.addHandler(handler)
    try:
        for number in range(200):
            audit("incident.commented", user="tech1", message=f"tech1 commented on the payroll fault {number}")
        clock.now = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
        audit("app.started", message="a new month")
    finally:
        log.removeHandler(handler)
        handler.close()
    archive = tmp_path / "archive" / "incident_bridge-2026-09.zip.enc"
    assert archive.is_file() and not (tmp_path / "logs" / "incident_bridge-2026-09.jsonl").exists()
    blob = archive.read_bytes()
    assert b"payroll" not in blob
    with zipfile.ZipFile(io.BytesIO(cipher.decrypt_archive(blob, "2026-09"))) as zipped:
        text = zipped.read(zipped.namelist()[0]).decode()
    assert text.count("payroll") == 200 and len(blob) < len(text) / 3          # compressed before encrypting
    assert [m["month"] for m in available_months(tmp_path / "logs", tmp_path / "archive")] == ["2026-10", "2026-09"]
    assert query(tmp_path / "logs", tmp_path / "archive", "2026-09", cipher=cipher)["total"] == 200


def test_hand_written_lines_count_as_unreadable_and_break_the_check(installed):
    path = current_handler().path_for(month())
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"event": "auth.login", "user": "intruder"}\n')
    assert installed.get("/logs").json()["unreadable"] == 1
    result = installed.get("/logs/verify").json()
    assert result["ok"] is False and any("not a valid record" in p["problem"] for p in result["problems"])


def test_the_default_view_leaves_out_http_and_data_records(installed):
    new_fault(installed)
    body = installed.get("/logs", params={"category": "!http,data"}).json()
    assert body["entries"] and all(r["category"] not in ("http", "data") for r in body["entries"])
    assert body["facets"]["category"]["data"] > 0                        # still counted in the menu


# --- the database's tamper evidence -----------------------------------------------------

def test_an_untouched_database_passes_the_check(installed):
    new_fault(installed)
    result = installed.get("/logs/verify").json()
    assert result["ok"] is True and result["data"]["ok"] is True and result["data"]["records"] >= 4
    kinds = {r["kind"] for r in installed.get("/logs", params={"category": "data", "limit": 1000}).json()["entries"]}
    assert {"incident", "user", "setting"} <= kinds


def edit_database(sql, *params):
    conn = database.connect(str(Path(current_handler().directory).parents[1] / "ib.db"))
    conn.execute(sql, params)
    conn.commit()
    conn.close()


@pytest.mark.parametrize("change, kind, problem", [
    (("UPDATE incidents SET title = 'Nothing happened' WHERE id = ?",), "incident", "changed"),
    (("DELETE FROM comments WHERE incident_id = ?",), "incident", "changed"),
    (("DELETE FROM incidents WHERE id = ?",), "incident", "deleted"),
])
def test_edits_made_outside_the_app_are_found(installed, change, kind, problem):
    fault = new_fault(installed)
    installed.post(f"/incidents/{fault}/comments", json={"text": "Restarted the service."})
    edit_database(change[0], fault)
    data = installed.get("/logs/verify").json()["data"]
    assert data["ok"] is False
    assert {"kind": kind, "key": fault, "problem": problem} in [
        {k: p[k] for k in ("kind", "key", "problem")} for p in data["problems"]]


def test_a_user_added_by_hand_and_a_changed_setting_are_found(installed):
    edit_database("INSERT INTO users (username, role, password_hash, created_at, updated_at) "
                  "VALUES ('mallory', 'admin', 'x', '2026-01-01', '2026-01-01')")
    edit_database("UPDATE app_settings SET value = '{\"AI_PROVIDER\": \"fake\"}' WHERE name = 'installation'")
    problems = {(p["kind"], p["key"], p["problem"]) for p in installed.get("/logs/verify").json()["data"]["problems"]}
    assert ("user", "mallory", "unrecorded") in problems
    assert ("setting", "installation", "changed") in problems


def test_a_record_the_app_deleted_and_that_reappears_is_found():
    records = [{"category": "data", "event": "data.saved", "kind": "ai_job", "key": "j1", "fingerprint": "f"},
               {"category": "data", "event": "data.deleted", "kind": "ai_job", "key": "j1"}]
    assert data_integrity.check(records, {})["ok"] is True                  # deleted by the app: fine
    result = data_integrity.check(records, {("ai_job", "j1"): "f"})
    assert result["problems"][0]["problem"] == "reappeared"


def test_the_ai_queue_records_its_deletions(installed):
    from app.ai.job_store import SqliteAnalysisJobStore
    from app.ai.jobs import AnalysisJob
    store = SqliteAnalysisJobStore(str(Path(current_handler().directory).parents[1] / "ib.db"))
    job = AnalysisJob(incident_id="f1", requested_by="admin")
    store.add(job)
    store.remove(job.id)
    assert installed.get("/logs/verify").json()["data"]["ok"] is True


# --- keys and export ----------------------------------------------------------------------

def test_stopping_the_app_releases_the_log_file(tmp_path, monkeypatch):
    """Windows cannot replace an open file: after a stop, nothing may hold the month's log open."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "ib.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    from app.api.app import app
    with TestClient(app) as client:
        finish_setup(client)
        handler = current_handler()
        assert handler is not None
    assert current_handler() is None and handler._stream is None
    month_file = next((tmp_path / "logs").rglob("incident_bridge-*.jsonl"))
    if Path("/proc/self/fd").is_dir():
        open_files = {str(Path(f).resolve()) for f in (str(fd.resolve()) for fd in Path("/proc/self/fd").iterdir())}
        assert str(month_file.resolve()) not in open_files


def test_a_new_recovery_key_re_encrypts_the_log_too(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "ib.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    from app.api.app import app
    with TestClient(app) as client:
        finish_setup(client)
        client.post("/auth/login", json={"username": "admin", "password": "Demo#Pass1"})
        new_fault(client)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import manage_keys
    from app.security import crypto
    shown = []
    real = crypto.new_recovery_key
    monkeypatch.setattr(crypto, "new_recovery_key", lambda: shown.append(real()) or shown[-1])
    monkeypatch.setattr(manage_keys, "_ask", lambda prompt: "-".join(shown[-1].split("-")[-2:]))
    manage_keys.main(["new-recovery-key"])
    assert "Activity log re-encrypted (1 file(s))" in capsys.readouterr().out
    with TestClient(app) as client:                                       # restart: the new keys open everything
        client.post("/auth/login", json={"username": "admin", "password": "Demo#Pass1"})
        assert client.get("/logs").json()["unreadable"] == 0
        assert client.get("/logs/verify").json()["ok"] is True


def test_the_export_writes_a_readable_copy(installed, tmp_path, monkeypatch, capsys):
    new_fault(installed)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import manage_logs
    out = tmp_path / "export.jsonl"
    manage_logs.main(["export", "--out", str(out)])
    records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert any(r.get("title") == "Disk full on the payroll server" for r in records)
    assert "NOT encrypted" in capsys.readouterr().out
