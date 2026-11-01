"""
Tests for the first-start setup (app/api/setup.py): the gate that holds
everything back until it is done, the setup code, the recovery key, the
first admin, the regular users from INCIDENT_BRIDGE_USERS, the AI settings
stored encrypted, and restarts of an installed app.

Run from backend/:  python -m pytest tests/test_setup.py -v
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.ai import stored_settings
from app.security.installation import Installation, InstallationError
from tests.install_helper import finish_setup


@pytest.fixture
def new_app(tmp_path, monkeypatch):
    """A brand-new installation on a file database; yields the TestClient (setup not done)."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "ib.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    from app.api.app import app
    with TestClient(app) as client:
        yield client


def code_of(client):
    return client.app.state.setup.code


def complete(client, recovery, **changes):
    body = {"code": code_of(client), "recovery_check": "-".join(recovery.split("-")[-2:]),
            "admin_username": "boss", "admin_password": "Chief#Start1", "confirm_password": "Chief#Start1",
            "ai": None, **changes}
    return client.post("/setup/complete", json=body)


# --- before the setup ------------------------------------------------------------------

def test_until_the_setup_only_the_setup_page_answers(new_app):
    assert new_app.get("/health").json()["setup_required"] is True
    for page in ("/", "/dashboard.html", "/users.html"):
        response = new_app.get(page, follow_redirects=False)
        assert response.status_code == 307 and response.headers["location"] == "/setup.html"
    blocked = new_app.post("/auth/login", json={"username": "admin", "password": "Demo#Pass1"})
    assert blocked.status_code == 503 and blocked.json()["setup_required"] is True
    for still_open in ("/setup.html", "/guide.html", "/css/styles.css", "/js/setup.js", "/auth/password-policy"):
        assert new_app.get(still_open).status_code == 200, still_open


def test_the_setup_code_is_required_and_too_many_wrong_codes_replace_it(new_app):
    assert new_app.post("/setup/start", json={"code": "WRNG-CODE"}).status_code == 403
    first = code_of(new_app)
    for _ in range(9):
        new_app.post("/setup/start", json={"code": "WRNG-CODE"})
    assert code_of(new_app) != first                                     # a new code was printed
    assert new_app.post("/setup/start", json={"code": first}).status_code == 403
    assert new_app.post("/setup/start", json={"code": code_of(new_app).lower()}).status_code == 200


def test_start_shows_the_recovery_key_and_the_users_to_be_created(new_app):
    started = new_app.post("/setup/start", json={"code": code_of(new_app)}).json()
    assert len(started["recovery_key"].split("-")) == 13 and len(started["fingerprint"]) == 8
    assert started["regular_users"] == ["tech1"] and started["users_problem"] is None
    assert started["key_store"]["name"] == "file"
    again = new_app.post("/setup/start", json={"code": code_of(new_app)}).json()
    assert again["recovery_key"] == started["recovery_key"]              # the same key until completed


@pytest.mark.parametrize("changes, status, reason", [
    ({"recovery_check": "AAAA-BBBB"}, 400, "do not match"),
    ({"admin_password": "weakpass", "confirm_password": "weakpass"}, 400, "uppercase letter"),
    ({"confirm_password": "Other#Pass1"}, 400, "confirmation do not match"),
    ({"admin_username": "ai-assistant"}, 400, "reserved"),
    ({"admin_username": "x"}, 400, "3-32 characters"),
    ({"ai": {"provider": "claude"}}, 400, "Enter the Claude API key"),
    ({"ai": {"provider": "claude", "api_key": "k"}}, 400, "ANTHROPIC_MODEL"),
])
def test_a_refused_completion_changes_nothing(new_app, changes, status, reason):
    recovery = new_app.post("/setup/start", json={"code": code_of(new_app)}).json()["recovery_key"]
    response = complete(new_app, recovery, **changes)
    assert response.status_code == status and reason in response.json()["detail"]
    assert new_app.app.state.setup is not None                           # still in setup
    assert new_app.app.state.installation.status() == "new"              # no keys were saved


