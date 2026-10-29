"""
Tests for the activity log: writing (app/audit.py - JSON lines, monthly
files, zip archives), reading (app/log_reader.py) and the admin API
(app/api/logs.py), plus what each part of the app records and what it must
never record.

Run from backend/:  python -m pytest tests/test_audit_log.py -v
"""

import json
import logging
import os
import zipfile
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
import requests
from fastapi.testclient import TestClient

from tests.install_helper import finish_setup

from app.audit import MonthlyJsonlHandler, audit, configure_audit_log, log, log_dirs_from_env
from app.log_reader import available_months, query


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class Clock:
    def __init__(self, year, month):
        self.now = datetime(year, month, 28, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


@pytest.fixture
def handler(tmp_path):
    """A handler on tmp_path with a movable clock, attached for the test only."""
    clock = Clock(2026, 9)
    h = MonthlyJsonlHandler(tmp_path / "logs", tmp_path / "logs" / "archive", clock=clock)
    h.clock = clock
    log.addHandler(h)
    yield h
    log.removeHandler(h)
    h.close()


# --- writing ------------------------------------------------------------------------

def test_a_record_is_one_json_line_with_the_standard_fields(handler):
    audit("incident.claimed", user="tech1", incident_id="seed-003", message="tech1 claimed fault seed-003")
    (record,) = lines(handler.path_for("2026-09"))
    assert list(record)[:7] == ["ts", "level", "category", "event", "user", "ip", "message"]
    assert (record["category"], record["event"], record["user"], record["level"]) == \
           ("incident", "incident.claimed", "tech1", "INFO")
    assert record["incident_id"] == "seed-003" and record["ts"].endswith("Z")


def test_a_new_month_starts_a_new_file_and_zips_the_finished_one(handler):
    audit("app.started", message="september")
    handler.clock.now = datetime(2026, 10, 1, 0, 0, 1, tzinfo=timezone.utc)
    audit("app.started", message="october")
    assert not handler.path_for("2026-09").exists()                       # moved into the archive
    with zipfile.ZipFile(handler.archive_path_for("2026-09")) as archive:
        (member,) = archive.infolist()
        assert member.compress_type == zipfile.ZIP_DEFLATED
        assert json.loads(archive.read(member).decode())["message"] == "september"
    assert lines(handler.path_for("2026-10"))[0]["message"] == "october"


def test_old_months_left_in_the_folder_are_archived_at_startup(tmp_path):
    folder = tmp_path / "logs"
    folder.mkdir()
    (folder / "incident_bridge-2026-07.jsonl").write_text('{"event": "app.started"}\n', encoding="utf-8")
    h = MonthlyJsonlHandler(folder, folder / "archive", clock=Clock(2026, 10))
    h.close()
    assert not (folder / "incident_bridge-2026-07.jsonl").exists()
    assert (folder / "archive" / "incident_bridge-2026-07.zip").is_file()


def test_the_settings_resolve_paths_and_can_switch_files_off(monkeypatch, tmp_path):
    monkeypatch.setenv("LOG_DIR", "custom-logs")
    monkeypatch.delenv("LOG_ARCHIVE_DIR", raising=False)
    log_dir, archive_dir, enabled = log_dirs_from_env()
    assert log_dir.name == "custom-logs" and log_dir.parent.name == "backend"   # relative to backend/
    assert archive_dir == log_dir / "archive" and enabled
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "x"))
    monkeypatch.setenv("LOG_FILE_ENABLED", "false")
    assert configure_audit_log() is None and not (tmp_path / "x").exists()
    monkeypatch.setenv("LOG_FILE_ENABLED", "sometimes")
    with pytest.raises(ValueError, match="LOG_FILE_ENABLED"):
        log_dirs_from_env()


# --- reading ------------------------------------------------------------------------

@pytest.fixture
def month_of_records(handler):
    """A small month: 3 incident records, a failed login, an HTTP 500, one broken line."""
    audit("incident.created", user="tech1", incident_id="f-1", message="tech1 created fault f-1 \"Disk full\"")
    audit("incident.claimed", user="tech2", incident_id="f-1", message="tech2 claimed fault f-1")
    audit("incident.closed", user="Tech1", incident_id="f-1", message="tech1 closed fault f-1")
    audit("auth.login_failed", level=logging.WARNING, user="ghost", message="Failed login for 'ghost'")
    audit("http.request", level=logging.ERROR, user="admin", status=500, message="GET /incidents 500")
    with open(handler.path_for("2026-09"), "a", encoding="utf-8") as f:
        f.write("not json\n")
    return handler


