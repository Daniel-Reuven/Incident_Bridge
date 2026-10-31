"""
Tests for app/app_config.py: the app's settings live encrypted in the
database. backend/.env gives their initial values at setup - once - and is
ignored after that (the AI API keys excepted: a key not saved in the app is
still read from the environment). Also the Server settings page's API for
them (GET/PUT /server/config in app/api/server.py).

Run from backend/:  python -m pytest tests/test_app_config.py -v
"""

import os

import pytest
from fastapi.testclient import TestClient

from app import app_config
from app.ai import stored_settings
from tests.install_helper import finish_setup


class MemoryStore:
    """The settings store's get/set, in memory."""
    def __init__(self, **rows):
        self.rows = dict(rows)

    def get(self, name):
        return self.rows.get(name)

    def set(self, name, value):
        self.rows[name] = value


def test_setup_copies_env_once(monkeypatch):
    monkeypatch.setenv("LOG_IP_MODE", "masked")
    monkeypatch.setenv("PORT", "9000")
    monkeypatch.setenv("GEMINI_API_KEY", "k-env")
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    store = MemoryStore()
    assert app_config.initialize(store) is True
    assert store.rows["config"]["LOG_IP_MODE"] == "masked" and store.rows["config"]["PORT"] == "9000"
    assert store.rows["ai"] == {"AI_PROVIDER": "gemini", "GEMINI_API_KEY": "k-env"}
    assert set(app_config.take_unrecorded()) == {"config", "ai"} and app_config.take_unrecorded() == []
    monkeypatch.setenv("LOG_IP_MODE", "full")
    assert app_config.initialize(store) is False                 # already set up: .env is not read again
    assert store.rows["config"]["LOG_IP_MODE"] == "masked"


def test_a_choice_made_at_setup_is_kept_and_bootstrap_names_stay_out(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    store = MemoryStore(ai={"AI_PROVIDER": "fake"})
    app_config.initialize(store)
    assert store.rows["ai"] == {"AI_PROVIDER": "fake"}
    assert not set(store.rows["config"]) & set(app_config.BOOTSTRAP)


def test_an_older_installation_s_page_values_carry_over():
    store = MemoryStore(server={"http_port": 8080, "https_enabled": True, "trusted_proxies": ["10.0.0.5/32"]})
    app_config.initialize(store)
    config = store.rows["config"]
    assert (config["PORT"], config["HTTPS_ENABLED"], config["TRUSTED_PROXIES"]) == ("8080", "true", "10.0.0.5/32")


def test_apply_makes_the_saved_values_the_only_ones(monkeypatch):
    monkeypatch.setenv("LOG_IP_MODE", "full")                    # in .env, but not saved
    monkeypatch.setenv("SITE_CHECK_SLOW_MS", "100")
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    store = MemoryStore(config={"SITE_CHECK_SLOW_MS": "3000"})
    app_config.apply(store)
    assert "LOG_IP_MODE" not in os.environ and os.environ["SITE_CHECK_SLOW_MS"] == "3000"
    assert stored_settings.setting("AI_PROVIDER") is None        # the AI choice: saved values only
    monkeypatch.setenv("GEMINI_API_KEY", "k-env")
    assert stored_settings.setting("GEMINI_API_KEY") == "k-env"  # ... the API keys excepted


def test_save_checks_everything_first():
    store = MemoryStore(config={"LOG_IP_MODE": "masked"})
    with pytest.raises(ValueError, match="Unknown"):
        app_config.save(store, {"DATABASE_PATH": "x.db"})       # a bootstrap setting is not the page's
    with pytest.raises(ValueError, match="SESSION_MAX_AGE_HOURS"):
        app_config.save(store, {"SESSION_MAX_AGE_HOURS": "soon", "LOG_IP_MODE": "full"})
    assert store.rows["config"] == {"LOG_IP_MODE": "masked"}    # nothing saved
    changed = app_config.save(store, {"LOG_IP_MODE": "", "SESSION_COOKIE_SECURE": "on"})
    assert changed == ["LOG_IP_MODE", "SESSION_COOKIE_SECURE"]
    assert store.rows["config"] == {"SESSION_COOKIE_SECURE": "true"}


def test_production_needs_https_first():
    store = MemoryStore(config={})
    with pytest.raises(ValueError, match="Production"):
        app_config.save(store, {"APP_MODE": "production"})


def test_the_page_shows_groups_defaults_and_the_restart_note():
    store = MemoryStore(config={"LOG_IP_MODE": "masked"})
    app_config.apply(store)
    shown = app_config.page(store)
    settings = {s["name"]: s for g in shown["groups"] for s in g["settings"]}
    assert settings["LOG_IP_MODE"]["value"] == "masked" and settings["LOG_IP_MODE"]["saved"]
    assert settings["AI_LOG_LEVEL"]["value"] is None and settings["AI_LOG_LEVEL"]["default"] == "INFO"
    assert shown["restart_needed"] is False
    app_config.save(store, {"LOG_IP_MODE": "off"})
    assert app_config.page(store)["restart_needed"] is True


# --- the app ------------------------------------------------------------------------------------

def test_after_setup_env_changes_are_ignored_and_the_log_stays_intact(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "ib.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("LOG_IP_MODE", "masked")
    from app.api.app import app
    with TestClient(app) as client:
        finish_setup(client)
    monkeypatch.setenv("LOG_IP_MODE", "full")                    # backend/.env edited after the setup
    with TestClient(app) as client:                              # a restart
        assert os.environ["LOG_IP_MODE"] == "masked"
        client.post("/auth/login", json={"username": "admin", "password": "Demo#Pass1"})
        verify = client.get("/logs/verify").json()
        assert verify["ok"] is True, verify["data"]["problems"]


def test_the_page_api(admin_client, user_client, caplog):
    assert user_client.get("/server/config").status_code == 403
    groups = admin_client.get("/server/config").json()["groups"]
    assert [g["name"] for g in groups][:2] == ["Server", "Accounts and logins"]
    caplog.set_level("INFO", logger="incident_bridge.audit")
    saved = admin_client.put("/server/config", json={"values": {"AI_REQUEST_DELAY_SECONDS": "2"}}).json()
    assert saved["changed"] == ["AI_REQUEST_DELAY_SECONDS"]
    record = next(r.audit for r in caplog.records if getattr(r, "audit", {}).get("event") == "server.config_changed")
    assert record["fields"] == ["AI_REQUEST_DELAY_SECONDS"]
    bad = admin_client.put("/server/config", json={"values": {"AI_REQUEST_DELAY_SECONDS": "-1"}})
    assert bad.status_code == 400