def test_an_admin_in_incident_bridge_users_is_refused(new_app, monkeypatch):
    monkeypatch.setenv("INCIDENT_BRIDGE_USERS", json.dumps([{"username": "root", "password": "Boss#Pass11",
                                                             "role": "admin"}]))
    started = new_app.post("/setup/start", json={"code": code_of(new_app)}).json()
    assert "admins are not created from INCIDENT_BRIDGE_USERS" in started["users_problem"]
    assert complete(new_app, started["recovery_key"]).status_code == 400


# --- completing it ---------------------------------------------------------------------

def test_the_completed_setup_creates_the_accounts_and_opens_the_app(new_app, monkeypatch):
    monkeypatch.setenv("REQUIRE_PASSWORD_CHANGE_ON_FIRST_LOGIN", "true")
    recovery = new_app.post("/setup/start", json={"code": code_of(new_app)}).json()["recovery_key"]
    done = complete(new_app, recovery)
    assert done.status_code == 200 and done.json()["regular_users"] == ["tech1"]
    assert new_app.get("/health").json()["setup_required"] is False
    assert new_app.get("/setup.html", follow_redirects=False).headers["location"] == "/"
    assert new_app.post("/setup/start", json={"code": "ANY-CODE"}).status_code == 409
    me = new_app.post("/auth/login", json={"username": "boss", "password": "Chief#Start1"}).json()
    assert me["role"] == "admin" and me["must_change_password"] is False   # chose their own password
    tech = new_app.post("/auth/login", json={"username": "tech1", "password": "Demo#Pass2"}).json()
    assert tech["must_change_password"] is True


def test_ai_settings_chosen_at_setup_are_stored_and_win_over_env(new_app, monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")                         # .env says Gemini, without a key
    started = new_app.post("/setup/start", json={"code": code_of(new_app)}).json()
    tested = new_app.post("/setup/test-ai", json={"code": code_of(new_app), "ai": {"provider": "fake"}}).json()
    assert tested["ok"] is True and tested["models"] == ["fake-model"]
    complete(new_app, started["recovery_key"], ai={"provider": "fake"})
    assert stored_settings.stored() == {"AI_PROVIDER": "fake"}
    assert new_app.app.state.settings.get("ai") == {"AI_PROVIDER": "fake"}
    assert new_app.app.state.analysis_worker is not None                # on, with the stored provider


def test_a_restart_unlocks_by_itself_and_keeps_logins_valid(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "ib.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SESSION_SECRET_KEY", "ignored-now")             # secrets are not read from .env any more
    from app.api.app import app
    with TestClient(app) as client:
        finish_setup(client)
        client.post("/auth/login", json={"username": "admin", "password": "Demo#Pass1"})
        cookies = dict(client.cookies)
        secret = app.state.settings.get("secrets")["session_secret"]
    assert secret != "ignored-now"
    with TestClient(app, cookies=cookies) as again:                       # a restart, same browser
        assert app.state.setup is None
        assert again.get("/auth/me").json()["username"] == "admin"
        assert again.get("/logs/verify").json()["ok"] is True
        events = {r["event"] for r in again.get("/logs", params={"category": "!http", "limit": 1000}).json()["entries"]}
        assert "setup.completed" in events


def test_an_interrupted_setup_continues_without_a_new_recovery_key(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "ib.db"))
    installation = Installation.from_env()
    _, key, data = installation.create()
    installation.save_new(key, data)                                     # keys saved, then the setup stopped
    from app.api.app import app
    with TestClient(app) as client:
        started = client.post("/setup/start", json={"code": code_of(client)}).json()
        assert started["keys_exist"] is True and started["recovery_key"] is None
        body = {"code": code_of(client), "admin_username": "boss", "admin_password": "Chief#Start1",
                "confirm_password": "Chief#Start1"}
        assert client.post("/setup/complete", json=body).status_code == 200
        assert client.post("/auth/login", json={"username": "boss", "password": "Chief#Start1"}).status_code == 200


def test_a_database_without_its_keys_file_does_not_start(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "ib.db"))
    (tmp_path / "ib.db").write_bytes(b"encrypted bytes")
    from app.api.app import app
    with pytest.raises(InstallationError, match="has no keys file"):
        with TestClient(app):
            pass