@pytest.mark.parametrize("filters, events", [
    ({}, ["http.request", "auth.login_failed", "incident.closed", "incident.claimed", "incident.created"]),
    ({"category": "incident"}, ["incident.closed", "incident.claimed", "incident.created"]),
    ({"event": "incident.claimed"}, ["incident.claimed"]),
    ({"user": "TECH1"}, ["incident.closed", "incident.created"]),          # ignoring letter case
    ({"level": "WARNING"}, ["http.request", "auth.login_failed"]),        # warnings and worse
    ({"level": "ERROR"}, ["http.request"]),
    ({"text": "disk FULL"}, ["incident.created"]),
])
def test_filters(month_of_records, filters, events):
    h = month_of_records
    result = query(h.directory, h.archive_dir, "2026-09", **filters)
    assert [r["event"] for r in result["entries"]] == events
    assert result["unreadable"] == 1


def test_pages_and_facets(month_of_records):
    h = month_of_records
    result = query(h.directory, h.archive_dir, "2026-09", limit=2, offset=1)
    assert result["total"] == 5 and [r["event"] for r in result["entries"]] == ["auth.login_failed", "incident.closed"]
    assert result["facets"]["category"] == {"auth": 1, "http": 1, "incident": 3}
    assert result["facets"]["level"] == {"ERROR": 1, "INFO": 3, "WARNING": 1}


def test_archived_and_current_months_are_both_readable(month_of_records):
    h = month_of_records
    h.clock.now = datetime(2026, 10, 2, tzinfo=timezone.utc)
    audit("app.started", message="october")
    months = available_months(h.directory, h.archive_dir)
    assert [(m["month"], m["archived"]) for m in months] == [("2026-10", False), ("2026-09", True)]
    assert query(h.directory, h.archive_dir, "2026-09")["total"] == 5            # read from the zip


# --- what the running app records ------------------------------------------------------

@pytest.fixture
def logged_app(tmp_path, monkeypatch):
    """A fresh app writing its activity log to tmp_path/logs; yields (client, log file path)."""
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("DATABASE_PATH", ":memory:")
    from app.api.app import app
    with TestClient(app) as client:
        finish_setup(client)
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        yield client, tmp_path / "logs" / f"incident_bridge-{month}.jsonl"


def events_in(path):
    return [r["event"] for r in lines(path)]


def test_logins_actions_and_requests_are_recorded(logged_app):
    client, path = logged_app
    client.post("/auth/login", json={"username": "admin", "password": "wrong"})
    client.post("/auth/login", json={"username": "admin", "password": "Demo#Pass1"})
    client.post("/incidents/faults", json={"title": "Disk full", "description": "d",
                                           "details": {"system_unavailable": True}})
    client.get("/css/styles.css")
    client.get("/health")
    records = lines(path)
    events = [r["event"] for r in records]
    assert events[0] == "app.started"
    assert "auth.login_failed" in events and "auth.login" in events and "incident.created" in events
    created = next(r for r in records if r["event"] == "incident.created")
    assert created["user"] == "admin" and created["title"] == "Disk full" and created["incident_type"] == "fault"
    requests_logged = [(r["method"], r["path"], r["status"], r["user"]) for r in records if r["event"] == "http.request"]
    assert ("POST", "/auth/login", 401, None) in requests_logged
    assert ("POST", "/auth/login", 200, "admin") in requests_logged          # the user who just logged in
    assert ("POST", "/incidents/faults", 201, "admin") in requests_logged or \
           ("POST", "/incidents/faults", 200, "admin") in requests_logged
    assert not any(p.startswith("/css/") or p == "/health" for _, p, _, _ in requests_logged)


def test_passwords_never_reach_the_log(logged_app):
    client, path = logged_app
    client.post("/auth/login", json={"username": "admin", "password": "Demo#Pass1"})
    client.post("/auth/change-password", json={"current_password": "Demo#Pass1", "new_password": "Brand#New99",
                                               "confirm_password": "Brand#New99"})
    client.post("/users", json={"username": "nora", "role": "user", "password": "Aboard-Today#7",
                                "confirm_password": "Aboard-Today#7"})
    text = path.read_text(encoding="utf-8")
    for secret in ("Demo#Pass1", "Brand#New99", "Aboard-Today#7", "pbkdf2_sha256"):
        assert secret not in text
    assert {"auth.password_changed", "users.created"} <= set(events_in(path))


def test_the_logs_api_is_admin_only_and_filters(logged_app):
    client, path = logged_app
    client.post("/auth/login", json={"username": "tech1", "password": "Demo#Pass2"})
    assert client.get("/logs").status_code == 403
    client.post("/auth/logout")
    client.post("/auth/login", json={"username": "admin", "password": "Demo#Pass1"})
    months = client.get("/logs/months").json()
    assert months[0]["archived"] is False
    body = client.get("/logs", params={"category": "auth", "user": "tech1"}).json()
    assert [r["event"] for r in body["entries"]] == ["auth.logout", "auth.login"]
    assert body["facets"]["category"]["auth"] >= 3
    assert client.get("/logs", params={"month": "bad"}).status_code == 400


