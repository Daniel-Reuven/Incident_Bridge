"""
Tests for per-user interface preferences: app/models/preferences.py, their
storage with the account (app/persistence_users.py) and PATCH /auth/preferences.

Run from backend/:  python -m pytest tests/test_preferences.py -v
"""

from app import database

import pytest

from app.models import Role, User
from app.models.preferences import DEFAULTS
from app.persistence_users import SqliteUserStore


def login(client, username, password):
    return client.post("/auth/login", json={"username": username, "password": password})


def test_a_new_account_has_the_defaults_in_login_and_me(app_client):
    assert login(app_client, "tech1", "Demo#Pass2").json()["preferences"] == DEFAULTS
    assert app_client.get("/auth/me").json()["preferences"] == {"theme": "system", "dark_level": "medium",
                                                                "text_scale": "1", "updates_order": "asc"}


def test_saved_preferences_follow_the_user_to_another_browser(user_client, second_client):
    saved = user_client.patch("/auth/preferences", json={"theme": "dark", "dark_level": "soft", "text_scale": "1.15"})
    assert saved.status_code == 200
    assert saved.json() == {"theme": "dark", "dark_level": "soft", "text_scale": "1.15",
                            "updates_order": "asc"}                                       # only the given ones
    user_client.patch("/auth/preferences", json={"updates_order": "desc"})
    other_browser = login(second_client, "tech1", "Demo#Pass2").json()["preferences"]
    assert other_browser == {"theme": "dark", "dark_level": "soft", "text_scale": "1.15", "updates_order": "desc"}


def test_preferences_are_per_user(admin_client, user_client):
    user_client.patch("/auth/preferences", json={"theme": "dark"})
    assert admin_client.get("/auth/me").json()["preferences"]["theme"] == "system"


@pytest.mark.parametrize("body, reason", [
    ({"theme": "purple"}, "theme must be one of: system, light, dark"),
    ({"text_scale": "2"}, "text_scale must be one of: 0.9, 1, 1.15, 1.3"),
    ({"dark_level": "black"}, "dark_level must be one of: soft, medium, deep"),
    ({"updates_order": "random"}, "updates_order must be one of: asc, desc"),
])
def test_invalid_values_are_refused_and_nothing_changes(user_client, body, reason):
    response = user_client.patch("/auth/preferences", json=body)
    assert response.status_code == 400 and reason in response.json()["detail"]
    assert user_client.get("/auth/me").json()["preferences"] == DEFAULTS


def test_logged_out_or_forced_to_change_password_cannot_save(admin_client, second_client, monkeypatch):
    assert second_client.patch("/auth/preferences", json={"theme": "dark"}).status_code == 401   # not logged in
    monkeypatch.setenv("REQUIRE_PASSWORD_CHANGE_ON_FIRST_LOGIN", "true")
    admin_client.post("/users", json={"username": "tech9", "role": "user", "password": "Start#Pass9",
                                      "confirm_password": "Start#Pass9"})
    login(second_client, "tech9", "Start#Pass9")
    assert second_client.patch("/auth/preferences", json={"theme": "dark"}).status_code == 403


def test_preferences_survive_a_restart_and_bad_stored_values_fall_back(tmp_path):
    db = str(tmp_path / "users.db")
    user = User("tech7", Role.USER, "Tech#Start7")
    user.update_preferences({"theme": "light", "updates_order": "desc"})
    SqliteUserStore(db).save(user)
    assert SqliteUserStore(db).load_all()[0].preferences == {**DEFAULTS, "theme": "light", "updates_order": "desc"}
    with database.connect(db) as conn:                                   # edited by hand into nonsense
        conn.execute("""UPDATE users SET preferences = '{"theme": "neon", "text_scale": "1.3"'""")
    assert SqliteUserStore(db).load_all()[0].preferences == DEFAULTS     # not JSON -> the defaults
    with database.connect(db) as conn:
        conn.execute("""UPDATE users SET preferences = '{"theme": "neon", "text_scale": "1.3"}'""")
    assert SqliteUserStore(db).load_all()[0].preferences == {**DEFAULTS, "text_scale": "1.3"}


def test_an_older_database_gets_the_column(tmp_path):
    db = str(tmp_path / "old.db")
    user = User("tech7", Role.USER, "Tech#Start7")
    with database.connect(db) as conn:                                   # the v0.5.x table, no preferences
        conn.execute("""CREATE TABLE users (username TEXT PRIMARY KEY, role TEXT NOT NULL,
                        password_hash TEXT NOT NULL, must_change_password INTEGER NOT NULL DEFAULT 0,
                        disabled INTEGER NOT NULL DEFAULT 0, session_version INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        conn.execute("INSERT INTO users VALUES (?, 'user', ?, 0, 0, 1, ?, ?)",
                     (user.username, user.password_hash, user.created_at.isoformat(), user.created_at.isoformat()))
    store = SqliteUserStore(db)
    loaded = store.load_all()[0]
    assert loaded.preferences == DEFAULTS and loaded.check_password("Tech#Start7")
    loaded.update_preferences({"theme": "dark"})
    store.save(loaded)
    assert SqliteUserStore(db).load_all()[0].preferences["theme"] == "dark"


def test_a_change_is_recorded_in_the_activity_log_only_when_something_changed(user_client):
    from app.audit import log
    records = []

    class Catch:
        level = 0

        def handle(self, record):
            records.append(record.audit)

    catcher = Catch()
    log.handlers.append(catcher)
    try:
        user_client.patch("/auth/preferences", json={"theme": "dark"})
        user_client.patch("/auth/preferences", json={"theme": "dark"})        # no change
    finally:
        log.handlers.remove(catcher)
    changed = [r for r in records if r["event"] == "auth.preferences_changed"]
    assert len(changed) == 1 and changed[0]["theme"] == "dark" and changed[0]["user"] == "tech1"