def test_outbound_api_calls_are_recorded_without_secrets(handler):
    from app.ai.providers.claude_provider import ClaudeProvider
    from app.api_client import ApiClient
    session = Mock(spec=requests.Session)
    response = requests.Response()
    response.status_code, response._content = 200, b'{"content": [{"type": "text", "text": "ok"}]}'
    session.request.return_value = response
    client = ApiClient("https://api.example.com/v1", headers={"x-api-key": "SECRET-KEY"}, session=session,
                       name="the Claude API")
    ClaudeProvider(models=("claude-x",), api_key="SECRET-KEY", timeout_seconds=5, client=client).analyze("i", "{}")
    session.request.side_effect = requests.ReadTimeout("slow")
    with pytest.raises(Exception):
        client.post_json("/messages", {"model": "claude-x"}, log_fields={"provider": "Claude", "model": "claude-x"})
    records = [r for r in lines(handler.path_for("2026-09")) if r["event"] == "external.request"]
    assert [(r["provider"], r["model"], r.get("status"), r["outcome"]) for r in records] == \
           [("Claude", "claude-x", 200, "ok"), ("Claude", "claude-x", None, "timeout")]
    assert records[0]["host"] == "api.example.com" and records[0]["path"] == "/v1/messages"
    assert records[1]["level"] == "WARNING"
    assert "SECRET-KEY" not in handler.path_for("2026-09").read_text()


def test_a_failing_listener_never_breaks_publishing():
    from app.events import EventBroadcaster
    broadcaster = EventBroadcaster()
    seen = []
    broadcaster.add_listener(lambda event: 1 / 0)
    broadcaster.add_listener(seen.append)
    broadcaster.publish({"action": "x"})
    assert seen == [{"action": "x"}]


# --- tamper evidence (app/audit_integrity.py) ---------------------------------------------

from app.audit_integrity import AuditChain, verify  # noqa: E402
from app.log_reader import iter_all_months  # noqa: E402

KEY = b"test-signing-key"


@pytest.fixture
def signed(tmp_path):
    """A signed log on tmp_path: 5 records in September and 2 in October; yields a helper."""
    clock = Clock(2026, 9)
    chain = AuditChain(KEY, str(tmp_path / "app.db"))
    h = MonthlyJsonlHandler(tmp_path / "logs", tmp_path / "logs" / "archive", clock=clock, chain=chain)
    log.addHandler(h)
    for i in range(5):
        audit("incident.commented", user="tech1", incident_id=f"f-{i}", message=f"comment {i}")
    clock.now = datetime(2026, 10, 3, tzinfo=timezone.utc)
    for i in range(2):
        audit("auth.login", user="admin", message=f"login {i}")

    class Signed:
        handler = h
        october = h.path_for("2026-10")
        september_zip = h.archive_path_for("2026-09")

        @staticmethod
        def check(key=KEY):
            return verify(iter_all_months(h.directory, h.archive_dir), key, chain.checkpoint())

    yield Signed
    log.removeHandler(h)
    h.close()


def rewrite(path, change):
    """Apply `change` to the list of JSON lines of a plain log file."""
    records = lines(path)
    change(records)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def test_records_are_chained_and_signed(signed):
    records = lines(signed.october)
    assert [r["seq"] for r in records] == [6, 7]               # one sequence across months
    assert records[1]["prev"] == records[0]["hash"] and len(records[0]["hash"]) == 64
    result = signed.check()
    assert result["ok"] and result["records"] == 7 and result["problems"] == []


@pytest.mark.parametrize("change, expected", [
    (lambda r: r[0].update(user="someone-else"), "changed after it was written"),
    (lambda r: r.pop(0), "records 6-6 are missing"),
    (lambda r: r.reverse(), "out of order"),
    (lambda r: r.pop(), "the newest records are missing"),
    (lambda r: r.append({**r[-1], "seq": 8, "prev": r[-1]["hash"], "hash": "f" * 64}), "changed after it was written"),
])
def test_edits_deletions_reordering_and_forgery_are_detected(signed, change, expected):
    rewrite(signed.october, change)
    result = signed.check()
    assert not result["ok"]
    assert any(expected in p["problem"] for p in result["problems"]), result["problems"]


def test_a_deleted_month_archive_is_detected(signed):
    signed.september_zip.unlink()
    problems = [p["problem"] for p in signed.check()["problems"]]
    assert any("the whole month is missing (5 records were written)" in p for p in problems)


def test_a_different_key_is_reported_as_such_not_as_tampering(signed):
    problems = [p["problem"] for p in signed.check(key=b"another-key")["problems"]]
    assert problems and all("signed with another key" in p for p in problems)


def test_the_verify_endpoint(logged_app):
    client, path = logged_app
    client.post("/auth/login", json={"username": "admin", "password": "Demo#Pass1"})
    body = client.get("/logs/verify").json()
    assert body["ok"] is True and body["records"] >= 2 and body["checkpoint"]["seq"] >= body["records"]
    rewrite(path, lambda r: r[0].update(message="nothing to see here"))
    body = client.get("/logs/verify").json()
    assert body["ok"] is False and body["problems"][0]["seq"] == 1


def test_actions_carry_the_requests_address_and_http_can_be_excluded(logged_app):
    client, path = logged_app
    client.post("/auth/login", json={"username": "admin", "password": "Demo#Pass1"})
    client.post("/incidents/faults", json={"title": "Disk full", "description": "d",
                                           "details": {"system_unavailable": True}})
    created = next(r for r in lines(path) if r["event"] == "incident.created")
    login = next(r for r in lines(path) if r["event"] == "auth.login")
    # The default LOG_IP_MODE=hashed: a token, the same for every record from that address.
    assert created["ip"].startswith("ip-") and created["ip"] == login["ip"] != "testclient"
    body = client.get("/logs", params={"category": "!http"}).json()
    assert body["entries"] and all(r["category"] != "http" for r in body["entries"])



# --- client addresses (app/ip_privacy.py) ------------------------------------------------

from app import ip_privacy  # noqa: E402


@pytest.mark.parametrize("ip, expected", [
    ("192.168.0.1", "***.***.***.1"),
    ("10.20.30.255", "***.***.***.255"),
    ("2001:db8::42", "****::42"),
    ("::1", "****::1"),
    ("::ffff:192.0.2.7", "***.***.***.7"),          # an IPv4 address carried in IPv6
    ("testclient", "***"),                           # not an address at all
])
def test_masked_keeps_only_the_last_part(ip, expected):
    assert ip_privacy.mask(ip) == expected


def test_each_mode():
    key = b"k" * 32
    assert ip_privacy.IpPrivacy("full", key).apply("192.168.0.1") == "192.168.0.1"
    assert ip_privacy.IpPrivacy("off", key).apply("192.168.0.1") is None
    assert ip_privacy.IpPrivacy("masked", key).apply("192.168.0.1") == "***.***.***.1"
    hashed = ip_privacy.IpPrivacy("hashed", key)
    token = hashed.apply("192.168.0.1")
    assert token.startswith("ip-") and len(token) == 15 and "192" not in token
    assert hashed.apply("192.168.0.1") == token                      # stable: same address, same token
    assert hashed.apply("192.168.0.2") != token
    assert ip_privacy.IpPrivacy("hashed", b"x" * 32).apply("192.168.0.1") != token   # needs the key
    assert hashed.apply(None) is None


def test_an_unknown_mode_stops_startup(monkeypatch):
    monkeypatch.setenv("LOG_IP_MODE", "partial")
    with pytest.raises(ValueError, match="LOG_IP_MODE must be one of hashed, masked, full, off"):
        ip_privacy.mode_from_env()


@pytest.mark.parametrize("mode, expected", [("full", "testclient"), ("masked", "***"), ("off", None)])
def test_the_mode_applies_to_every_record(tmp_path, monkeypatch, mode, expected):
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("DATABASE_PATH", ":memory:")
    monkeypatch.setenv("LOG_IP_MODE", mode)
    from app.api.app import app
    with TestClient(app) as client:
        finish_setup(client)
        client.post("/auth/login", json={"username": "admin", "password": "wrong"})
        client.get("/auth/me")
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    records = lines(tmp_path / "logs" / f"incident_bridge-{month}.jsonl")
    with_requests = [r for r in records if r["category"] in ("http", "auth")]
    assert with_requests and all(r.get("ip") == expected for r in with_requests)
    monkeypatch.delenv("LOG_IP_MODE")
    ip_privacy.configure(ip_privacy.DEFAULT_MODE, b"test")


def test_the_console_access_line_follows_the_mode():
    from app.logs import ConsoleFormatter
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                               ("192.168.0.1:63317", "GET", "/auth/me", "1.1", 200), None)
    try:
        ip_privacy.configure("masked", b"k")
        assert '***.***.***.1 - "GET /auth/me HTTP/1.1" 200 OK' in ConsoleFormatter(use_colors=False).format(record)
        ip_privacy.configure("full", b"k")
        assert "192.168.0.1:63317 - " in ConsoleFormatter(use_colors=False).format(record)
        ip_privacy.configure("off", b"k")
        assert '- - "GET /auth/me' in ConsoleFormatter(use_colors=False).format(record)
    finally:
        ip_privacy.configure(ip_privacy.DEFAULT_MODE, b"test")
